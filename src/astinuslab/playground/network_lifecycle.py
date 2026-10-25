"""Playground network lifecycle: defining and tearing down a lab network.

Split out of `orchestrator.py` so that module stays readable. The two Celery
tasks stay there -- `playground.create_network` and `playground.destroy_network`
-- and delegate straight here, because a task has to be defined where it is
imported for registration to happen, and this module is imported from inside
those task bodies.

`_wait_for_active_networks` deliberately stayed behind. It reads network rows,
but its only caller is `spawn_vm`, and a helper whose consumers are all outside
this domain is shared infrastructure -- the same reason `libvirt_net_name` and
`_pending_recipe_reservation` stayed.

`settings`, `session_scope`, `libvirt` and the two shared primitives this needs
(`libvirt_net_name`, which `attach_nic` also calls, and
`_pending_recipe_reservation`, which the spawn path also calls) are reached
through the orchestrator module object (`po.<name>`) rather than imported by
name. That is not style. `orchestrator` is what the tests monkeypatch, and a
`from .orchestrator import settings` here would bind the original at import
time, so a patched value would silently not apply to this module. Attribute
access resolves at call time and keeps that working.

Names this module owns are called bare, because a bare global resolves through
this module's own namespace at call time; patching
`network_lifecycle._networks_helper` reaches every caller here. Tests that used
to patch `orchestrator._networks_helper` must patch this module instead --
`monkeypatch.setattr` raises on an absent attribute, so that mistake fails
loudly rather than silently reaching real libvirt.

What every custody check here guards against is one failure: a libvirt network
left defined on the host with no database row claiming it. That is a leaked
subnet, and it blocks the next create with "Network is already in use". Each
check has a case in tests/python/playground/test_orchestrator.py.
"""

from __future__ import annotations

import ipaddress

from . import orchestrator as po
from . import repositories as pr
from .networks import PlaygroundNetworks
from .resource_custody import LostResourceCustody, ResourceClaimLease


def _networks_helper() -> PlaygroundNetworks:
    return PlaygroundNetworks(libvirt_module=po.libvirt, libvirt_uri=po.settings.libvirt_uri)


def _cidrs_overlap(a: str, b: str) -> bool:
    return ipaddress.ip_network(a, strict=False).overlaps(ipaddress.ip_network(b, strict=False))


def create_network(
    *, job_id: str, network_id: str, name: str, subnet_cidr: str, isolated: bool, reservation_token: str | None = None
) -> str:
    libvirt_name = po.libvirt_net_name(job_id, name)
    with po.session_scope() as s:
        from ..db.models import JobNetwork
        from ..db.repositories import has_active_non_playground_job

        pr.lock_resource_transitions(s)
        pr.require_session_open_for_authoring(s, job_id)
        if has_active_non_playground_job(s):
            raise RuntimeError("a forensic job is currently active")
        existing = pr.list_session_networks(s, job_id)
        current = next((n for n in existing if n.id == network_id), None)
        expected_reservation = (
            po._pending_recipe_reservation(current, job_id, reservation_token)
            if reservation_token is not None
            else None
        )
        others = [n for n in existing if n.id != network_id and n.state != "destroyed"]
        if any(n.name == name for n in others):
            raise RuntimeError(f"network name '{name}' already exists")
        if any(_cidrs_overlap(n.subnet_cidr, subnet_cidr) for n in others):
            raise RuntimeError(f"CIDR {subnet_cidr} overlaps existing network")
        if current is None:
            pr.register_network(
                s,
                network_id=network_id,
                job_id=job_id,
                name=name,
                subnet_cidr=subnet_cidr,
                isolated=isolated,
                libvirt_net_name=libvirt_name,
            )
        else:
            expected = {
                "session_id": job_id,
                "name": name,
                "subnet_cidr": subnet_cidr,
                "isolated": isolated,
                "libvirt_net_name": libvirt_name,
            }
            mismatched = [field for field, value in expected.items() if getattr(current, field) != value]
            if mismatched:
                raise RuntimeError(f"network retry identity mismatch for {network_id}: {', '.join(mismatched)}")
            libvirt_name = current.libvirt_net_name
            if current.state == "active":
                return network_id
        claimed = (
            pr.claim_network_for_create(s, network_id, expected_updated_at=expected_reservation)
            if expected_reservation is not None
            else pr.claim_network_for_create(s, network_id)
        )
        if not claimed:
            raise RuntimeError(f"network {network_id} is not available for create retry")
        claimed_network = s.get(JobNetwork, network_id)
        assert claimed_network is not None  # noqa: S101 - successful CAS guarantees the row
        s.refresh(claimed_network)
        lease_token = claimed_network.updated_at
    claim_lease = ResourceClaimLease(
        kind="network",
        resource_id=network_id,
        state="creating",
        token=lease_token,
        session_scope_factory=po.session_scope,
        grace_seconds=getattr(po.settings, "celery_reconciliation_grace_seconds", 120),
    )
    try:
        claim_lease.start()
        helper = _networks_helper()
        # Reap networks left active on the host by dead sessions before defining
        # this one; otherwise a leaked subnet blocks creation with "Network is
        # already in use". Helper construction and reaping are part of the same
        # claimed transition: either may fail before libvirt create is reached.
        helper.reap_orphans(session_scope=po.session_scope)
        claim_lease.assert_owned()
        with po.session_scope() as s:
            pr.require_session_open_for_authoring(s, job_id)
        helper.create_network(
            libvirt_net_name=libvirt_name,
            network_id=network_id,
            cidr=subnet_cidr,
            isolated=isolated,
        )
        claim_lease.assert_owned()
    except (LostResourceCustody, pr.AuthoringClosed):
        raise
    except Exception as exc:
        try:
            expected_updated_at = claim_lease.publication_token()
        except LostResourceCustody as custody_error:
            raise custody_error from exc
        with po.session_scope() as s:
            if not pr.finish_network_create(
                s,
                network_id,
                "failed",
                expected_updated_at=expected_updated_at,
                error=str(exc),
            ):
                raise LostResourceCustody(f"network {network_id} lost create custody on failure") from exc
        raise
    finally:
        claim_lease.stop()
    expected_updated_at = claim_lease.publication_token()
    with po.session_scope() as s:
        if not pr.finish_network_create(
            s,
            network_id,
            "active",
            expected_updated_at=expected_updated_at,
        ):
            raise RuntimeError(f"network {network_id} lost create custody before activation")
    return network_id


def destroy_network(*, network_id: str) -> str:
    attached = False
    with po.session_scope() as s:
        from ..db.models import JobNetwork

        pr.lock_resource_transitions(s)
        net = s.query(JobNetwork).filter_by(id=network_id).with_for_update().one()
        cleanup_requested = net.state == "teardown_failed"
        if pr.network_has_attached_vms(s, network_id):
            pr.cancel_queued_network_destroy(
                s,
                network_id,
                "active",
                error="network destroy cancelled because a VM attached before worker claim",
            )
            attached = True
            libvirt_name = ""
        else:
            if net.state == "destroyed":
                return network_id
            libvirt_name = net.libvirt_net_name
            if not pr.claim_network_for_destroy(s, network_id):
                raise RuntimeError(f"network {network_id} is busy in state {net.state}")
            s.refresh(net)
            lease_token = net.updated_at
    if attached:
        raise RuntimeError(f"network {network_id} has attached VMs; destroy them first")
    claim_lease = ResourceClaimLease(
        kind="network",
        resource_id=network_id,
        state="destroying",
        token=lease_token,
        session_scope_factory=po.session_scope,
        grace_seconds=getattr(po.settings, "celery_reconciliation_grace_seconds", 120),
    )
    try:
        claim_lease.start()
        claim_lease.assert_owned()
        _networks_helper().destroy_network(libvirt_name)
        claim_lease.assert_owned()
    except LostResourceCustody:
        raise
    except Exception as exc:
        try:
            expected_updated_at = claim_lease.publication_token()
        except LostResourceCustody as custody_error:
            raise custody_error from exc
        with po.session_scope() as s:
            if not pr.finish_network_destroy(
                s,
                network_id,
                "teardown_failed" if cleanup_requested else "failed",
                expected_updated_at=expected_updated_at,
                error=str(exc),
            ):
                raise LostResourceCustody(f"network {network_id} lost destroy custody on failure") from exc
        raise
    finally:
        claim_lease.stop()
    expected_updated_at = claim_lease.publication_token()
    with po.session_scope() as s:
        if not pr.finish_network_destroy(
            s,
            network_id,
            "destroyed",
            expected_updated_at=expected_updated_at,
        ):
            raise RuntimeError(f"network {network_id} lost destroy custody before completion")
    return network_id
