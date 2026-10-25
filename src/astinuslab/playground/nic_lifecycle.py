"""Playground NIC attach and detach: the two-place resource.

Split out of `orchestrator.py` so that module stays readable. The Celery tasks
stay there -- `playground.attach_nic` and `playground.detach_nic` -- as wrappers
that delegate straight here, because a task has to be defined where it is
imported for registration to happen, and this module is imported from inside
those wrappers.

Three NIC-named neighbours stayed behind, and none of them is NIC-domain code.
`_reserve_nic_rows` runs inside the spawn transaction under the spawn claim
token and raises spawn-custody errors; `_resolve_nic_addresses` lets lookup
failures travel into the spawn summary and logs them as `spawn_vm`; and
`generate_mac` is called from both. All three go with the VM lifecycle.

`session_scope`, `_vm_lifecycle`, `_libvirt_conn` and `generate_mac` are reached
through the orchestrator module object (`po.<name>`) rather than imported by
name. That is not style. `orchestrator` is what the tests monkeypatch --
`po._libvirt_conn` and `po._vm_lifecycle` among them -- and a
`from .orchestrator import _vm_lifecycle` here would bind the original at import
time, so a patched value would silently not apply to this module. Attribute
access resolves at call time and keeps that working.

A NIC is the one playground resource that exists in two places at once: a row in
the database and an interface on a running domain. Every guard here is a point
where those two can disagree -- a redelivery that would add a second interface,
a claim another worker holds, a publication that lost its race, a detach token
that cannot be proved. When a rollback cannot put the host back, that is
reported rather than folded into the custody error, because an interface no row
claims is the one outcome an operator cannot see from the database alone.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta

from . import orchestrator as po
from . import repositories as pr
from .resource_custody import LostResourceCustody

logger = logging.getLogger(__name__)


def attach_nic(*, job_id: str, vm_id: str, network_id: str, nic_id: str | None = None) -> dict[str, str]:
    mac = po.generate_mac()
    nic_id = nic_id or f"nic-{secrets.token_hex(4)}"
    with po.session_scope() as s:
        from ..db.models import JobNetwork, JobVM, JobVMNic, PlaygroundSession

        pr.lock_resource_transitions(s)
        parent = s.query(PlaygroundSession).filter_by(id=job_id).with_for_update().one()
        vm = s.query(JobVM).filter_by(id=vm_id, session_id=job_id).with_for_update().one()
        net = s.query(JobNetwork).filter_by(id=network_id, session_id=job_id).with_for_update().one()
        existing = s.query(JobVMNic).filter_by(id=nic_id).with_for_update().one_or_none()
        if parent.status not in {"queued", "running"} or vm.state != "running" or net.state != "active":
            raise LostResourceCustody(f"NIC attach lost eligible parent resource custody for VM {vm_id}")
        domain_name = vm.domain_name
        libvirt_net_name = net.libvirt_net_name
        if existing is None:
            claimed = pr.attach_nic(
                s,
                nic_id=nic_id,
                vm_id=vm_id,
                network_id=network_id,
                mac=mac,
                state="attaching",
            )
        else:
            if existing.job_vm_id != vm_id or existing.job_network_id != network_id:
                raise LostResourceCustody(f"NIC {nic_id} belongs to a different VM or network")
            mac = existing.mac
            if existing.state == "active":
                return {"nic_id": nic_id, "mac": mac}
            if existing.state != "pending" or not pr.claim_nic_for_attach(
                s,
                nic_id,
                expected_vm_id=vm_id,
                expected_network_id=network_id,
                expected_mac=existing.mac,
                expected_created_at=existing.created_at,
            ):
                raise LostResourceCustody(f"NIC {nic_id} lost pending custody before host mutation")
            claimed = existing
        expected_vm_id = claimed.job_vm_id
        expected_network_id = claimed.job_network_id
        expected_mac = claimed.mac
        expected_created_at = claimed.created_at

    lifecycle = po._vm_lifecycle()
    with po._libvirt_conn() as conn:
        if conn:
            lifecycle.attach_nic(conn, domain_name, libvirt_net_name, mac)

        with po.session_scope() as s:
            published = pr.mark_nic_active(
                s,
                nic_id,
                expected_vm_id=expected_vm_id,
                expected_network_id=expected_network_id,
                expected_mac=expected_mac,
                expected_created_at=expected_created_at,
            )
        if not published:
            with po.session_scope() as s:
                from ..db.models import JobVMNic

                current = s.query(JobVMNic).filter_by(id=nic_id).one_or_none()
                if (
                    current is not None
                    and current.job_vm_id == expected_vm_id
                    and current.job_network_id == expected_network_id
                    and current.mac == expected_mac
                    and current.created_at == expected_created_at
                    and current.state == "active"
                ):
                    return {"nic_id": nic_id, "mac": mac}
                old_mac_owned = (
                    s.query(JobVMNic.id)
                    .filter(
                        JobVMNic.job_vm_id == expected_vm_id,
                        JobVMNic.mac == expected_mac,
                        JobVMNic.state.in_(["pending", "attaching", "active"]),
                    )
                    .first()
                    is not None
                )
            if conn and not old_mac_owned:
                try:
                    lifecycle.detach_nic(conn, domain_name, expected_mac)
                except Exception as exc:
                    if not lifecycle.is_missing_interface_error(exc):
                        raise RuntimeError(f"NIC {nic_id} lost custody and strict host cleanup failed") from exc
            raise LostResourceCustody(f"NIC {nic_id} lost attaching custody")
    return {"nic_id": nic_id, "mac": mac}


def detach_nic(*, job_id: str, vm_id: str, nic_id: str, detach_token: str | None = None) -> str:
    with po.session_scope() as s:
        from ..db.models import JobVM, JobVMNic, PlaygroundSession

        pr.lock_resource_transitions(s)
        parent = s.query(PlaygroundSession).filter_by(id=job_id).with_for_update().one()
        vm = s.query(JobVM).filter_by(id=vm_id, session_id=job_id).with_for_update().one()
        nic = s.query(JobVMNic).filter_by(id=nic_id, job_vm_id=vm_id).with_for_update().one()
        if parent.status not in {"queued", "running"}:
            raise LostResourceCustody(f"NIC detach lost eligible parent resource custody for VM {vm_id}")
        nic_state = getattr(nic, "state", "active")
        stored_token = getattr(nic, "detach_token", None)
        if nic_state == "active":
            # The parent and NIC locks make this transition the freeze barrier:
            # closing authoring cannot pass while a host detach is in flight.
            detach_token = detach_token or secrets.token_hex(16)
            nic.state = "detaching"
            if hasattr(nic, "detach_token"):
                nic.detach_token = detach_token
                nic.detach_claimed_at = None
            nic_state = "detaching"
        elif nic_state == "detaching":
            if stored_token is not None and detach_token != stored_token:
                raise LostResourceCustody(f"NIC {nic_id} has a different detach request in flight")
            detach_token = stored_token or detach_token or secrets.token_hex(16)
            if hasattr(nic, "detach_token") and nic.detach_token is None:
                nic.detach_token = detach_token
        elif nic_state == "detach_claimed":
            if stored_token is None or detach_token != stored_token:
                raise LostResourceCustody(f"NIC {nic_id} is already claimed by another detach worker")
            claimed_at = getattr(nic, "detach_claimed_at", None)
            if claimed_at is not None and claimed_at.tzinfo is None:
                claimed_at = claimed_at.replace(tzinfo=UTC)
            if claimed_at is None or claimed_at > datetime.now(UTC) - timedelta(seconds=pr.NIC_DETACH_CLAIM_SECONDS):
                raise LostResourceCustody(f"NIC {nic_id} is already claimed by another detach worker")
            # A stale claim is explicitly recoverable after the bounded lease.
            detach_token = secrets.token_hex(16)
            nic.detach_token = detach_token
            nic_state = "detaching"
        else:
            raise LostResourceCustody(f"NIC {nic_id} is not eligible for detach")
        if nic_state == "detaching" and hasattr(nic, "detach_claimed_at"):
            nic.state = "detach_claimed"
            nic.detach_claimed_at = datetime.now(UTC)
        domain_name = vm.domain_name
        mac = nic.mac

    lifecycle = po._vm_lifecycle()
    with po._libvirt_conn() as conn:
        if conn:
            try:
                lifecycle.detach_nic(conn, domain_name, mac)
            except Exception as exc:
                if not lifecycle.is_missing_interface_error(exc):
                    # Return custody to the caller when the host operation
                    # failed.  A later retry must be able to enqueue again;
                    # leaving the row detaching would permanently block both
                    # retries and authored-session freezing.
                    with po.session_scope() as s:
                        from ..db.models import JobVMNic

                        if hasattr(JobVMNic, "detach_token"):
                            pr.restore_nic_after_detach_failure(
                                s,
                                nic_id,
                                vm_id=vm_id,
                                detach_token=detach_token or "",
                            )
                    raise
                logger.info(
                    "detach_nic: libvirt interface already absent for nic=%s mac=%s; clearing DB row",
                    nic_id,
                    mac,
                )

    with po.session_scope() as s:
        if not pr.delete_nic_if_detach_claim(
            s,
            nic_id,
            vm_id=vm_id,
            detach_token=detach_token or "",
        ):
            raise LostResourceCustody(f"NIC {nic_id} lost detach custody before DB cleanup")
    return nic_id
