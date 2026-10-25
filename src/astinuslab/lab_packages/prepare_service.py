"""Durable acquisition of package assets before a lab launch.

Preparation is intentionally a separate operation from launch.  It resolves
only immutable, digest-addressed bytes and records the verified result before
the launch service is allowed to use it.  Embedded bytes, the local
content-store, and declared HTTPS sources are tried in that order.  A failed
source transfer leaves only the content-store's private resumable prefix; the
final path is published by :class:`ContentStore` only after a complete digest
and size check.
"""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from ..core.config import settings
from ..db import session_scope
from ..db.models import (
    LabContentObject,
    LabOperationOutbox,
    LabPackage,
    LabPackageAsset,
    LabPackageTombstone,
    LabPrepareAttempt,
    LabStorageLease,
)
from ..db.transactions import reserve_sqlite_write
from ..jobs.celery_app import celery_app
from .archive import ArchiveInspection
from .canonical import canonical_sha256
from .content_store import ContentStoreError, InvalidCachedAssetError, StoredAsset
from .models import AssetReference, LabDefinition
from .package_service import LabPackageService, PackageDeletionConflictError
from .prepare_authority import (
    PrepareClaim,
    PrepareClaimLost,
    PreparePackageBusy,
    PreparePackageLock,
    end_read_transaction,
    owned_prepare_write,
)
from .resolver import AssetResolutionError, AssetResolver, ResolutionDiagnostic, ResolvedAsset, ResolverPolicy

PREPARE_LEASE_SECONDS = 60 * 60
PREPARE_STATES = frozenset({"reserved", "running", "completed", "failed", "cancelled"})
PREPARE_ASSET_STATES = frozenset({"missing", "resolving", "ready", "failed", "cancelled"})


class PrepareError(ValueError):
    """Raised for an invalid, unsafe, or unavailable prepare operation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PrepareAssetHandle:
    id: str
    logical_path: str
    sha256: str
    size_bytes: int
    state: str
    origin: str | None = None
    error_summary: str | None = None


@dataclass(frozen=True, slots=True)
class PrepareHandle:
    id: str
    package_record_id: str
    state: str
    progress_percent: int
    error_summary: str | None = None
    network_opt_in: bool = False
    assets: tuple[PrepareAssetHandle, ...] = ()
    created: bool = False


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _load_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _definition(record: LabPackage) -> LabDefinition:
    try:
        return LabDefinition.model_validate(json.loads(record.definition_json))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PrepareError("invalid_package_definition", "stored package definition is invalid") from exc


def _asset_reference(record: LabPackage, asset: LabPackageAsset, definition: LabDefinition) -> AssetReference:
    """Recover the immutable contract, retaining compatibility with old rows."""

    for vm in definition.vms:
        reference = vm.effective_boot_layer
        if reference is not None and reference.sha256 == asset.expected_sha256:
            return reference
    sources = _load_json(asset.source_json, [])
    if not isinstance(sources, list) or not all(isinstance(source, str) for source in sources):
        sources = []
    try:
        return AssetReference(
            sha256=asset.expected_sha256,
            size_bytes=asset.expected_size_bytes,
            media_type=asset.media_type,
            sources=tuple(sources),
            embedded=asset.origin in {"embedded", "embedded-fallback"},
            license=record.license,
            # This flag controls export policy.  Preparation only verifies
            # bytes, and must not invent a redistribution claim.
            redistribution_confirmed=False,
        )
    except ValueError as exc:
        raise PrepareError("invalid_asset_contract", "package asset contract is invalid") from exc


def _package_assets(session: Session, record_id: str) -> list[LabPackageAsset]:
    return list(
        session.scalars(
            select(LabPackageAsset)
            .where(
                LabPackageAsset.package_record_id == record_id,
                or_(
                    LabPackageAsset.role == "boot-layer",
                    LabPackageAsset.logical_path.like("assets/sha256/%"),
                ),
            )
            .order_by(LabPackageAsset.logical_path, LabPackageAsset.id)
        )
    )


def _asset_handles(session: Session, record_id: str) -> tuple[PrepareAssetHandle, ...]:
    rows = _package_assets(session, record_id)
    return tuple(
        PrepareAssetHandle(
            id=row.id,
            logical_path=row.logical_path,
            sha256=row.expected_sha256,
            size_bytes=row.expected_size_bytes,
            state=row.prepare_state,
            origin=row.prepared_origin or row.origin,
            error_summary=row.prepare_error_summary,
        )
        for row in rows
    )


def _handle(session: Session, attempt: LabPrepareAttempt, *, created: bool = False) -> PrepareHandle:
    return PrepareHandle(
        id=attempt.id,
        package_record_id=attempt.package_record_id,
        state=attempt.state,
        progress_percent=attempt.progress_percent,
        error_summary=attempt.error_summary,
        network_opt_in=attempt.network_opt_in,
        assets=_asset_handles(session, attempt.package_record_id),
        created=created,
    )


def _release_leases(session: Session, attempt: LabPrepareAttempt, *, package: LabPackage | None = None) -> None:
    session.execute(update(LabStorageLease).where(LabStorageLease.owner_id == attempt.id).values(expires_at=_now()))
    # Terminal state, outbox completion and lease release commit together.
    # Package assets continue to retain ownership of verified content.
    session.query(LabStorageLease).filter(LabStorageLease.owner_id == attempt.id).delete(synchronize_session=False)


def _asset_result(attempt: LabPrepareAttempt) -> dict[str, Any]:
    payload = _load_json(attempt.result_json, {})
    if not isinstance(payload, dict):
        payload = {}
    assets = payload.get("assets")
    if not isinstance(assets, dict):
        assets = {}
    payload["assets"] = assets
    return payload


def _diagnostic_payload(diagnostics: tuple[ResolutionDiagnostic, ...]) -> list[dict[str, str]]:
    """Persist stable reasons without leaking a source URL or local path."""

    result: list[dict[str, str]] = []
    for item in diagnostics:
        candidate = item.candidate
        if candidate.startswith("http://") or candidate.startswith("https://"):
            candidate = "source"
        elif candidate.startswith("source"):
            candidate = "source"
        result.append({"candidate": candidate, "reason": item.reason[:256]})
    return result


def _archive_embedded_sources(service: LabPackageService, archive_path: Path) -> ArchiveInspection | None:
    """Read fallback inputs only after the caller has content mutation authority."""
    try:
        return service.inspect(archive_path)[0]
    except Exception:
        # An absent/corrupt embedded archive is represented as a failed
        # embedded candidate.  The resolver may still use an explicitly
        # allowed pinned source.
        return None


def _embedded_source(inspection: ArchiveInspection | None, logical_path: str) -> tuple[str, Any] | None:
    if inspection is None:
        return None
    if logical_path in inspection.assets:
        return "bytes", inspection.assets[logical_path]
    if logical_path in inspection.asset_descriptors:
        return "descriptor", inspection.asset_descriptors[logical_path]
    return None


def _outbox(session: Session, attempt_id: str) -> LabOperationOutbox | None:
    return session.scalar(
        select(LabOperationOutbox).where(
            LabOperationOutbox.operation_kind == "lab_prepare_assets",
            LabOperationOutbox.operation_id == attempt_id,
        )
    )


def _terminalize_locked(
    session: Session,
    attempt: LabPrepareAttempt,
    *,
    package_lock: PreparePackageLock,
    state: str,
    error: str | None = None,
) -> None:
    """Finish intent in an already reserved writer transaction under custody.

    Workers call only inside owned_prepare_write; idle cancellation/deletion
    hold the same package lock and writer reservation without an execution.
    """
    package_lock.require(attempt.package_record_id)
    assets = _package_assets(session, attempt.package_record_id)
    if attempt.cancel_requested:
        state, error = "cancelled", "prepare_cancelled: preparation was cancelled"
        for asset in assets:
            if asset.prepare_state in {"missing", "resolving", "failed"}:
                _set_asset_state(session, attempt, asset, "cancelled", error="cancelled by caller")
    payload = _asset_result(attempt)
    for asset in assets:
        entry = payload["assets"].setdefault(asset.id, {})
        entry.update(state=asset.prepare_state, origin=asset.prepared_origin, error=asset.prepare_error_summary)
    attempt.state = state
    attempt.progress_percent = 100
    attempt.error_summary = error
    attempt.result_json = _json(payload)
    event = _outbox(session, attempt.id)
    if event is not None:
        event.state = "completed"
        event.completed_at = _now()
    _release_leases(session, attempt)


def _claim_attempt(
    session: Session,
    attempt_id: str,
    *,
    service: LabPackageService,
    package_lock: PreparePackageLock,
) -> PrepareClaim | None:
    end_read_transaction(session)
    try:
        reserve_sqlite_write(session)
        session.expire_all()
        attempt = session.get(LabPrepareAttempt, attempt_id)
        if attempt is None:
            session.rollback()
            return None
        package_lock.require(attempt.package_record_id)
        event = _outbox(session, attempt_id)
        if attempt.state not in {"reserved", "running"} or event is None or event.state not in {"pending", "claimed"}:
            session.rollback()
            return None
        if session.scalar(
            select(LabPackageTombstone.id).where(
                LabPackageTombstone.package_record_id == attempt.package_record_id,
                LabPackageTombstone.state.in_(("pending", "cleanup_incomplete")),
            )
        ):
            session.rollback()
            return None
        if session.scalar(
            select(LabPrepareAttempt.id).where(
                LabPrepareAttempt.package_record_id == attempt.package_record_id,
                LabPrepareAttempt.id != attempt.id,
                LabPrepareAttempt.state.in_(("reserved", "running")),
            )
        ):
            raise PrepareError("prepare_conflict", "package has another nonterminal preparation")
        if session.get(LabPackage, attempt.package_record_id) is None:
            _terminalize_locked(
                session,
                attempt,
                package_lock=package_lock,
                state="failed",
                error="package_deleted: package was removed before preparation",
            )
            session.commit()
            return None
        _acquire_prepare_leases(session, service, attempt, _package_assets(session, attempt.package_record_id))
        token = uuid4().hex
        attempt.execution_token = token
        attempt.state = "running"
        attempt.claimed_at = _now()
        attempt.error_summary = None
        event.state = "claimed"
        event.claimed_at = _now()
        claim = PrepareClaim(attempt.id, attempt.package_record_id, token)
        session.commit()
        return claim
    except BaseException:
        session.rollback()
        raise


def _retain_owned_content(
    session: Session, service: LabPackageService, attempt: LabPrepareAttempt, asset: LabPackageAsset
) -> bool:
    """SQL-only classification; retention of owned bytes never grants mutation."""
    digest, size = asset.expected_sha256, asset.expected_size_bytes
    if (asset.content_sha256, asset.content_size_bytes) != (digest, size):
        return False
    content = session.get(LabContentObject, {"sha256": digest, "size_bytes": size}, populate_existing=True)
    # path_for secures/creates directories. Admission must inspect only the
    # deterministic mapping, without filesystem I/O under SQLite's writer.
    expected = service.content_store.root / "sha256" / digest[:2] / digest
    if content is None or Path(content.path) != expected:
        return False
    pin = session.scalar(
        select(LabStorageLease).where(
            LabStorageLease.resource_kind == "content",
            LabStorageLease.resource_id == digest,
            LabStorageLease.owner_id == attempt.id,
        )
    )
    if pin is None:
        session.add(
            LabStorageLease(
                id=f"lpl-{uuid4().hex[:16]}",
                resource_kind="content",
                resource_id=digest,
                owner_id=attempt.id,
                operation_kind="package_prepare_retention",
                expires_at=_now() + timedelta(seconds=PREPARE_LEASE_SECONDS),
            )
        )
    elif pin.operation_kind not in {"package_prepare_retention", "package_prepare_content"}:
        raise PrepareError("prepare_conflict", "content retention owner has another operation lease")
    return True


def _acquire_prepare_leases(
    session: Session,
    service: LabPackageService,
    attempt: LabPrepareAttempt,
    assets: list[LabPackageAsset],
) -> None:
    """Restore complete custody before reopening intent in the caller transaction."""

    expires_at = _now() + timedelta(seconds=PREPARE_LEASE_SECONDS)
    resources: list[tuple[str, str, str, LabPackageAsset | None]] = [
        ("package", attempt.package_record_id, "package_prepare", None)
    ]
    resources.extend(("content", asset.expected_sha256, "package_prepare_content", asset) for asset in assets)
    try:
        for kind, resource_id, operation, asset in resources:
            if asset is None or not _retain_owned_content(session, service, attempt, asset):
                service._acquire_lease(  # noqa: SLF001 - lease conflict policy is service-owned
                    session,
                    kind=kind,
                    resource_id=resource_id,
                    owner_id=attempt.id,
                    operation=operation,
                    ttl_seconds=PREPARE_LEASE_SECONDS,
                )
            session.flush()
            session.execute(
                update(LabStorageLease)
                .where(
                    LabStorageLease.resource_kind == kind,
                    LabStorageLease.resource_id == resource_id,
                    LabStorageLease.owner_id == attempt.id,
                )
                .values(expires_at=expires_at)
            )
    except PackageDeletionConflictError as exc:
        raise PrepareError("prepare_conflict", str(exc)) from exc
    attempt.expires_at = expires_at


def create_prepare(
    session: Session,
    *,
    record: LabPackage,
    service: LabPackageService,
    idempotency_key: str,
    network_opt_in: bool = False,
) -> PrepareHandle:
    """Reserve under package custody; caller retains the writer through commit."""
    if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 128:
        raise PrepareError("invalid_idempotency_key", "prepare requires a non-empty idempotency key")
    record_id = record.id
    try:
        with service.try_prepare_package_lock(record_id):
            reserve_sqlite_write(session)
            # Do not discard staged caller changes; refresh only admission rows.
            session.flush()
            current_record = session.get(LabPackage, record_id, populate_existing=True)
            if current_record is None or session.scalar(
                select(LabPackageTombstone.id).where(
                    LabPackageTombstone.package_record_id == record_id,
                    LabPackageTombstone.state.in_(("pending", "cleanup_incomplete")),
                )
            ):
                raise PrepareError("prepare_conflict", "package is being deleted")
            if session.scalar(
                select(LabPrepareAttempt.id).where(
                    LabPrepareAttempt.package_record_id == record_id,
                    LabPrepareAttempt.idempotency_key != idempotency_key,
                    LabPrepareAttempt.state.in_(("reserved", "running")),
                )
            ):
                raise PrepareError("prepare_conflict", "package has another nonterminal preparation")
            return _create_prepare_locked(
                session,
                record=current_record,
                service=service,
                idempotency_key=idempotency_key,
                network_opt_in=network_opt_in,
            )
    except PreparePackageBusy as exc:
        # A duplicate request may observe live intent, but must not renew leases,
        # reopen the outbox or dispatch again. A different key is a conflict.
        existing = session.scalar(
            select(LabPrepareAttempt)
            .where(
                LabPrepareAttempt.package_record_id == record_id,
                LabPrepareAttempt.idempotency_key == idempotency_key,
            )
            .execution_options(populate_existing=True)
        )
        if existing is None:
            raise PrepareError("prepare_conflict", "package preparation is active") from exc
        if existing.network_opt_in != network_opt_in:
            raise PrepareError(
                "idempotency_conflict", "prepare key was already used with different network policy"
            ) from exc
        return _handle(session, existing)


def _create_prepare_locked(
    session: Session,
    *,
    record: LabPackage,
    service: LabPackageService,
    idempotency_key: str,
    network_opt_in: bool = False,
) -> PrepareHandle:
    """Reserve a prepare operation and its leases in the caller transaction."""

    if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 128:
        raise PrepareError("invalid_idempotency_key", "prepare requires a non-empty idempotency key")
    existing = session.scalar(
        select(LabPrepareAttempt)
        .where(
            LabPrepareAttempt.package_record_id == record.id,
            LabPrepareAttempt.idempotency_key == idempotency_key,
        )
        .execution_options(populate_existing=True)
    )
    if existing is not None:
        if existing.network_opt_in != network_opt_in:
            raise PrepareError("idempotency_conflict", "prepare key was already used with different network policy")
        if network_opt_in and not settings.packages_allow_source_network:
            # A retry of the exact same operation is still idempotent, even if
            # the local policy changed after its reservation.  The worker will
            # retain the original result rather than silently changing scope.
            return _handle(session, existing)
        if existing.state in {"failed", "running"}:
            _acquire_prepare_leases(session, service, existing, _package_assets(session, record.id))
            # Failed transfers retain their verified prefix and ready assets.
            # Reusing the same client key is an explicit resume, not a second
            # operation, so wake its one durable outbox event again.
            existing.state = "reserved"
            existing.cancel_requested = False
            existing.error_summary = None
            existing.claimed_at = None
            event = session.scalar(
                select(LabOperationOutbox).where(
                    LabOperationOutbox.operation_kind == "lab_prepare_assets",
                    LabOperationOutbox.operation_id == existing.id,
                )
            )
            if event is not None:
                event.state = "pending"
                event.claimed_at = None
                event.completed_at = None
            session.flush()
            return _handle(session, existing, created=True)
        return _handle(session, existing)
    if network_opt_in and not settings.packages_allow_source_network:
        raise PrepareError("resolver_policy_denied", "source network access is disabled by local policy")
    definition = _definition(record)
    assets = _package_assets(session, record.id)
    now = _now()
    attempt = LabPrepareAttempt(
        id=f"lpa-{uuid4().hex[:16]}",
        package_record_id=record.id,
        definition_sha256=canonical_sha256(definition),
        idempotency_key=idempotency_key,
        state="reserved",
        result_json=_json({"assets": {}, "diagnostics": [], "network_opt_in": network_opt_in}),
        progress_percent=0,
        cancel_requested=False,
        network_opt_in=network_opt_in,
        expires_at=now + timedelta(seconds=PREPARE_LEASE_SECONDS),
    )
    session.add(attempt)
    session.flush()
    _acquire_prepare_leases(session, service, attempt, assets)
    session.add(
        LabOperationOutbox(
            id=f"lbo-{uuid4().hex[:16]}",
            operation_kind="lab_prepare_assets",
            operation_id=attempt.id,
            payload_json=_json({"attempt_id": attempt.id, "package_record_id": record.id}),
            state="pending",
        )
    )
    for asset in assets:
        asset.prepare_state = "missing"
        asset.prepare_attempt_id = attempt.id
        asset.prepare_error_summary = None
    session.flush()
    return _handle(session, attempt, created=True)


def status_prepare(session: Session, attempt_id: str) -> PrepareHandle:
    attempt = session.get(LabPrepareAttempt, attempt_id)
    if attempt is None:
        raise PrepareError("prepare_not_found", "package preparation was not found")
    return _handle(session, attempt)


def cancel_prepare(
    session: Session,
    attempt_id: str,
    *,
    service: LabPackageService | None = None,
) -> PrepareHandle:
    reserve_sqlite_write(session)
    attempt = session.get(LabPrepareAttempt, attempt_id, populate_existing=True)
    if attempt is None:
        raise PrepareError("prepare_not_found", "package preparation was not found")
    if attempt.state not in {"completed", "failed", "cancelled"}:
        attempt.cancel_requested = True
        session.flush()
        try:
            # The sole writer->file-lock exception is this nonblocking try.
            # A live resolver needs the writer released to observe its flag.
            with (service or LabPackageService()).try_prepare_package_lock(attempt.package_record_id) as lock:
                _terminalize_locked(session, attempt, package_lock=lock, state="cancelled")
        except PreparePackageBusy:
            pass
        session.flush()
    return _handle(session, attempt)


def _set_asset_state(
    session: Session,
    attempt: LabPrepareAttempt,
    asset: LabPackageAsset,
    state: str,
    *,
    origin: str | None = None,
    error: str | None = None,
) -> None:
    if state not in PREPARE_ASSET_STATES:
        raise ValueError("invalid prepare asset state")
    asset.prepare_state = state
    asset.prepare_attempt_id = attempt.id
    asset.prepare_error_summary = error[:1000] if error else None
    if origin is not None:
        asset.prepared_origin = origin
    asset.prepared_at = _now() if state == "ready" else None


def _persist_content(session: Session, service: LabPackageService, asset: LabPackageAsset, path: Path) -> None:
    try:
        fingerprint = service._content_fingerprint(path)  # noqa: SLF001
    except OSError as exc:
        raise PrepareError("content_unavailable", "verified content disappeared") from exc
    row = session.get(LabContentObject, {"sha256": asset.expected_sha256, "size_bytes": asset.expected_size_bytes})
    now = _now()
    if row is None:
        session.add(
            LabContentObject(
                sha256=asset.expected_sha256,
                size_bytes=asset.expected_size_bytes,
                path=str(path),
                filesystem_fingerprint=fingerprint,
                last_verified_at=now,
                last_used_at=now,
            )
        )
    else:
        row.path = str(path)
        row.filesystem_fingerprint = fingerprint
        row.last_verified_at = now
        row.last_used_at = now
    asset.content_sha256 = asset.expected_sha256
    asset.content_size_bytes = asset.expected_size_bytes


def run_prepare(
    session: Session,
    attempt_id: str,
    *,
    service: LabPackageService | None = None,
) -> PrepareHandle:
    """Execute under process custody; recover immediately after process death."""
    end_read_transaction(session)
    current = session.get(LabPrepareAttempt, attempt_id)
    if current is None:
        raise PrepareError("prepare_not_found", "package preparation was not found")
    record_id = current.package_record_id
    end_read_transaction(session)
    package_service = service or LabPackageService()
    try:
        with package_service.try_prepare_package_lock(record_id) as lock:
            claim = _claim_attempt(session, attempt_id, service=package_service, package_lock=lock)
            if claim is None:
                return status_prepare(session, attempt_id)
            try:
                _execute_prepare(session, claim, package_service, lock)
            except Exception as exc:
                session.rollback()
                if not isinstance(exc, PrepareClaimLost):
                    try:
                        with owned_prepare_write(session, claim, package_lock=lock) as attempt:
                            _terminalize_locked(
                                session,
                                attempt,
                                package_lock=lock,
                                state="failed",
                                error=f"prepare_failed: {_reason(exc)}"[:1000],
                            )
                    except Exception:
                        # Never mask the resolver/commit failure with a secondary
                        # cleanup failure, and never mutate after losing custody.
                        session.rollback()
                raise
            return status_prepare(session, attempt_id)
    except PreparePackageBusy:
        return status_prepare(session, attempt_id)


def _record_ready_asset(
    session: Session,
    claim: PrepareClaim,
    service: LabPackageService,
    lock: PreparePackageLock,
    asset_id: str,
    item: StoredAsset | ResolvedAsset,
    *,
    origin: str,
    progress: int,
    diagnostics: tuple[ResolutionDiagnostic, ...] = (),
    preserve_ready: bool = False,
) -> bool:
    """Commit ownership before digest unlock; return whether cancellation won."""
    with owned_prepare_write(session, claim, package_lock=lock) as attempt:
        asset = session.get(LabPackageAsset, asset_id)
        if asset is None:
            raise PrepareClaimLost("package asset disappeared during preparation")
        if (
            preserve_ready
            and asset.prepare_state == "ready"
            and (asset.content_sha256, asset.content_size_bytes) == (item.sha256, item.size_bytes)
        ):
            if attempt.cancel_requested:
                _terminalize_locked(session, attempt, package_lock=lock, state="cancelled")
                return True
            return False
        _persist_content(session, service, asset, item.path)
        _set_asset_state(session, attempt, asset, "ready", origin=origin)
        payload = _asset_result(attempt)
        payload["assets"][asset.id] = {
            "state": "ready",
            "origin": origin,
            "sha256": item.sha256,
            "size_bytes": item.size_bytes,
            "diagnostics": _diagnostic_payload(diagnostics),
        }
        attempt.result_json = _json(payload)
        attempt.progress_percent = max(attempt.progress_percent, progress)
        if attempt.cancel_requested:
            _terminalize_locked(session, attempt, package_lock=lock, state="cancelled")
            return True
    return False


def _authorize_content_mutation(
    session: Session,
    claim: PrepareClaim,
    service: LabPackageService,
    lock: PreparePackageLock,
    reference: AssetReference,
) -> bool:
    """A retention pin may proceed to resolver I/O only after exclusive custody."""
    with owned_prepare_write(session, claim, package_lock=lock) as attempt:
        if attempt.cancel_requested:
            _terminalize_locked(session, attempt, package_lock=lock, state="cancelled")
            return False
        try:
            service._acquire_lease(  # noqa: SLF001 - exclusive service authority remains unchanged
                session,
                kind="content",
                resource_id=reference.sha256,
                owner_id=attempt.id,
                operation="package_prepare_content",
                ttl_seconds=PREPARE_LEASE_SECONDS,
            )
        except PackageDeletionConflictError as exc:
            raise PrepareError("prepare_conflict", str(exc)) from exc
    # Preserve unsafe links even if no other operation currently has a lease.
    path = service.content_store.path_for(reference.sha256)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return True
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise PrepareError("content_unavailable", "cached content is not a single-link regular file")
    return True


def _execute_prepare(
    session: Session,
    claim: PrepareClaim,
    service: LabPackageService,
    lock: PreparePackageLock,
) -> None:
    package = session.get(LabPackage, claim.package_record_id)
    if package is None:
        raise PrepareClaimLost("package disappeared during preparation")
    assets = _package_assets(session, package.id)
    definition = _definition(package)
    archive_path = Path(package.archive_path)
    needs_embedded = any(asset.origin in {"embedded", "embedded-fallback"} for asset in assets)
    inspection = None
    inspection_loaded = False
    # Copy immutable inputs before ending reads. No lazy ORM access may open a
    # transaction during digest-lock waits or resolver I/O.
    inputs = [(asset.id, asset.logical_path, _asset_reference(package, asset, definition)) for asset in assets]
    end_read_transaction(session)
    had_failure = False
    for index, (asset_id, logical_path, reference) in enumerate(inputs, start=1):
        with owned_prepare_write(session, claim, package_lock=lock) as attempt:
            if attempt.cancel_requested:
                _terminalize_locked(session, attempt, package_lock=lock, state="cancelled")
                return
            asset = session.get(LabPackageAsset, asset_id)
            if asset is None:
                raise PrepareClaimLost("package asset disappeared during preparation") from None
            if asset.prepare_state != "ready":
                _set_asset_state(session, attempt, asset, "resolving")
            attempt.progress_percent = max(attempt.progress_percent, int(5 + 90 * (index - 1) / max(1, len(inputs))))
        try:
            with service.content_lock(reference.sha256):
                # Cancellation can arrive while waiting for another digest owner.
                with owned_prepare_write(session, claim, package_lock=lock) as attempt:
                    if attempt.cancel_requested:
                        _terminalize_locked(session, attempt, package_lock=lock, state="cancelled")
                        return
                    network_opt_in = attempt.network_opt_in
                    content = session.get(
                        LabContentObject, {"sha256": reference.sha256, "size_bytes": reference.size_bytes}
                    )
                    expected_path = service.content_store.root / "sha256" / reference.sha256[:2] / reference.sha256
                    if content is not None and Path(content.path) != expected_path:
                        raise PrepareError(
                            "content_unavailable", "cached content identity is bound to a different path"
                        )
                progress = int(5 + 90 * index / max(1, len(inputs)))
                try:
                    with service.content_store.locked_cached_asset(
                        reference.sha256, size_bytes=reference.size_bytes
                    ) as cached:
                        if cached is not None:
                            if _record_ready_asset(
                                session,
                                claim,
                                service,
                                lock,
                                asset_id,
                                cached,
                                origin="cache",
                                progress=progress,
                                preserve_ready=True,
                            ):
                                return
                            continue
                except InvalidCachedAssetError:
                    # Only definite corruption may enter exclusive repair.
                    # I/O, permission, alias and concurrent-change failures stop.
                    pass
                if not _authorize_content_mutation(session, claim, service, lock, reference):
                    return
                if needs_embedded and not inspection_loaded:
                    inspection = _archive_embedded_sources(service, archive_path)
                    inspection_loaded = True
                resolver = AssetResolver(
                    service.content_store,
                    policy=ResolverPolicy(
                        allow_http=False,
                        allow_source_network=network_opt_in,
                        allow_private_network=False,
                        allow_missing_embedded_fallback=True,
                        max_response_bytes=settings.packages_max_asset_bytes,
                    ),
                )
                source = _embedded_source(inspection, logical_path)
                if source is not None and source[0] == "bytes":
                    resolved = resolver.resolve(reference, embedded=source[1])
                elif source is not None and source[0] == "descriptor" and inspection is not None:
                    with inspection.open_asset(logical_path) as stream:
                        resolved = resolver.resolve(reference, embedded=stream)
                else:
                    if network_opt_in and not settings.packages_allow_source_network:
                        raise PrepareError(
                            "resolver_policy_denied", "source network access is disabled by local policy"
                        )
                    resolved = resolver.resolve(reference)
                if _record_ready_asset(
                    session,
                    claim,
                    service,
                    lock,
                    asset_id,
                    resolved,
                    origin=resolved.source_kind,
                    progress=progress,
                    diagnostics=resolved.diagnostics,
                ):
                    return
        except (PrepareError, AssetResolutionError, ContentStoreError, OSError, ValueError) as exc:
            had_failure = True
            code = exc.code if isinstance(exc, PrepareError) else "asset_unavailable"
            reason = str(exc) if isinstance(exc, PrepareError) else _reason(exc)
            with owned_prepare_write(session, claim, package_lock=lock) as attempt:
                asset = session.get(LabPackageAsset, asset_id)
                if asset is None:
                    raise PrepareClaimLost("package asset disappeared during preparation") from None
                _set_asset_state(session, attempt, asset, "failed", error=f"{code}: {reason}")
                payload = _asset_result(attempt)
                payload["assets"][asset.id] = {
                    "state": "failed",
                    "error": f"{code}: {reason}"[:1000],
                    "diagnostics": _diagnostic_payload(getattr(exc, "diagnostics", ())),
                }
                diagnostics = payload.setdefault("diagnostics", [])
                if not isinstance(diagnostics, list):
                    diagnostics = payload["diagnostics"] = []
                diagnostics.append({"asset": logical_path, "code": code, "message": reason[:256]})
                attempt.result_json = _json(payload)
                attempt.progress_percent = max(attempt.progress_percent, int(5 + 90 * index / max(1, len(inputs))))
                if attempt.cancel_requested:
                    _terminalize_locked(session, attempt, package_lock=lock, state="cancelled")
                    return
    with owned_prepare_write(session, claim, package_lock=lock) as attempt:
        _terminalize_locked(
            session,
            attempt,
            package_lock=lock,
            state="failed" if had_failure else "completed",
            error="prepare_required: one or more package assets could not be verified" if had_failure else None,
        )


def _reason(exc: BaseException) -> str:
    from .storage_custody import PackageCustodyError, failure_message

    cause = exc
    for _ in range(8):
        if isinstance(cause, (PackageCustodyError, PermissionError)):
            return failure_message("package storage permission refused", cause)[:256]
        if cause.__cause__ is None:
            break
        cause = cause.__cause__
    if isinstance(exc, AssetResolutionError):
        diagnostics = _diagnostic_payload(exc.diagnostics)
        if diagnostics:
            return "; ".join(f"{item['candidate']}: {item['reason']}" for item in diagnostics)[:256]
        return "all asset candidates failed"
    if isinstance(exc, ContentStoreError):
        return "content store rejected asset"
    if isinstance(exc, OSError):
        return "asset I/O failed"
    message = str(exc).strip()
    return message[:256] if message else exc.__class__.__name__.lower()


@celery_app.task(name="lab_packages.prepare_assets")
def prepare_assets_task(attempt_id: str) -> str:
    """Celery entry point; all input is recovered from the durable attempt."""

    with session_scope() as session:
        result = run_prepare(session, attempt_id)
        return result.id


def dispatch_prepare_after_commit(attempt_id: str) -> None:
    prepare_assets_task.delay(attempt_id)


def reconcile_prepares() -> int:
    """Requeue reserved/running attempts after a worker restart."""

    with session_scope() as session:
        pending = list(
            session.scalars(
                select(LabPrepareAttempt.id)
                .join(
                    LabOperationOutbox,
                    (LabOperationOutbox.operation_kind == "lab_prepare_assets")
                    & (LabOperationOutbox.operation_id == LabPrepareAttempt.id),
                )
                .where(
                    LabPrepareAttempt.state.in_(("reserved", "running")),
                    LabOperationOutbox.state.in_(("pending", "claimed")),
                )
            )
        )
    for attempt_id in pending:
        prepare_assets_task.delay(attempt_id)
    return len(pending)
