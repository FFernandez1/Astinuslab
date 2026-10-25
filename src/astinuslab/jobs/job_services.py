"""Service-layer operations for job lifecycle, runtime control, and retention.

Validates request-level domain rules, coordinates JobRepository with Celery task
dispatch, and translates domain failures into HTTPException responses. See
``forensic_services`` for capture/Volatility operations and ``case_services`` for
case/evidence management. Keep libvirt and guest-execution details in orchestration
modules.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal, cast

from fastapi import HTTPException
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from ..api.errors import ApiError
from ..api.schemas import JobCreate, LabDefinition
from ..catalog import (
    annotate_workloads_for_target,
    is_windows_family,
    load_scenarios,
    load_targets,
    load_workloads,
    workload_by_id,
    workload_compatibility_for_target,
)
from ..core.config import settings
from ..db.repositories import JobRepository, has_active_playground_session
from ..db.transactions import reserve_sqlite_write
from ..storage.guardrails import enforce_disk_guardrail
from ..storage.keystore import get_active_key_paths
from .celery_app import TASK_MAP, revert_lab_snapshot_task, teardown_job_task
from .job_control import kill_job_subprocesses, request_job_cancel
from .job_status import TERMINAL_STATUSES, JobStatus

logger = logging.getLogger(__name__)

_VALID_EXECUTION_MODES = {"immediate", "post_script", "manual_trigger", "playground"}
_VALID_CAPTURE_TYPES = {"memory", "disk", "both", "none"}
_DISK_MODES = {"post_script", "manual_trigger"}

_MODE_TO_ACTION = {
    "immediate": "run_immediate_job",
    "post_script": "run_post_script_job",
    "manual_trigger": "run_manual_trigger_job",
}


def safe_artifact_path(path_str: str) -> Path:
    """Resolve an artifact path and enforce the local file policy.

    Only files inside a ``dumps`` directory under one of
    ``settings.acl_allowed_storage_roots`` are servable; anything else —
    including ``/tmp`` — is rejected with 403. The path must exist and be a
    regular file.
    """
    resolved = Path(path_str).resolve()
    allowed_roots = tuple((Path(root) / "dumps").resolve() for root in settings.acl_allowed_storage_roots)
    if not any(resolved == root or resolved.is_relative_to(root) for root in allowed_roots):
        raise HTTPException(status_code=403, detail="artifact path is outside the allowed directory")
    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(status_code=404, detail="artifact file not found on host filesystem")
    return resolved


def safe_delete_artifact_path(path_str: str) -> Path:
    """Resolve an artifact path for deletion, allowing missing files."""
    resolved = Path(path_str).resolve()
    allowed_roots = tuple((Path(root) / "dumps").resolve() for root in settings.acl_allowed_storage_roots)
    if not any(resolved == root or resolved.is_relative_to(root) for root in allowed_roots):
        raise HTTPException(status_code=403, detail="artifact path is outside the allowed directory")
    if resolved.exists() and not resolved.is_file():
        raise HTTPException(status_code=404, detail="artifact file not found on host filesystem")
    return resolved


def _dispatch_action_after_commit(
    session: Session,
    repo: JobRepository,
    *,
    job_id: str,
    action_id: int,
    action_type: str,
    task_fn=None,
) -> None:
    """Commit job/action rows before handing their IDs to a Celery worker."""
    task_fn = task_fn or TASK_MAP.get(action_type)
    if task_fn is None:
        raise HTTPException(status_code=500, detail=f"No task registered for action '{action_type}'")

    from datetime import UTC, datetime

    from sqlalchemy import update

    from ..db.models import HostAction

    # Durable send lease closes the crash gap before broker submission. Scheduler
    # retries are bounded even when persistence of the returned task id fails.
    lease = session.execute(
        update(HostAction)
        .where(HostAction.id == action_id, HostAction.status == "queued")
        .values(
            dispatched_at=datetime.now(UTC),
            dispatch_attempts=HostAction.dispatch_attempts + 1,
        )
    )
    session.commit()
    if cast(CursorResult, lease).rowcount == 0:
        # A stale scheduler intent may already have been claimed or finished.
        # Preserve its delivery identity and avoid publishing nonqueued work.
        return
    try:
        result = task_fn.delay(job_id, action_id)
    except Exception as exc:
        session.rollback()
        compensated = repo.compensate_dispatch_failure(job_id, action_id)
        repo.add_event(job_id, "task_queue_error", f"Celery dispatch failed: {exc}")
        session.commit()
        if not compensated:
            logger.warning("[%s] broker acknowledgement failed after action %s was claimed", job_id, action_id)
            return
        raise HTTPException(status_code=503, detail="Task queue unavailable") from exc

    # The task is already queued; failing to persist its id is non-fatal —
    # it only degrades cancel-by-task-id. Swallow + warn rather than raising
    # 503, which would falsely tell the caller the task wasn't dispatched.
    try:
        repo.update_job(job_id, celery_task_id=result.id)
        repo.update_action(action_id, celery_task_id=result.id)
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.warning(
            "Failed to persist celery_task_id for task=%s job=%s task_id=%s: %s",
            action_type,
            job_id,
            result.id,
            exc,
        )


def list_targets_service() -> list[dict]:
    return load_targets()


def list_scenarios_service() -> list[dict]:
    return load_scenarios()


def build_lab_definition(payload: JobCreate, scenario: dict) -> LabDefinition:
    """Translate a validated job payload and catalog scenario into the immutable lab definition used by runners."""
    scenario_id = scenario.get("id")
    scenario_mode = scenario.get("mode")
    if scenario_id != payload.scenario_id or scenario_mode != payload.execution_mode:
        raise ValueError("payload and scenario must describe the same lab definition")
    return LabDefinition(
        target_id=payload.target_id,
        scenario_id=scenario_id,
        # scenario dict is validated upstream; cast the mode to its Literal type
        mode=cast(Literal["immediate", "post_script", "manual_trigger"], scenario_mode),
        capture_type=payload.capture_type,
        post_boot_script=scenario.get("script_content") if scenario_mode == "post_script" else None,
        workload_id=payload.workload_id,
        attacker_vm_id=payload.attacker_vm_id,
        vm_vcpus=payload.vm_vcpus,
        vm_memory_mb=payload.vm_memory_mb,
        vm_disk_gb=payload.vm_disk_gb,
        attacker_vm_vcpus=payload.attacker_vm_vcpus,
        attacker_vm_memory_mb=payload.attacker_vm_memory_mb,
        attacker_vm_disk_gb=payload.attacker_vm_disk_gb,
    )


def create_job_service(session: Session, payload: JobCreate):
    """Validate job creation policy, persist job/action rows, then dispatch the corresponding worker task after commit."""
    enforce_disk_guardrail()
    # Input validation: playground sessions live in their own table; this guard
    # keeps execution_mode='playground' out of jobs permanently.
    if payload.execution_mode == "playground":
        raise HTTPException(
            status_code=400,
            detail="execution_mode 'playground' cannot be created via the job API; "
            "use the playground orchestrator entry point instead",
        )

    if payload.execution_mode not in _VALID_EXECUTION_MODES:
        raise HTTPException(status_code=400, detail="execution_mode is unsupported")

    reserve_sqlite_write(session)
    if has_active_playground_session(session):
        raise ApiError(409, "a playground session is currently active", "conflict")

    required_vms = 2 if payload.attacker_vm_id else 1
    max_concurrent_vms = getattr(settings, "max_concurrent_vms", None)
    if isinstance(max_concurrent_vms, int) and required_vms > max_concurrent_vms:
        raise HTTPException(
            status_code=400,
            detail=(
                f"job requires {required_vms} VM(s) but orchestration.max_concurrent_vms "
                f"is {max_concurrent_vms}; raise the limit or drop the attacker VM"
            ),
        )

    # An SSH key must be configured before a job can run — the runner uses it
    # to connect to the guest VM. Fail early here rather than letting the job
    # sit in queued and fail later with a cryptic error.
    try:
        get_active_key_paths()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=400,
            detail="No active SSH key is configured. Go to Security in the web UI to generate and select one before creating a job.",
        ) from exc

    targets = {item["id"]: item for item in load_targets() if item["enabled"]}
    scenarios = {item["id"]: item for item in load_scenarios() if item["enabled"]}

    if payload.target_id not in targets:
        raise HTTPException(status_code=400, detail="target_id is unsupported or disabled")
    if payload.scenario_id not in scenarios:
        raise HTTPException(status_code=400, detail="scenario_id is unsupported or disabled")
    if payload.capture_type not in _VALID_CAPTURE_TYPES:
        raise HTTPException(status_code=400, detail="capture_type must be memory, disk, both, or none")
    if payload.capture_type not in ("memory", "none") and payload.execution_mode not in _DISK_MODES:
        raise HTTPException(
            status_code=400, detail="disk capture is only available for post_script and manual_trigger modes"
        )
    if payload.workload_id and payload.execution_mode != "manual_trigger":
        raise HTTPException(status_code=400, detail="workload_id is only supported for manual_trigger scenarios")
    if payload.build_vol3_isf and payload.execution_mode != "immediate":
        raise HTTPException(
            status_code=400,
            detail="build_vol3_isf is only available for immediate mode; post_script jobs proceed to capture dispatch",
        )
    target = targets[payload.target_id]
    if payload.build_vol3_isf and is_windows_family(target.get("family")):
        raise HTTPException(
            status_code=400,
            detail="build_vol3_isf is not available for Windows targets; capture memory and run Volatility 3 analysis instead",
        )
    workload = workload_by_id(payload.workload_id) if payload.workload_id else None
    if payload.workload_id and workload is None:
        raise HTTPException(status_code=400, detail=f"workload_id '{payload.workload_id}' not found")
    if workload is not None:
        compatibility = workload_compatibility_for_target(workload, targets[payload.target_id])
        if compatibility["compatibility_status"] != "compatible":
            reason = compatibility["compatibility_reason"] or "workload is not compatible with the selected target"
            raise HTTPException(
                status_code=400,
                detail=f"Workload '{payload.workload_id}' is incompatible with target '{payload.target_id}': {reason}",
            )

    scenario = scenarios[payload.scenario_id]
    if scenario["mode"] != payload.execution_mode:
        raise HTTPException(
            status_code=400,
            detail=f"execution_mode '{payload.execution_mode}' does not match scenario mode '{scenario['mode']}'",
        )
    from .capacity import reserve_job, validate_total
    from .capacity_host import freeze_requirements, read_host_capacity
    from .capacity_runtime import adopt_legacy_reservations

    adopt_legacy_reservations(session, settings, targets=targets)
    frozen, requirements = freeze_requirements(payload, target, workload, settings)
    payload = payload.model_copy(update=frozen)
    host = read_host_capacity(settings, include_domains=False)
    try:
        validate_total(requirements, host, settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    lab_definition = build_lab_definition(payload, scenario)

    repo = JobRepository(session)
    job = repo.create_job(payload)
    logger.info("Job created: %s (target=%s, mode=%s)", job.id, payload.target_id, payload.execution_mode)

    action_type = _MODE_TO_ACTION[lab_definition.mode]
    action_payload: dict = {
        "target_id": lab_definition.target_id,
        "scenario_id": lab_definition.scenario_id,
        "image_mode": payload.image_mode,
    }
    if payload.build_vol3_isf and lab_definition.mode in ("immediate", "post_script"):
        action_payload["isf_only"] = True
    action = repo.enqueue_action(
        job.id,
        action_type,
        action_payload,
    )
    repo.add_event(job.id, "action_queued", f"Host action queued: {action_type}")

    if not reserve_job(session, job, requirements, host, settings):
        repo.add_event(job.id, "waiting_for_capacity", job.capacity_reason or "Waiting for capacity")
        session.commit()
        return job

    _dispatch_action_after_commit(
        session,
        repo,
        job_id=job.id,
        action_id=action.id,
        action_type=action_type,
    )

    return job


def delete_job_service(session: Session, job_id: str, delete_files: bool = False) -> list[int]:
    """Delete empty activity metadata after custody-aware Storage deletion."""
    reserve_sqlite_write(session)
    session.flush()
    repo = JobRepository(session)
    job = repo.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    session.refresh(job)
    if job.status not in TERMINAL_STATUSES:
        raise ApiError(409, "only terminal jobs can be deleted (completed, failed, cancelled)", "conflict")

    if repo.job_has_evidence(job_id):
        raise ApiError(
            409,
            "Retained evidence or custody history prevents Job deletion. Storage manages eligible evidence separately.",
            "evidence_retained",
        )

    if not repo.delete_job(job_id):
        raise ApiError(
            409, "Job cleanup is not confirmed; retained VM resources still consume capacity", "cleanup_pending"
        )
    return []


def retry_job_service(session: Session, job_id: str):
    """Create a fresh queued job from a failed job while preserving the original lab inputs."""
    repo = JobRepository(session)
    original = repo.get_job(job_id)
    if original is None:
        raise HTTPException(status_code=404, detail="job not found")
    if original.status != JobStatus.FAILED:
        raise ApiError(409, "only failed jobs can be retried", "conflict")
    if original.execution_mode == "artifact_analysis":
        raise HTTPException(status_code=409, detail="Artifact analysis must be rerun in the Volatility workbench")
    new_payload = JobCreate(
        target_id=original.target_id,
        scenario_id=original.scenario_id,
        execution_mode=original.execution_mode,
        capture_type=cast(Literal["memory", "disk", "both", "none"], original.capture_type or "memory"),
        image_mode=original.image_mode,
        image_path=original.image_path,
        custom_script=original.custom_script,
        workload_id=original.workload_id,
        attacker_vm_id=original.attacker_vm_id,
        vm_vcpus=original.vm_vcpus,
        vm_memory_mb=original.vm_memory_mb,
        vm_disk_gb=original.vm_disk_gb,
        attacker_vm_vcpus=original.attacker_vm_vcpus,
        attacker_vm_memory_mb=original.attacker_vm_memory_mb,
        attacker_vm_disk_gb=original.attacker_vm_disk_gb,
        build_vol3_isf=original.build_vol3_isf,
    )
    new_job = create_job_service(session, new_payload)
    logger.info("Retry job created: %s (original=%s)", new_job.id, job_id)
    return new_job


def reset_lab_service(job_id: str) -> bool:
    """Dispatch a lab-clean snapshot revert to the runner worker."""
    from ..db import session_scope

    with session_scope() as session:
        repo = JobRepository(session)
        job = repo.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        if job.status != JobStatus.WAITING_FOR_CAPTURE:
            raise ApiError(409, "lab reset is only available while waiting for capture", "conflict")
        if not job.lab_snapshot_name:
            raise ApiError(409, "no snapshot available for this job", "conflict")

    try:
        revert_lab_snapshot_task.delay(job_id)
    except Exception as exc:
        logger.warning("[%s] revert_lab_snapshot dispatch failed: %s", job_id, exc)
        raise HTTPException(status_code=503, detail="Task queue unavailable") from exc
    return True


def list_jobs_service(session: Session, *, limit: int | None = None, offset: int = 0):
    repo = JobRepository(session)
    jobs = repo.list_jobs(limit=limit, offset=offset)
    repo.prefetch_latest_artifacts(jobs)
    return jobs


def get_job_service(session: Session, job_id: str):
    job = JobRepository(session).get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


def get_job_logs_service(session: Session, job_id: str, *, limit: int | None = None, offset: int = 0):
    repo = JobRepository(session)
    if repo.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")
    return repo.list_events(job_id, limit=limit, offset=offset)


def cancel_job_service(session: Session, job_id: str):
    """Request cancellation through the shared state-transition helper so workers and UI observe one path."""
    repo = JobRepository(session)
    job = repo.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if not request_job_cancel(job_id):
        raise ApiError(409, "job cannot be cancelled in its current state", "conflict")
    kill_job_subprocesses(job_id)
    session.refresh(job)
    return job


def force_fail_job_service(session: Session, job_id: str):
    """Force a job into the ``failed`` state and destroy its libvirt VMs.

    Idempotent: if the job is already in a terminal state (``completed``,
    ``failed`` or ``cancelled``) the existing row is returned unchanged and a
    debug line is logged. On a live job we force-transition to ``failed``,
    kill any tracked subprocesses, and dispatch `teardown_job` to the runner
    worker, which destroys the target and attacker libvirt domains and
    survives API restarts.
    """
    repo = JobRepository(session)
    job = repo.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status in TERMINAL_STATUSES:
        logger.debug("force_fail_job_service: job %s already terminal (%s); no-op", job_id, job.status)
        return job
    repo.force_transition(
        job_id, JobStatus.FAILED, current_stage=JobStatus.FAILED, error_summary="Job was force-failed via API"
    )
    kill_job_subprocesses(job_id)
    repo.add_event(job_id, "force_failed", "Job was force-failed via API")
    try:
        teardown_job_task.delay(job_id, "failed")
    except Exception as exc:
        logger.warning("[%s] teardown_job dispatch failed (non-fatal): %s", job_id, exc)
    return repo.get_job(job_id)


def force_kill_job_service(session: Session, job_id: str):
    """Force-fail a job and mark it cancelled — UI variant of force-fail.

    Mirrors ``force_fail_job_service`` (force-transition + subprocess kill +
    worker-dispatched libvirt teardown) but marks the job ``cancelled`` rather than
    ``failed`` to reflect that the operator pressed the destructive UI
    button. Idempotent on terminal jobs in the same way.
    """
    from datetime import UTC, datetime

    repo = JobRepository(session)
    job = repo.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status in TERMINAL_STATUSES:
        logger.debug("force_kill_job_service: job %s already terminal (%s); no-op", job_id, job.status)
        return job
    repo.force_transition(job_id, JobStatus.CANCELLED, finished_at=datetime.now(UTC))
    kill_job_subprocesses(job_id)
    repo.add_event(job_id, "cancelled", "VM force-destroyed from the UI")
    try:
        teardown_job_task.delay(job_id, "cancelled")
    except Exception as exc:
        logger.warning("[%s] teardown_job dispatch failed (non-fatal): %s", job_id, exc)
    return repo.get_job(job_id)


def get_lab_info_service(session: Session, job_id: str):
    """Build the operator-facing lab summary from job state, runtimes, and workload catalog metadata."""
    repo = JobRepository(session)
    job = repo.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if not job.workload_id:
        raise HTTPException(status_code=404, detail="this job has no workload")
    workload = workload_by_id(job.workload_id)
    services = workload.get("services", []) if workload else []
    guest_ip = job.guest_ip or ""
    enriched = [
        {
            "name": svc["name"],
            "port": svc["port"],
            "url": f"http://{guest_ip}:{svc['port']}{svc.get('url_path', '/')}",
            "credentials": svc.get("credentials"),
        }
        for svc in services
    ]
    return {
        "workload_id": job.workload_id,
        "lab_url": job.lab_url,
        "guest_ip": guest_ip,
        "snapshot_available": bool(job.lab_snapshot_name),
        "services": enriched,
    }


def list_host_actions_service(session: Session, *, limit: int | None = None, offset: int = 0):
    return JobRepository(session).list_actions(limit=limit, offset=offset)


def list_workloads_service(target_id: str | None, cve: str | None, compatible_only: bool):
    workloads = annotate_workloads_for_target(target_id, load_workloads(), only_compatible=compatible_only)
    if cve:
        needle = cve.upper()
        workloads = [w for w in workloads if needle in [c.upper() for c in w.get("cve_ids", [])]]
    return workloads


def trigger_retention_service():
    from ..storage.retention import run_retention

    return run_retention()


def list_job_runtimes_service(session: Session, job_id: str):
    repo = JobRepository(session)
    job = repo.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return repo.list_runtimes(job_id)


def runtime_intent_lock_dir() -> Path:
    """Directory holding the per-Job intent locks shared with forensic capture.

    Capture requests and runtime-control requests take the same job-scoped lock,
    so the two can never both pass validation for one Job, and the lifecycle
    worker can hold it to fence a capture out while it drives the hypervisor.
    """
    from ..core.locks import capture_lock_dir

    return capture_lock_dir(settings, "forensic")


def _pending_control_role(action) -> str | None:
    """Role of a pending control action; None when its payload is unreadable."""
    try:
        payload = json.loads(action.payload or "{}")
    except (TypeError, ValueError):
        return None
    return payload.get("role") if isinstance(payload, dict) else None


def control_runtime_service(job_id: str, role: str, action: str) -> bool:
    """Record runtime-control intent for the lifecycle worker to execute.

    The API process holds no hypervisor privilege. It validates policy, commits
    a durable ``control_runtime`` action and hands its id to the single
    lifecycle worker, which re-checks the same policy before touching libvirt.
    """
    from ..core.locks import CaptureLockBusy, capture_lock
    from ..db import session_scope
    from .job_status import RUNTIME_CONTROLLABLE_STATUSES
    from .runtime_control import CAPTURE_ACTION_TYPES, SUPPORTED_ACTIONS, SUPPORTED_ROLES

    if role not in SUPPORTED_ROLES:
        raise HTTPException(status_code=400, detail="unsupported runtime role")
    if action not in SUPPORTED_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail="unsupported runtime action. Pause and Resume are disabled for forensic consistency.",
        )

    with session_scope() as session:
        repo = JobRepository(session)
        try:
            with capture_lock(job_id, base_dir=runtime_intent_lock_dir()):
                job = repo.get_job(job_id)
                if job is None:
                    raise HTTPException(status_code=404, detail="job not found")
                if repo.get_runtime(job_id, role) is None:
                    raise ApiError(409, f"{role} runtime is not available for this job", "conflict")
                if job.status not in RUNTIME_CONTROLLABLE_STATUSES:
                    raise ApiError(409, "runtime control is only available while the lab is active", "conflict")

                pending = [a for a in repo.list_actions(job_id) if a.status in {"queued", "running"}]
                if any(a.action_type in CAPTURE_ACTION_TYPES for a in pending):
                    raise ApiError(409, "runtime control is unavailable while a capture is pending", "conflict")
                # An unreadable payload is treated as covering this role: never
                # publish a second control the worker might execute twice.
                if any(
                    a.action_type == "control_runtime" and _pending_control_role(a) in (role, None) for a in pending
                ):
                    raise ApiError(409, f"{role} runtime control is already queued or running", "conflict")

                intent = repo.enqueue_action(job_id, "control_runtime", {"role": role, "action": action})
                repo.add_event(job_id, "runtime_control", f"Runtime action queued: {action} on {role}")
                session.commit()
                action_id = intent.id
        except CaptureLockBusy as exc:
            raise ApiError(409, "another capture or runtime-control request is in progress", "conflict") from exc

        # Dispatch outside the intent lock: the worker takes the same lock, and
        # an in-process (eager) broker would otherwise deadlock against itself.
        _dispatch_action_after_commit(session, repo, job_id=job_id, action_id=action_id, action_type="control_runtime")

    return True
