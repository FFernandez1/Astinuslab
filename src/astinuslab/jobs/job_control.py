from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading

from .. import db
from ..core.config import settings
from ..db.repositories import JobRepository
from ..virtualization.process_support import (
    PID_REGISTRY_SCHEMA,
    job_pid_dir,
    verify_process_identity,
)
from .celery_app import teardown_job_task as _teardown_job_task
from .job_status import CANCELLABLE_STATUSES

logger = logging.getLogger(__name__)

JOB_PROCESSES: dict[str, set[subprocess.Popen[str]]] = {}
PROCESS_LOCK = threading.Lock()
ACTION_BY_JOB: dict[str, int] = {}


def _read_pid_record(pid_path, job_id: str, pid: int) -> dict | None:
    """Load a registry record, or None when it carries no verifiable identity."""
    try:
        raw = pid_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("[%s] Failed to read subprocess registry entry %d: %s", job_id, pid, exc)
        return None
    if not raw.strip():
        return None  # legacy empty marker: no identity to verify
    try:
        record = json.loads(raw)
    except ValueError:
        logger.warning("[%s] Subprocess registry entry %d is not valid JSON", job_id, pid)
        return None
    if not isinstance(record, dict) or record.get("schema_version") != PID_REGISTRY_SCHEMA:
        return None
    return record


def kill_job_subprocesses(job_id: str) -> None:
    """Forcefully terminate all running subprocesses tracked for this job."""
    with PROCESS_LOCK:
        processes = list(JOB_PROCESSES.get(job_id, set()))
    for process in processes:
        try:
            logger.info("[%s] Killing associated subprocess PID %d", job_id, process.pid)
            os.killpg(process.pid, signal.SIGKILL)
        except Exception as exc:
            logger.warning("[%s] Failed to kill subprocess PID %d: %s", job_id, process.pid, exc)

    pid_dir = job_pid_dir(settings.storage_dir, job_id)
    try:
        pid_paths = list(pid_dir.iterdir())
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("[%s] Failed to list registered subprocess PIDs: %s", job_id, exc)
        return

    for pid_path in pid_paths:
        if pid_path.name.startswith("."):
            continue
        try:
            pid = int(pid_path.name)
        except ValueError:
            continue

        record = _read_pid_record(pid_path, job_id, pid)
        if record is None or not verify_process_identity(record):
            # Either a record from a release that stored no identity, or a PID
            # the kernel has since handed to something else. Signalling on a
            # number alone is how an unrelated process group gets killed, so the
            # record is cleared and nothing is sent.
            logger.info("[%s] Not signalling PID %d: registry record does not match a live process", job_id, pid)
        else:
            try:
                pgid = record.get("pgid") or pid
                logger.info("[%s] Killing verified subprocess group %s (pid %d)", job_id, pgid, pid)
                os.killpg(int(pgid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                logger.warning("[%s] Failed to kill registered subprocess PID %d: %s", job_id, pid, exc)
            except OSError as exc:
                logger.warning("[%s] Failed to kill registered subprocess PID %d: %s", job_id, pid, exc)
        # The record is cleared either way: a verified kill is done, and an
        # unverifiable one must not be retried on the next cancellation.
        try:
            pid_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("[%s] Failed to remove registered subprocess PID %d: %s", job_id, pid, exc)

    try:
        pid_dir.rmdir()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def is_cancel_requested(job_id: str) -> bool:
    with db.session_scope() as session:
        repo = JobRepository(session)
        job = repo.get_job(job_id)
        return bool(job and job.status == "cancel_requested")


def request_job_cancel(job_id: str) -> bool:
    with db.session_scope() as session:
        from ..db.transactions import reserve_sqlite_write
        from .capacity import confirm_cleanup

        reserve_sqlite_write(session)
        repo = JobRepository(session)
        job = repo.get_job(job_id)
        if job is None:
            return False
        prior_status = job.status
        if job.status == "cancel_requested":
            return True
        if job.status not in CANCELLABLE_STATUSES:
            # Terminal states and capture/publish phases: refuse cleanly instead
            # of raising InvalidJobTransitionError out of transition_job_status.
            return False
        if prior_status in {"queued", "waiting_for_capacity"} and confirm_cleanup(session, job_id, never_started=True):
            from datetime import UTC, datetime

            repo.transition_job_status(job_id, "cancelled", finished_at=datetime.now(UTC), capacity_reason=None)
            for action in repo.list_actions(job_id):
                if action.status in {"queued", "waiting_for_capacity"}:
                    repo.update_action(action.id, status="cancelled", finished_at=datetime.now(UTC))
            repo.add_event(job_id, "cancelled", "Cancelled before VM admission")
            return True
        repo.transition_job_status(job_id, "cancel_requested")
        repo.add_event(job_id, "cancel_requested", "Cancellation request received")
        actions = repo.list_actions(job_id)
        for action in actions:
            if action.status in {"queued", "running", "waiting_for_capacity"}:
                repo.update_action(action.id, status="cancel_requested")

    if prior_status == "waiting_for_capture":
        try:
            _teardown_job_task.delay(job_id, "cancelled")
        except Exception as exc:
            logger.warning("[%s] teardown_job dispatch failed (non-fatal): %s", job_id, exc)

    return True
