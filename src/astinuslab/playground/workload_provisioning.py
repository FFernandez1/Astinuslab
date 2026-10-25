"""Playground workload provisioning: install a workload into a booted VM.

Split out of `orchestrator.py` so that module stays readable. The Celery task
stays there -- `playground.provision_vm_workload` -- as a wrapper that delegates
straight here, because a task has to be defined where it is imported for
registration to happen, and this module is imported from inside that wrapper.

Two neighbours deliberately stayed behind. `_dispatch_workload_provisioning` is
named for this domain but is spawn-path cleanup: it publishes one message and
then tears the VM down with `_cleanup_spawn_artifacts` and `finish_vm_destroy`,
so it belongs with the VM lifecycle. The workload *cache* group serves
`_resolve_base_image`, which is image resolution, not provisioning.

`settings`, `session_scope` and the shared primitives this needs are reached
through the orchestrator module object (`po.<name>`) rather than imported by
name. That is not style. `orchestrator` is what the tests monkeypatch, and a
`from .orchestrator import settings` here would bind the original at import
time, so a patched value would silently not apply to this module. Attribute
access resolves at call time and keeps that working.

Names this module owns are called bare, because a bare global resolves through
this module's own namespace at call time; patching
`workload_provisioning._provision_playground_workload` reaches every caller
here. Tests that used to patch it on `orchestrator` must patch this module
instead -- `monkeypatch.setattr` raises on an absent attribute, so that mistake
fails loudly rather than silently running a real install against a guest.

What every custody check here guards against is a VM that has been booted and
written to, whose row then moves before the outcome can be recorded. That leaves
a VM no operator can account for without a manual libvirt sweep, which is why
both halves of each pair have a case in
tests/python/playground/test_orchestrator.py.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .. import catalog
from ..virtualization import vm_lifecycle as vmlc
from . import orchestrator as po
from . import repositories as pr
from . import workloads as pw
from .resource_custody import LostResourceCustody, ResourceClaimLease

logger = logging.getLogger(__name__)


def _cleanup_failed_provision(*, domain_name: str, disk_path: Path) -> str | None:
    lifecycle: vmlc.VMLifecycle | None = None
    setup_error: str | None = None
    try:
        lifecycle = po._vm_lifecycle()
    except Exception as cleanup_exc:
        logger.warning("provision_vm_workload: lifecycle setup failed for %s: %s", domain_name, cleanup_exc)
        setup_error = f"libvirt cleanup failed: {cleanup_exc}"
    if setup_error is not None:
        return setup_error
    cleanup_error = po._cleanup_spawn_artifacts(
        lifecycle=lifecycle,
        domain_name=domain_name,
        disk_path=disk_path,
    )
    return "; ".join(part for part in (setup_error, cleanup_error) if part) or None


def _select_workload_guest_ip(vm) -> tuple[str | None, str | None]:
    ip_nics = [nic for nic in vm.nics if nic.ip]
    if not ip_nics:
        return None, "workload provisioning requires a guest IP"

    routed = [nic for nic in ip_nics if getattr(getattr(nic, "network", None), "isolated", True) is False]
    if routed:
        return routed[0].ip, None

    chosen = min(ip_nics, key=lambda nic: (nic.created_at, nic.id))
    segment = getattr(getattr(chosen, "network", None), "name", chosen.job_network_id)
    logger.info("workload provisioning: using isolated segment %s (%s)", segment, chosen.ip)
    return chosen.ip, None


def _provision_playground_workload(*, workload: dict, guest_ip: str, image: dict, guest_user: str) -> None:
    pw.install_playground_workload(
        workload=workload,
        guest_ip=guest_ip,
        guest_user=guest_user,
        target_context=pw.target_context_from_playground_image(image),
    )


def provision_vm_workload(*, vm_id: str) -> str:
    cleanup_queued = False
    with po.session_scope() as s:
        from ..db.models import JobVM

        vm = s.query(JobVM).filter_by(id=vm_id).one()
        if not vm.workload_id:
            return vm_id
        claimed = pr.claim_vm_for_workload(s, vm_id)
        if not claimed:
            cleanup_queued = pr.queue_rejected_workload_for_destroy(s, vm_id)
        if not claimed and not cleanup_queued:
            return vm_id
        if claimed:
            s.refresh(vm)
            lease_token = vm.updated_at
        else:
            lease_token = None
        domain_name = vm.domain_name
        disk_path = Path(vm.disk_path)
        prebaked = vm.workload_state == "prebaked"
        workload = catalog.workload_by_id(vm.workload_id)
        guest_ip, ip_error = _select_workload_guest_ip(vm)
        if workload is None:
            error = f"Workload not found: {vm.workload_id}"
        elif ip_error is not None and not prebaked:
            error = ip_error
        else:
            error = None
        image = po.lookup_playground_image(vm.image_id, role=vm.role) or {"id": vm.image_id}

    if cleanup_queued:
        po.destroy_vm.apply(kwargs={"vm_id": vm_id}).get(disable_sync_subtasks=False)
        return vm_id

    assert lease_token is not None  # noqa: S101 - successful claim guarantees a timestamp
    claim_lease = ResourceClaimLease(
        kind="vm",
        resource_id=vm_id,
        state="provisioning_workload_active",
        token=lease_token,
        session_scope_factory=po.session_scope,
        grace_seconds=getattr(po.settings, "celery_reconciliation_grace_seconds", 120),
    )
    claim_lease.start()
    try:
        if error is not None:
            claim_lease.assert_owned()
            cleanup_error = _cleanup_failed_provision(domain_name=domain_name, disk_path=disk_path)
            claim_lease.assert_owned()
            error_summary = error
            if cleanup_error is not None:
                error_summary = f"{error}; cleanup retry required: {cleanup_error}"
            expected_updated_at = claim_lease.publication_token()
            with po.session_scope() as s:
                finished = pr.finish_vm_workload(
                    s,
                    vm_id,
                    "teardown_failed" if cleanup_error is not None else "failed",
                    "failed",
                    expected_updated_at=expected_updated_at,
                    error=error_summary,
                    workload_error=error,
                )
            if not finished:
                raise LostResourceCustody(f"VM {vm_id} lost workload custody during cleanup")
            raise RuntimeError(error)

        assert workload is not None  # noqa: S101 - narrowed above
        if prebaked:
            if guest_ip is not None:
                pw.best_effort_health_check(workload, guest_ip)
            claim_lease.assert_owned()
        else:
            assert guest_ip is not None  # noqa: S101 - narrowed above
            try:
                claim_lease.assert_owned()
                _provision_playground_workload(
                    workload=workload,
                    guest_ip=guest_ip,
                    image=image,
                    guest_user=po._playground_guest_user(image),
                )
                claim_lease.assert_owned()
            except LostResourceCustody:
                raise
            except Exception as exc:
                claim_lease.assert_owned()
                cleanup_error = _cleanup_failed_provision(domain_name=domain_name, disk_path=disk_path)
                claim_lease.assert_owned()
                error_summary = str(exc)
                if cleanup_error is not None:
                    error_summary = f"{error_summary}; cleanup retry required: {cleanup_error}"
                expected_updated_at = claim_lease.publication_token()
                with po.session_scope() as s:
                    finished = pr.finish_vm_workload(
                        s,
                        vm_id,
                        "teardown_failed" if cleanup_error is not None else "failed",
                        "failed",
                        expected_updated_at=expected_updated_at,
                        error=error_summary,
                        workload_error=str(exc),
                    )
                if not finished:
                    raise LostResourceCustody(f"VM {vm_id} lost workload custody during cleanup") from exc
                raise

        expected_updated_at = claim_lease.publication_token()
        with po.session_scope() as s:
            finished = pr.finish_vm_workload(
                s,
                vm_id,
                "running",
                "ready",
                expected_updated_at=expected_updated_at,
            )
        if not finished:
            raise LostResourceCustody(f"VM {vm_id} lost workload custody before completion")
        return vm_id
    finally:
        claim_lease.stop()
