"""Retention policy: evidence-aware purge of capture artifacts plus log cleanup.

Evidence (memory/disk capture artifacts) is purged from the Artifact table,
never by bare file age: artifacts protected by the custody model (non-terminal
job, linked Acquisition, non-closed Case) are skipped, and eligible deletions
remove the file, its manifest and sha256 sidecars, and the DB row together,
leaving a ``retention_deleted`` JobEvent as the audit record. Logs are not
evidence and keep the simple mtime sweep — in two passes, because the per-job
serial console logs sit in a subdirectory the top-level sweep does not enter.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..core.config import settings
from ..core.log_layout import MARKER as LOG_LAYOUT_MARKER
from ..db import session_scope
from ..db.models import Artifact
from . import deletion

logger = logging.getLogger(__name__)

# Default retention windows (days). Override via the retention app settings.
DUMPS_RETENTION_DAYS = 90
LOGS_RETENTION_DAYS = 180

_EVIDENCE_KINDS = ("memory", "disk")


def _purge_old_files(directory: Path, older_than_days: int, suffix: str = "", *, dry_run: bool = False) -> int:
    """Delete files in *directory* whose mtime is older than *older_than_days*.

    With ``dry_run`` the eligible files are counted but left in place.
    Returns the number of files deleted (or that would be deleted).
    """
    if not directory.exists():
        return 0
    cutoff = time.time() - older_than_days * 86400
    count = 0
    for path in directory.iterdir():
        if path.name == LOG_LAYOUT_MARKER:
            continue
        if not path.is_file():
            continue
        if suffix and not path.name.endswith(suffix):
            continue
        if path.stat().st_mtime < cutoff:
            if dry_run:
                count += 1
                continue
            try:
                path.unlink()
                logger.info("retention: deleted %s", path)
                count += 1
            except OSError as exc:
                logger.warning("retention: could not delete %s: %s", path, exc)
    return count


def _evidence_roots() -> tuple[Path, ...]:
    """Dump roots retention may delete under (both sides of the storage-root split)."""
    return deletion.evidence_roots(settings)


def _protection_reason(session, artifact: Artifact) -> str | None:
    """Why *artifact* must not be deleted, or None when it is eligible.

    Delegates to the deletion service: retention and the Dump Vault used to carry
    separate copies of this policy, and the UI's copy was empty.
    """
    return deletion.protection_reason(session, artifact)


def _purge_old_evidence_artifacts(older_than_days: int, *, dry_run: bool = False) -> tuple[int, int]:
    """Delete eligible capture artifacts older than the window.

    Returns (deleted_count, protected_skipped_count). Deletion removes the
    artifact file, manifest, sha256 sidecar, and the Artifact row, and writes
    a ``retention_deleted`` JobEvent on the owning job as the audit record.
    With ``dry_run`` eligible artifacts are counted but nothing is deleted.
    """
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    roots = _evidence_roots()
    deleted = 0
    protected = 0
    with session_scope() as session:
        if not dry_run:
            deletion.cleanup_deletions(session, roots=roots)
        ids = []
        rows = (
            session.query(Artifact)
            .filter(
                Artifact.producer == "job_capture",
                Artifact.kind.in_(_EVIDENCE_KINDS),
                Artifact.created_at < cutoff.replace(tzinfo=None),
            )
            .all()
        )
        for artifact in rows:
            reason = _protection_reason(session, artifact)
            if reason is not None:
                protected += 1
                logger.info("retention: skipping %s (%s): %s", artifact.id, artifact.path, reason)
                continue
            if dry_run:
                deleted += 1
                continue
            outcome = deletion.delete_artifact(
                session,
                artifact,
                actor="retention",
                reason=f"older than {older_than_days}d",
                roots=roots,
                event_state="retention_deleted",
            )
            if not outcome.deleted:
                protected += 1
                logger.warning("retention: %s", outcome.protection_reason)
                continue
            logger.info("retention: deleted artifact %s (%s)", artifact.id, artifact.path)
            deleted += 1
            if outcome.tombstone_id is None:
                raise RuntimeError("deleted artifact is missing its custody record")
            ids.append(outcome.tombstone_id)
        if not dry_run:
            session.commit()
            deletion.cleanup_deletions(session, ids, roots=roots)
    return deleted, protected


def _get_retention_days() -> tuple[int, int]:
    from ..app_settings import load_app_settings

    data = load_app_settings().get("retention", {})
    dumps = int(data.get("dumps_days", DUMPS_RETENTION_DAYS))
    logs = int(data.get("logs_days", LOGS_RETENTION_DAYS))
    return dumps, logs


def _purge_serial_logs(older_than_days: int, *, dry_run: bool = False) -> int:
    """Sweep the per-job serial console logs, which live one level down.

    ``_purge_old_files`` is deliberately non-recursive — it deletes files, and
    widening it to walk a tree would widen the blast radius of every other
    caller — so the serial directory needs its own pass. Serial output is guest
    console output rather than a capture artifact, so it has no Artifact row and
    no custody protection to consult; it follows the log window. An operator who
    wants guest console data gone sooner lowers ``logs_days``.
    """
    return _purge_old_files(settings.serial_log_dir, older_than_days, dry_run=dry_run)


def _record_last_run(dumps_deleted: int, logs_deleted: int) -> None:
    """Persist the last actual-run timestamp + counts for the settings UI."""
    from ..app_settings import save_app_settings

    try:
        save_app_settings(
            {
                "retention": {
                    "last_run_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
                    "last_dumps_deleted": dumps_deleted,
                    "last_logs_deleted": logs_deleted,
                }
            }
        )
    except Exception as exc:  # never let bookkeeping break a cleanup run
        logger.warning("retention: could not record last-run metadata: %s", exc)


def run_retention(*, dry_run: bool = False, retention_days: tuple[int, int] | None = None) -> dict[str, int]:
    """Run all retention passes. Returns counts of deleted items per category.

    With ``dry_run`` nothing is deleted and no last-run metadata is recorded;
    the returned counts are what an actual run would delete right now.
    """
    logs_dir = settings.storage_dir / "logs"

    dumps_days, logs_days = retention_days if retention_days is not None else _get_retention_days()
    dumps_deleted, protected_skipped = _purge_old_evidence_artifacts(dumps_days, dry_run=dry_run)
    logs_deleted = _purge_old_files(logs_dir, logs_days, dry_run=dry_run)
    serial_logs_deleted = _purge_serial_logs(logs_days, dry_run=dry_run)

    logger.info(
        "retention %s: dumps=%d protected=%d logs=%d serial_logs=%d",
        "dry-run" if dry_run else "complete",
        dumps_deleted,
        protected_skipped,
        logs_deleted,
        serial_logs_deleted,
    )
    if not dry_run:
        # The Settings page shows a single "logs" figure; both sweeps feed it.
        _record_last_run(dumps_deleted, logs_deleted + serial_logs_deleted)
    return {
        "dumps_deleted": dumps_deleted,
        "logs_deleted": logs_deleted,
        "serial_logs_deleted": serial_logs_deleted,
        "protected_skipped": protected_skipped,
    }
