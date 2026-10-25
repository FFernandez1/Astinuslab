"""Playground persistence helpers.

Transaction ownership lives with the caller: functions here never commit.
Mutating helpers ``flush()`` so their changes are visible to later queries in
the same transaction; the enclosing ``session_scope()`` (or other unit of
work) commits or rolls back.
"""

from __future__ import annotations

import ipaddress
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, true
from sqlalchemy.orm import Session

from ..db.models import JobNetwork, JobVM, JobVMNic, PlaygroundSession, PlaygroundTopologyLayout
from ..db.transactions import reserve_sqlite_write

# A playground session is "active" — and therefore blocks new sessions and
# new forensic jobs — from the moment it is created until cleanup has fully
# finished (success or failure). destroy_requested means the operator clicked
# end-session but cleanup tasks have not yet run; destroying means cleanup is
# in flight. authoring_closed means the creator froze the lab for clean-run
# verification: the VMs, networks and disk overlays still exist because the
# verification worker has to observe and capture them, so the singleton stays
# occupied until that worker runs cleanup.
ACTIVE_PLAYGROUND_STATES = (
    "pending",
    "queued",
    "running",
    "cancel_requested",
    "destroy_requested",
    "destroying",
    "destroy_failed",
    "failure_cleanup",
    "authoring_closed",
)

CLEANUP_BACKLOG_STATES = ("destroy_cleanup_pending", "failure_cleanup_pending")

# The frozen authoring state. The browser, the API error envelope and the
# verification worker all compare against it, so it is named once here.
AUTHORING_CLOSED = "authoring_closed"

NIC_DETACH_CLAIM_SECONDS = 5 * 60

# Only a session that is still accepting authoring work may be frozen. A
# session already in cleanup (destroy_*/failure_cleanup) is on its way out and
# its overlays may already be gone, so freezing it would promise artifacts that
# no longer exist.
AUTHORING_FREEZABLE_STATES = ("pending", "queued", "running")

_AUTHORING_BUSY_VM_STATES = frozenset(
    {
        "pending",
        "starting",
        "provisioning_workload",
        "provisioning_workload_active",
        "powering_on",
        "powering_off",
        "rebooting",
        "destroy_queued",
        "destroying",
    }
)
_AUTHORING_BUSY_NETWORK_STATES = frozenset({"pending", "creating", "destroy_queued", "destroying"})
_AUTHORING_BUSY_NIC_STATES = frozenset({"pending", "attaching", "detaching", "detach_claimed"})


class AuthoredSessionUnavailable(Exception):
    """The authored Playground session cannot be frozen for verification."""


class AuthoringClosed(Exception):
    """The frozen Playground session no longer accepts operator mutations."""


def require_session_open_for_authoring(session: Session, session_id: str) -> PlaygroundSession | None:
    """Return the session unless verification has frozen its authored state."""

    playground_session = (
        session.query(PlaygroundSession).filter(PlaygroundSession.id == session_id).with_for_update().one_or_none()
    )
    if playground_session is not None and playground_session.status == AUTHORING_CLOSED:
        raise AuthoringClosed("Playground authoring is frozen for clean-run verification")
    return playground_session


def close_authored_session(session: Session, session_id: str) -> PlaygroundSession:
    """Freeze one authored session so a clean run can observe and capture it.

    This is a state transition only: VM, network and NIC rows keep their
    identities and every disk overlay stays on disk. Cleanup remains the
    verification worker's job, once capture has succeeded or the attempt has
    reached a terminal failure.

    Freezing is idempotent so a resumed verification finds the same frozen
    session instead of a spurious conflict.
    """

    _begin_immediate_for_sqlite(session)
    playground_session = (
        session.query(PlaygroundSession).filter(PlaygroundSession.id == session_id).with_for_update().one_or_none()
    )
    if playground_session is None:
        raise AuthoredSessionUnavailable("authored Playground session was not found")
    if playground_session.status == AUTHORING_CLOSED:
        return playground_session
    if playground_session.status not in AUTHORING_FREEZABLE_STATES:
        raise AuthoredSessionUnavailable(
            f"authored Playground session is {playground_session.status} and can no longer be verified"
        )
    busy_vm = (
        session.query(JobVM.id)
        .filter(JobVM.session_id == session_id, JobVM.state.in_(_AUTHORING_BUSY_VM_STATES))
        .first()
    )
    busy_network = (
        session.query(JobNetwork.id)
        .filter(JobNetwork.session_id == session_id, JobNetwork.state.in_(_AUTHORING_BUSY_NETWORK_STATES))
        .first()
    )
    busy_nic = (
        session.query(JobVMNic.id)
        .join(JobVM, JobVM.id == JobVMNic.job_vm_id)
        .filter(JobVM.session_id == session_id, JobVMNic.state.in_(_AUTHORING_BUSY_NIC_STATES))
        .first()
    )
    if busy_vm is not None or busy_network is not None or busy_nic is not None:
        raise AuthoredSessionUnavailable("an authored resource operation is still in progress")
    playground_session.status = AUTHORING_CLOSED
    session.flush()
    return playground_session


def claim_authored_session_cleanup(session: Session, session_id: str) -> bool:
    """Atomically transfer a frozen session to the cleanup coordinator."""

    _begin_immediate_for_sqlite(session)
    claimed = (
        session.query(PlaygroundSession)
        .filter(
            PlaygroundSession.id == session_id,
            PlaygroundSession.status == AUTHORING_CLOSED,
        )
        .update(
            {
                PlaygroundSession.status: "destroying",
                PlaygroundSession.finished_at: None,
            },
            synchronize_session=False,
        )
    )
    session.flush()
    return claimed == 1


def promote_cleanup_backlog(session: Session) -> PlaygroundSession | None:
    """Adopt exactly one legacy cleanup row while retaining global exclusion."""
    _begin_immediate_for_sqlite(session)
    if session.query(PlaygroundSession.id).filter(PlaygroundSession.status.in_(ACTIVE_PLAYGROUND_STATES)).first():
        return None
    candidate = (
        session.query(PlaygroundSession)
        .filter(PlaygroundSession.status.in_(CLEANUP_BACKLOG_STATES))
        .order_by(PlaygroundSession.created_at, PlaygroundSession.id)
        .first()
    )
    if candidate is None:
        return None
    candidate.status = "failure_cleanup" if candidate.status == "failure_cleanup_pending" else "destroy_failed"
    candidate.finished_at = None
    session.flush()
    return candidate


def create_playground_session(session: Session, *, job_id: str) -> PlaygroundSession:
    from ..api.errors import ApiError
    from ..db.repositories import has_active_non_playground_job, has_active_playground_session

    _begin_immediate_for_sqlite(session)
    if has_active_non_playground_job(session):
        raise ApiError(409, "a forensic job or its cleanup is currently active", "conflict")
    # The unique active-session index covers ordinary sessions; a durable
    # package reset also owns admission while it replaces its session row.
    if active_playground_session(session) is None and has_active_playground_session(session):
        raise ApiError(409, "Playground cleanup or a package reset is currently active", "conflict")
    playground_session = PlaygroundSession(
        id=job_id,
        status="running",
        started_at=datetime.now(UTC),
    )
    session.add(playground_session)
    session.flush()
    return playground_session


def active_playground_session(session: Session) -> PlaygroundSession | None:
    return session.query(PlaygroundSession).filter(PlaygroundSession.status.in_(ACTIVE_PLAYGROUND_STATES)).one_or_none()


def latest_destroy_failed_session(session: Session) -> PlaygroundSession | None:
    return (
        session.query(PlaygroundSession)
        .filter(PlaygroundSession.status == "destroy_failed")
        .order_by(PlaygroundSession.finished_at.desc())
        .first()
    )


def mark_session_destroying(session: Session, job_id: str) -> None:
    playground_session = session.query(PlaygroundSession).filter_by(id=job_id).one()
    playground_session.status = "destroying"
    session.flush()


def mark_session_destroy_requested(session: Session, job_id: str) -> None:
    playground_session = session.query(PlaygroundSession).filter_by(id=job_id).one()
    playground_session.status = "destroy_requested"
    session.flush()


def mark_session_destroy_failed(session: Session, job_id: str, summary: str | None = None) -> None:
    playground_session = session.query(PlaygroundSession).filter_by(id=job_id).one()
    playground_session.status = "destroy_failed"
    playground_session.error_summary = summary
    playground_session.finished_at = datetime.now(UTC)
    session.flush()


def _cidrs_overlap(a: str, b: str) -> bool:
    return ipaddress.ip_network(a, strict=False).overlaps(ipaddress.ip_network(b, strict=False))


def next_free_cidr(existing: list[str]) -> str:
    """Return the lowest 10.42.x.0/24 that overlaps no entry in `existing`."""
    for octet in range(1, 255):
        candidate = f"10.42.{octet}.0/24"
        if not any(_cidrs_overlap(candidate, taken) for taken in existing):
            return candidate
    raise RuntimeError("playground CIDR pool exhausted")


def resolve_cidr(session: Session, job_id: str, requested_cidr: str) -> tuple[str, str | None]:
    """Return (final_cidr, substituted_from).

    If `requested_cidr` overlaps no existing session network it is returned
    unchanged with substituted_from None. Otherwise the next free CIDR is
    returned and substituted_from is set to the original request.
    """
    existing = [n.subnet_cidr for n in list_live_session_networks(session, job_id)]
    if not any(_cidrs_overlap(requested_cidr, taken) for taken in existing):
        return requested_cidr, None
    return next_free_cidr(existing), requested_cidr


def _begin_immediate_for_sqlite(session: Session) -> None:
    """Take SQLite's write lock before the CIDR-overlap check.

    SQLite acquires its single write lock at the first write statement of a
    transaction, so a plain check-then-insert can interleave with another
    writer. When no write has been flushed yet on this connection, BEGIN
    IMMEDIATE grabs the lock up front so the overlap check runs serialized
    against other writers. When the connection is already in a database-level
    transaction the lock is already held and nothing needs to be done.
    """
    reserve_sqlite_write(session)


def serialize_write_transaction(session: Session) -> None:
    """Acquire SQLite's write lock before a read/then-write workflow.

    Callers on databases with row-level locks still need ``FOR UPDATE`` on
    the row that serializes their operation.  SQLite has no such lock, so the
    transaction itself must be made exclusive before the first lookup.
    """

    _begin_immediate_for_sqlite(session)


def mark_session_completed(session: Session, job_id: str) -> None:
    playground_session = session.query(PlaygroundSession).filter_by(id=job_id).one()
    playground_session.status = "completed"
    playground_session.finished_at = datetime.now(UTC)
    session.flush()
    promote_cleanup_backlog(session)


def mark_session_failed(session: Session, job_id: str, summary: str) -> None:
    playground_session = session.query(PlaygroundSession).filter_by(id=job_id).one()
    playground_session.error_summary = summary
    dirty_vm = session.query(JobVM.id).filter(JobVM.session_id == job_id, JobVM.state != "destroyed").first()
    dirty_network = (
        session.query(JobNetwork.id).filter(JobNetwork.session_id == job_id, JobNetwork.state != "destroyed").first()
    )
    if dirty_vm is not None or dirty_network is not None:
        playground_session.status = "failure_cleanup"
        playground_session.finished_at = None
    else:
        playground_session.status = "failed"
        playground_session.finished_at = datetime.now(UTC)
    session.flush()
    if playground_session.status == "failed":
        promote_cleanup_backlog(session)


def mark_session_cancelled(session: Session, job_id: str, summary: str | None = None) -> None:
    playground_session = session.query(PlaygroundSession).filter_by(id=job_id).one()
    playground_session.status = "cancelled"
    playground_session.error_summary = summary
    playground_session.finished_at = datetime.now(UTC)
    session.flush()
    promote_cleanup_backlog(session)


def register_vm(
    session: Session,
    *,
    vm_id: str,
    job_id: str,
    label: str,
    image_id: str,
    domain_name: str,
    disk_path: str,
    role: str = "target",
    workload_id: str | None = None,
    vcpus: int | None = None,
    memory_mb: int | None = None,
    disk_gb: int | None = None,
) -> JobVM:
    _require_resource_registration_session(session, job_id)
    vm = JobVM(
        id=vm_id,
        session_id=job_id,
        label=label,
        image_id=image_id,
        domain_name=domain_name,
        disk_path=disk_path,
        state="pending",
        role=role,
        workload_id=workload_id,
        workload_state="planned" if workload_id else None,
        vcpus=vcpus,
        memory_mb=memory_mb,
        disk_gb=disk_gb,
    )
    session.add(vm)
    session.flush()
    return vm


def update_vm_state(session: Session, vm_id: str, state: str, *, error: str | None = None) -> None:
    vm = session.query(JobVM).filter_by(id=vm_id).one()
    vm.state = state
    vm.updated_at = datetime.now(UTC)
    if error is not None:
        vm.error_summary = error
    session.flush()


def fail_pending_vm_for_resource_validation(
    session: Session,
    *,
    vm_id: str,
    session_id: str,
    label: str,
    image_id: str,
    role: str,
    workload_id: str | None,
    domain_name: str,
    disk_path: str,
    vcpus: int | None,
    memory_mb: int | None,
    disk_gb: int | None,
    error: str,
) -> bool:
    """Fence a marked worker's invalid resources to its exact pending row."""
    workload_filter = JobVM.workload_id.is_(None) if workload_id is None else JobVM.workload_id == workload_id
    disk_filter = JobVM.disk_gb.is_(None) if disk_gb is None else JobVM.disk_gb == disk_gb
    changed = (
        session.query(JobVM)
        .filter(
            JobVM.id == vm_id,
            JobVM.session_id == session_id,
            JobVM.label == label,
            JobVM.image_id == image_id,
            JobVM.role == role,
            workload_filter,
            JobVM.domain_name == domain_name,
            JobVM.disk_path == disk_path,
            JobVM.state == "pending",
            JobVM.vcpus == vcpus,
            JobVM.memory_mb == memory_mb,
            disk_filter,
        )
        .update(
            {
                JobVM.state: "failed",
                JobVM.error_summary: error[:512],
                JobVM.updated_at: datetime.now(UTC),
            },
            synchronize_session=False,
        )
    )
    session.flush()
    return changed == 1


_VM_POWER_ACTION_STATES = {
    ("stopped", "powering_on"),
    ("running", "powering_off"),
    ("running", "rebooting"),
}


def claim_vm_for_power_action(
    session: Session,
    vm_id: str,
    *,
    expected_state: str,
    claim_state: str,
) -> datetime | None:
    """Claim an exact VM power transition before invoking virsh."""
    if (expected_state, claim_state) not in _VM_POWER_ACTION_STATES:
        raise ValueError(f"unsupported VM power transition: {expected_state} -> {claim_state}")
    claimed_at = datetime.now(UTC)
    claimed = (
        session.query(JobVM)
        .filter(JobVM.id == vm_id, JobVM.state == expected_state)
        .filter(
            JobVM.session_id.in_(
                select(PlaygroundSession.id).where(PlaygroundSession.status.in_(["queued", "running"]))
            )
        )
        .update(
            {JobVM.state: claim_state, JobVM.updated_at: claimed_at},
            synchronize_session=False,
        )
    )
    session.flush()
    if claimed != 1:
        return None
    session.expire_all()
    vm = session.query(JobVM).filter_by(id=vm_id).one()
    return vm.updated_at


def finish_vm_power_action(
    session: Session,
    vm_id: str,
    *,
    claim_state: str,
    expected_updated_at: datetime,
    state: str,
    error: str | None = None,
) -> bool:
    if claim_state not in {claim for _, claim in _VM_POWER_ACTION_STATES}:
        raise ValueError(f"unsupported VM power claim state: {claim_state}")
    values: dict[Any, Any] = {
        JobVM.state: state,
        JobVM.updated_at: datetime.now(UTC),
    }
    if error is not None:
        values[JobVM.error_summary] = error
    finished = (
        session.query(JobVM)
        .filter(
            JobVM.id == vm_id,
            JobVM.state == claim_state,
            JobVM.updated_at == expected_updated_at,
        )
        .update(values, synchronize_session=False)
    )
    session.flush()
    return finished == 1


def claim_vm_for_spawn(session: Session, vm_id: str, *, expected_updated_at: datetime | None = None) -> bool:
    """Atomically transfer a retryable VM row from teardown to spawn custody."""
    claimed = (
        session.query(JobVM)
        .filter(JobVM.id == vm_id)
        .filter(JobVM.state.in_(["pending", "failed", "teardown_failed"]))
        .filter(true() if expected_updated_at is None else JobVM.state == "pending")
        .filter(true() if expected_updated_at is None else JobVM.updated_at == expected_updated_at)
        .filter(
            JobVM.session_id.in_(
                select(PlaygroundSession.id).where(PlaygroundSession.status.in_(["queued", "running"]))
            )
        )
        .update(
            {
                JobVM.state: "starting",
                JobVM.error_summary: None,
                JobVM.workload_error: None,
                JobVM.updated_at: datetime.now(UTC),
            },
            synchronize_session=False,
        )
    )
    session.flush()
    return claimed == 1


def finish_vm_spawn(
    session: Session,
    vm_id: str,
    state: str,
    *,
    expected_updated_at: datetime,
    error: str | None = None,
    workload_state: str | None = None,
) -> bool:
    values: dict[Any, Any] = {
        JobVM.state: state,
        JobVM.error_summary: error,
        JobVM.updated_at: datetime.now(UTC),
    }
    if workload_state is not None:
        values[JobVM.workload_state] = workload_state
    finished = (
        session.query(JobVM)
        .filter(
            JobVM.id == vm_id,
            JobVM.state == "starting",
            JobVM.updated_at == expected_updated_at,
        )
        .update(values, synchronize_session=False)
    )
    session.flush()
    return finished == 1


def claim_vm_for_workload(session: Session, vm_id: str) -> bool:
    claimed = (
        session.query(JobVM)
        .filter(JobVM.id == vm_id, JobVM.state == "provisioning_workload")
        .filter(
            JobVM.session_id.in_(
                select(PlaygroundSession.id).where(PlaygroundSession.status.in_(["queued", "running"]))
            )
        )
        .update(
            {JobVM.state: "provisioning_workload_active", JobVM.updated_at: datetime.now(UTC)},
            synchronize_session=False,
        )
    )
    session.flush()
    return claimed == 1


def queue_rejected_workload_for_destroy(session: Session, vm_id: str) -> bool:
    """Transfer an undelivered workload handoff to teardown after parent stop."""
    queued = (
        session.query(JobVM)
        .filter(JobVM.id == vm_id, JobVM.state == "provisioning_workload")
        .filter(
            JobVM.session_id.in_(
                select(PlaygroundSession.id).where(PlaygroundSession.status.not_in(["queued", "running"]))
            )
        )
        .update(
            {JobVM.state: "destroy_queued", JobVM.updated_at: datetime.now(UTC)},
            synchronize_session=False,
        )
    )
    session.flush()
    return queued == 1


def finish_vm_workload(
    session: Session,
    vm_id: str,
    state: str,
    workload_state: str,
    *,
    expected_updated_at: datetime,
    error: str | None = None,
    workload_error: str | None = None,
) -> bool:
    finished = (
        session.query(JobVM)
        .filter(
            JobVM.id == vm_id,
            JobVM.state == "provisioning_workload_active",
            JobVM.updated_at == expected_updated_at,
        )
        .update(
            {
                JobVM.state: state,
                JobVM.workload_state: workload_state,
                JobVM.error_summary: error,
                JobVM.workload_error: workload_error,
                JobVM.updated_at: datetime.now(UTC),
            },
            synchronize_session=False,
        )
    )
    session.flush()
    return finished == 1


def claim_vm_for_workload_enqueue_cleanup(session: Session, vm_id: str) -> bool:
    """Claim a booted VM only if no workload worker accepted the handoff."""
    claimed = (
        session.query(JobVM)
        .filter(JobVM.id == vm_id, JobVM.state == "provisioning_workload")
        .update(
            {JobVM.state: "destroying", JobVM.updated_at: datetime.now(UTC)},
            synchronize_session=False,
        )
    )
    session.flush()
    return claimed == 1


def claim_vm_for_destroy(session: Session, vm_id: str) -> bool:
    # `stopping` observes a completed shutdown request, not an active worker.
    # An uncooperative guest must remain explicitly destroyable.
    claimed = (
        session.query(JobVM)
        .filter(JobVM.id == vm_id)
        .filter(
            JobVM.state.in_(
                ["pending", "running", "stopped", "stopping", "failed", "teardown_failed", "destroy_queued"]
            )
        )
        .update(
            {JobVM.state: "destroying", JobVM.updated_at: datetime.now(UTC)},
            synchronize_session=False,
        )
    )
    session.flush()
    return claimed == 1


def queue_vm_for_destroy(session: Session, vm_id: str) -> bool:
    queued = (
        session.query(JobVM)
        .filter(JobVM.id == vm_id)
        .filter(JobVM.state.in_(["pending", "running", "stopped", "stopping", "failed", "teardown_failed"]))
        .update(
            {JobVM.state: "destroy_queued", JobVM.updated_at: datetime.now(UTC)},
            synchronize_session=False,
        )
    )
    session.flush()
    return queued == 1


def cancel_queued_vm_destroy(session: Session, vm_id: str, prior_state: str, *, error: str) -> bool:
    cancelled = (
        session.query(JobVM)
        .filter(JobVM.id == vm_id, JobVM.state == "destroy_queued")
        .update(
            {
                JobVM.state: prior_state,
                JobVM.error_summary: error,
                JobVM.updated_at: datetime.now(UTC),
            },
            synchronize_session=False,
        )
    )
    session.flush()
    return cancelled == 1


def finish_vm_destroy(
    session: Session,
    vm_id: str,
    state: str,
    *,
    expected_updated_at: datetime,
    error: str | None = None,
) -> bool:
    finished = (
        session.query(JobVM)
        .filter(
            JobVM.id == vm_id,
            JobVM.state == "destroying",
            JobVM.updated_at == expected_updated_at,
        )
        .update(
            {
                JobVM.state: state,
                JobVM.error_summary: error,
                JobVM.updated_at: datetime.now(UTC),
            },
            synchronize_session=False,
        )
    )
    session.flush()
    return finished == 1


def claim_vm_for_teardown_retry(session: Session, vm_id: str) -> bool:
    """Claim only a still-failed teardown; a concurrent spawn wins by leaving this false."""
    claimed = (
        session.query(JobVM)
        .filter(JobVM.id == vm_id, JobVM.state == "teardown_failed")
        .update(
            {
                JobVM.state: "destroying",
                JobVM.updated_at: datetime.now(UTC),
            },
            synchronize_session=False,
        )
    )
    session.flush()
    return claimed == 1


def claim_legacy_vm_for_teardown(
    session: Session,
    vm_id: str,
    expected_state: str,
    expected_updated_at: datetime,
) -> bool:
    """Claim a pre-hardening terminal row whose libvirt domain still exists.

    Spawn retries accept the same states, so the expected-state compare is the
    custody boundary: whichever transition commits first owns the host action.
    """
    if expected_state not in {"failed", "destroyed"}:
        raise ValueError(f"unsupported legacy teardown state: {expected_state}")
    claimed = (
        session.query(JobVM)
        .filter(
            JobVM.id == vm_id,
            JobVM.state == expected_state,
            JobVM.updated_at == expected_updated_at,
        )
        .update(
            {
                JobVM.state: "destroying",
                JobVM.updated_at: datetime.now(UTC),
            },
            synchronize_session=False,
        )
    )
    session.flush()
    return claimed == 1


def update_vm_workload_state(session: Session, vm_id: str, state: str, *, error: str | None = None) -> None:
    vm = session.query(JobVM).filter_by(id=vm_id).one()
    vm.workload_state = state
    vm.updated_at = datetime.now(UTC)
    if error is not None:
        vm.workload_error = error
    session.flush()


def list_session_vms(session: Session, job_id: str) -> list[JobVM]:
    return list(session.query(JobVM).filter_by(session_id=job_id).order_by(JobVM.created_at).all())


def register_network(
    session: Session,
    *,
    network_id: str,
    job_id: str,
    name: str,
    subnet_cidr: str,
    isolated: bool,
    libvirt_net_name: str,
) -> JobNetwork:
    _require_resource_registration_session(session, job_id)
    existing = list_live_session_networks(session, job_id)
    if any(n.id != network_id and _cidrs_overlap(n.subnet_cidr, subnet_cidr) for n in existing):
        raise RuntimeError(f"CIDR {subnet_cidr} overlaps existing network")
    net = JobNetwork(
        id=network_id,
        session_id=job_id,
        name=name,
        subnet_cidr=subnet_cidr,
        isolated=isolated,
        libvirt_net_name=libvirt_net_name,
        state="pending",
    )
    session.add(net)
    session.flush()
    return net


def update_network_state(session: Session, network_id: str, state: str, *, error: str | None = None) -> None:
    net = session.query(JobNetwork).filter_by(id=network_id).one()
    net.state = state
    net.updated_at = datetime.now(UTC)
    if error is not None:
        net.error_summary = error
    session.flush()


def claim_network_for_create(session: Session, network_id: str, *, expected_updated_at: datetime | None = None) -> bool:
    """Atomically transfer a retryable network row to create custody."""
    claimed = (
        session.query(JobNetwork)
        .filter(JobNetwork.id == network_id)
        .filter(JobNetwork.state.in_(["pending", "failed"]))
        .filter(true() if expected_updated_at is None else JobNetwork.state == "pending")
        .filter(true() if expected_updated_at is None else JobNetwork.updated_at == expected_updated_at)
        .filter(
            JobNetwork.session_id.in_(
                select(PlaygroundSession.id).where(PlaygroundSession.status.in_(["queued", "running"]))
            )
        )
        .update(
            {
                JobNetwork.state: "creating",
                JobNetwork.error_summary: None,
                JobNetwork.updated_at: datetime.now(UTC),
            },
            synchronize_session=False,
        )
    )
    session.flush()
    return claimed == 1


def lock_resource_transitions(session: Session) -> None:
    """Serialize read-only idempotency decisions with concurrent CAS writes."""
    _begin_immediate_for_sqlite(session)


def _require_resource_registration_session(session: Session, job_id: str) -> None:
    """Serialize registration with terminalization and accept only mutable sessions."""
    _begin_immediate_for_sqlite(session)
    parent = (
        session.query(PlaygroundSession.id)
        .filter(
            PlaygroundSession.id == job_id,
            PlaygroundSession.status.in_(["queued", "running"]),
        )
        .with_for_update()
        .one_or_none()
    )
    if parent is None:
        raise RuntimeError(f"playground session {job_id} is not accepting new resources")


def finish_network_create(
    session: Session,
    network_id: str,
    state: str,
    *,
    expected_updated_at: datetime,
    error: str | None = None,
) -> bool:
    values: dict[Any, Any] = {JobNetwork.state: state, JobNetwork.updated_at: datetime.now(UTC)}
    if error is not None:
        values[JobNetwork.error_summary] = error
    finished = (
        session.query(JobNetwork)
        .filter(
            JobNetwork.id == network_id,
            JobNetwork.state == "creating",
            JobNetwork.updated_at == expected_updated_at,
        )
        .update(values, synchronize_session=False)
    )
    session.flush()
    return finished == 1


def claim_legacy_network_for_teardown(
    session: Session,
    network_id: str,
    expected_state: str,
    expected_updated_at: datetime,
) -> bool:
    """Claim a terminal row with leaked host state without racing create retry."""
    if expected_state not in {"failed", "destroyed", "teardown_failed"}:
        raise ValueError(f"unsupported legacy network teardown state: {expected_state}")
    claimed = (
        session.query(JobNetwork)
        .filter(
            JobNetwork.id == network_id,
            JobNetwork.state == expected_state,
            JobNetwork.updated_at == expected_updated_at,
        )
        .update(
            {JobNetwork.state: "destroying", JobNetwork.updated_at: datetime.now(UTC)},
            synchronize_session=False,
        )
    )
    session.flush()
    return claimed == 1


def claim_network_for_destroy(session: Session, network_id: str) -> bool:
    claimed = (
        session.query(JobNetwork)
        .filter(JobNetwork.id == network_id)
        .filter(JobNetwork.state.in_(["active", "failed", "teardown_failed", "destroy_queued"]))
        .update(
            {JobNetwork.state: "destroying", JobNetwork.updated_at: datetime.now(UTC)},
            synchronize_session=False,
        )
    )
    session.flush()
    return claimed == 1


def queue_network_for_destroy(session: Session, network_id: str) -> bool:
    queued = (
        session.query(JobNetwork)
        .filter(JobNetwork.id == network_id)
        .filter(JobNetwork.state.in_(["active", "failed"]))
        .update(
            {JobNetwork.state: "destroy_queued", JobNetwork.updated_at: datetime.now(UTC)},
            synchronize_session=False,
        )
    )
    session.flush()
    return queued == 1


def cancel_queued_network_destroy(session: Session, network_id: str, prior_state: str, *, error: str) -> bool:
    cancelled = (
        session.query(JobNetwork)
        .filter(JobNetwork.id == network_id, JobNetwork.state == "destroy_queued")
        .update(
            {
                JobNetwork.state: prior_state,
                JobNetwork.error_summary: error,
                JobNetwork.updated_at: datetime.now(UTC),
            },
            synchronize_session=False,
        )
    )
    session.flush()
    return cancelled == 1


def finish_network_destroy(
    session: Session,
    network_id: str,
    state: str,
    *,
    expected_updated_at: datetime,
    error: str | None = None,
) -> bool:
    finished = (
        session.query(JobNetwork)
        .filter(
            JobNetwork.id == network_id,
            JobNetwork.state == "destroying",
            JobNetwork.updated_at == expected_updated_at,
        )
        .update(
            {
                JobNetwork.state: state,
                JobNetwork.error_summary: error,
                JobNetwork.updated_at: datetime.now(UTC),
            },
            synchronize_session=False,
        )
    )
    session.flush()
    return finished == 1


def list_session_networks(session: Session, job_id: str) -> list[JobNetwork]:
    return list(session.query(JobNetwork).filter_by(session_id=job_id).order_by(JobNetwork.created_at).all())


def list_live_session_networks(session: Session, job_id: str) -> list[JobNetwork]:
    """Networks the session still holds.

    A destroyed network keeps its row for the audit trail but releases its name
    and its subnet, so everything that asks "is this identity taken" has to read
    the live set. Spelling that filter out at each call site is what let the
    question be answered inconsistently across the API, the orchestrator and the
    unique index at once.
    """
    return [network for network in list_session_networks(session, job_id) if network.state != "destroyed"]


def list_topology_layout(session: Session, job_id: str) -> list[PlaygroundTopologyLayout]:
    return list(
        session.query(PlaygroundTopologyLayout)
        .filter_by(session_id=job_id)
        .order_by(PlaygroundTopologyLayout.node_id)
        .all()
    )


def save_topology_node(session: Session, job_id: str, node_id: str, node: dict) -> PlaygroundTopologyLayout:
    row = session.query(PlaygroundTopologyLayout).filter_by(session_id=job_id, node_id=node_id).one_or_none()
    if row is None:
        row = PlaygroundTopologyLayout(session_id=job_id, node_id=node_id)
        session.add(row)
    row.node_type = node["node_type"]
    row.x = node["x"]
    row.y = node["y"]

    session.flush()
    return row


def network_has_attached_vms(session: Session, network_id: str) -> bool:
    return (
        session.query(JobVMNic)
        .join(JobVM, JobVM.id == JobVMNic.job_vm_id)
        .filter(JobVMNic.job_network_id == network_id)
        .filter(JobVM.state != "destroyed")
        .count()
        > 0
    )


def vm_has_attaching_nics(session: Session, vm_id: str) -> bool:
    """Return whether a host NIC attach owns an in-flight claim for the VM."""
    return (
        session.query(JobVMNic.id).filter(JobVMNic.job_vm_id == vm_id, JobVMNic.state == "attaching").first()
        is not None
    )


def attach_nic(
    session: Session,
    *,
    nic_id: str,
    vm_id: str,
    network_id: str,
    mac: str,
    ordinal: int | None = None,
    state: str = "active",
) -> JobVMNic:
    if ordinal is None:
        latest_ordinal = session.scalar(select(func.max(JobVMNic.ordinal)).where(JobVMNic.job_vm_id == vm_id))
        ordinal = 0 if latest_ordinal is None else latest_ordinal + 1
    if ordinal < 0:
        raise ValueError("NIC ordinal must be non-negative")
    nic = JobVMNic(
        id=nic_id,
        job_vm_id=vm_id,
        job_network_id=network_id,
        mac=mac,
        ordinal=ordinal,
        state=state,
    )
    session.add(nic)
    session.flush()
    return nic


def mark_nic_active(
    session: Session,
    nic_id: str,
    *,
    expected_vm_id: str,
    expected_network_id: str,
    expected_mac: str,
    expected_created_at: datetime,
) -> bool:
    """Publish only the exact attach claim observed by the caller."""
    promoted = (
        session.query(JobVMNic)
        .filter(
            JobVMNic.id == nic_id,
            JobVMNic.state == "attaching",
            JobVMNic.job_vm_id == expected_vm_id,
            JobVMNic.job_network_id == expected_network_id,
            JobVMNic.mac == expected_mac,
            JobVMNic.created_at == expected_created_at,
        )
        .update({JobVMNic.state: "active"}, synchronize_session=False)
    )
    session.flush()
    return promoted == 1


def claim_nic_for_attach(
    session: Session,
    nic_id: str,
    *,
    expected_vm_id: str,
    expected_network_id: str,
    expected_mac: str,
    expected_created_at: datetime,
) -> bool:
    claimed = (
        session.query(JobVMNic)
        .filter(
            JobVMNic.id == nic_id,
            JobVMNic.state == "pending",
            JobVMNic.job_vm_id == expected_vm_id,
            JobVMNic.job_network_id == expected_network_id,
            JobVMNic.mac == expected_mac,
            JobVMNic.created_at == expected_created_at,
        )
        .update({JobVMNic.state: "attaching"}, synchronize_session=False)
    )
    session.flush()
    return claimed == 1


def detach_nic(session: Session, nic_id: str) -> None:
    nic = session.query(JobVMNic).filter_by(id=nic_id).one()
    session.delete(nic)
    session.flush()


def claim_nic_for_detach(session: Session, nic_id: str, *, expected_vm_id: str) -> bool:
    """Atomically move an active NIC into the detach-in-flight state."""

    claimed = (
        session.query(JobVMNic)
        .filter(
            JobVMNic.id == nic_id,
            JobVMNic.job_vm_id == expected_vm_id,
            JobVMNic.state == "active",
        )
        .update({JobVMNic.state: "detaching"}, synchronize_session=False)
    )
    session.flush()
    return claimed == 1


def reopen_nic_detach_claim(
    session: Session,
    nic_id: str,
    *,
    vm_id: str,
    detach_token: str | None,
    claimed_at: datetime | None,
    new_token: str,
) -> bool:
    """Requeue only the exact expired claim observed under the transition lock.

    The caller checks the lease age and commits before enqueueing. Keep the
    original timestamp for this CAS, including NULL and naive SQLite values.
    """
    reopened = (
        session.query(JobVMNic)
        .filter(
            JobVMNic.id == nic_id,
            JobVMNic.job_vm_id == vm_id,
            JobVMNic.state == "detach_claimed",
            JobVMNic.detach_token == detach_token,
            JobVMNic.detach_claimed_at == claimed_at,
        )
        .update(
            {
                JobVMNic.state: "detaching",
                JobVMNic.detach_token: new_token,
                JobVMNic.detach_claimed_at: None,
            },
            synchronize_session=False,
        )
    )
    session.flush()
    return reopened == 1


def compensate_nic_detach_enqueue_failure(
    session: Session,
    nic_id: str,
    *,
    vm_id: str,
    detach_token: str,
    force: bool = False,
) -> bool:
    """Restore or force-delete only this failed request's still-queued token."""
    claim = session.query(JobVMNic).filter(
        JobVMNic.id == nic_id,
        JobVMNic.job_vm_id == vm_id,
        JobVMNic.state == "detaching",
        JobVMNic.detach_token == detach_token,
    )
    # Fence the actual mutation: SQLite ignores SELECT ... FOR UPDATE, and
    # custody may have moved to a worker or a retry while enqueue was failing.
    if force:
        changed = claim.delete(synchronize_session=False)
    else:
        changed = claim.update(
            {
                JobVMNic.state: "active",
                JobVMNic.detach_token: None,
                JobVMNic.detach_claimed_at: None,
            },
            synchronize_session=False,
        )
    session.flush()
    return changed == 1


def delete_nic_if_exists(session: Session, nic_id: str) -> bool:
    """Delete the JobVMNic identified by ``nic_id`` if present.
    Returns True when a row was removed, False when already gone.
    Idempotent partner to ``detach_nic`` for recovery paths.
    """
    nic = session.query(JobVMNic).filter_by(id=nic_id).one_or_none()
    if nic is None:
        return False
    session.delete(nic)
    session.flush()
    return True


def delete_nic_if_detach_claim(
    session: Session,
    nic_id: str,
    *,
    vm_id: str,
    detach_token: str,
) -> bool:
    """Delete only the NIC claim that completed the host detach."""

    # ``detach_token`` rotates whenever a stale claim is recovered, so this
    # single conditional DELETE is the ownership CAS.  SQLite does not honor
    # SELECT ... FOR UPDATE; a read-then-delete would let an expired worker
    # remove a successor's claim.
    deleted = (
        session.query(JobVMNic)
        .filter(
            JobVMNic.id == nic_id,
            JobVMNic.job_vm_id == vm_id,
            JobVMNic.state == "detach_claimed",
            JobVMNic.detach_token == detach_token,
        )
        .delete(synchronize_session=False)
    )
    session.flush()
    return deleted == 1


def restore_nic_after_detach_failure(session: Session, nic_id: str, *, vm_id: str, detach_token: str) -> bool:
    """Return a failed detach to active only for its exact worker token."""

    restored = (
        session.query(JobVMNic)
        .filter(
            JobVMNic.id == nic_id,
            JobVMNic.job_vm_id == vm_id,
            JobVMNic.state == "detach_claimed",
            JobVMNic.detach_token == detach_token,
        )
        .update(
            {
                JobVMNic.state: "active",
                JobVMNic.detach_token: None,
                JobVMNic.detach_claimed_at: None,
            },
            synchronize_session=False,
        )
    )
    session.flush()
    return restored == 1


def delete_nics_for_vm(session: Session, vm_id: str) -> int:
    """Delete every JobVMNic row owned by ``vm_id``. Returns the count removed.

    Called by destroy_vm and spawn_vm failure cleanup so NIC rows do not
    outlive their VM. Idempotent — repeated calls return 0 once cleared.
    """
    deleted = session.query(JobVMNic).filter(JobVMNic.job_vm_id == vm_id).delete(synchronize_session=False)
    return deleted


def update_nic_ip(session: Session, nic_id: str, ip: str) -> None:
    nic = session.query(JobVMNic).filter_by(id=nic_id).one()
    nic.ip = ip
    session.flush()
