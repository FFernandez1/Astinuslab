"""Short runner admission/reconciliation ticks; no worker waits for capacity."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select

from ..db.models import CapacityReservation, HostAction, Job, JobRuntime
from ..db.repositories import JobRepository
from ..db.transactions import reserve_sqlite_write
from .capacity import (
    LAUNCH_ACTIONS,
    Requirements,
    _set_waiting,
    admit_waiting,
    confirm_cleanup,
    playground_blocks_admission,
    shortage,
)
from .capacity_host import freeze_requirements, probe_cleanup, read_host_capacity
from .job_status import RUNTIME_CONTROLLABLE_STATUSES, TERMINAL_STATUSES

logger = logging.getLogger(__name__)


def adopt_legacy_reservations(session, config, *, targets=None, workloads=None) -> int:
    """Reconstruct old/power-loss state conservatively before any new admission."""
    reserve_sqlite_write(session)
    from ..catalog import load_targets, workload_by_id

    targets = targets if targets is not None else {target["id"]: target for target in load_targets()}
    count = 0
    for job in session.scalars(select(Job).order_by(Job.created_at, Job.id)):
        existing = session.get(CapacityReservation, job.id)
        if existing is not None:
            if existing.state == "released" and job.status in TERMINAL_STATUSES and job.network_isolated:
                # Older cleanup released capacity after VM/disk absence while
                # forgetting Job-owned forwarding rules. Reclaim the original
                # budget so no new admission can outrun their removal.
                existing.state = "reserved"
                existing.released_at = None
                existing.boot_started = True
                # The previous release already ended VM preservation; that
                # historical policy must not suppress firewall cleanup retries.
                existing.preserve_failed_vm = False
                count += 1
            continue
        runtimes = list(session.scalars(select(JobRuntime).where(JobRuntime.job_id == job.id)))
        retained = any(runtime.state != "destroyed" for runtime in runtimes)
        if job.status in TERMINAL_STATUSES and not retained:
            continue
        target = targets.get(job.target_id, {})
        workload = (
            (workloads.get(job.workload_id) if workloads is not None else workload_by_id(job.workload_id))
            if job.workload_id
            else None
        )
        frozen, request = freeze_requirements(job, target, workload, config)
        for key, value in frozen.items():
            setattr(job, key, value)
        started = job.started_at is not None or any(
            runtime.state not in {"pending", "destroyed"} for runtime in runtimes
        )
        # A crash can happen between claim and setting Job.started_at.
        started = started or any(
            a.started_at is not None
            for a in JobRepository(session).list_actions(job.id)
            if a.action_type in LAUNCH_ACTIONS
        )
        waiting = job.status in {"queued", "waiting_for_capacity"} and not started
        session.add(
            CapacityReservation(
                job_id=job.id,
                vm_count=request.vm_count,
                memory_mb=request.memory_mb,
                storage_bytes=request.storage_bytes,
                preserve_failed_vm=request.preserve_failed_vm,
                state="waiting" if waiting else "reserved",
                boot_started=started,
                created_at=job.created_at,
            )
        )
        if waiting:
            _set_waiting(session, job, "Waiting for capacity: recovering durable launch intent")
        count += 1
    session.flush()
    return count


def prepare_launch(session, job_id, action_id, config, host) -> bool:
    """Recheck the actual host under the same writer lock as the execution claim."""
    reserve_sqlite_write(session)
    action = session.get(HostAction, action_id)
    job = session.get(Job, job_id)
    if action is None or job is None or action.status != "queued" or job.status != "queued":
        return False
    row = session.get(CapacityReservation, job_id)
    if row is None:
        adopt_legacy_reservations(session, config)
        admit_waiting(session, host, config)
        row = session.get(CapacityReservation, job_id)
    if row is None or row.state != "reserved" or row.boot_started:
        return False
    reason = shortage(
        session, Requirements(row.vm_count, row.memory_mb, row.storage_bytes), host, config, exclude_job=job_id
    )
    if reason is None and playground_blocks_admission(session, host):
        reason = "Waiting for capacity: Playground cleanup is still active"
    if reason is None:
        return True
    # Return younger, unstarted reservations to the same FIFO when a physical
    # shortage appears after dispatch. Their late deliveries cannot claim them.
    for candidate in session.scalars(
        select(CapacityReservation).where(
            CapacityReservation.state == "reserved", CapacityReservation.boot_started.is_(False)
        )
    ):
        if (candidate.created_at, candidate.job_id) >= (row.created_at, row.job_id):
            candidate.state = "waiting"
            waiting_job = session.get(Job, candidate.job_id)
            if waiting_job is not None:
                _set_waiting(
                    session,
                    waiting_job,
                    reason
                    if candidate.job_id == job_id
                    else f"Waiting for capacity: earlier job {job_id} has priority (FIFO)",
                )
    session.flush()
    return False


def reconcile_capacity(session, config, *, cleanup_probe=probe_cleanup, host=None) -> int:
    """Never release a booted claim on database state alone."""
    released = 0
    reserve_sqlite_write(session)
    for row in session.scalars(select(CapacityReservation).where(CapacityReservation.state != "released")):
        job = session.get(Job, row.job_id)
        if job is None:
            continue
        if job.status in TERMINAL_STATUSES and confirm_cleanup(session, job.id, never_started=True):
            released += 1
            continue
        if row.state != "reserved":
            continue
        if not row.boot_started or job.status not in TERMINAL_STATUSES | {"waiting_for_capture"}:
            continue
        if job.status == "waiting_for_capture":
            if host is None or host.error or host.synthetic:
                continue
            from .capacity import _names

            if _names(row) <= host.domains.keys():
                continue  # A healthy manual session survives normal restarts.
            JobRepository(session).transition_job_status(
                job.id,
                "failed",
                finished_at=datetime.now(UTC),
                error_summary="A retained VM disappeared; remaining resources require cleanup",
            )
        proof = cleanup_probe(config, job.id, row.vm_count)
        if not proof.domains_absent or not proof.disks_absent:
            if job.status in TERMINAL_STATUSES:
                job.capacity_reason = "Capacity retained: VM or owned storage cleanup is not confirmed"
            continue
        # Actual absence can repair stale DB rows after cleanup completed but
        # the worker died before recording destruction.
        roles = ["target", "attacker"] if row.vm_count == 2 else ["target"]
        repo = JobRepository(session)
        for role in roles:
            name = f"astinuslab-{job.id}" + ("-attacker" if role == "attacker" else "")
            repo.upsert_runtime(job.id, role, name, state="destroyed", power_state="off")
        session.flush()
        if confirm_cleanup(session, job.id, proof=proof):
            released += 1
            job.capacity_reason = None
            if job.status == "waiting_for_capture":
                repo.transition_job_status(
                    job.id,
                    "failed",
                    finished_at=datetime.now(UTC),
                    error_summary="Live VM resources disappeared; cleanup absence verified",
                )
    return released


def schedule_capacity(session_scope=None, config=None, *, host=None, cleanup_probe=probe_cleanup):
    from .. import db
    from ..core.config import settings
    from .job_services import _dispatch_action_after_commit

    scope = session_scope or db.session_scope
    config = config or settings
    host = host if host is not None else read_host_capacity(config)
    with scope() as session:
        adopt_legacy_reservations(session, config)
        released = reconcile_capacity(session, config, cleanup_probe=cleanup_probe, host=host)
        admitted = admit_waiting(session, host, config)
        queue_cleanup_intents(session, config)
        # Durable queued actions are retried after a crash between commit and
        # publish. Duplicate deliveries are harmless because of the claim CAS.
        pending = []
        stale = datetime.now(UTC) - timedelta(hours=1)
        for action in session.scalars(
            select(HostAction).where(
                HostAction.status == "queued",
                HostAction.action_type.in_(
                    LAUNCH_ACTIONS | {"acquire_memory_dump", "cleanup_job_resources", "control_runtime"}
                ),
                or_(HostAction.dispatched_at.is_(None), HostAction.dispatched_at <= stale),
            )
        ):
            parent = session.get(Job, action.job_id)
            if parent is None or (action.action_type in LAUNCH_ACTIONS and parent.status != "queued"):
                continue
            if action.action_type == "acquire_memory_dump" and parent.status in TERMINAL_STATUSES | {
                "cancel_requested"
            }:
                continue
            # A runtime control that was never delivered is only worth retrying
            # while its Job is still in a phase that permits one.
            if action.action_type == "control_runtime" and parent.status not in RUNTIME_CONTROLLABLE_STATUSES:
                continue
            if action.dispatch_attempts >= 3 and action.dispatched_at is not None:
                JobRepository(session).compensate_dispatch_failure(action.job_id, action.id)
            else:
                pending.append((action.job_id, action.id, action.action_type))
    dispatched = 0
    for job_id, action_id, action_type in pending:
        try:
            with scope() as session:
                _dispatch_action_after_commit(
                    session, JobRepository(session), job_id=job_id, action_id=action_id, action_type=action_type
                )
                dispatched += 1
        except Exception:
            logger.warning("[%s] deferred launch dispatch failed; durable compensation retained", job_id, exc_info=True)
    return {"released": released, "admitted": len(admitted), "dispatched": dispatched}


def capacity_launch(fn):
    def preclaim(session, job_id, action_id):
        from ..core.config import settings
        from . import capacity_host

        if not prepare_launch(session, job_id, action_id, settings, capacity_host.read_host_capacity(settings)):
            return False
        session.get(CapacityReservation, job_id).boot_started = True
        return True

    fn._preclaim = preclaim
    return fn


def queue_cleanup_intents(session, config):
    """One durable cleanup action at a time, at most one new retry per minute."""
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=60)
    for row in session.scalars(
        select(CapacityReservation).where(
            CapacityReservation.state == "reserved", CapacityReservation.boot_started.is_(True)
        )
    ):
        job = session.get(Job, row.job_id)
        if job is None or job.status not in {"completed", "failed", "cancelled", "cancel_requested"}:
            continue
        if job.status == "failed" and row.preserve_failed_vm:
            continue
        repo = JobRepository(session)
        prior = [a for a in repo.list_actions(job.id) if a.action_type == "cleanup_job_resources"]
        if any(a.status in {"queued", "running"} for a in prior):
            continue
        if prior and (prior[-1].finished_at or prior[-1].created_at).replace(tzinfo=None) > cutoff:
            continue
        repo.enqueue_action(
            job.id,
            "cleanup_job_resources",
            {"final_status": "cancelled" if job.status in {"cancelled", "cancel_requested"} else job.status},
        )


def dispatch_analysis_intents(session_scope):
    from .job_services import _dispatch_action_after_commit

    with session_scope() as session:
        pending = [
            (a.job_id, a.id, a.action_type)
            for a in session.scalars(
                select(HostAction).where(
                    HostAction.status == "queued",
                    HostAction.action_type == "run_vol3_plugin",
                    HostAction.dispatched_at.is_(None),
                )
            )
        ]
    for job_id, action_id, kind in pending:
        try:
            with session_scope() as session:
                _dispatch_action_after_commit(
                    session, JobRepository(session), job_id=job_id, action_id=action_id, action_type=kind
                )
        except Exception:
            logger.warning("[%s] recovered analysis dispatch failed", job_id, exc_info=True)
