"""HTTP adapter for passive package inspection and local import."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from .. import db
from ..core.config import settings
from .archive import ArchiveInspection, PackageArchiveError, write_archive
from .content_store import ContentStoreError
from .export_service import PackageExportError, export_locked, stream_file
from .launch_service import PackageLaunchError as TransactionalLaunchError
from .launch_service import launch_package
from .models import (
    PACKAGE_ID_PATTERN,
    VERSION_PATTERN,
    HostResources,
    LabDefinition,
    LabExperience,
    LabManifest,
    ResourceRecommendations,
    normalize_legacy_definition_payload,
)
from .package_service import (
    PACKAGE_COPY_CHUNK_BYTES,
    LabPackageService,
    PackageDeletionConflictError,
    PackageImportConflictError,
    PackageImportError,
    PackageInspectionExpiredError,
    StagedPackageInspection,
    default_package_service,
)
from .playground_adapter import definition_from_session
from .preflight import (
    PreflightIssue,
    issue_warning_consent_token,
    policy_revision,
    preflight_package,
    validate_warning_consent_token,
)
from .prepare_service import (
    PrepareError,
    PrepareHandle,
    cancel_prepare,
    create_prepare,
    dispatch_prepare_after_commit,
    status_prepare,
)
from .runner_preflight import request_runner_facts
from .signing import SignatureStatus
from .storage_custody import failure_message, secure_file
from .verification_service import (
    VerificationError,
    VerificationHandle,
    dispatch_verification_after_commit,
    request_cancel,
    save_draft,
    start_verification,
    verification_handle,
    verification_status,
    verified_result,
)

router = APIRouter(prefix="/api/v1/lab-packages", tags=["lab-packages"])
logger = logging.getLogger(__name__)


class LabPackageAssetOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    sha256: str
    size_bytes: int
    media_type: str
    embedded: bool
    sources: list[str] = Field(default_factory=list)
    license: str | None = None
    redistribution_confirmed: bool | None = None


class LabPackageInspectionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package_id: str
    version: str
    title: str
    description: str
    creator: str
    license: str
    reproducibility: str
    resources: dict[str, Any]
    required_capabilities: list[str]
    signature_status: str
    creator_key_fingerprint: str | None = None
    assets: list[LabPackageAssetOut]
    supporting_files: list[str]
    runbook: str
    inspection_id: str | None = None
    expires_at: str | None = None


class LabPackageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: str
    package_id: str
    version: str
    title: str
    description: str
    creator: str
    license: str
    reproducibility: str
    archive_sha256: str
    archive_size_bytes: int
    manifest_sha256: str
    required_capabilities: list[str]
    signature_status: str
    creator_key_fingerprint: str | None = None
    imported_at: str
    last_used_at: str | None = None


class LabPackageImportOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package: LabPackageOut
    embedded_assets_cached: int
    signature_status: str


class LabPackageExperienceOut(LabPackageOut):
    package_record_id: str
    runbook: str
    experience: LabExperience
    resources: ResourceRecommendations
    supporting_files: list[str]


class LabPackagePreflightOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ready: bool
    required_disk_bytes: int
    retained_disk_bytes: int
    launch_disk_bytes: int
    host: dict[str, Any]
    errors: list[dict[str, str]]
    warnings: list[dict[str, str]]
    warning_token: str


class LabPackagePrepareRequest(BaseModel):
    """Explicit request to acquire package assets before launch."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=128)
    # Source acquisition is never implicit.  The local settings policy must
    # also permit it; otherwise the operation is rejected with a stable code.
    network_opt_in: bool = False
    # Descriptive alias for API clients that mirror the settings name.  When
    # supplied it takes precedence over ``network_opt_in``.
    allow_source_network: bool | None = None


class LabPackagePrepareAssetOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    logical_path: str
    sha256: str
    size_bytes: int
    state: str
    origin: str | None = None
    error_summary: str | None = None


class LabPackagePrepareOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    package_record_id: str
    state: str
    progress_percent: int
    error_summary: str | None = None
    network_opt_in: bool
    assets: list[LabPackagePrepareAssetOut]


class LabPackageLaunchRequest(BaseModel):
    """Explicit acknowledgement data that is revalidated at launch time."""

    model_config = ConfigDict(extra="forbid")

    acknowledged_warning_token: str | None = Field(default=None, min_length=1, max_length=4096)
    acknowledge_guest_egress: bool = False
    # Kept optional at schema level so a rejected preflight retains its useful
    # error response; a launch that is otherwise admissible requires it.
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class LabPackageInstanceOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    package_record_id: str
    session_id: str | None
    state: str
    error_summary: str | None = None


class LabPackageExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str | None = None
    package_id: str = Field(pattern=PACKAGE_ID_PATTERN)
    version: str = Field(pattern=VERSION_PATTERN)
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    creator: str = Field(min_length=1, max_length=160)
    license: str = Field(min_length=1, max_length=256)
    resources: ResourceRecommendations
    runbook: str = Field(min_length=1, max_length=2 * 1024 * 1024)
    experience: LabExperience = Field(default_factory=LabExperience)
    # ``unlocked`` remains a useful topology/runbook export, but only a
    # completed clean-run verification may be exported as reproducible.
    reproducibility: str = Field(default="unlocked", pattern="^(locked|unlocked)$")
    verification_id: str | None = Field(default=None, min_length=1, max_length=64)
    clean_run_verification_id: str | None = Field(default=None, min_length=1, max_length=64)
    embed_assets: bool = True
    embedding: bool | None = None
    redistribution_confirmed: bool = False
    redistribution_confirmation: bool | None = None


class LabVerificationExportRequest(BaseModel):
    """Everything an export needs beyond the verification itself is already locked.

    Identity, resources, experience and runbook come from the attested draft,
    so the caller may only choose whether the clean-start bytes travel with the
    archive and whether they hold the rights to redistribute them.
    """

    model_config = ConfigDict(extra="forbid")

    embed_assets: bool = False
    redistribution_confirmed: bool = False


class LabDefinitionFromSessionRequest(BaseModel):
    """Creator-supplied identity for a definition derived from authored state."""

    model_config = ConfigDict(extra="forbid")

    session_id: str | None = Field(default=None, min_length=1, max_length=64)
    package_id: str = Field(pattern=PACKAGE_ID_PATTERN)
    version: str = Field(pattern=VERSION_PATTERN)
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    creator: str = Field(min_length=1, max_length=160)
    license: str = Field(min_length=1, max_length=256)
    # Creators who have not yet measured host requirements may omit these and
    # edit the derived recommendation before saving a draft.
    resources: ResourceRecommendations | None = None
    experience: LabExperience = Field(default_factory=LabExperience)


class LabDefinitionFromSessionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_session_id: str
    resources_derived: bool
    definition: LabDefinition


class LabDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition: LabDefinition
    runbook: str = Field(default="", max_length=2 * 1024 * 1024)
    supporting_files: dict[str, str] = Field(default_factory=dict, max_length=64)
    authored_session_id: str | None = Field(default=None, max_length=64)


class LabDraftOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    digest: str


class LabVerificationOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    draft_id: str
    state: str
    progress_percent: int
    authored_session_id: str | None = None
    error_code: str | None = None
    error_summary: str | None = None
    package_id: str | None = None
    package_version: str | None = None
    package_title: str | None = None
    attestation_expires_at: str | None = None
    attestation_status: Literal["unexpired", "expired", "unavailable"] = "unavailable"


def _verification_response(handle: VerificationHandle) -> LabVerificationOut:
    return LabVerificationOut(**asdict(handle))


class LabVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_id: str = Field(min_length=1, max_length=64)
    authored_session_closed: bool = False


class LabPackageActivityOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prepares: list[LabPackagePrepareOut]
    verifications: list[LabVerificationOut]
    recent_terminal_verification_count: int = 0
    instances: list[LabPackageInstanceOut]


def get_session():
    """Commit request work before invoking registered instance dispatchers."""

    callbacks: list[Callable[[], None]] = []
    try:
        with db.session_scope() as session:
            yield session
            callbacks = list(session.info.get("package_post_commit", []))
    except Exception:
        # The transaction rolled back, so its outbox rows must never be sent.
        raise
    for callback in callbacks:
        try:
            callback()
        except Exception:
            # A durable pending outbox row is the recovery mechanism when a
            # broker is unavailable after the request commit.
            logger.exception("Could not dispatch committed package operation")
            continue


def dispatch_instance_after_commit(instance_id: str) -> None:
    """Submit a committed outbox operation without making launch synchronous."""

    from ..jobs.package_instance_coordinator import coordinate_instance

    coordinate_instance.delay(instance_id)


def dispatch_instance_action_after_commit(instance_id: str, action: str) -> None:
    """Submit a committed cancellation/teardown operation."""

    from ..jobs.package_instance_coordinator import coordinate_instance_action

    coordinate_instance_action.delay(instance_id, action)


def _stage_upload(file: UploadFile, service: LabPackageService, session: Session) -> StagedPackageInspection:
    """Copy one bounded Starlette spool into journal-reserved managed staging.

    FastAPI parses multipart input before this handler receives ``UploadFile``.
    Rejecting its measured spool size before reserving a managed path limits the
    unavoidable parser spool to one file and prevents a second full copy for
    requests already above the package archive limit.
    """
    spooled_size = getattr(file, "size", None)
    if isinstance(spooled_size, int) and spooled_size > service.archive_max_bytes:
        raise HTTPException(status_code=413, detail="package archive is too large")
    inspection = service.reserve_inspection(session)
    temporary = service.inspection_partial_path(inspection)
    descriptor: int | None = None
    completed = False
    error_summary = "upload interrupted"
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        secure_file(descriptor, temporary)
        total = 0
        with os.fdopen(descriptor, "wb") as target:
            descriptor = None
            while chunk := file.file.read(1024 * 1024):
                total += len(chunk)
                if total > service.archive_max_bytes:
                    raise HTTPException(status_code=413, detail="package archive is too large")
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        staged = service.finalize_staged_upload(inspection.id, session)
        completed = True
        return staged
    except HTTPException as exc:
        error_summary = str(exc.detail)
        raise
    except PackageImportError as exc:
        error_summary = str(exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OSError, ValueError) as exc:
        error_summary = failure_message("could not stage package upload", exc)
        raise HTTPException(status_code=400, detail=error_summary) from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if not completed:
            try:
                service.abandon_inspection(inspection.id, session, error_summary=error_summary)
            except PackageImportError:
                pass
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _inspection_response(
    inspection: ArchiveInspection,
    signature_status: SignatureStatus,
    fingerprint: str | None,
    *,
    inspection_id: str | None = None,
    expires_at: str | None = None,
):
    manifest_payloads = {payload.path: payload for payload in inspection.manifest.payloads}
    clean_layers = {
        layer.sha256: layer for vm in inspection.definition.vms if (layer := vm.effective_boot_layer) is not None
    }
    assets: list[LabPackageAssetOut] = []
    embedded_digests: set[str] = set()
    for path in sorted(set(inspection.assets) | set(inspection.asset_descriptors)):
        payload = manifest_payloads[path]
        reference = clean_layers.get(payload.sha256)
        embedded_digests.add(payload.sha256)
        assets.append(
            LabPackageAssetOut(
                path=path,
                sha256=payload.sha256,
                size_bytes=payload.size_bytes,
                media_type=payload.media_type,
                embedded=True,
                sources=list(reference.sources) if reference is not None else [],
                license=reference.license if reference is not None else None,
                redistribution_confirmed=reference.redistribution_confirmed if reference is not None else None,
            )
        )
    for digest, reference in sorted(clean_layers.items()):
        if digest in embedded_digests:
            continue
        assets.append(
            LabPackageAssetOut(
                path=f"assets/sha256/{digest}",
                sha256=reference.sha256,
                size_bytes=reference.size_bytes,
                media_type=reference.media_type,
                embedded=False,
                sources=list(reference.sources),
                license=reference.license,
                redistribution_confirmed=reference.redistribution_confirmed,
            )
        )
    return LabPackageInspectionOut(
        package_id=inspection.definition.package_id,
        version=inspection.definition.version,
        title=inspection.definition.title,
        description=inspection.definition.description,
        creator=inspection.definition.creator,
        license=inspection.definition.license,
        reproducibility=inspection.definition.reproducibility,
        resources=inspection.definition.resources.model_dump(mode="json"),
        required_capabilities=list(inspection.manifest.required_capabilities),
        signature_status=signature_status.value,
        creator_key_fingerprint=fingerprint,
        assets=assets,
        supporting_files=sorted(inspection.supporting_files),
        runbook=inspection.runbook,
        inspection_id=inspection_id,
        expires_at=expires_at,
    )


def _record_response(record: Any) -> LabPackageOut:
    try:
        capabilities = json.loads(record.required_capabilities_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="stored package capabilities are invalid") from exc
    if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
        raise HTTPException(status_code=500, detail="stored package capabilities are invalid")
    return LabPackageOut(
        id=record.id,
        package_id=record.package_id,
        version=record.version,
        title=record.title,
        description=record.description,
        creator=record.creator,
        license=record.license,
        reproducibility=record.reproducibility,
        archive_sha256=record.archive_sha256,
        archive_size_bytes=record.archive_size_bytes,
        manifest_sha256=record.manifest_sha256,
        required_capabilities=capabilities,
        signature_status=record.signature_status,
        creator_key_fingerprint=record.creator_key_fingerprint,
        imported_at=record.imported_at.isoformat(),
        last_used_at=record.last_used_at.isoformat() if record.last_used_at is not None else None,
    )


@router.post("/inspect", response_model=LabPackageInspectionOut)
def inspect_lab_package(
    file: UploadFile = File(...),
) -> LabPackageInspectionOut:
    service = default_package_service()
    with db.session_scope() as session:
        staged = _stage_upload(file, service, session)
        try:
            return _inspection_response(
                staged.inspection,
                staged.signature.status,
                staged.signature.fingerprint,
                inspection_id=staged.id,
                expires_at=staged.expires_at.isoformat(),
            )
        except PackageImportError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/import", response_model=LabPackageImportOut, status_code=status.HTTP_201_CREATED)
def import_lab_package(
    file: UploadFile = File(...),
) -> LabPackageImportOut:
    service = default_package_service()
    with db.session_scope() as session:
        staged = _stage_upload(file, service, session)
        try:
            imported = service.commit_inspection(staged.id, session)
            return LabPackageImportOut(
                package=_record_response(imported.record),
                embedded_assets_cached=len(imported.embedded_assets),
                signature_status=imported.signature.status.value,
            )
        except PackageImportConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PackageImportError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/imports/{inspection_id}/commit", response_model=LabPackageImportOut, status_code=status.HTTP_201_CREATED)
def commit_lab_package_inspection(
    inspection_id: str,
    session: Session = Depends(get_session),
) -> LabPackageImportOut:
    service = default_package_service()
    try:
        imported = service.commit_inspection(inspection_id, session)
        return LabPackageImportOut(
            package=_record_response(imported.record),
            embedded_assets_cached=len(imported.embedded_assets),
            signature_status=imported.signature.status.value,
        )
    except PackageInspectionExpiredError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    except PackageImportConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PackageImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/export")
def export_lab_package(
    payload: LabPackageExportRequest,
    session: Session = Depends(get_session),
) -> Response:
    """Export a current local Playground session as a shareable package."""

    if payload.reproducibility == "locked":
        verification_id = payload.verification_id or payload.clean_run_verification_id
        embed_assets = payload.embed_assets if payload.embedding is None else payload.embedding
        redistribution_confirmed = (
            payload.redistribution_confirmed
            if payload.redistribution_confirmation is None
            else payload.redistribution_confirmation
        )
        if verification_id is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "verification_required", "message": "Verify Clean Run before locked export"},
            )
        if embed_assets and not redistribution_confirmed:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "redistribution_unconfirmed",
                    "message": "Embedding boot layers requires explicit redistribution confirmation",
                },
            )
        try:
            clean_run = verified_result(session, verification_id)
            if (clean_run.locked_definition.package_id, clean_run.locked_definition.version) != (
                payload.package_id,
                payload.version,
            ):
                raise VerificationError("attestation_mismatch", "export identity differs from the verified definition")
        except VerificationError as exc:
            raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc
        # The runbook is part of the saved draft.  Read it directly here so a
        # caller cannot export bytes that were not covered by verification.
        from ..db.models import LabDraft, LabPackageAttestation

        draft = session.get(LabDraft, clean_run.draft_id)
        if draft is None:
            raise HTTPException(status_code=409, detail={"code": "draft_not_found", "message": "saved draft missing"})
        runbook = draft.runbook
        if runbook != payload.runbook:
            raise HTTPException(
                status_code=409,
                detail={"code": "attestation_mismatch", "message": "runbook differs from the verified draft"},
            )
        attestation = session.get(LabPackageAttestation, clean_run.attestation_id)
        if attestation is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "attestation_missing", "message": "clean-run attestation is missing"},
            )
        try:
            draft_file_digests = json.loads(draft.supporting_files_json)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "invalid_draft", "message": "saved supporting-file references are invalid"},
            ) from exc
        if not isinstance(draft_file_digests, dict):
            raise HTTPException(
                status_code=409,
                detail={"code": "invalid_draft", "message": "saved supporting-file references are invalid"},
            )
        try:
            service = default_package_service()
            # Drafts persist immutable content digests rather than request
            # bytes.  Re-open and verify those exact objects while exporting,
            # so the attested experience includes the same supporting files.
            supporting_files: dict[str, bytes] = {}
            for path, expected_digest in draft_file_digests.items():
                if not isinstance(path, str) or not isinstance(expected_digest, str):
                    raise PackageExportError("invalid_supporting_file", "saved supporting-file reference is invalid")
                try:
                    handle = service.content_store.open(expected_digest)
                    try:
                        content = handle.read(8 * 1024 * 1024 + 1)
                    finally:
                        handle.close()
                except ContentStoreError as exc:
                    raise PackageExportError(
                        "supporting_file_unavailable", f"supporting file is not cached: {path}"
                    ) from exc
                if len(content) > 8 * 1024 * 1024 or hashlib.sha256(content).hexdigest() != expected_digest:
                    raise PackageExportError("supporting_file_mismatch", f"supporting file changed: {path}")
                supporting_files[path] = content
            artifact = export_locked(
                session,
                definition=clean_run.locked_definition,
                runbook=runbook,
                content_store=service.content_store,
                embedded_layers=clean_run.boot_layers if embed_assets else {},
                supporting_files=supporting_files,
                attestation=attestation,
                draft_supporting_files=draft_file_digests,
                package_root=service.package_root,
            )
        except PackageExportError as exc:
            raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc
        filename = f"{payload.package_id}-{payload.version}.astinuslab"
        return StreamingResponse(
            stream_file(artifact.path),
            media_type="application/vnd.astinuslab.lab+zip",
            headers={
                "Content-Length": str(artifact.archive_size_bytes),
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-AstinusLab-Definition-SHA256": artifact.definition_sha256,
            },
        )

    from ..playground import repositories as playground_repositories

    session_id = payload.session_id
    if session_id is None:
        current = playground_repositories.active_playground_session(session)
        if current is None:
            raise HTTPException(status_code=404, detail="no active playground session")
        session_id = current.id
    try:
        definition = definition_from_session(
            session,
            session_id=session_id,
            package_id=payload.package_id,
            version=payload.version,
            title=payload.title,
            description=payload.description,
            creator=payload.creator,
            license=payload.license,
            resources=payload.resources,
            experience=payload.experience,
        )
        archive = write_archive(definition, payload.runbook, max_archive_bytes=settings.packages_max_archive_bytes)
    except (PackageArchiveError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    filename = f"{payload.package_id}-{payload.version}.astinuslab"
    return Response(
        content=archive,
        media_type="application/vnd.astinuslab.lab+zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# Sizing hints are a starting point, not a measurement: the derived minimum is
# what the authored session already declares, and the recommendation doubles it
# within the schema's own bounds.
_MAX_CPU_THREADS = 256
_MIN_HOST_MEMORY_MB = 512
_DERIVATION_PLACEHOLDER_RESOURCES = ResourceRecommendations(
    minimum=HostResources(cpu_threads=1, memory_mb=_MIN_HOST_MEMORY_MB, free_disk_bytes=1),
    recommended=HostResources(cpu_threads=1, memory_mb=_MIN_HOST_MEMORY_MB, free_disk_bytes=1),
)


def _resources_from_definition(definition: LabDefinition) -> ResourceRecommendations:
    """Derive an editable host recommendation from what the session actually runs."""

    cpu_threads = max(sum(vm.vcpus for vm in definition.vms), 1)
    memory_mb = max(sum(vm.memory_mb for vm in definition.vms), _MIN_HOST_MEMORY_MB)
    free_disk_bytes = max(sum(vm.disk_gb for vm in definition.vms) * 1024**3, 1)
    return ResourceRecommendations(
        minimum=HostResources(
            cpu_threads=min(cpu_threads, _MAX_CPU_THREADS),
            memory_mb=memory_mb,
            free_disk_bytes=free_disk_bytes,
        ),
        recommended=HostResources(
            cpu_threads=min(cpu_threads * 2, _MAX_CPU_THREADS),
            memory_mb=memory_mb * 2,
            free_disk_bytes=free_disk_bytes * 2,
        ),
    )


@router.post("/definitions/from-session", response_model=LabDefinitionFromSessionOut)
def build_definition_from_session(
    payload: LabDefinitionFromSessionRequest, session: Session = Depends(get_session)
) -> LabDefinitionFromSessionOut:
    """Generate a canonical definition from authored Playground state.

    This is a read-only handoff: it never freezes, snapshots, or releases the
    session it reads, so the creator can keep editing and regenerate.
    """

    from ..db.models import PlaygroundSession
    from ..playground import repositories as playground_repositories

    session_id = payload.session_id
    if session_id is None:
        current = playground_repositories.active_playground_session(session)
        if current is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "no_active_session", "message": "no active playground session"},
            )
        session_id = current.id
    elif session.get(PlaygroundSession, session_id) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "session_not_found", "message": f"playground session not found: {session_id}"},
        )

    resources_derived = payload.resources is None
    try:
        definition = definition_from_session(
            session,
            session_id=session_id,
            package_id=payload.package_id,
            version=payload.version,
            title=payload.title,
            description=payload.description,
            creator=payload.creator,
            license=payload.license,
            resources=payload.resources or _DERIVATION_PLACEHOLDER_RESOURCES,
            experience=payload.experience,
        )
        if resources_derived:
            # Sizing depends on the VMs the adapter resolved, so it is applied
            # to the validated definition rather than guessed from raw rows.
            definition = definition.model_copy(update={"resources": _resources_from_definition(definition)})
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "definition_not_derivable", "message": str(exc)},
        ) from exc
    return LabDefinitionFromSessionOut(
        source_session_id=session_id,
        resources_derived=resources_derived,
        definition=definition,
    )


@router.post("/drafts", response_model=LabDraftOut, status_code=status.HTTP_201_CREATED)
def save_lab_draft(payload: LabDraftRequest, session: Session = Depends(get_session)) -> LabDraftOut:
    """Save the creator's canonical inputs before any clean-run operation."""

    try:
        handle = save_draft(
            session,
            payload.definition,
            runbook=payload.runbook,
            supporting_files=payload.supporting_files,
            authored_session_id=payload.authored_session_id,
        )
    except VerificationError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc
    return LabDraftOut(id=handle.id, digest=handle.digest)


@router.post("/verify-clean-run", response_model=LabVerificationOut, status_code=status.HTTP_202_ACCEPTED)
def verify_clean_run(payload: LabVerificationRequest, session: Session = Depends(get_session)) -> LabVerificationOut:
    """Queue explicit creator-side clean-run verification from a saved draft."""

    from ..db.models import LabDraft

    draft = session.get(LabDraft, payload.draft_id)
    if draft is not None and not draft.runbook.strip():
        raise HTTPException(
            status_code=409,
            detail={"code": "runbook_missing", "message": "add a runbook before starting verification"},
        )

    try:
        handle = start_verification(
            session, draft_id=payload.draft_id, authored_session_closed=payload.authored_session_closed
        )
    except VerificationError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc
    session.info.setdefault("package_post_commit", []).append(
        lambda attempt_id=handle.id: dispatch_verification_after_commit(attempt_id)
    )
    return _verification_response(handle)


# A resource-shaped alias makes the operation discoverable to clients that
# prefer noun routes while retaining the verb route used by the UI.
@router.post("/verifications", response_model=LabVerificationOut, status_code=status.HTTP_202_ACCEPTED)
def create_lab_verification(
    payload: LabVerificationRequest, session: Session = Depends(get_session)
) -> LabVerificationOut:
    return verify_clean_run(payload, session)


@router.get("/verifications/{attempt_id}", response_model=LabVerificationOut)
def get_lab_verification(attempt_id: str, session: Session = Depends(get_session)) -> LabVerificationOut:
    try:
        handle = verification_status(session, attempt_id)
    except VerificationError as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code, "message": str(exc)}) from exc
    return _verification_response(handle)


@router.post(
    "/verifications/{attempt_id}/cancel", response_model=LabVerificationOut, status_code=status.HTTP_202_ACCEPTED
)
def cancel_lab_verification(attempt_id: str, session: Session = Depends(get_session)) -> LabVerificationOut:
    try:
        handle = request_cancel(session, attempt_id)
    except VerificationError as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code, "message": str(exc)}) from exc
    return _verification_response(handle)


@router.post("/verifications/{attempt_id}/export")
def export_verified_lab_package(
    attempt_id: str,
    payload: LabVerificationExportRequest,
    session: Session = Depends(get_session),
) -> Response:
    """Export a completed clean run without its authoring session.

    The verification is the durable record of what was proved, so a creator can
    export it after the authored session has been cleaned up and after any
    number of browser reloads.  Locked metadata and the saved draft runbook are
    the only inputs; the shared locked-export route still performs every
    attestation, custody and consent check.
    """

    from ..db.models import LabDraft

    try:
        clean_run = verified_result(session, attempt_id)
    except VerificationError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc
    draft = session.get(LabDraft, clean_run.draft_id)
    if draft is None:
        raise HTTPException(status_code=409, detail={"code": "draft_not_found", "message": "saved draft missing"})
    if not draft.runbook:
        raise HTTPException(
            status_code=409,
            detail={"code": "runbook_missing", "message": "the verified draft has no runbook to export"},
        )
    locked = clean_run.locked_definition
    return export_lab_package(
        LabPackageExportRequest(
            package_id=locked.package_id,
            version=locked.version,
            title=locked.title,
            description=locked.description,
            creator=locked.creator,
            license=locked.license,
            resources=locked.resources,
            runbook=draft.runbook,
            experience=locked.experience,
            reproducibility="locked",
            verification_id=attempt_id,
            embed_assets=payload.embed_assets,
            redistribution_confirmed=payload.redistribution_confirmed,
        ),
        session,
    )


@router.get("", response_model=list[LabPackageOut])
def list_lab_packages(session: Session = Depends(get_session)) -> list[LabPackageOut]:
    from .package_catalog import LabPackageRepository

    service = default_package_service()
    repository = LabPackageRepository(session, archive_root=service.package_root)
    return [_record_response(record) for record in repository.list_packages()]


@router.post("/gc")
def garbage_collect_lab_packages(session: Session = Depends(get_session)) -> dict[str, int]:
    try:
        return default_package_service().garbage_collect(session)
    except (PackageImportError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/activity", response_model=LabPackageActivityOut)
def list_lab_package_activity(session: Session = Depends(get_session)) -> LabPackageActivityOut:
    from ..db.models import LabInstance, LabPrepareAttempt, LabVerificationAttempt

    prepares = (
        session.query(LabPrepareAttempt)
        .filter(LabPrepareAttempt.state.in_(("reserved", "running")))
        .order_by(LabPrepareAttempt.updated_at.desc(), LabPrepareAttempt.id.asc())
        .all()
    )
    # Verified attempts stay in the activity feed: a completed clean run is the
    # creator's durable export handle, and it must survive a browser reload.
    verifications = (
        session.query(LabVerificationAttempt)
        .filter(LabVerificationAttempt.state.in_(("reserved", "running", "verified")))
        .order_by(LabVerificationAttempt.updated_at.desc(), LabVerificationAttempt.id.asc())
        .all()
    )
    recent_terminal = (
        session.query(LabVerificationAttempt)
        .filter(LabVerificationAttempt.state.in_(("failed", "cancelled")))
        .order_by(LabVerificationAttempt.updated_at.desc(), LabVerificationAttempt.id.asc())
        .limit(20)
        .all()
    )
    verifications.extend(recent_terminal)
    # Stable ID ties with the same global recency order as the active slice.
    verifications.sort(key=lambda attempt: attempt.id)
    verifications.sort(key=lambda attempt: attempt.updated_at, reverse=True)
    instances = (
        session.query(LabInstance)
        .filter(LabInstance.state != "destroyed")
        .order_by(LabInstance.updated_at.desc(), LabInstance.id.asc())
        .all()
    )
    return LabPackageActivityOut(
        prepares=[_prepare_response(status_prepare(session, attempt.id)) for attempt in prepares],
        verifications=[_verification_response(verification_handle(session, attempt)) for attempt in verifications],
        recent_terminal_verification_count=len(recent_terminal),
        instances=[
            LabPackageInstanceOut(
                id=instance.id,
                package_record_id=instance.package_record_id,
                session_id=instance.playground_session_id,
                state=instance.state,
                error_summary=instance.error_summary,
            )
            for instance in instances
        ],
    )


@router.get("/{record_id}", response_model=LabPackageOut)
def get_lab_package(record_id: str, session: Session = Depends(get_session)) -> LabPackageOut:
    from .package_catalog import LabPackageRepository

    service = default_package_service()
    record = LabPackageRepository(session, archive_root=service.package_root).get_by_id(record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="lab package not found")
    return _record_response(record)


@router.delete("/{record_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_lab_package(record_id: str, session: Session = Depends(get_session)) -> Response:
    try:
        default_package_service().delete_package(record_id, session)
    except PackageDeletionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PackageImportError as exc:
        message = str(exc)
        raise HTTPException(status_code=404 if message == "lab package not found" else 400, detail=message) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{record_id}/preflight", response_model=LabPackagePreflightOut)
def preflight_lab_package(record_id: str, session: Session = Depends(get_session)) -> LabPackagePreflightOut:
    from .package_catalog import LabPackageRepository

    service = default_package_service()
    record = LabPackageRepository(session, archive_root=service.package_root).get_by_id(record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="lab package not found")
    try:
        definition = LabDefinition.model_validate(
            normalize_legacy_definition_payload(json.loads(record.definition_json))
        )
        manifest = LabManifest.model_validate(json.loads(record.manifest_json))
        result = _package_preflight(record, definition, manifest, service, session)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="stored package definition is invalid") from exc
    return LabPackagePreflightOut(
        ready=result.ready,
        required_disk_bytes=result.required_disk_bytes,
        retained_disk_bytes=result.retained_disk_bytes,
        launch_disk_bytes=result.launch_disk_bytes,
        host={
            "cpu_threads": result.host.cpu_threads,
            "memory_mb": result.host.memory_mb,
            "free_disk_bytes": result.host.free_disk_bytes,
            "architecture": result.host.architecture,
            "kvm_available": result.host.kvm_available,
            "capabilities": sorted(result.host.capabilities),
        },
        errors=[_issue_response(issue) for issue in result.errors],
        warnings=[_issue_response(issue) for issue in result.warnings],
        warning_token=_warning_token(record, definition, result),
    )


@router.post("/{record_id}/launch", response_model=LabPackageInstanceOut, status_code=status.HTTP_202_ACCEPTED)
def launch_lab_package(
    record_id: str,
    payload: LabPackageLaunchRequest = LabPackageLaunchRequest(),
    session: Session = Depends(get_session),
) -> LabPackageInstanceOut:
    from ..db.models import LabInstance
    from .package_catalog import LabPackageRepository

    service = default_package_service()
    repository = LabPackageRepository(session, archive_root=service.package_root)
    record = repository.get_by_id(record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="lab package not found")
    # An idempotent retry must return its durable operation even if its old
    # consent token has meanwhile expired or host facts have changed.  The
    # original request already passed those checks in the transaction which
    # created the instance; retrying it must never create a second launch.
    if payload.idempotency_key:
        existing = (
            session.query(LabInstance)
            .filter(
                LabInstance.package_record_id == record.id,
                LabInstance.idempotency_key == payload.idempotency_key,
            )
            .one_or_none()
        )
        if existing is not None:
            return LabPackageInstanceOut(
                id=existing.id,
                package_record_id=existing.package_record_id,
                session_id=existing.playground_session_id,
                state=existing.state,
                error_summary=existing.error_summary,
            )
    try:
        definition = LabDefinition.model_validate(
            normalize_legacy_definition_payload(json.loads(record.definition_json))
        )
        manifest = LabManifest.model_validate(json.loads(record.manifest_json))
        result = _package_preflight(record, definition, manifest, service, session)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="stored package definition is invalid") from exc
    if not result.ready:
        missing_content = any(issue.code == "missing-clean-layer" for issue in result.errors)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "prepare_required" if missing_content else "preflight_failed",
                "message": "Prepare the package assets before launching"
                if missing_content
                else "package preflight failed",
                "errors": [_issue_response(issue) for issue in result.errors],
                "warnings": [_issue_response(issue) for issue in result.warnings],
            },
        )
    requested_capabilities = _requested_capabilities(definition)
    if "guest_egress" in requested_capabilities and not payload.acknowledge_guest_egress:
        raise HTTPException(
            status_code=409,
            detail={"code": "warnings_unacknowledged", "message": "Guest egress requires explicit acknowledgement"},
        )
    if not validate_warning_consent_token(
        payload.acknowledged_warning_token,
        package_digest=record.archive_sha256,
        warnings=result.warnings,
        policy_revision=policy_revision(),
        requested_capabilities=requested_capabilities,
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "warnings_unacknowledged", "message": "Preflight warnings must be acknowledged again"},
        )
    try:
        operation = launch_package(
            session,
            record=record,
            definition=definition,
            manifest=manifest,
            service=service,
            idempotency_key=payload.idempotency_key or "",
            acknowledged_warning_token=payload.acknowledged_warning_token,
            acknowledge_guest_egress=payload.acknowledge_guest_egress,
            runner=result.runner,
        )
    except TransactionalLaunchError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc
    if operation.created:
        session.info.setdefault("package_post_commit", []).append(
            lambda instance_id=operation.instance_id: dispatch_instance_after_commit(instance_id)
        )
    instance = session.get(LabInstance, operation.instance_id)
    if instance is None:
        # The operation can be supplied by a compatibility adapter in an
        # integration deployment; its durable identifiers remain the response.
        return LabPackageInstanceOut(
            id=operation.instance_id,
            package_record_id=record.id,
            session_id=operation.session_id,
            state="registered",
        )
    return LabPackageInstanceOut(
        id=instance.id,
        package_record_id=instance.package_record_id,
        session_id=instance.playground_session_id,
        state=instance.state,
        error_summary=instance.error_summary,
    )


@router.get("/operations/{instance_id}", response_model=LabPackageInstanceOut)
def lab_package_operation_status(instance_id: str, session: Session = Depends(get_session)) -> LabPackageInstanceOut:
    from ..db.models import LabInstance

    instance = session.get(LabInstance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="package operation not found")
    return LabPackageInstanceOut(
        id=instance.id,
        package_record_id=instance.package_record_id,
        session_id=instance.playground_session_id,
        state=instance.state,
        error_summary=instance.error_summary,
    )


@router.post(
    "/operations/{instance_id}/{action}", response_model=LabPackageInstanceOut, status_code=status.HTTP_202_ACCEPTED
)
def request_lab_package_instance_action(
    instance_id: str, action: str, session: Session = Depends(get_session)
) -> LabPackageInstanceOut:
    from ..db.models import LabInstance
    from ..jobs.package_instance_coordinator import request_instance_action

    instance = session.get(LabInstance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="package operation not found")
    try:
        request_instance_action(session, instance, action)
    except NotImplementedError as exc:
        raise HTTPException(status_code=409, detail={"code": "not_implemented", "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "invalid_instance_action", "message": str(exc)}) from exc
    session.info.setdefault("package_post_commit", []).append(
        lambda instance_id=instance.id, requested_action=action: dispatch_instance_action_after_commit(
            instance_id, requested_action
        )
    )
    return LabPackageInstanceOut(
        id=instance.id,
        package_record_id=instance.package_record_id,
        session_id=instance.playground_session_id,
        state=instance.state,
        error_summary=instance.error_summary,
    )


@router.get("/{record_id}/experience", response_model=LabPackageExperienceOut)
def get_lab_package_experience(record_id: str, session: Session = Depends(get_session)) -> LabPackageExperienceOut:
    from .package_catalog import LabPackageRepository

    service = default_package_service()
    record = LabPackageRepository(session, archive_root=service.package_root).get_by_id(record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="lab package not found")
    try:
        owner_id = service.acquire_download_lease(record.id, session)
    except PackageDeletionConflictError as exc:
        raise HTTPException(status_code=409, detail={"code": "package_busy", "message": str(exc)}) from exc
    handle = None
    try:
        handle = _open_verified_archive(record, service)
        inspection = service.inspect_experience(handle)
        if (
            inspection.manifest_digest != record.manifest_sha256
            or inspection.definition.package_id != record.package_id
            or inspection.definition.version != record.version
        ):
            raise HTTPException(status_code=410, detail="retained package identity failed integrity checks")
        return LabPackageExperienceOut(
            **_record_response(record).model_dump(),
            package_record_id=record.id,
            runbook=inspection.runbook,
            experience=inspection.definition.experience,
            resources=inspection.definition.resources,
            supporting_files=sorted(inspection.supporting_files),
        )
    except (PackageArchiveError, OSError, ValueError) as exc:
        raise HTTPException(status_code=410, detail="retained package instructions are unavailable") from exc
    finally:
        try:
            if handle is not None:
                handle.close()
        finally:
            service.release_download_lease(record.id, owner_id)


@router.get("/{record_id}/download")
def download_lab_package(record_id: str, session: Session = Depends(get_session)) -> StreamingResponse:
    from .package_catalog import LabPackageRepository

    service = default_package_service()
    repository = LabPackageRepository(session, archive_root=service.package_root)
    record = repository.get_by_id(record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="lab package not found")
    try:
        owner_id = service.acquire_download_lease(record.id, session)
    except PackageDeletionConflictError as exc:
        raise HTTPException(status_code=409, detail={"code": "package_busy", "message": str(exc)}) from exc
    try:
        handle = _open_verified_archive(record, service)
        repository.touch_last_used(record.id)
    except Exception:
        service.release_download_lease(record.id, owner_id)
        raise
    filename = f"{record.package_id}-{record.version}.astinuslab"
    return StreamingResponse(
        _stream_archive(handle, release=lambda: service.release_download_lease(record.id, owner_id)),
        media_type="application/vnd.astinuslab.lab+zip",
        headers={
            "Content-Length": str(record.archive_size_bytes),
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


def _open_verified_archive(record: Any, service: LabPackageService):
    """Open and verify a retained archive, pinning the response to its fd."""

    path = Path(record.archive_path)
    try:
        inspected = path.lstat()
        package_root = service.package_root.resolve(strict=True)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise HTTPException(status_code=410, detail="retained package archive is unavailable") from exc
    if (
        stat.S_ISLNK(inspected.st_mode)
        or not stat.S_ISREG(inspected.st_mode)
        or not resolved.is_relative_to(package_root)
    ):
        raise HTTPException(status_code=410, detail="retained package archive is unavailable")

    descriptor: int | None = None
    try:
        descriptor = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size != record.archive_size_bytes:
            raise HTTPException(status_code=410, detail="retained package archive failed integrity checks")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, PACKAGE_COPY_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            total != record.archive_size_bytes
            or digest.hexdigest() != record.archive_sha256
            or (before.st_dev, before.st_ino, before.st_size) != (after.st_dev, after.st_ino, after.st_size)
        ):
            raise HTTPException(status_code=410, detail="retained package archive failed integrity checks")
        os.lseek(descriptor, 0, os.SEEK_SET)
        handle = os.fdopen(descriptor, "rb", closefd=True)
        descriptor = None
        return handle
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(status_code=410, detail="retained package archive is unavailable") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _stream_archive(handle, *, release=None):
    try:
        while chunk := handle.read(PACKAGE_COPY_CHUNK_BYTES):
            yield chunk
    finally:
        handle.close()
        if release is not None:
            release()


def _issue_response(issue: PreflightIssue) -> dict[str, str]:
    return {"severity": issue.severity, "code": issue.code, "message": issue.message}


def _available_asset_digests(definition: LabDefinition, service: LabPackageService) -> frozenset[str]:
    available: set[str] = set()
    for vm in definition.vms:
        layer = vm.effective_boot_layer
        if layer is None:
            continue
        try:
            if service.content_store.has(layer.sha256, size_bytes=layer.size_bytes):
                available.add(layer.sha256)
        except ContentStoreError:
            continue
    return frozenset(available)


def _requested_capabilities(definition: LabDefinition) -> tuple[str, ...]:
    """Capabilities requested by this launch, independent of package claims."""

    return ("guest_egress",) if any(network.guest_egress for network in definition.networks) else ()


def _warning_token(record: Any, definition: LabDefinition, result: Any) -> str:
    return issue_warning_consent_token(
        package_digest=record.archive_sha256,
        warnings=result.warnings,
        policy_revision=policy_revision(),
        requested_capabilities=_requested_capabilities(definition),
    )


def _package_preflight(
    record: Any,
    definition: LabDefinition,
    manifest: LabManifest,
    service: LabPackageService,
    session: Session,
):
    """Use identical real-host preflight inputs for display and launch."""

    return preflight_package(
        definition,
        manifest,
        storage_path=service.package_root,
        content_store_path=service.content_store.root,
        playground_disk_path=settings.playground_disk_dir,
        available_assets=_available_asset_digests(definition, service),
        session=session,
        package_digest=record.archive_sha256,
        signature_status=record.signature_status,
        retained_present=True,
        runner=request_runner_facts(),
    )


def _prepare_response(handle: PrepareHandle) -> LabPackagePrepareOut:
    return LabPackagePrepareOut(
        id=handle.id,
        package_record_id=handle.package_record_id,
        state=handle.state,
        progress_percent=handle.progress_percent,
        error_summary=handle.error_summary,
        network_opt_in=handle.network_opt_in,
        assets=[
            LabPackagePrepareAssetOut(
                id=asset.id,
                logical_path=asset.logical_path,
                sha256=asset.sha256,
                size_bytes=asset.size_bytes,
                state=asset.state,
                origin=asset.origin,
                error_summary=asset.error_summary,
            )
            for asset in handle.assets
        ],
    )


@router.post("/{record_id}/prepare", response_model=LabPackagePrepareOut, status_code=status.HTTP_202_ACCEPTED)
def prepare_lab_package(
    record_id: str,
    payload: LabPackagePrepareRequest,
    session: Session = Depends(get_session),
) -> LabPackagePrepareOut:
    from .package_catalog import LabPackageRepository

    service = default_package_service()
    record = LabPackageRepository(session, archive_root=service.package_root).get_by_id(record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="lab package not found")
    try:
        handle = create_prepare(
            session,
            record=record,
            service=service,
            idempotency_key=payload.idempotency_key,
            network_opt_in=(
                payload.allow_source_network if payload.allow_source_network is not None else payload.network_opt_in
            ),
        )
    except PrepareError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc
    if handle.created:
        session.info.setdefault("package_post_commit", []).append(
            lambda attempt_id=handle.id: dispatch_prepare_after_commit(attempt_id)
        )
    return _prepare_response(handle)


@router.get("/prepare/{attempt_id}", response_model=LabPackagePrepareOut)
def get_prepare_status(attempt_id: str, session: Session = Depends(get_session)) -> LabPackagePrepareOut:
    try:
        return _prepare_response(status_prepare(session, attempt_id))
    except PrepareError as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code, "message": str(exc)}) from exc


@router.get("/{record_id}/prepare/{attempt_id}", response_model=LabPackagePrepareOut)
def get_record_prepare_status(
    record_id: str, attempt_id: str, session: Session = Depends(get_session)
) -> LabPackagePrepareOut:
    try:
        handle = status_prepare(session, attempt_id)
    except PrepareError as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code, "message": str(exc)}) from exc
    if handle.package_record_id != record_id:
        raise HTTPException(status_code=404, detail="package preparation not found")
    return _prepare_response(handle)


@router.post("/prepare/{attempt_id}/cancel", response_model=LabPackagePrepareOut, status_code=status.HTTP_202_ACCEPTED)
def cancel_prepare_operation(attempt_id: str, session: Session = Depends(get_session)) -> LabPackagePrepareOut:
    try:
        return _prepare_response(cancel_prepare(session, attempt_id, service=default_package_service()))
    except PrepareError as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code, "message": str(exc)}) from exc


@router.post(
    "/{record_id}/prepare/{attempt_id}/cancel",
    response_model=LabPackagePrepareOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def cancel_record_prepare_operation(
    record_id: str, attempt_id: str, session: Session = Depends(get_session)
) -> LabPackagePrepareOut:
    try:
        handle = cancel_prepare(session, attempt_id, service=default_package_service())
    except PrepareError as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code, "message": str(exc)}) from exc
    if handle.package_record_id != record_id:
        raise HTTPException(status_code=404, detail="package preparation not found")
    return _prepare_response(handle)
