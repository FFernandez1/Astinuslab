"""Custody-aware evidence deletion — one path for every caller.

Before this module there were two rules for the same bytes. Retention refused to
touch an artifact whose job was still running, whose Acquisition still owned it,
or whose Case was open. The Dump Vault unlinked the file and dropped the rows
with no checks at all, and it was reachable from a button in the UI.

Everything now goes through :func:`delete_artifact`, which:

- decides protection with one function, shared with retention;
- pins the artifact to a trusted root and refuses a symlink standing in for it;
- refuses sidecars that have wandered out of the artifact's own directory;
- writes a tombstone recording the hash, actor, reason and exact file set, in the
  same transaction that drops the Artifact row and *before* any unlink;
- reports what it could not remove instead of swallowing it.

Deliberate ordering: the row and the tombstone commit first, then the bytes go.
A crash in between leaves files an operator can find through the tombstone, which
is recoverable. The other order leaves a row pointing at bytes that no longer
exist, which reads as intact evidence and is not.
"""

from __future__ import annotations

import json
import logging
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..db.models import Acquisition, Artifact, Case, EvidenceTombstone, HostAction, Job, JobEvent, Report

logger = logging.getLogger(__name__)

# Statuses a job must have left before its evidence can be considered inactive.
TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled"}


@dataclass
class DeletionOutcome:
    """What the deletion did, or why it refused."""

    deleted: bool
    protection_reason: str | None = None
    removed_paths: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    tombstone_id: int | None = None


def protection_reason(session: Session, artifact: Artifact, *, refresh: bool = False) -> str | None:
    """Why this artifact must not be deleted, or None when it is eligible.

    Retention and the UI both call this. Two implementations of custody policy is
    one too many: the weaker one becomes the way evidence disappears.
    """
    if artifact.ownership_reconciliation_json:
        return "artifact ownership requires reconciliation; historical custody is unresolved"

    job = session.get(Job, artifact.job_id, populate_existing=refresh) if artifact.job_id else None
    if job is not None and job.status not in TERMINAL_JOB_STATUSES:
        return f"job {job.id} is {job.status} (not terminal)"

    # A queued or running analysis owns its input until it reaches a terminal
    # status. The direct pin covers every analysis action; the Job-context join
    # additionally covers legacy artifact-analysis actions queued before pinning.
    pinned_analysis = session.scalars(
        select(HostAction.id).where(
            HostAction.source_artifact_id == artifact.id, HostAction.status.in_(("queued", "running"))
        )
    ).first()
    if pinned_analysis is not None:
        return "input of queued or running analysis"

    active_analysis = session.scalars(
        select(HostAction.id)
        .join(Job, HostAction.job_id == Job.id)
        .where(Job.source_artifact_id == artifact.id, HostAction.status.in_(("queued", "running")))
    ).first()
    if active_analysis is not None:
        return "source of queued or running artifact analysis"

    acquisition_id = artifact.acquisition_id or (job.acquisition_id if job is not None else None)
    if acquisition_id:
        acquisition = session.get(Acquisition, acquisition_id, populate_existing=refresh)
        label = acquisition.status if acquisition is not None else "unknown"
        return f"enrolled in acquisition {acquisition_id} ({label})"

    case_id = artifact.case_id or (job.acquisition.case_id if job is not None and job.acquisition else None)
    if case_id:
        case = session.get(Case, case_id, populate_existing=refresh)
        if case is not None and case.status != "closed":
            return f"case {case_id} is {case.status}"
    return None


def _path_reason(artifact_path: Path, roots: tuple[Path, ...]) -> str | None:
    """Refuse anything that is not a plain file inside a trusted root."""
    try:
        info = artifact_path.lstat()
    except FileNotFoundError:
        return None  # handled separately: a missing file still clears its row
    except OSError as exc:
        return f"artifact path could not be inspected: {exc}"

    if stat.S_ISLNK(info.st_mode):
        return f"artifact path is a symlink and will not be followed: {artifact_path}"
    if not stat.S_ISREG(info.st_mode):
        return f"artifact path is not a regular file: {artifact_path}"

    resolved = artifact_path.resolve()
    if not any(resolved.is_relative_to(root) for root in roots):
        return f"artifact path is outside every allowed evidence root: {artifact_path}"
    return None


def _sidecar_paths(artifact: Artifact, artifact_path: Path, roots: tuple[Path, ...]) -> tuple[list[Path], str | None]:
    """Sidecars must sit beside their artifact, inside a trusted root."""
    paths: list[Path] = []
    for raw in (artifact.manifest_path, artifact.sha256_sidecar_path):
        if not raw:
            continue
        sidecar = Path(raw)
        if sidecar.is_symlink():
            return [], f"sidecar is a symlink and will not be followed: {sidecar}"
        if sidecar.parent.resolve() != artifact_path.parent.resolve():
            return [], f"sidecar is outside the artifact directory: {sidecar}"
        if not any(sidecar.resolve().is_relative_to(root) for root in roots):
            return [], f"sidecar is outside every allowed evidence root: {sidecar}"
        paths.append(sidecar)
    return paths, None


def delete_artifact(
    session: Session,
    artifact: Artifact,
    *,
    actor: str,
    reason: str,
    roots: tuple[Path, ...] | list[Path],
    force: bool = False,
    event_state: str = "evidence_deleted",
    delete_files: bool = True,
) -> DeletionOutcome:
    """Stage deletion metadata without committing or unlinking any files.

    The transaction owner must commit, then call ``cleanup_deletions``.
    ``delete_files=False`` records metadata removal and permanently retains bytes.

    `force` is the audited escape hatch: it overrides custody protection, and the
    tombstone records both that it was forced and what protection it overrode.

    `event_state` names the job event this writes. Retention keeps its documented
    `retention_deleted` state - it appears in FORENSIC_INTEGRITY.md and three
    translations, and an operator's saved filter should not break because the
    implementation was unified underneath it.

    The caller owns the metadata transaction. Rollback before commit preserves
    all evidence bytes; only committed intents authorize cleanup.
    """
    from ..db.transactions import reserve_sqlite_write

    reserve_sqlite_write(session)
    session.flush()
    if session.get(Artifact, artifact.id, populate_existing=True) is None:
        return DeletionOutcome(deleted=False, protection_reason="artifact row no longer exists")

    roots = tuple(Path(root).resolve() for root in roots)
    artifact_path = Path(artifact.path)

    blocked = protection_reason(session, artifact, refresh=True)
    override = None
    if blocked is not None:
        if not force:
            logger.info("deletion refused for %s: %s", artifact.id, blocked)
            return DeletionOutcome(deleted=False, protection_reason=blocked)
        override = blocked

    # Path checks are never overridable. Force is about custody policy, not about
    # deleting a symlink's target or a file outside the evidence tree.
    path_problem = _path_reason(artifact_path, roots) if delete_files else None
    if path_problem is not None:
        logger.warning("deletion refused for %s: %s", artifact.id, path_problem)
        return DeletionOutcome(deleted=False, protection_reason=path_problem)

    sidecars, sidecar_problem = (
        _sidecar_paths(artifact, artifact_path, roots)
        if delete_files
        else ([Path(p) for p in (artifact.manifest_path, artifact.sha256_sidecar_path) if p], None)
    )
    if sidecar_problem is not None:
        logger.warning("deletion refused for %s: %s", artifact.id, sidecar_problem)
        return DeletionOutcome(deleted=False, protection_reason=sidecar_problem)

    victims = [artifact_path, *sidecars]
    notes: list[str] = []
    if not artifact_path.exists():
        notes.append(f"artifact file was already missing at {artifact_path}")

    tombstone = EvidenceTombstone(
        artifact_id=artifact.id,
        path=str(artifact_path),
        kind=artifact.kind,
        producer=artifact.producer,
        sha256=artifact.sha256,
        size_bytes=artifact.size_bytes,
        job_id=artifact.job_id,
        session_id=artifact.session_id,
        acquisition_id=artifact.acquisition_id,
        case_id=artifact.case_id,
        file_set=json.dumps([str(path) for path in victims], sort_keys=True),
        actor=actor,
        reason=reason,
        forced=bool(force and override),
        protection_override=override,
        created_at=datetime.now(UTC),
        cleanup_state="pending" if delete_files else "retained",
        cleanup_metadata=json.dumps({str(path): _file_identity(path) for path in victims}) if delete_files else None,
    )
    session.add(tombstone)
    if artifact.job_id:
        session.add(
            JobEvent(
                job_id=artifact.job_id,
                state=event_state,
                message=(
                    f"{artifact.kind or 'artifact'} {artifact.filename or artifact.path} deleted by {actor}: "
                    f"{reason} (sha256={artifact.sha256 or 'unknown'}"
                    + (", forced over: " + override if override else "")
                    + ")"
                ),
            )
        )
    # Preserve report identity and its audited disposal history. Analysis-only
    # contexts must lose their source explicitly so another dump cannot become
    # an unintended fallback after evidence removal.
    for report in session.scalars(select(Report).where(Report.output_artifact_id == artifact.id)):
        report.status = "deleted"
    for context_job in session.scalars(select(Job).where(Job.source_artifact_id == artifact.id)):
        context_job.source_artifact_id = None

    from ..db.repositories import clear_artifact_references

    clear_artifact_references(session, artifact.id)
    session.delete(artifact)
    if notes:
        tombstone.notes = " | ".join(notes)
    session.flush()
    return DeletionOutcome(deleted=True, tombstone_id=tombstone.id)


def _file_identity(path: Path) -> list[int] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def cleanup_deletions(
    owner: Session,
    tombstone_ids: list[int] | None = None,
    *,
    roots: tuple[Path, ...] | list[Path],
) -> list[DeletionOutcome]:
    """Retry committed pending/failed cleanup using an independent transaction.

    The caller must end its transaction first. This function never commits caller
    state. Legacy tombstones and metadata-only removals are never cleanup jobs.
    File identities make retry safe if an operator replaced a recorded path.
    Passing no IDs recovers all pending/failed intents (used by retention).
    """
    if owner.in_transaction():
        raise RuntimeError("commit or roll back the deletion transaction before cleanup")
    bind = owner.get_bind()
    if not isinstance(bind, Engine):
        raise RuntimeError("cleanup requires an engine-bound session, not an external transaction")
    roots = tuple(Path(root).resolve() for root in roots)
    outcomes = []
    from ..db.transactions import reserve_sqlite_write

    with Session(bind) as session, session.begin():
        reserve_sqlite_write(session)
        query = select(EvidenceTombstone).where(EvidenceTombstone.cleanup_state.in_(("pending", "failed")))
        if tombstone_ids is not None:
            query = query.where(EvidenceTombstone.id.in_(tombstone_ids))
        for tombstone in session.scalars(query):
            removed, errors = [], []
            identities = json.loads(tombstone.cleanup_metadata or "{}")
            file_set = json.loads(tombstone.file_set)
            live_reference = session.scalar(
                select(Artifact.id)
                .where(
                    or_(
                        Artifact.path.in_(file_set),
                        Artifact.manifest_path.in_(file_set),
                        Artifact.sha256_sidecar_path.in_(file_set),
                    )
                )
                .limit(1)
            )
            if live_reference is not None:
                errors.append(f"files are referenced by live artifact {live_reference}")
            for raw in () if live_reference is not None else file_set:
                path = Path(raw)
                try:
                    problem = _path_reason(path, roots)
                    current = _file_identity(path)
                    if problem:
                        errors.append(problem)
                    elif current is None:
                        continue  # already cleaned, including after an interrupted attempt
                    elif raw not in identities or current != identities[raw]:
                        errors.append(f"file changed since deletion intent: {path}")
                    else:
                        path.unlink(missing_ok=True)
                        removed.append(raw)
                except OSError as exc:
                    errors.append(f"could not delete {path}: {exc}")
            tombstone.cleanup_state = "failed" if errors else "complete"
            if errors:
                message = " | ".join(errors)
                # Keep the first failure as an audit record without unbounded retries.
                if message not in (tombstone.notes or ""):
                    tombstone.notes = " | ".join(filter(None, (tombstone.notes, message)))
                logger.warning("evidence cleanup %s: %s", tombstone.id, message)
            outcomes.append(
                DeletionOutcome(deleted=True, tombstone_id=tombstone.id, removed_paths=removed, errors=errors)
            )
    return outcomes


def evidence_roots(settings) -> tuple[Path, ...]:
    """Dump roots deletion may operate under (both sides of the storage-root split)."""
    roots = [Path(root) / "dumps" for root in getattr(settings, "acl_allowed_storage_roots", ())]
    for setting in ("base_dir", "data_dir"):
        directory = getattr(settings, setting, None)
        if directory is not None:
            roots.append(Path(directory) / "storage" / "dumps")
    storage_dir = getattr(settings, "storage_dir", None)
    if storage_dir is not None:
        roots.append(Path(storage_dir) / "dumps")
    playground_captures = getattr(settings, "playground_capture_dir", None)
    if playground_captures is not None:
        roots.append(Path(playground_captures))
    resolved = []
    for root in roots:
        try:
            resolved.append(root.resolve())
        except OSError:  # pragma: no cover - unresolvable root is simply not trusted
            continue
    return tuple(dict.fromkeys(resolved))


__all__ = [
    "DeletionOutcome",
    "delete_artifact",
    "cleanup_deletions",
    "evidence_roots",
    "protection_reason",
]
