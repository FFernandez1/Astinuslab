from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from celery import Celery
from celery.signals import before_task_publish
from sqlalchemy import select

from ..core.config import settings
from ..core.request_context import REQUEST_ID_HEADER, current_request_id
from .action_lifecycle import owned_action
from .capacity_runtime import capacity_launch

logger = logging.getLogger(__name__)

celery_app = Celery(
    "astinuslab",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

# Beat tasks share the default-queue concurrency=1 worker; long captures
# delay reconciliation/expiry by design. Interactive workbench plugin runs
# are routed to the separate "analysis" queue/worker so they are not
# serialized behind captures. See docs/architecture/components.md#scaling.
_beat_schedule: dict = {
    "run-retention": {"task": "run_retention", "schedule": 3600},
    "admit-waiting-jobs": {"task": "admit_waiting_jobs", "schedule": 15},
}
if settings.celery_reconciliation_enabled:
    _beat_schedule["reconcile-libvirt-resources"] = {
        "task": "reconcile_libvirt_resources",
        "schedule": settings.celery_reconciliation_interval_seconds,
    }
    _beat_schedule["expire-waiting-captures"] = {
        "task": "expire_waiting_captures",
        "schedule": settings.celery_reconciliation_interval_seconds,
    }
    _beat_schedule["reconcile-verifications"] = {
        "task": "reconcile_verifications",
        "schedule": settings.celery_reconciliation_interval_seconds,
    }

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    imports=(
        "astinuslab.lab_packages.prepare_service",
        "astinuslab.lab_packages.verification_service",
        "astinuslab.jobs.package_instance_coordinator",
        "astinuslab.lab_packages.runner_preflight",
    ),
    beat_schedule=_beat_schedule,
    beat_scheduler="astinuslab.jobs.beat_scheduler:RecoveringPersistentScheduler",
    # Interactive, read-only analysis must not queue behind long captures.
    # Task names are frozen; only the queue assignment is new. Same-job vol3
    # work stays serialized across workers by the per-job blocking_file_lock.
    task_routes={"run_vol3_plugin": {"queue": "analysis"}},
)


@before_task_publish.connect
def _attach_request_id(headers=None, **_) -> None:
    """Carry the publishing request's id into the task message.

    Registered here rather than at each ``.delay()`` site: both the API and the
    worker publish tasks, and a correlation that depends on every call site
    remembering to pass it is one that stops working at the first new one.
    """
    if headers is None:
        return
    request_id = current_request_id()
    if request_id != "-":
        headers[REQUEST_ID_HEADER] = request_id


if os.getenv("CELERY_TASK_ALWAYS_EAGER", "").lower() in {"1", "true", "yes", "on"}:
    logger.warning(
        "CELERY_TASK_ALWAYS_EAGER is active: tasks run synchronously in-process. "
        "This is a test-only mode; never enable in deployments."
    )
    celery_app.conf.update(
        task_always_eager=True,
        task_eager_propagates=True,
    )


def _dispatch_pending_actions(job_id: str) -> None:
    """After a task completes, dispatch Celery tasks for any newly-queued DB actions.

    Handles internal chaining: e.g. run_manual_trigger_job enqueues acquire_memory_dump
    internally when auto-capture fires; this picks it up and dispatches it.
    """
    from ..db import session_scope
    from ..db.repositories import JobRepository

    with session_scope() as session:
        repo = JobRepository(session)
        queued = [(a.id, a.action_type) for a in repo.list_actions(job_id) if a.status == "queued"]

    for action_id, action_type in queued:
        task_fn = TASK_MAP.get(action_type)
        if task_fn is None:
            logger.warning("[%s] no Celery task for action_type '%s'", job_id, action_type)
            continue
        from .job_services import _dispatch_action_after_commit

        try:
            with session_scope() as session:
                _dispatch_action_after_commit(
                    session,
                    JobRepository(session),
                    job_id=job_id,
                    action_id=action_id,
                    action_type=action_type,
                    task_fn=task_fn,
                )
        except Exception:
            logger.warning("[%s] follow-up dispatch failed; intent compensated", job_id, exc_info=True)


@celery_app.task(name="run_immediate_job")
@owned_action
@capacity_launch
def run_immediate_job_task(job_id: str, action_id: int | None = None) -> None:
    from ..orchestration.boot import run_immediate_job

    run_immediate_job(job_id, action_id=action_id)
    _dispatch_pending_actions(job_id)


@celery_app.task(name="run_post_script_job")
@owned_action
@capacity_launch
def run_post_script_job_task(job_id: str, action_id: int | None = None) -> None:
    from ..orchestration.boot import run_post_script_job

    run_post_script_job(job_id, action_id=action_id)
    _dispatch_pending_actions(job_id)


@celery_app.task(name="run_manual_trigger_job")
@owned_action
@capacity_launch
def run_live_session_job_task(job_id: str, action_id: int | None = None) -> None:
    from ..live_session import run_live_session_job

    run_live_session_job(job_id, action_id=action_id)
    _dispatch_pending_actions(job_id)


@celery_app.task(name="acquire_memory_dump")
@owned_action
def acquire_memory_dump_task(job_id: str, action_id: int | None = None) -> None:
    # ISF/symbol generation must never run here: it SSHes into the live guest and
    # would contaminate the dump. ISF is built only as a separate job.
    from ..orchestration.runners.boot_capture_runner import acquire_memory_dump

    acquire_memory_dump(job_id, action_id=action_id)
    _dispatch_pending_actions(job_id)


def _requires_action_type(action_type: str):
    """Refuse the claim when the delivered action is of another type.

    ``owned_action`` fences on the job id but not on the action type, so a
    mis-routed delivery would otherwise claim — and then fail — an unrelated
    Job action. Rejecting before the claim leaves that row untouched.
    """

    def decorate(fn):
        def preclaim(session, job_id, action_id):
            from ..db.models import HostAction

            action = session.get(HostAction, action_id)
            return action is not None and action.action_type == action_type

        fn._preclaim = preclaim
        return fn

    return decorate


@celery_app.task(name="control_runtime")
@owned_action
@_requires_action_type("control_runtime")
def control_runtime_task(job_id: str, action_id: int | None = None) -> None:
    from .runtime_control import run_control_runtime_action

    if action_id is None:
        logger.error("[%s] control_runtime_task called without action_id", job_id)
        return
    run_control_runtime_action(job_id, action_id)


@celery_app.task(name="run_vol3_analysis")
@owned_action
def run_vol3_analysis_task(job_id: str, action_id: int | None = None) -> None:
    from ..forensics.vol3_analysis import run_vol3_analysis

    run_vol3_analysis(job_id, action_id=action_id)


@celery_app.task(name="run_vol3_plugin")
def run_vol3_plugin_task(job_id: str, action_id: int | None = None) -> None:
    from ..forensics.vol3_plugin_run import run_vol3_plugin

    run_vol3_plugin(job_id, action_id=action_id)


@celery_app.task(name="run_retention")
def run_retention_task() -> dict:
    from ..app_settings import load_app_settings

    if not load_app_settings().get("retention", {}).get("auto_enabled"):
        return {"skipped": True}
    try:
        from ..storage.retention import run_retention

        return run_retention()
    except Exception:
        logger.exception("run_retention task failed")
        return {"error": True}


@celery_app.task(name="reconcile_libvirt_resources")
def reconcile_libvirt_resources_task() -> dict[str, int]:
    try:
        import libvirt

        from ..db import session_scope
        from ..playground.reconciliation import reconcile_libvirt_resources

        return reconcile_libvirt_resources(
            session_scope=session_scope,
            libvirt_module=libvirt,
            libvirt_uri=settings.libvirt_uri,
            grace_seconds=settings.celery_reconciliation_grace_seconds,
        )
    except Exception:
        logger.exception("reconcile_libvirt_resources task failed")
        return {
            "orphan_domains_destroyed": 0,
            "orphan_networks_destroyed": 0,
            "zombie_vms_failed": 0,
            "zombie_networks_failed": 0,
            "zombie_runtimes_failed": 0,
            "stale_runtimes_released": 0,
            "power_states_synced": 0,
        }


TASK_MAP: dict = {
    "run_immediate_job": run_immediate_job_task,
    "run_post_script_job": run_post_script_job_task,
    "run_manual_trigger_job": run_live_session_job_task,
    "acquire_memory_dump": acquire_memory_dump_task,
    "control_runtime": control_runtime_task,
    "run_vol3_analysis": run_vol3_analysis_task,
    "run_vol3_plugin": run_vol3_plugin_task,
}


def _capture_wait_lock_dir():
    from ..forensics.services import _forensic_enqueue_lock_dir

    return _forensic_enqueue_lock_dir()


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@celery_app.task(name="teardown_job")
def teardown_job_task(job_id: str, final_status: str) -> None:
    from .. import db
    from ..db.repositories import JobRepository
    from ..orchestration.lab_lifecycle import teardown_vm

    if final_status not in {"cancelled", "failed"}:
        logger.error("[%s] teardown_job called with invalid final_status=%s", job_id, final_status)
        return

    for vm_name in (job_id, f"{job_id}-attacker"):
        try:
            teardown_vm(vm_name)
        except Exception as exc:
            logger.warning("[%s] teardown_job failed to teardown %s: %s", job_id, vm_name, exc)

    with db.session_scope() as session:
        repo = JobRepository(session)
        job = repo.get_job(job_id)
        if job is None:
            return
        finished_at = datetime.now(UTC)
        if job.status == final_status:
            repo.update_job(job_id, finished_at=finished_at)
            return
        if final_status == "cancelled" and job.status == "cancel_requested":
            repo.transition_job_status(job_id, "cancelled", finished_at=finished_at)
            repo.add_event(job_id, "cancelled", "Job cancelled")
            return
        if final_status == "failed" and job.status == "waiting_for_capture":
            repo.transition_job_status(job_id, "failed", finished_at=finished_at)
            return
        logger.warning("[%s] teardown_job not transitioning %s -> %s", job_id, job.status, final_status)


@celery_app.task(name="revert_lab_snapshot")
def revert_lab_snapshot_task(job_id: str) -> None:
    """Revert a waiting lab to its clean snapshot on the runner worker.

    Runs on the default queue, so it is serialized behind any in-flight
    capture by the --concurrency=1 worker — a revert can never interleave
    with a capture for the same VM. State is re-checked here because the
    job may have moved on while this task sat in the queue.
    """
    from .. import db
    from ..db.repositories import JobRepository
    from ..orchestration.lab_lifecycle import revert_lab_snapshot

    with db.session_scope() as session:
        repo = JobRepository(session)
        job = repo.get_job(job_id)
        if job is None or job.status != "waiting_for_capture" or not job.lab_snapshot_name:
            logger.info("[%s] revert_lab_snapshot skipped: job state changed since request", job_id)
            return
    revert_lab_snapshot(job_id)


@celery_app.task(name="expire_waiting_captures")
def expire_waiting_captures_task() -> dict[str, int]:
    from .. import db
    from ..core.locks import CaptureLockBusy, capture_lock
    from ..db.models import Job
    from ..db.repositories import JobRepository

    now = datetime.now(UTC)
    with db.session_scope() as session:
        expired_jobs = list(
            session.scalars(
                select(Job).where(
                    Job.status == "waiting_for_capture",
                    Job.capture_wait_deadline.is_not(None),
                )
            )
        )
        expired_job_ids = [
            job.id
            for job in expired_jobs
            if job.capture_wait_deadline is not None and _coerce_utc(job.capture_wait_deadline) <= now
        ]

    summary = {"auto_captured": 0, "failed": 0, "skipped": 0}
    for job_id in expired_job_ids:
        try:
            with capture_lock(job_id, base_dir=_capture_wait_lock_dir()):
                with db.session_scope() as session:
                    repo = JobRepository(session)
                    job = repo.get_job(job_id)
                    if job is None or job.status != "waiting_for_capture":
                        summary["skipped"] += 1
                        continue
                    deadline = job.capture_wait_deadline
                    if deadline is None or _coerce_utc(deadline) > now:
                        summary["skipped"] += 1
                        continue

                    if job.workload_id:
                        if any(
                            action.status in {"queued", "running"} and action.action_type == "acquire_memory_dump"
                            for action in repo.list_actions(job_id)
                        ):
                            summary["skipped"] += 1
                            continue
                        capture_type = job.capture_type or "memory"
                        repo.transition_job_status(job_id, "capture_requested")
                        repo.add_event(
                            job_id,
                            "capture_requested",
                            "Lab session timer expired — auto-capturing memory",
                        )
                        action = repo.enqueue_action(
                            job_id,
                            "acquire_memory_dump",
                            {"capture_type": capture_type},
                        )
                        repo.add_event(job_id, "action_queued", "Host action queued: acquire_memory_dump")
                        repo.add_event(
                            job_id,
                            "capture_requested",
                            "Capture request received; waiting job will hand off to acquire_memory_dump",
                        )
                        session.commit()

                        task_fn = TASK_MAP.get("acquire_memory_dump")
                        if task_fn is None:
                            raise RuntimeError("No task registered for action 'acquire_memory_dump'")
                        result = task_fn.delay(job_id, action.id)
                        with db.session_scope() as update_session:
                            JobRepository(update_session).update_job(job_id, celery_task_id=result.id)
                        summary["auto_captured"] += 1
                        continue

                    wait_started_at = job.created_at
                    for event in reversed(repo.list_events(job_id)):
                        if event.state == "waiting_for_capture":
                            wait_started_at = event.created_at
                            break
                    max_wait = max(
                        1,
                        int((_coerce_utc(deadline) - _coerce_utc(wait_started_at)).total_seconds()),
                    )
                    message = f"Timed out after {max_wait}s without receiving a capture request"
                    repo.transition_job_status(
                        job_id,
                        "failed",
                        error_summary=message,
                        finished_at=now,
                    )
                    repo.add_event(job_id, "failed", message)
                    session.commit()
                    teardown_job_task.delay(job_id, "failed")
                    summary["failed"] += 1
        except CaptureLockBusy:
            summary["skipped"] += 1
    return summary


@celery_app.task(name="reconcile_verifications")
def reconcile_verifications_task() -> dict[str, int]:
    """Reconcile frozen sessions and requeue uncompleted verification work."""
    try:
        from ..lab_packages.verification_service import (
            reconcile_authored_sessions,
            reconcile_verifications,
        )

        released = reconcile_authored_sessions()
        requeued = reconcile_verifications()
        return {
            "released_authored_sessions": released,
            "requeued_verifications": requeued,
        }
    except Exception:
        logger.exception("reconcile_verifications beat task failed")
        return {"released_authored_sessions": 0, "requeued_verifications": 0}


@celery_app.task(name="admit_waiting_jobs")
def admit_waiting_jobs_task():
    from .capacity_runtime import schedule_capacity

    return schedule_capacity()


@celery_app.task(name="cleanup_job_resources")
@owned_action
def cleanup_job_resources_task(job_id, action_id=None):
    import json

    from .. import db
    from ..db.models import CapacityReservation
    from ..db.repositories import JobRepository
    from .capacity_host import probe_cleanup

    with db.session_scope() as session:
        repo = JobRepository(session)
        action = repo.get_action(action_id)
        if action is None:
            return
        job = repo.get_job(job_id)
        row = session.get(CapacityReservation, job_id)
        if (
            job is None
            or row is None
            or row.state != "reserved"
            or job.status not in {"completed", "failed", "cancelled", "cancel_requested"}
        ):
            repo.update_action(action_id, status="cancelled", finished_at=datetime.now(UTC))
            return
        final_status = json.loads(action.payload or "{}")["final_status"]
        count = row.vm_count
    if final_status == "completed":
        # A successful capture can outlive failed best-effort cleanup. Only the
        # retained boot reservation authorizes this retry; its evidence and
        # successful parent status are unchanged (analysis owns no reservation).
        from ..orchestration.lab_lifecycle import teardown_vm

        for identifier in [job_id] + ([f"{job_id}-attacker"] if count > 1 else []):
            try:
                teardown_vm(identifier)
            except Exception:
                logger.warning("[%s] completed capture cleanup retry failed", job_id, exc_info=True)
    else:
        teardown_job_task(job_id, final_status)
    proof = probe_cleanup(settings, job_id, count)
    success = proof.domains_absent and proof.disks_absent
    with db.session_scope() as session:
        repo = JobRepository(session)
        job = repo.get_job(job_id)
        success = success and job is not None and not job.network_isolated
        repo.update_action(
            action_id,
            status="completed" if success else "failed",
            finished_at=datetime.now(UTC),
            error_summary=None if success else "Cleanup remains incomplete; retry is scheduled",
        )


TASK_MAP["cleanup_job_resources"] = cleanup_job_resources_task
