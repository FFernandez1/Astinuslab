"""Lifecycle-worker execution of operator runtime-control intent.

The API records intent only; libvirt is reached exclusively from here, on the
single lifecycle worker that owns hypervisor access. Every policy decision the
API made is re-checked at this boundary, because a Job can move on between the
request and its execution.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from ..db import session_scope
from ..db.repositories import JobRepository
from .job_status import RUNTIME_CONTROLLABLE_STATUSES

logger = logging.getLogger(__name__)

SUPPORTED_ROLES = frozenset({"target", "attacker"})
# Pause/resume stay disabled: a suspended guest drifts in time and produces
# forensically inconsistent memory.
SUPPORTED_ACTIONS = frozenset({"shutdown", "reboot"})
# Action types whose presence means the guest's state belongs to an acquisition.
CAPTURE_ACTION_TYPES = frozenset({"acquire_memory_dump"})
_PENDING_STATUSES = frozenset({"queued", "running"})


class RuntimeControlIdentityError(RuntimeError):
    """The claimed action or VM identity does not belong to this Job."""


def _superseded_reason(repo: JobRepository, job_id: str) -> str | None:
    """Why this control must no longer run, or None if it may proceed."""
    job = repo.get_job(job_id)
    if job is None:
        return "the Job no longer exists"
    if job.status not in RUNTIME_CONTROLLABLE_STATUSES:
        return f"the Job moved to {job.status}; runtime control is only available while the lab is active"
    if any(
        pending.action_type in CAPTURE_ACTION_TYPES and pending.status in _PENDING_STATUSES
        for pending in repo.list_actions(job_id)
    ):
        return "a capture request now owns the guest state"
    return None


def _job_vm_name(repo: JobRepository, job_id: str, role: str) -> str:
    """Resolve the VM to act on from this Job's own runtime row.

    The name comes from the current record rather than being re-derived, and is
    accepted only when it still matches this Job's identity, so a substituted or
    stale row can never point a Job's control action at an unrelated domain.
    """
    from .runtime_support import runtime_vm_name

    runtime = repo.get_runtime(job_id, role)
    if runtime is None:
        raise RuntimeControlIdentityError(f"{role} runtime is not available for this job")
    recorded = (runtime.vm_name or "").strip()
    expected = runtime_vm_name(job_id, role)
    if recorded != expected:
        raise RuntimeControlIdentityError(
            f"{role} runtime record names {recorded or '<empty>'}, which is not this job's VM identity"
        )
    return recorded


def _settle(job_id: str, action_id: int, status: str, *, error_summary: str | None = None) -> None:
    with session_scope() as session:
        JobRepository(session).update_action(
            action_id,
            status=status,
            finished_at=datetime.now(UTC),
            active_pid=None,
            error_summary=error_summary,
        )


def run_control_runtime_action(job_id: str, action_id: int) -> None:
    """Execute one claimed ``control_runtime`` action against libvirt.

    Outcomes are explicit: ``completed`` when the host action ran, ``cancelled``
    when policy no longer permits it (the request is superseded, not broken),
    and ``failed`` — re-raised so the caller sees it — for malformed intent,
    unusable VM identity, or a host command that did not succeed.
    """
    from ..core.config import settings
    from ..core.locks import CaptureLockBusy, blocking_file_lock, capture_lock, capture_lock_dir, capture_lock_path

    with session_scope() as session:
        repo = JobRepository(session)
        action = repo.get_action(action_id)
        if action is None:
            raise RuntimeError(f"Action {action_id} not found")
        # Identity fence for direct calls that bypass the claim decorator: a
        # mis-routed delivery must leave the other action's row untouched.
        if action.job_id != job_id or action.action_type != "control_runtime":
            raise RuntimeControlIdentityError(f"action {action_id} is not a control_runtime action of job {job_id}")
        if action.status not in _PENDING_STATUSES:
            # Already settled: a duplicate delivery must not repeat the work.
            return
        raw_payload = action.payload
        repo.update_action(action_id, status="running", started_at=action.started_at or datetime.now(UTC))

    reason: str | None = None
    try:
        payload = json.loads(raw_payload or "{}")
        if not isinstance(payload, dict):
            raise RuntimeError("runtime control payload is not an object")
        role, runtime_action = payload.get("role"), payload.get("action")
        if role not in SUPPORTED_ROLES or runtime_action not in SUPPORTED_ACTIONS:
            raise RuntimeError(f"unsupported runtime control payload: role={role!r} action={runtime_action!r}")

        lock_dir = capture_lock_dir(settings, "forensic")
        # Wait for any in-flight API request to publish its intent, then hold
        # that same lock across the host call so a capture cannot be accepted
        # against the power state this action is about to change.
        with blocking_file_lock(capture_lock_path(job_id, base_dir=lock_dir)):
            with session_scope() as session:
                repo = JobRepository(session)
                reason = _superseded_reason(repo, job_id)
                vm_name = None if reason else _job_vm_name(repo, job_id, role)
            if vm_name is not None:
                try:
                    # Excludes an acquisition already running against this VM.
                    with capture_lock(vm_name, base_dir=lock_dir):
                        control_runtime(job_id, role, runtime_action, vm_name=vm_name)
                except CaptureLockBusy:
                    reason = "a capture is already in progress on this VM"
    except Exception as exc:
        _settle(job_id, action_id, "failed", error_summary=str(exc))
        with session_scope() as session:
            JobRepository(session).add_event(job_id, "runtime_control", f"Runtime action failed: {exc}")
        raise

    if reason is not None:
        logger.info("[%s] runtime control action %s superseded: %s", job_id, action_id, reason)
        _settle(job_id, action_id, "cancelled", error_summary=f"Runtime control not executed: {reason}")
        with session_scope() as session:
            JobRepository(session).add_event(job_id, "runtime_control", f"Runtime action not executed because {reason}")
        return

    _settle(job_id, action_id, "completed")


def refresh_runtime(job_id: str, role: str, deps=None) -> dict | None:
    from ..orchestration.deps import JobRunDeps
    from .runtime_actions import refresh_runtime as _refresh_runtime_impl

    deps = deps if deps is not None else JobRunDeps.default()
    return _refresh_runtime_impl(
        deps.session_scope,
        deps.job_repository_cls,
        deps.runtime_vm_name,
        deps.read_vm_power_state,
        deps.parse_guest_ip,
        deps.sync_runtime_record,
        job_id,
        role,
    )


def control_runtime(job_id: str, role: str, action: str, deps=None, *, vm_name: str | None = None) -> bool:
    from ..orchestration.deps import JobRunDeps
    from .runtime_actions import control_runtime as _control_runtime_impl

    deps = deps if deps is not None else JobRunDeps.default()
    return _control_runtime_impl(
        deps.session_scope,
        deps.job_repository_cls,
        deps.settings,
        deps.run_command,
        lambda jid, jrole: refresh_runtime(jid, jrole, deps=deps),
        job_id,
        role,
        action,
        vm_name=vm_name,
    )
