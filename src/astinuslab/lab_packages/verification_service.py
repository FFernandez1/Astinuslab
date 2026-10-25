"""Durable creator-side clean-run verification.

Verification is an explicit operation over a saved :class:`LabDraft`.  The
worker may be interrupted at any point; the attempt and its outbox event remain
the source of truth and a retry either resumes the same attempt or reports the
recorded result.  No package is marked locked merely because a Playground
definition could be serialized.
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import fake_mode
from ..core.config import settings
from ..db import session_scope
from ..db.models import (
    LabContentObject,
    LabDraft,
    LabOperationOutbox,
    LabPackageAttestation,
    LabStorageLease,
    LabVerificationAttempt,
    PlaygroundSession,
)
from ..jobs.celery_app import celery_app
from ..playground import acquisition as playground_acquisition
from ..playground import orchestrator, repositories
from .canonical import canonical_bytes, canonical_sha256
from .content_store import ContentStore, StoredAsset
from .export_service import PackageExportError, build_locked_definition, image_spec_from_catalog, materialize_boot_layer
from .models import (
    AssetReference,
    GuestAssertion,
    GuestObservation,
    LabDefinition,
    LabReadinessProbe,
    LabWorkloadSpec,
    validate_checkpoint,
)
from .verification_evaluators import build_checkpoint_evaluator, build_readiness_checker

VERIFICATION_EXPIRY_SECONDS = 24 * 60 * 60
VERIFICATION_LEASE_SECONDS = VERIFICATION_EXPIRY_SECONDS
OUTBOX_CLAIM_SECONDS = 5 * 60
CLEANUP_CLAIM_SECONDS = 30 * 60
MAX_SUPPORTING_FILES = 64
MAX_CHECKPOINTS = 64

logger = logging.getLogger(__name__)


class _LostOutboxClaim(RuntimeError):
    """A verification worker no longer owns its durable outbox lease."""


class _OutboxClaimHeartbeat:
    """Renew and fence one verification/cleanup outbox claim.

    ``claimed_at`` is both the worker lease and, after terminalization, the
    cleanup lease. Every update compares the last timestamp token, so a worker
    whose lease was reclaimed cannot later publish a result or keep renewing
    another worker's claim.
    """

    def __init__(
        self,
        *,
        attempt_id: str,
        token: datetime,
        session_scope_factory=None,
        interval_seconds: float | None = None,
        claim_states: tuple[str, ...] = ("claimed",),
    ) -> None:
        self.attempt_id = attempt_id
        self._token = token
        self._session_scope = session_scope_factory
        self._interval = interval_seconds or max(1.0, min(60.0, OUTBOX_CLAIM_SECONDS / 3))
        self._claim_states = claim_states
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def _next_token(previous: datetime) -> datetime:
        refreshed = _now()
        if refreshed <= previous:
            refreshed = previous + timedelta(microseconds=1)
        return refreshed

    def _renew_locked(self, session: Session, *, commit: bool) -> None:
        if self._lost.is_set():
            raise _LostOutboxClaim(f"verification {self.attempt_id} lost its worker claim")
        attempt_state = session.scalar(
            select(LabVerificationAttempt.state).where(LabVerificationAttempt.id == self.attempt_id)
        )
        claim_states = self._claim_states
        if claim_states == ("claimed",) and attempt_state not in {"reserved", "running"}:
            # A direct heartbeat constructed by older callers may be handed a
            # terminal cleanup marker. Adopt that marker only when the outbox
            # is already terminal; an old active-worker token still cannot
            # renew because its claimed_at value has been fenced.
            terminal_state = session.scalar(
                select(LabOperationOutbox.state).where(
                    LabOperationOutbox.operation_kind == "lab_verify_clean_run",
                    LabOperationOutbox.operation_id == self.attempt_id,
                )
            )
            if attempt_state in {"verified", "failed", "cancelled"} and terminal_state in {"completed", "failed"}:
                claim_states = (terminal_state,)
            else:
                self._lost.set()
                raise _LostOutboxClaim(f"verification {self.attempt_id} is no longer active")
        refreshed = self._next_token(self._token)
        renewed = (
            session.query(LabOperationOutbox)
            .filter(
                LabOperationOutbox.operation_kind == "lab_verify_clean_run",
                LabOperationOutbox.operation_id == self.attempt_id,
                LabOperationOutbox.state.in_(claim_states),
                LabOperationOutbox.claimed_at == self._token,
            )
            .update({LabOperationOutbox.claimed_at: refreshed}, synchronize_session=False)
        )
        if renewed != 1:
            session.rollback()
            self._lost.set()
            raise _LostOutboxClaim(f"verification {self.attempt_id} lost its worker claim")
        if commit:
            session.commit()
        else:
            session.flush()
        self._token = refreshed

    def owns_claim(self, session: Session) -> bool:
        """Return whether this worker still owns the durable verification claim."""

        return (
            session.scalar(
                select(LabOperationOutbox.id).where(
                    LabOperationOutbox.operation_kind == "lab_verify_clean_run",
                    LabOperationOutbox.operation_id == self.attempt_id,
                    LabOperationOutbox.state == "claimed",
                    LabOperationOutbox.claimed_at == self._token,
                )
            )
            is not None
        )

    def complete(self, session: Session, *, terminal_state: str) -> None:
        """Publish a terminal outbox state under the current claim token."""

        if terminal_state not in {"completed", "failed"}:
            raise ValueError("invalid terminal outbox state")
        with self._lock:
            if self._lost.is_set():
                raise _LostOutboxClaim(f"verification {self.attempt_id} lost its worker claim")
            refreshed = self._next_token(self._token)
            updated = (
                session.query(LabOperationOutbox)
                .filter(
                    LabOperationOutbox.operation_kind == "lab_verify_clean_run",
                    LabOperationOutbox.operation_id == self.attempt_id,
                    LabOperationOutbox.state == "claimed",
                    LabOperationOutbox.claimed_at == self._token,
                )
                .update(
                    {
                        LabOperationOutbox.state: terminal_state,
                        LabOperationOutbox.claimed_at: refreshed,
                        LabOperationOutbox.completed_at: _now(),
                    },
                    synchronize_session=False,
                )
            )
            if updated != 1:
                session.rollback()
                self._lost.set()
                raise _LostOutboxClaim(f"verification {self.attempt_id} lost its worker claim")
            session.commit()
            self._token = refreshed

    def refresh(self) -> None:
        """Renew using an independent short transaction."""

        scope = self._session_scope or session_scope
        with self._lock, scope() as session:
            self._renew_locked(session, commit=False)

    def commit_progress(self, session: Session) -> None:
        """Commit caller progress and the new fencing token atomically."""

        with self._lock:
            self._renew_locked(session, commit=True)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.refresh()
            except _LostOutboxClaim:
                return
            except Exception:
                # SQLite may briefly hold its single writer lock while a safe
                # checkpoint commits. A later tick retries; only a failed CAS
                # proves custody was actually lost.
                logger.warning("verification claim heartbeat failed for %s", self.attempt_id, exc_info=True)

    def start(self) -> None:
        # Renew synchronously so startup reconciliation cannot observe a stale
        # token between task claim and the first background tick.
        self.refresh()
        self._thread = threading.Thread(
            target=self._run,
            name=f"lab-verification-lease-{self.attempt_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)


class VerificationError(ValueError):
    """Raised for an invalid or unsafe clean-run request."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class DraftHandle:
    id: str
    digest: str


@dataclass(frozen=True, slots=True)
class VerificationHandle:
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


_VERIFICATION_FAILURES = {
    "checkpoint_failed": "A clean-run checkpoint did not pass. Review the checkpoint definition and guest state before rebuilding the lab.",
    "readiness_failed": "A workload did not become ready. Review its image and preparation requirements before rebuilding the lab.",
    "capture_failed": "The clean-start capture failed. Check runner logs using this verification ID before rebuilding the lab.",
    "vm_unavailable": "A source VM was unavailable for clean-start capture. Rebuild the authored lab before verifying again.",
    "base_image_unavailable": "A required base image was unavailable. Prepare the image before rebuilding the lab.",
    **dict.fromkeys(
        ("authored_session_unavailable", "authored_session_lost", "authored_session_unbound"),
        "The authored session is unavailable for this verification. Rebuild the lab before verifying again.",
    ),
    "verification_evaluator_unavailable": "The verification evaluator is unavailable. Ask the host maintainer to check runner configuration.",
    "fake_mode_unsupported": "Synthetic execution cannot produce an exportable clean-run attestation.",
    "verification_cancelled": "The clean-run verification was cancelled.",
    "verification_failed": "The clean-run verification failed. Check runner logs using this verification ID.",
}


def verification_handle(session: Session, attempt: LabVerificationAttempt) -> VerificationHandle:
    """Project public status without disclosing private diagnostic suffixes."""
    code = None
    if attempt.state == "cancelled":
        code = "verification_cancelled"
    elif attempt.state == "failed":
        prefix = (attempt.error_summary or "").partition(":")[0]
        code = prefix if prefix in _VERIFICATION_FAILURES else "verification_failed"
    draft = session.get(LabDraft, attempt.draft_id)
    definition = None
    if draft is not None:
        try:
            definition = LabDefinition.model_validate_json(draft.definition_json)
        except (ValueError, TypeError):
            pass
    expires, attestation_status = _attestation_read_metadata(session, attempt, draft)
    return VerificationHandle(
        attempt.id,
        attempt.draft_id,
        attempt.state,
        attempt.progress_percent,
        attempt.authored_session_id,
        code,
        _VERIFICATION_FAILURES[code] if code else None,
        definition.package_id if definition else None,
        definition.version if definition else None,
        definition.title if definition else None,
        expires,
        attestation_status,
    )


def _verified_payload(attempt: LabVerificationAttempt) -> tuple[LabDefinition, dict[str, AssetReference], str]:
    try:
        payload = json.loads(attempt.result_json or "null")
        locked = LabDefinition.model_validate(payload["locked_definition"])
        refs = {vm_id: AssetReference.model_validate(value) for vm_id, value in payload["boot_layers"].items()}
        attestation_id = payload["attestation_id"]
        if not isinstance(attestation_id, str) or not attestation_id:
            raise ValueError("invalid attestation ID")
        return locked, refs, attestation_id
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise VerificationError("invalid_result", "stored verification result is invalid") from exc


def _attestation_read_metadata(
    session: Session, attempt: LabVerificationAttempt, draft: LabDraft | None
) -> tuple[str | None, Literal["unexpired", "expired", "unavailable"]]:
    if attempt.state != "verified" or draft is None:
        return None, "unavailable"
    try:
        locked, refs, attestation_id = _verified_payload(attempt)
        attestation = session.get(LabPackageAttestation, attestation_id)
        if (
            attestation is None
            or attestation.input_draft_sha256 != draft.definition_sha256
            or attestation.locked_definition_sha256 != canonical_sha256(locked)
            or any(refs.get(vm.id) != vm.boot_layer for vm in locked.vms)
            or not isinstance(json.loads(attestation.runtime_json), dict)
        ):
            return None, "unavailable"
    except (VerificationError, ValueError, TypeError):
        return None, "unavailable"
    expiry = attestation.expires_at
    if expiry is None:
        return None, "expired"
    utc_expiry = expiry.replace(tzinfo=UTC) if expiry.tzinfo is None else expiry.astimezone(UTC)
    utc_now = _now().replace(tzinfo=UTC)
    return utc_expiry.isoformat().replace("+00:00", "Z"), "expired" if utc_expiry <= utc_now else "unexpired"


@dataclass(frozen=True, slots=True)
class CleanRunResult:
    attempt_id: str
    draft_id: str
    locked_definition: LabDefinition
    boot_layers: Mapping[str, AssetReference]
    attestation_id: str


def verified_result(session: Session, attempt_id: str) -> CleanRunResult:
    """Load a completed result and recheck its attestation before export."""

    attempt = _attempt_by_id(session, attempt_id)
    if attempt.state != "verified" or not attempt.result_json:
        raise VerificationError("verification_incomplete", "clean-run verification is not complete")
    locked, refs, attestation_id = _verified_payload(attempt)
    draft = session.get(LabDraft, attempt.draft_id)
    if draft is None:
        raise VerificationError("draft_not_found", "lab draft was not found")
    verify_attestation(session, attestation_id=attestation_id, draft_digest=draft.definition_sha256, definition=locked)
    # Ensure the result still has exactly the same independent layer identity
    # as the locked definition.  This catches hand-edited result JSON even if
    # the attestation row itself is intact.
    for vm in locked.vms:
        reference = refs.get(vm.id)
        if reference is None or vm.boot_layer != reference:
            raise VerificationError("attestation_mismatch", "attested boot layers do not match the definition")
    return CleanRunResult(attempt.id, draft.id, locked, refs, attestation_id)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _json(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise VerificationError("invalid_snapshot", "draft contains unsupported JSON") from exc


def _authored_session_is_live(session: Session, session_id: str | None) -> bool:
    if session_id is None:
        return False
    authored = session.get(PlaygroundSession, session_id)
    return authored is not None and authored.status in repositories.ACTIVE_PLAYGROUND_STATES


def save_draft(
    session: Session,
    definition: LabDefinition,
    *,
    runbook: str = "",
    supporting_files: Mapping[str, str] | None = None,
    authored_session_id: str | None = None,
) -> DraftHandle:
    """Persist the immutable creator inputs used by a later verification."""

    if not isinstance(definition, LabDefinition):
        raise TypeError("definition must be a LabDefinition")
    if definition.reproducibility == "locked":
        raise VerificationError("draft_must_be_unlocked", "a clean-run draft cannot claim a boot layer")
    if not isinstance(runbook, str) or len(runbook.encode("utf-8")) > 2 * 1024 * 1024:
        raise VerificationError("runbook_too_large", "runbook exceeds the supported size")
    if supporting_files is None:
        supporting_files = {}
    if len(supporting_files) > MAX_SUPPORTING_FILES:
        raise VerificationError("too_many_supporting_files", "too many supporting files")
    normalized_files: dict[str, str] = {}
    for path, digest in supporting_files.items():
        if (
            not isinstance(path, str)
            or not path.startswith("experience/")
            or path == "experience/runbook.md"
            or "/../" in f"/{path}/"
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise VerificationError("invalid_supporting_file", "supporting files must be digest references")
        normalized_files[path] = digest
    if authored_session_id is not None:
        authored = session.get(PlaygroundSession, authored_session_id)
        if authored is None:
            raise VerificationError("session_not_found", "authored Playground session was not found")
    definition_payload = json.loads(canonical_bytes(definition).decode("utf-8"))
    snapshot = {"definition": definition_payload, "runbook": runbook, "supporting_files": normalized_files}
    snapshot_json = _json(snapshot)
    digest = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
    existing = session.scalar(select(LabDraft).where(LabDraft.definition_sha256 == digest))
    if existing is not None:
        # The digest covers the authored inputs, not the session that produced
        # them, so re-saving an identical lab from a new authoring session must
        # rebind the draft. The previous pointer is only kept while it still
        # names a live session, which the singleton guarantees is this one.
        if (
            authored_session_id is not None
            and existing.authored_session_id != authored_session_id
            and not _authored_session_is_live(session, existing.authored_session_id)
        ):
            existing.authored_session_id = authored_session_id
            session.flush()
        return DraftHandle(existing.id, existing.definition_sha256)
    draft = LabDraft(
        id=f"ld-{uuid4().hex[:16]}",
        definition_json=canonical_bytes(definition).decode("utf-8"),
        definition_sha256=digest,
        runbook=runbook,
        supporting_files_json=_json(normalized_files),
        authored_session_id=authored_session_id,
    )
    session.add(draft)
    session.flush()
    return DraftHandle(draft.id, digest)


def _attempt_by_id(session: Session, attempt_id: str) -> LabVerificationAttempt:
    attempt = session.get(LabVerificationAttempt, attempt_id)
    if attempt is None:
        raise VerificationError("verification_not_found", "clean-run verification was not found")
    return attempt


def start_verification(
    session: Session,
    *,
    draft_id: str,
    authored_session_closed: bool = False,  # noqa: ARG001 - accepted for compatibility
) -> VerificationHandle:
    """Freeze the authored session, then create one durable attempt and event.

    Starting a verification *is* the "save and close" action: it performs the
    ``authoring_closed`` transition itself rather than trusting a client-side
    checkbox that the operator had already destroyed the lab.  The frozen
    session keeps its VMs, networks and overlays so the worker can observe and
    capture them; the worker owns cleanup afterwards.

    ``authored_session_closed`` is retained only so older clients keep working;
    its value is deliberately ignored.
    """

    # Serialize before the idempotency lookup. SQLite needs a database write
    # lock; databases with row locks serialize on the immutable draft row.
    repositories.serialize_write_transaction(session)
    draft = session.query(LabDraft).filter(LabDraft.id == draft_id).with_for_update().one_or_none()
    if draft is None:
        raise VerificationError("draft_not_found", "lab draft was not found")

    # A frozen session remains owned by its first attempt until cleanup has
    # atomically moved it to ``destroying``. This includes failed/cancelled
    # attempts: their terminal result is idempotently returned during the
    # narrow terminal-before-cleanup window instead of starting a second run.
    if draft.authored_session_id is not None:
        authored = (
            session.query(PlaygroundSession)
            .filter(PlaygroundSession.id == draft.authored_session_id)
            .with_for_update()
            .one_or_none()
        )
        if authored is not None and authored.status == repositories.AUTHORING_CLOSED:
            owner = session.scalar(
                select(LabVerificationAttempt).where(LabVerificationAttempt.authored_session_id == authored.id)
            )
            if owner is not None:
                return verification_handle(session, owner)
    existing = session.scalar(
        select(LabVerificationAttempt).where(
            LabVerificationAttempt.draft_id == draft.id,
            LabVerificationAttempt.authored_session_id == draft.authored_session_id,
            LabVerificationAttempt.state.in_(("reserved", "running", "verified")),
        )
    )
    if existing is not None:
        return verification_handle(session, existing)
    authored_session_id: str | None = None
    if draft.authored_session_id is not None:
        try:
            # An already frozen session is resumable.  A completed, cancelled
            # or failed one is rejected: its overlays may already be gone, so
            # there is nothing honest left to capture.
            authored_session_id = repositories.close_authored_session(session, draft.authored_session_id).id
        except repositories.AuthoredSessionUnavailable as exc:
            raise VerificationError("authored_session_unavailable", str(exc)) from exc
    attempt = LabVerificationAttempt(
        id=f"lva-{uuid4().hex[:16]}",
        draft_id=draft.id,
        authored_session_id=authored_session_id,
        definition_sha256=draft.definition_sha256,
        state="reserved",
        progress_percent=0,
        cancel_requested=False,
    )
    session.add(attempt)
    session.flush()
    session.add(
        LabOperationOutbox(
            id=f"lbo-{uuid4().hex[:16]}",
            operation_kind="lab_verify_clean_run",
            operation_id=attempt.id,
            payload_json=_json({"attempt_id": attempt.id, "draft_id": draft.id}),
            state="pending",
        )
    )
    session.flush()
    return verification_handle(session, attempt)


def request_cancel(session: Session, attempt_id: str) -> VerificationHandle:
    attempt = _attempt_by_id(session, attempt_id)
    if attempt.state in {"verified", "failed", "cancelled"}:
        return verification_handle(session, attempt)
    attempt.cancel_requested = True
    session.flush()
    return verification_handle(session, attempt)


def verification_status(session: Session, attempt_id: str) -> VerificationHandle:
    attempt = _attempt_by_id(session, attempt_id)
    return verification_handle(session, attempt)


def _write_capture(source: Path, destination: Path, *, domain_name: str) -> None:
    playground_acquisition.capture_clean_layer(
        domain_name=domain_name, source_disk_path=str(source), output_path=destination
    )


def _capture_sources(
    session: Session,
    draft: LabDraft,
    *,
    captured_layers: Mapping[str, bytes | Path] | None,
    content_store: ContentStore,
    authored_session_id: str | None = None,
    owner_id: str | None = None,
    expires_at: datetime | None = None,
    claim_heartbeat: _OutboxClaimHeartbeat | None = None,
) -> dict[str, bytes | Path | StoredAsset]:
    if captured_layers is not None:
        return dict(captured_layers)
    if fake_mode.FAKE_PLAYGROUND:
        return {vm.id: b"astinuslab fake clean boot layer\n" for vm in _definition(draft).vms}
    if authored_session_id is None:
        raise VerificationError("session_required", "clean-run verification requires an authored session")
    rows = repositories.list_session_vms(session, authored_session_id)
    by_label = {row.label: row for row in rows}
    result: dict[str, bytes | Path | StoredAsset] = {}
    with tempfile.TemporaryDirectory(prefix="astinuslab-clean-run-") as temp_root:
        # Capture outputs are immediately ingested into the content store.  A
        # temporary path never becomes package-owned state.
        for vm in _definition(draft).vms:
            row = by_label.get(vm.id)
            if row is None or not row.disk_path:
                raise VerificationError("vm_unavailable", f"VM {vm.id} is unavailable for clean capture")
            destination = Path(temp_root) / f"{vm.id}.qcow2"
            try:
                _write_capture(Path(row.disk_path), destination, domain_name=row.domain_name)
            except Exception as exc:
                raise VerificationError("capture_failed", f"could not capture VM {vm.id}") from exc
            # Keep the bytes in the store before this temporary directory exits;
            # returning the path would leave a dangling source after cleanup.
            try:
                stored, _ = materialize_boot_layer(content_store, destination)
            except PackageExportError as exc:
                raise VerificationError(exc.code, str(exc)) from exc
            if owner_id is not None and expires_at is not None:
                _record_content_asset(session, stored, owner_id=owner_id, expires_at=expires_at)
                # Capture publication and ownership are durable independently
                # of the eventual attestation transaction.  If a later VM
                # capture fails, cleanup can release this exact lease.
                if claim_heartbeat is None:
                    session.commit()
                else:
                    claim_heartbeat.commit_progress(session)
            result[vm.id] = stored
        return result


def _definition(draft: LabDraft) -> LabDefinition:
    try:
        return LabDefinition.model_validate(json.loads(draft.definition_json))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise VerificationError("invalid_draft", "saved draft definition is invalid") from exc


def _base_images(definition: LabDefinition) -> dict[str, Any]:
    """Resolve and hash every base image from the local image catalog.

    A draft's serialized ``base_image`` is an input hint only.  It may be
    stale or hand-edited, so a verification must obtain the current catalog
    entry and compare the resulting immutable facts before it can attest the
    VM.  In particular, a missing catalog entry is not replaced by draft
    metadata or synthetic evidence.
    """

    result: dict[str, Any] = {}
    for vm in definition.vms:
        image = orchestrator.lookup_playground_image(vm.image_id, role=vm.role)
        if image is None:
            raise VerificationError("base_image_unavailable", f"base image is unavailable: {vm.image_id}")
        try:
            catalog_spec = image_spec_from_catalog(vm.image_id, image)
        except PackageExportError as exc:
            raise VerificationError(exc.code, str(exc)) from exc
        if vm.base_image is not None and vm.base_image != catalog_spec:
            raise VerificationError(
                "base_image_mismatch",
                f"draft base-image provenance does not match catalog image: {vm.image_id}",
            )
        result[vm.id] = catalog_spec
    return result


def _record_content_asset(
    session: Session,
    asset: StoredAsset,
    *,
    owner_id: str,
    expires_at: datetime,
) -> None:
    """Record content-store ownership before a verification can publish it."""

    now = _now()
    content = session.get(LabContentObject, {"sha256": asset.sha256, "size_bytes": asset.size_bytes})
    if content is None:
        session.add(
            LabContentObject(
                sha256=asset.sha256,
                size_bytes=asset.size_bytes,
                path=str(asset.path),
                last_verified_at=now,
                last_used_at=now,
            )
        )
    else:
        if Path(content.path).resolve(strict=False) != asset.path.resolve(strict=False):
            raise VerificationError("content_identity_mismatch", "content digest is bound to a different path")
        content.last_verified_at = now
        content.last_used_at = now
    if (
        session.scalar(
            select(LabStorageLease.id).where(
                LabStorageLease.resource_kind == "content",
                LabStorageLease.resource_id == asset.sha256,
                LabStorageLease.owner_id == owner_id,
            )
        )
        is None
    ):
        session.add(
            LabStorageLease(
                id=f"lpl-{uuid4().hex[:16]}",
                resource_kind="content",
                resource_id=asset.sha256,
                operation_kind="lab_verification",
                owner_id=owner_id,
                expires_at=expires_at,
            )
        )


def _release_verification_leases(session: Session, attempt_id: str) -> None:
    leases = session.scalars(
        select(LabStorageLease).where(
            LabStorageLease.resource_kind == "content",
            LabStorageLease.owner_id == attempt_id,
        )
    ).all()
    for lease in leases:
        session.delete(lease)


def _evaluate_checkpoints(
    definition: LabDefinition,
    *,
    checkpoint_evaluator: Callable[[GuestObservation | GuestAssertion], bool] | None,
    heartbeat: Callable[[], None] | None = None,
) -> list[dict[str, Any]]:
    if len(definition.experience.checkpoints) > MAX_CHECKPOINTS:
        raise VerificationError("too_many_checkpoints", "too many checkpoints")
    results: list[dict[str, Any]] = []
    for raw in definition.experience.checkpoints:
        try:
            operation = validate_checkpoint(raw)
        except ValueError as exc:
            raise VerificationError("untyped_checkpoint", str(exc)) from exc
        # A locked claim requires an actual evaluator for every typed operation;
        # silently treating an assertion as passed would make the attestation
        # meaningless.
        passed = checkpoint_evaluator(operation) if checkpoint_evaluator is not None else False
        if heartbeat is not None:
            heartbeat()
        results.append(
            {
                "operation": operation.model_dump(mode="json"),
                "passed": bool(passed),
            }
        )
        if not passed:
            raise VerificationError("checkpoint_failed", f"clean-run checkpoint failed: {operation.operation}")
    return results


def _require_frozen_authored_session(session: Session, attempt: LabVerificationAttempt, draft: LabDraft) -> None:
    authored_session_id = attempt.authored_session_id
    if authored_session_id is None:
        if draft.authored_session_id is not None:
            # Attempts created before a036 did not persist the exact session
            # they owned. Falling back to the mutable draft pointer could
            # capture a later session and attest to the wrong execution.
            raise VerificationError(
                "authored_session_unbound",
                "the verification attempt is not bound to its authored Playground session",
            )
        return
    authored = session.get(PlaygroundSession, authored_session_id)
    if authored is None or authored.status != repositories.AUTHORING_CLOSED:
        raise VerificationError(
            "authored_session_unavailable",
            "the authored Playground session is no longer frozen for verification",
        )


def _run_verification(
    session: Session,
    attempt_id: str,
    *,
    content_store: ContentStore | None = None,
    captured_layers: Mapping[str, bytes | Path] | None = None,
    readiness_checker: Callable[[str, tuple[LabReadinessProbe, ...]], bool] | None = None,
    checkpoint_evaluator: Callable[[GuestObservation | GuestAssertion], bool] | None = None,
    expiry_seconds: int = VERIFICATION_EXPIRY_SECONDS,
    claim_heartbeat: _OutboxClaimHeartbeat | None = None,
) -> CleanRunResult:
    """Execute/resume a verification attempt and persist its attestation."""

    if isinstance(expiry_seconds, bool) or not isinstance(expiry_seconds, int) or expiry_seconds < 1:
        raise VerificationError("invalid_expiry", "verification expiry must be a positive integer")
    attempt = _attempt_by_id(session, attempt_id)
    if attempt.state == "verified" and attempt.result_json:
        try:
            result = json.loads(attempt.result_json)
            definition = LabDefinition.model_validate(result["locked_definition"])
            layers = {vm_id: AssetReference.model_validate(ref) for vm_id, ref in result["boot_layers"].items()}
            return CleanRunResult(attempt.id, attempt.draft_id, definition, layers, result["attestation_id"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise VerificationError("invalid_result", "stored verification result is invalid") from exc
    if attempt.state in {"failed", "cancelled"}:
        raise VerificationError("verification_terminal", attempt.error_summary or "verification already ended")
    # This operation creates evidence that is later accepted as a locked
    # package.  Test doubles and absent production evaluators must fail before
    # any evidence is captured; synthetic bytes must never reach an
    # attestation through an otherwise successful code path.
    if fake_mode.fake_mode_active():
        raise VerificationError("fake_mode_unsupported", "clean-run attestation is unavailable in fake mode")
    if readiness_checker is None:
        raise VerificationError(
            "verification_evaluator_unavailable",
            "a production readiness evaluator is required before clean-run attestation",
        )
    draft = session.get(LabDraft, attempt.draft_id)
    if draft is None:
        raise VerificationError("draft_not_found", "lab draft was not found")
    # Readiness, checkpoints and capture all read the authored guests, so the
    # session has to still be frozen.  A destroyed or re-opened session means
    # the evidence this attempt would attest to no longer exists.
    _require_frozen_authored_session(session, attempt, draft)
    store = content_store or ContentStore(
        settings.storage_dir / "lab-content", max_bytes=settings.packages_max_asset_bytes
    )
    attempt.state = "running"
    attempt.progress_percent = max(attempt.progress_percent, 5)
    if claim_heartbeat is None:
        session.flush()
    else:
        claim_heartbeat.commit_progress(session)
    if attempt.cancel_requested:
        attempt.state = "cancelled"
        attempt.progress_percent = 100
        session.flush()
        raise VerificationError("verification_cancelled", "clean-run verification was cancelled")
    definition = _definition(draft)
    if definition.experience.checkpoints and checkpoint_evaluator is None:
        raise VerificationError(
            "verification_evaluator_unavailable",
            "a production checkpoint evaluator is required for declared checkpoints",
        )
    # Readiness and guest assertions must observe the running workload.  Only
    # after these checks pass do we pause/flush the VMs for immutable capture.
    for vm in definition.vms:
        workload = vm.workload or LabWorkloadSpec(
            workload_baked=True,
            immutable_inputs={"workload_id": vm.workload_id} if vm.workload_id else {},
        )
        probes = tuple(workload.readiness_probes)
        if probes and not readiness_checker(vm.id, probes):
            raise VerificationError("readiness_failed", f"workload readiness failed for VM {vm.id}")
        if claim_heartbeat is not None:
            claim_heartbeat.commit_progress(session)
    checkpoint_results = _evaluate_checkpoints(
        definition,
        checkpoint_evaluator=checkpoint_evaluator,
        heartbeat=(lambda: claim_heartbeat.commit_progress(session)) if claim_heartbeat is not None else None,
    )
    base_images = _base_images(definition)
    attempt.progress_percent = 30
    if claim_heartbeat is None:
        session.flush()
    else:
        claim_heartbeat.commit_progress(session)

    # The capture implementation pauses/flushes each guest.  It is deliberately
    # invoked only after readiness/checkpoint success (see above).
    lease_expiry = _now() + timedelta(seconds=expiry_seconds)
    source_map = _capture_sources(
        session,
        draft,
        captured_layers=captured_layers,
        content_store=store,
        authored_session_id=attempt.authored_session_id,
        owner_id=attempt.id,
        expires_at=lease_expiry,
        claim_heartbeat=claim_heartbeat,
    )
    refs: dict[str, AssetReference] = {}
    try:
        for index, vm in enumerate(definition.vms, start=1):
            source = source_map.get(vm.id)
            if source is None:
                raise VerificationError("boot_layer_missing", f"clean capture is missing VM {vm.id}")
            if isinstance(source, StoredAsset):
                stored, media_type = source, "application/x-qcow2"
            else:
                try:
                    stored, media_type = materialize_boot_layer(store, source)
                except PackageExportError as exc:
                    raise VerificationError(exc.code, str(exc)) from exc
            refs[vm.id] = AssetReference(
                sha256=stored.sha256,
                size_bytes=stored.size_bytes,
                media_type=media_type,
                # The clean layer itself is generated locally.  Export still
                # requires an explicit redistribution acknowledgement before
                # these bytes can leave the creator host.
                embedded=True,
                license=definition.license,
                redistribution_confirmed=True,
            )
            _record_content_asset(session, stored, owner_id=attempt.id, expires_at=lease_expiry)
            attempt.progress_percent = 10 + int(45 * index / max(1, len(definition.vms)))
            # Capture publication and its lease must survive a worker crash
            # before the final attestation transaction.  The attempt remains
            # resumable and a retry reuses these immutable cache bytes.
            if claim_heartbeat is None:
                session.commit()
            else:
                claim_heartbeat.commit_progress(session)
            if attempt.cancel_requested:
                attempt.state = "cancelled"
                attempt.progress_percent = 100
                session.flush()
                raise VerificationError("verification_cancelled", "clean-run verification was cancelled")
    finally:
        # A caller-provided path is caller-owned; this service never deletes it.
        pass
    attempt.progress_percent = 75
    if claim_heartbeat is None:
        session.flush()
    else:
        claim_heartbeat.commit_progress(session)
    locked = build_locked_definition(
        definition,
        boot_layers=refs,
        base_images=base_images,
        embed_layers=True,
        redistribution_confirmed=True,
    )
    locked_digest = canonical_sha256(locked)
    input_digests = {
        "draft": draft.definition_sha256,
        "base_images": {vm.id: vm.base_image.sha256 for vm in locked.vms if vm.base_image is not None},
        "boot_layers": {vm_id: ref.sha256 for vm_id, ref in refs.items()},
        "workloads": {vm.id: canonical_sha256(vm.workload) for vm in locked.vms if vm.workload is not None},
    }
    runtime = {
        "astinuslab_version": "1.0.0",
        "libvirt": "unknown",
        "qemu": "unknown",
        "fake_mode": fake_mode.fake_mode_flags(),
        "capabilities": ["x86_64", "qcow2"],
    }
    attestation = LabPackageAttestation(
        id=f"lpa-{uuid4().hex[:16]}",
        input_draft_sha256=draft.definition_sha256,
        locked_definition_sha256=locked_digest,
        input_digests_json=_json(input_digests),
        runtime_json=_json(runtime),
        result_json=_json({"checkpoints": checkpoint_results, "state": "verified"}),
        expires_at=_now() + timedelta(seconds=expiry_seconds),
    )
    session.add(attestation)
    attempt.result_json = _json(
        {
            "locked_definition": json.loads(canonical_bytes(locked).decode("utf-8")),
            "boot_layers": {vm_id: ref.model_dump(mode="json") for vm_id, ref in refs.items()},
            "attestation_id": attestation.id,
            "locked_definition_sha256": locked_digest,
        }
    )
    attempt.state = "verified"
    attempt.progress_percent = 100
    attempt.error_summary = None
    event = session.scalar(
        select(LabOperationOutbox).where(
            LabOperationOutbox.operation_kind == "lab_verify_clean_run",
            LabOperationOutbox.operation_id == attempt.id,
        )
    )
    if claim_heartbeat is not None and event is not None:
        # The terminal outbox transition is itself fenced.  It also commits
        # the attestation and attempt in the same transaction as the CAS.
        claim_heartbeat.complete(session, terminal_state="completed")
    else:
        if event is not None:
            event.state = "completed"
            event.completed_at = _now()
        session.flush()
    return CleanRunResult(attempt.id, draft.id, locked, refs, attestation.id)


def run_verification(
    session: Session,
    attempt_id: str,
    *,
    claim_heartbeat: _OutboxClaimHeartbeat | None = None,
    **kwargs: Any,
) -> CleanRunResult:
    """Run a verification and durably record ordinary worker failures."""

    try:
        return _run_verification(session, attempt_id, claim_heartbeat=claim_heartbeat, **kwargs)
    except Exception as exc:
        # A failed or cancelled attempt no longer owns captured bytes.  Roll
        # back first in case capture/catalog validation failed during flush,
        # then release only this attempt's leases and retain the content row
        # for normal grace-period GC.
        session.rollback()
        if isinstance(exc, _LostOutboxClaim):
            raise
        attempt = session.get(LabVerificationAttempt, attempt_id)
        if attempt is not None and attempt.state != "verified":
            attempt.state = (
                "cancelled" if isinstance(exc, VerificationError) and exc.code == "verification_cancelled" else "failed"
            )
            attempt.progress_percent = 100
            code = exc.code if isinstance(exc, VerificationError) else "verification_failed"
            attempt.error_summary = f"{code}: {exc}"[:1000]
            _release_verification_leases(session, attempt_id)
            event = session.scalar(
                select(LabOperationOutbox).where(
                    LabOperationOutbox.operation_kind == "lab_verify_clean_run",
                    LabOperationOutbox.operation_id == attempt_id,
                )
            )
            if claim_heartbeat is not None and event is not None:
                # Keep the failure transition fenced just like success.
                claim_heartbeat.complete(session, terminal_state="failed")
            elif event is not None:
                event.state = "failed"
                event.completed_at = _now()
                session.commit()
            else:
                session.flush()
        raise


def verify_attestation(
    session: Session,
    *,
    attestation_id: str,
    draft_digest: str,
    definition: LabDefinition,
) -> LabPackageAttestation:
    """Require an unexpired attestation over the exact definition bytes."""

    attestation = session.get(LabPackageAttestation, attestation_id)
    if attestation is None:
        raise VerificationError("attestation_missing", "clean-run attestation is required")
    if attestation.expires_at is None or attestation.expires_at <= _now():
        raise VerificationError("attestation_expired", "clean-run attestation has expired")
    try:
        runtime = json.loads(attestation.runtime_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise VerificationError("invalid_attestation", "attestation runtime facts are invalid") from exc
    if not isinstance(runtime, dict):
        raise VerificationError("invalid_attestation", "attestation runtime facts are invalid")
    fake_flags = runtime.get("fake_mode")
    if isinstance(fake_flags, dict) and any(bool(value) for value in fake_flags.values()):
        raise VerificationError("fake_mode_unsupported", "synthetic clean-run evidence cannot be exported")
    if attestation.input_draft_sha256 != draft_digest:
        raise VerificationError("attestation_mismatch", "attestation does not match the saved draft")
    if attestation.locked_definition_sha256 != canonical_sha256(definition):
        raise VerificationError("attestation_mismatch", "attestation does not match the locked definition")
    return attestation


TERMINAL_ATTEMPT_STATES = ("verified", "failed", "cancelled")

_CLEANUP_STATUS_BY_ATTEMPT_STATE = {
    "verified": "completed",
    "cancelled": "cancelled",
    "failed": "failed",
}


def release_authored_session(attempt_id: str) -> bool:
    """Hand the frozen authoring session to the existing cleanup coordinator.

    Called once the attempt is terminal, so capture has already happened (or
    can never happen). The atomic ``authoring_closed`` to ``destroying`` claim
    makes this exactly-once: a second call from a retry, crash recovery, or
    startup reconciliation can no longer claim the session.
    """

    from ..playground import cleanup as playground_cleanup

    cleanup_token: datetime | None = None
    with session_scope() as session:
        attempt = session.get(LabVerificationAttempt, attempt_id)
        if attempt is None or attempt.authored_session_id is None:
            return False
        if attempt.state not in TERMINAL_ATTEMPT_STATES:
            return False
        authored = session.get(PlaygroundSession, attempt.authored_session_id)
        if authored is None or authored.status != repositories.AUTHORING_CLOSED:
            return False
        session_id = authored.id
        final_status = _CLEANUP_STATUS_BY_ATTEMPT_STATE.get(attempt.state, "failed")
        error_summary = None if attempt.state == "verified" else (attempt.error_summary or attempt.state)
        if not repositories.claim_authored_session_cleanup(session, session_id):
            return False
        event = session.scalar(
            select(LabOperationOutbox).where(
                LabOperationOutbox.operation_kind == "lab_verify_clean_run",
                LabOperationOutbox.operation_id == attempt.id,
            )
        )
        if event is not None:
            # Once terminal, claimed_at becomes the cleanup lease timestamp.
            # Verification claiming never consults terminal events, so this is
            # safe and gives startup recovery a durable freshness signal.
            if event.state not in {"completed", "failed"}:
                event.state = "completed" if attempt.state == "verified" else "failed"
                event.completed_at = _now()
            event.claimed_at = _now()
            cleanup_token = event.claimed_at
    cleanup_heartbeat = None
    if cleanup_token is not None:
        cleanup_heartbeat = _OutboxClaimHeartbeat(
            attempt_id=attempt_id,
            token=cleanup_token,
            interval_seconds=max(1.0, min(60.0, CLEANUP_CLAIM_SECONDS / 3)),
            claim_states=("completed", "failed"),
        )
        cleanup_heartbeat.start()
    try:
        playground_cleanup.purge_session_resources(
            job_id=session_id,
            final_status=final_status,
            error_summary=error_summary,
        )
    finally:
        if cleanup_heartbeat is not None:
            cleanup_heartbeat.stop()
    return True


@celery_app.task(name="lab_packages.verify_clean_run")
def verify_clean_run_task(attempt_id: str) -> str:
    """Celery entry point; all state is recovered from the durable attempt."""

    claim_token = _claim_verification_outbox(attempt_id)
    if claim_token is None:
        # A worker can commit the terminal attempt and crash before entering
        # its finally block. A redelivery cannot claim the completed outbox,
        # but it still owns the idempotent cleanup handoff.
        release_authored_session(attempt_id)
        return attempt_id
    heartbeat = _OutboxClaimHeartbeat(
        attempt_id=attempt_id,
        token=claim_token,
    )
    heartbeat.start()
    release_cleanup = True
    try:
        with session_scope() as session:
            attempt = session.get(LabVerificationAttempt, attempt_id)
            authored_session_id = attempt.authored_session_id if attempt is not None else None
            readiness_checker = build_readiness_checker(session, authored_session_id)
            checkpoint_evaluator = build_checkpoint_evaluator(session, authored_session_id)
            result = run_verification(
                session,
                attempt_id,
                readiness_checker=readiness_checker,
                checkpoint_evaluator=checkpoint_evaluator,
                claim_heartbeat=heartbeat,
            )
        return result.attempt_id
    except _LostOutboxClaim:
        release_cleanup = False
        raise
    except Exception as exc:
        with session_scope() as session:
            attempt = session.get(LabVerificationAttempt, attempt_id)
            if attempt is None:
                release_cleanup = False
            elif attempt.state in {"verified", "failed", "cancelled"}:
                # run_verification normally records its own terminal failure;
                # a wrapper exception must not resurrect or overwrite it.
                release_cleanup = True
            elif not heartbeat.owns_claim(session):
                # A reclaimed worker has no authority to mutate state or
                # release the authored session owned by its successor.
                release_cleanup = False
            else:
                attempt.state = "failed"
                attempt.progress_percent = 100
                code = exc.code if isinstance(exc, VerificationError) else "verification_failed"
                attempt.error_summary = f"{code}: {exc}"[:1000]
                event = session.scalar(
                    select(LabOperationOutbox).where(
                        LabOperationOutbox.operation_kind == "lab_verify_clean_run",
                        LabOperationOutbox.operation_id == attempt_id,
                    )
                )
                if event is not None:
                    heartbeat.complete(session, terminal_state="failed")
                else:
                    session.commit()
                release_cleanup = True
        raise
    finally:
        heartbeat.stop()
        if release_cleanup:
            # Cleanup runs only after the attempt has recorded a terminal state, so
            # readiness, checkpoints and capture have all observed live guests.
            release_authored_session(attempt_id)


def dispatch_verification_after_commit(attempt_id: str) -> None:
    verify_clean_run_task.delay(attempt_id)


def _claim_verification_outbox(attempt_id: str) -> datetime | None:
    """Atomically claim one verification event for a worker lease.

    Celery delivery is at-least-once.  The compare-and-set update prevents two
    workers from both running capture for the same attempt while still making
    abandoned claims recoverable after a bounded timeout.
    """

    with session_scope() as session:
        event = session.scalar(
            select(LabOperationOutbox).where(
                LabOperationOutbox.operation_kind == "lab_verify_clean_run",
                LabOperationOutbox.operation_id == attempt_id,
            )
        )
        attempt = session.get(LabVerificationAttempt, attempt_id)
        if event is None or attempt is None or attempt.state in {"verified", "failed", "cancelled"}:
            return None
        now = _now()
        stale_before = now - timedelta(seconds=OUTBOX_CLAIM_SECONDS)
        claimed = (
            session.query(LabOperationOutbox)
            .filter(LabOperationOutbox.id == event.id)
            .filter(
                (LabOperationOutbox.state == "pending")
                | (
                    (LabOperationOutbox.state == "claimed")
                    & (LabOperationOutbox.claimed_at.is_(None) | (LabOperationOutbox.claimed_at <= stale_before))
                )
            )
            .update({"state": "claimed", "claimed_at": now}, synchronize_session=False)
        )
        return now if claimed == 1 else None


def reconcile_authored_sessions() -> int:
    """Resolve frozen authoring sessions a crashed worker left behind.

    Runs before ``reconcile_verifications`` and the Playground recovery sweep
    at worker start. Pending work and a fresh worker claim are preserved. Once
    the durable worker lease is stale, the attempt is not resumed because doing
    so could attest to a different guest execution; it is failed closed and its
    session is handed to the ordinary cleanup coordinator. A session whose
    attempt already ended - or which has no attempt at all - is simply cleaned
    up.
    """

    from ..playground import cleanup as playground_cleanup

    released = 0
    released_session_ids: set[str] = set()
    with session_scope() as session:
        frozen_ids = list(
            session.scalars(
                select(PlaygroundSession.id).where(PlaygroundSession.status == repositories.AUTHORING_CLOSED)
            )
        )
    for session_id in frozen_ids:
        attempt_id: str | None = None
        with session_scope() as session:
            attempt = session.scalar(
                select(LabVerificationAttempt)
                .where(LabVerificationAttempt.authored_session_id == session_id)
                .order_by(LabVerificationAttempt.created_at.desc(), LabVerificationAttempt.id.desc())
            )
            if attempt is not None:
                attempt_id = attempt.id
                event = session.scalar(
                    select(LabOperationOutbox).where(
                        LabOperationOutbox.operation_kind == "lab_verify_clean_run",
                        LabOperationOutbox.operation_id == attempt.id,
                    )
                )
                stale_before = _now() - timedelta(seconds=OUTBOX_CLAIM_SECONDS)
                claim_is_stale = (
                    event is None
                    or event.state in {"failed", "completed"}
                    or (event.state == "claimed" and (event.claimed_at is None or event.claimed_at <= stale_before))
                )
                if attempt.state not in TERMINAL_ATTEMPT_STATES and claim_is_stale:
                    if not _fail_interrupted_attempt(
                        session,
                        attempt,
                        expected_event_state=None if event is None else event.state,
                        expected_claimed_at=None if event is None else event.claimed_at,
                    ):
                        # A concurrent worker/reconciler won the event CAS;
                        # leave its attempt and authored session untouched.
                        continue
                elif attempt.state not in TERMINAL_ATTEMPT_STATES:
                    # Pending work is dispatched by reconcile_verifications;
                    # a fresh claim belongs to a worker that may still be
                    # running. Neither is an orphan.
                    continue
        if attempt_id is not None and release_authored_session(attempt_id):
            released += 1
            released_session_ids.add(session_id)
            continue
        # No attempt owns this session; it can only have been orphaned between
        # the freeze and the attempt insert, so cleanup is the honest outcome.
        with session_scope() as session:
            orphan = session.get(PlaygroundSession, session_id)
            if orphan is None or orphan.status != repositories.AUTHORING_CLOSED:
                continue
            if not repositories.claim_authored_session_cleanup(session, session_id):
                continue
        playground_cleanup.purge_session_resources(
            job_id=session_id,
            final_status="failed",
            error_summary="authored_session_orphaned: no clean-run verification owns this frozen session",
        )
        released += 1
        released_session_ids.add(session_id)

    # ``release_authored_session`` commits its cleanup claim before touching
    # libvirt. If the process dies in that narrow gap, the session is already
    # ``destroying`` and must resume from its terminal attempt on next startup.
    with session_scope() as session:
        claimed_rows = list(
            session.execute(
                select(
                    LabVerificationAttempt.id,
                    LabVerificationAttempt.authored_session_id,
                    LabVerificationAttempt.state,
                    LabVerificationAttempt.error_summary,
                    LabOperationOutbox.id,
                    LabOperationOutbox.state,
                    LabOperationOutbox.claimed_at,
                )
                .join(
                    PlaygroundSession,
                    PlaygroundSession.id == LabVerificationAttempt.authored_session_id,
                )
                .join(
                    LabOperationOutbox,
                    (LabOperationOutbox.operation_kind == "lab_verify_clean_run")
                    & (LabOperationOutbox.operation_id == LabVerificationAttempt.id),
                )
                .where(PlaygroundSession.status == "destroying")
                .where(LabVerificationAttempt.state.in_(TERMINAL_ATTEMPT_STATES))
                .where(LabOperationOutbox.state.in_(("completed", "failed")))
                .where(
                    LabOperationOutbox.claimed_at.is_(None)
                    | (LabOperationOutbox.claimed_at <= _now() - timedelta(seconds=CLEANUP_CLAIM_SECONDS))
                )
                .order_by(LabVerificationAttempt.created_at.desc(), LabVerificationAttempt.id.desc())
            )
        )
    resumed_session_ids: set[str] = set()
    for (
        _attempt_id,
        session_id,
        attempt_state,
        error_summary,
        event_id,
        event_state,
        observed_claimed_at,
    ) in claimed_rows:
        if session_id is None or session_id in released_session_ids or session_id in resumed_session_ids:
            continue
        with session_scope() as session:
            claim = session.query(LabOperationOutbox).filter(LabOperationOutbox.id == event_id)
            claim = claim.filter(LabOperationOutbox.state == event_state)
            if observed_claimed_at is None:
                claim = claim.filter(LabOperationOutbox.claimed_at.is_(None))
            else:
                claim = claim.filter(LabOperationOutbox.claimed_at == observed_claimed_at)
            cleanup_token = _now()
            claimed = claim.update({LabOperationOutbox.claimed_at: cleanup_token}, synchronize_session=False)
            if claimed != 1:
                continue
        resumed_session_ids.add(session_id)
        cleanup_heartbeat = _OutboxClaimHeartbeat(
            attempt_id=_attempt_id,
            token=cleanup_token,
            interval_seconds=max(1.0, min(60.0, CLEANUP_CLAIM_SECONDS / 3)),
            claim_states=(event_state,),
        )
        cleanup_heartbeat.start()
        try:
            playground_cleanup.purge_session_resources(
                job_id=session_id,
                final_status=_CLEANUP_STATUS_BY_ATTEMPT_STATE.get(attempt_state, "failed"),
                error_summary=None if attempt_state == "verified" else (error_summary or attempt_state),
            )
        finally:
            cleanup_heartbeat.stop()
        released += 1
    return released


def _fail_interrupted_attempt(
    session: Session,
    attempt: LabVerificationAttempt,
    *,
    expected_event_state: str | None,
    expected_claimed_at: datetime | None,
) -> bool:
    """Fail an interrupted attempt only while retaining its observed claim."""

    event = session.scalar(
        select(LabOperationOutbox).where(
            LabOperationOutbox.operation_kind == "lab_verify_clean_run",
            LabOperationOutbox.operation_id == attempt.id,
        )
    )
    if event is not None:
        if expected_event_state is None or event.state != expected_event_state:
            return False
        claim = session.query(LabOperationOutbox).filter(LabOperationOutbox.id == event.id)
        if expected_claimed_at is None:
            claim = claim.filter(LabOperationOutbox.claimed_at.is_(None))
        else:
            claim = claim.filter(LabOperationOutbox.claimed_at == expected_claimed_at)
        failed_at = _now()
        updated = claim.update(
            {
                LabOperationOutbox.state: "failed",
                # Invalidate any abandoned worker token.  The timestamp is
                # also the cleanup lease marker used by the next recovery pass.
                LabOperationOutbox.claimed_at: failed_at,
                LabOperationOutbox.completed_at: failed_at,
            },
            synchronize_session=False,
        )
        if updated != 1:
            return False
    attempt.state = "failed"
    attempt.progress_percent = 100
    attempt.error_summary = "authored_session_lost: the worker was interrupted before clean-run verification completed"
    _release_verification_leases(session, attempt.id)
    session.flush()
    return True


def reconcile_verifications() -> int:
    """Requeue durable verification outbox work after a worker restart."""

    with session_scope() as session:
        pending = list(
            session.scalars(
                select(LabVerificationAttempt.id)
                .join(
                    LabOperationOutbox,
                    (LabOperationOutbox.operation_kind == "lab_verify_clean_run")
                    & (LabOperationOutbox.operation_id == LabVerificationAttempt.id),
                )
                .where(LabVerificationAttempt.state.in_(("reserved", "running")))
                .where(LabOperationOutbox.state.in_(("pending", "claimed")))
            )
        )
    for attempt_id in pending:
        verify_clean_run_task.delay(attempt_id)
    return len(pending)
