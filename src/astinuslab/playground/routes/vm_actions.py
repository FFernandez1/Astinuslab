from __future__ import annotations

import subprocess

from fastapi import APIRouter, HTTPException, status

from ... import db as db_mod
from ...api.errors import ApiError
from ...api.schemas import PlaygroundVMActionRequest
from .. import admission
from .. import orchestrator as po
from .. import repositories as pr
from ..enqueue import ENQUEUE_FAILED_DETAIL as _ENQUEUE_FAILED_DETAIL
from ..enqueue import enqueue_or_raise
from ..serializers import NON_DESTROY_ACTIVE, PlaygroundAcceptedOut, vm_action_flags
from .sessions import _raise_if_authoring_closed

router = APIRouter()

# virsh start/shutdown/reboot return once libvirt accepts the request. A hung
# libvirtd must not hold the request thread and the power-action claim forever.
_VIRSH_POWER_ACTION_TIMEOUT_SECONDS = 60


@router.delete("/vms/{vm_id}", status_code=status.HTTP_202_ACCEPTED, response_model=PlaygroundAcceptedOut)
def destroy_vm_endpoint(vm_id: str) -> PlaygroundAcceptedOut:
    with db_mod.session_scope() as s:
        from ...db.models import JobVM

        pr.lock_resource_transitions(s)
        active = pr.active_playground_session(s)
        vm = s.query(JobVM).filter_by(id=vm_id).with_for_update().one_or_none()
        if vm is None or active is None or vm.session_id != active.id:
            raise HTTPException(status_code=404, detail="VM not found")
        _raise_if_authoring_closed(s, active.id)
        if pr.vm_has_attaching_nics(s, vm_id):
            raise ApiError(409, "VM destroy unavailable while NIC attachment is in progress", "conflict")
        job_id = active.id
        prior_state = vm.state
        if not pr.queue_vm_for_destroy(s, vm_id):
            raise ApiError(409, f"VM destroy unavailable in state {prior_state}", "conflict")

    def _on_destroy_enqueue_failure(_exc: Exception) -> None:
        with db_mod.session_scope() as s:
            pr.cancel_queued_vm_destroy(s, vm_id, prior_state, error=_ENQUEUE_FAILED_DETAIL)

    task_id = enqueue_or_raise(po.destroy_vm, on_failure=_on_destroy_enqueue_failure, vm_id=vm_id)
    return PlaygroundAcceptedOut(resource_id=vm_id, job_id=job_id, session_id=job_id, task_id=task_id)


@router.post("/vms/{vm_id}/actions", status_code=status.HTTP_202_ACCEPTED, response_model=PlaygroundAcceptedOut)
def vm_action_endpoint(vm_id: str, payload: PlaygroundVMActionRequest) -> PlaygroundAcceptedOut:
    with db_mod.session_scope() as s:
        from ...db.models import JobVM

        pr.lock_resource_transitions(s)
        active = pr.active_playground_session(s)
        vm = s.query(JobVM).filter_by(id=vm_id).with_for_update().one_or_none()
        if vm is None or active is None or vm.session_id != active.id:
            raise HTTPException(status_code=404, detail="VM not found")
        _raise_if_authoring_closed(s, active.id)
        job_id = active.id
        _can_mem, _can_disk, _can_destroy, can_start, can_stop, can_reboot, reason = vm_action_flags(
            vm, session_active=active.status in NON_DESTROY_ACTIVE
        )
        allowed = {"start": can_start, "stop": can_stop, "reboot": can_reboot}[payload.action]
        if not allowed:
            raise ApiError(409, reason or f"VM action unavailable: {payload.action}", "conflict")
        if payload.action == "start":
            admission.ensure_vm_capacity(s, requested=1)
        domain_name = vm.domain_name
        prior_state = vm.state
        claim_state = {
            "start": "powering_on",
            "stop": "powering_off",
            "reboot": "rebooting",
        }[payload.action]
        claim_token = pr.claim_vm_for_power_action(
            s,
            vm_id,
            expected_state=prior_state,
            claim_state=claim_state,
        )
        if claim_token is None:
            raise ApiError(409, f"VM action lost resource custody: {payload.action}", "conflict")

    virsh_action = "shutdown" if payload.action == "stop" else payload.action
    try:
        subprocess.run(
            ["virsh", "-c", po.settings.libvirt_uri, virsh_action, domain_name],
            check=True,
            capture_output=True,
            text=True,
            timeout=_VIRSH_POWER_ACTION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        if isinstance(exc, subprocess.CalledProcessError):
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
        elif isinstance(exc, subprocess.TimeoutExpired):
            detail = f"virsh {virsh_action} timed out after {_VIRSH_POWER_ACTION_TIMEOUT_SECONDS}s"
        else:
            detail = str(exc)
        with db_mod.session_scope() as s:
            finished = pr.finish_vm_power_action(
                s,
                vm_id,
                claim_state=claim_state,
                expected_updated_at=claim_token,
                state=prior_state,
                error=f"VM action failed: {detail}",
            )
        if not finished:
            raise ApiError(
                409, f"VM action lost resource custody after virsh failure: {payload.action}", "conflict"
            ) from exc
        raise HTTPException(status_code=500, detail=f"VM action failed: {detail}") from exc

    # `starting` is the renewable spawn-worker claim. Public power-on uses the
    # probe-synchronized `booting` state so stale-claim recovery never mistakes
    # a successful virsh start for an abandoned spawn and tears it down.
    next_state = {"start": "booting", "stop": "stopping", "reboot": "booting"}[payload.action]
    with db_mod.session_scope() as s:
        if not pr.finish_vm_power_action(
            s,
            vm_id,
            claim_state=claim_state,
            expected_updated_at=claim_token,
            state=next_state,
        ):
            raise ApiError(409, f"VM action lost resource custody after virsh: {payload.action}", "conflict")
    return PlaygroundAcceptedOut(resource_id=vm_id, job_id=job_id, session_id=job_id)
