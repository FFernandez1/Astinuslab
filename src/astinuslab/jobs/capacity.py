"""Atomic FIFO admission against committed VM/RAM/storage requirements.

Every path uses SQLite's writer mutex before reading capacity or product
exclusion. Reservations include pending boots and survive terminal Job states.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from ..db.models import CapacityReservation, HostAction, Job, JobRuntime, JobVM
from ..db.transactions import reserve_sqlite_write

GIB = 1024**3
MIB = 1024**2
LAUNCH_ACTIONS = {"run_immediate_job", "run_post_script_job", "run_manual_trigger_job"}


@dataclass(frozen=True)
class Requirements:
    vm_count: int
    memory_mb: int
    storage_bytes: int
    preserve_failed_vm: bool = False


@dataclass(frozen=True)
class HostCapacity:
    memory_total_mb: int
    memory_available_mb: int
    storage_total_bytes: int
    storage_free_bytes: int
    domains: dict[str, int]  # actual retained domain names -> configured RAM MiB
    inactive_domains: frozenset[str] = frozenset()  # defined but powered off
    error: str | None = None
    synthetic: bool = False


def validate_total(request: Requirements, host: HostCapacity, config) -> None:
    if min(request.vm_count, request.memory_mb, request.storage_bytes) <= 0:
        raise ValueError("capacity requirements must be positive")
    limit = config.max_concurrent_vms
    if limit is not None and request.vm_count > limit:
        raise ValueError(f"job requires {request.vm_count} VM(s), configured capacity is {limit}")
    if host.error:
        return  # Unknown host state queues closed; it cannot prove impossibility.
    if request.memory_mb + config.capacity_ram_headroom_mb > host.memory_total_mb:
        raise ValueError(f"job requires {request.memory_mb} MiB RAM plus host headroom, exceeding total RAM")
    if request.storage_bytes + config.capacity_storage_headroom_gb * GIB > host.storage_total_bytes:
        raise ValueError("job storage requirement plus host headroom exceeds total storage capacity")


def _names(row: CapacityReservation) -> set[str]:
    names = {f"astinuslab-{row.job_id}"}
    if row.vm_count > 1:
        names.add(f"astinuslab-{row.job_id}-attacker")
    return names


def playground_blocks_admission(session, host: HostCapacity) -> bool:
    from ..db.repositories import has_active_playground_session

    return has_active_playground_session(session) or any(name.startswith("astinuslab-pg-") for name in host.domains)


def shortage(session, request: Requirements, host: HostCapacity, config, *, exclude_job=None) -> str | None:
    if host.error:
        return f"Waiting for capacity: {host.error}"
    rows = list(session.scalars(select(CapacityReservation).where(CapacityReservation.state == "reserved")))
    rows = [row for row in rows if row.job_id != exclude_job]
    names = set().union(*(_names(row) for row in rows)) if rows else set()
    # Unknown/failed/powered-off resources remain charged until destruction is
    # confirmed. Names deduplicate DB runtime rows against actual host domains.
    external = {name: memory + config.capacity_vm_overhead_mb for name, memory in host.domains.items()}
    tracked: set[str] = set()
    for runtime in session.scalars(select(JobRuntime).where(JobRuntime.state != "destroyed")):
        if runtime.job_id == exclude_job:
            continue
        parent = session.get(Job, runtime.job_id)
        if (
            runtime.state == "pending"
            and parent is not None
            and parent.started_at is None
            and parent.status in {"queued", "waiting_for_capacity"}
        ):
            continue  # Placeholder rows created by the API do not imply a boot.
        tracked.add(runtime.vm_name)
        external.setdefault(runtime.vm_name, 2048 + config.capacity_vm_overhead_mb)
    playground_storage = 0
    for vm in session.scalars(select(JobVM).where(JobVM.state != "destroyed")):
        # Portable package VMs persist requested dimensions before boot. Keep
        # those pending commitments even if no libvirt domain exists yet, and
        # retain the larger bound when the actual guest was externally resized.
        tracked.add(vm.domain_name)
        external[vm.domain_name] = max(
            external.get(vm.domain_name, 0), (vm.memory_mb or 2048) + config.capacity_vm_overhead_mb
        )
        playground_storage += (vm.disk_gb or 40) * GIB
    # A powered-off definition no AstinusLab record claims holds disk, not RAM.
    # Charging its configured maximum would let unrelated host VMs deadlock
    # admission forever; the next probe recharges it as soon as it boots.
    external = {
        name: 0 if name in host.inactive_domains and name not in tracked else memory
        for name, memory in external.items()
    }
    external = {name: memory for name, memory in external.items() if name not in names}
    count = sum(row.vm_count for row in rows) + len(external)
    if config.max_concurrent_vms is not None and count + request.vm_count > config.max_concurrent_vms:
        return f"Waiting for capacity: VM limit {config.max_concurrent_vms}; {count} committed, {request.vm_count} requested"
    memory = sum(external.values())
    pending = sum(max(0, memory - host.domains.get(name, 0)) for name, memory in external.items())
    for row in rows:
        observed = [host.domains[name] for name in _names(row) if name in host.domains]
        # An adopted or externally resized domain can exceed its stored budget.
        # Deduplicate its name without discarding the larger actual commitment.
        committed = max(row.memory_mb, sum(observed) + len(observed) * config.capacity_vm_overhead_mb)
        memory += committed
        pending += max(0, committed - sum(observed))
    # MemAvailable already reflects resident guests. Subtract only the pending
    # commitment here, and enforce total committed RAM independently.
    if (
        min(host.memory_total_mb - memory, host.memory_available_mb - pending)
        < request.memory_mb + config.capacity_ram_headroom_mb
    ):
        return f"Waiting for capacity: RAM headroom; {request.memory_mb} MiB requested, {config.capacity_ram_headroom_mb} MiB reserved for host"
    # Full writable-disk bounds are deliberately conservative for retained
    # Playground resources; sparse overlays may still grow after admission.
    storage = sum(row.storage_bytes for row in rows) + playground_storage
    if host.storage_free_bytes - storage < request.storage_bytes + config.capacity_storage_headroom_gb * GIB:
        return f"Waiting for capacity: storage headroom; {request.storage_bytes / GIB:.1f} GiB requested, {config.capacity_storage_headroom_gb} GiB reserved for host"
    return None


def _set_waiting(session, job, reason):
    job.status = "waiting_for_capacity"
    job.current_stage = "waiting_for_capacity"
    job.capacity_reason = reason
    for action in session.scalars(
        select(HostAction).where(
            HostAction.job_id == job.id,
            HostAction.action_type.in_(LAUNCH_ACTIONS),
            HostAction.status.in_(("queued", "waiting_for_capacity")),
        )
    ):
        action.status = "waiting_for_capacity"
        # The boot delivery was consumed without claiming execution. Its send
        # lease must not suppress the next admission's fresh delivery.
        action.dispatched_at = None
        action.celery_task_id = None
        action.dispatch_attempts = 0
    session.flush()


def reserve_job(session, job, request: Requirements, host: HostCapacity, config) -> bool:
    reserve_sqlite_write(session)
    validate_total(request, host, config)
    row = session.get(CapacityReservation, job.id)
    if row is None:
        row = CapacityReservation(
            job_id=job.id,
            vm_count=request.vm_count,
            memory_mb=request.memory_mb,
            storage_bytes=request.storage_bytes,
            preserve_failed_vm=request.preserve_failed_vm,
            state="waiting",
            created_at=job.created_at,
        )
        session.add(row)
        session.flush()
    if row.state == "reserved":
        return True
    _set_waiting(session, job, "Waiting for capacity: admission pending")
    admit_waiting(session, host, config)
    return row.state == "reserved"


def admit_waiting(session, host: HostCapacity, config) -> list[tuple[str, int]]:
    reserve_sqlite_write(session)

    rows = list(
        session.scalars(
            select(CapacityReservation)
            .where(CapacityReservation.state == "waiting")
            .order_by(CapacityReservation.created_at, CapacityReservation.job_id)
        )
    )
    admitted = []
    blocked = None
    for row in rows:
        job = session.get(Job, row.job_id)
        if job is None or job.status != "waiting_for_capacity":
            continue
        reason = blocked
        if reason is None and playground_blocks_admission(session, host):
            reason = "Waiting for capacity: Playground cleanup is still active"
        request = Requirements(row.vm_count, row.memory_mb, row.storage_bytes)
        if reason is None:
            reason = shortage(session, request, host, config)
        if reason is not None:
            _set_waiting(session, job, reason)
            blocked = f"Waiting for capacity: earlier job {job.id} has priority (FIFO)"
            continue
        row.state = "reserved"
        job.status = "queued"
        job.current_stage = "queued"
        job.capacity_reason = None
        for action in session.scalars(
            select(HostAction).where(HostAction.job_id == row.job_id, HostAction.status == "waiting_for_capacity")
        ):
            action.status = "queued"
            admitted.append((job.id, action.id))
        session.flush()
    return admitted


def confirm_cleanup(session, job_id: str, *, never_started=False, proof=None) -> bool:
    """Release only after all expected runtime roles have destruction proof."""
    reserve_sqlite_write(session)
    row = session.get(CapacityReservation, job_id)
    if row is None or row.state == "released":
        return False
    job = session.get(Job, job_id)
    if job is not None and job.network_isolated:
        # Firewall rules outlive domains/disks and can affect reused guest IPs.
        # Keep the reservation so the durable cleanup action continues retrying.
        return False
    roles = {
        runtime.role: runtime.state
        for runtime in session.scalars(select(JobRuntime).where(JobRuntime.job_id == job_id))
    }
    expected = {"target", "attacker"} if row.vm_count == 2 else {"target"}
    if never_started:
        if row.boot_started or any(state != "pending" for state in roles.values()):
            return False
        for runtime in session.scalars(select(JobRuntime).where(JobRuntime.job_id == job_id)):
            runtime.state = "destroyed"
            runtime.power_state = "off"
    elif (
        proof is None
        or proof.job_id != job_id
        or not proof.domains_absent
        or not proof.disks_absent
        or any(roles.get(role) != "destroyed" for role in expected)
    ):
        return False
    row.state = "released"
    row.released_at = datetime.now(UTC)
    session.flush()
    return True
