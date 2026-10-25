"""Product-level VM admission control for orchestration.max_concurrent_vms."""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..api.errors import ApiError
from ..core.config import settings
from ..db.models import JobVM
from .repositories import _begin_immediate_for_sqlite

CAPACITY_EXEMPT_VM_STATES = ("destroyed", "failed", "stopped", "shutoff", "shutdown")


def count_capacity_vms(session: Session) -> int:
    return session.query(JobVM).filter(JobVM.state.not_in(CAPACITY_EXEMPT_VM_STATES)).count()


def ensure_vm_capacity(session: Session, *, requested: int = 1) -> None:
    """Reject admission when the configured VM cap would be exceeded.

    Call inside the same transaction that registers or re-starts the VM. For
    SQLite, BEGIN IMMEDIATE serializes count-then-insert against concurrent
    admitters using the same lock primitive as network CIDR registration.
    """
    limit = settings.max_concurrent_vms
    if limit is None:
        return

    _begin_immediate_for_sqlite(session)
    active = count_capacity_vms(session)
    if active + requested > limit:
        raise ApiError(
            409,
            f"VM capacity reached: {active} active VM(s), requested {requested}, "
            f"limit {limit} (orchestration.max_concurrent_vms)",
            "vm_capacity",
        )
