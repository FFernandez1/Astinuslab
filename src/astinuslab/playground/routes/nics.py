from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Response, status

from ... import db as db_mod
from ...api.schemas import PlaygroundAttachNicRequest
from .. import orchestrator as po
from .. import repositories as pr
from ..enqueue import enqueue_or_raise
from ..serializers import NON_DESTROY_ACTIVE, PlaygroundAcceptedOut
from .sessions import _raise_if_authoring_closed

router = APIRouter()


@router.post(
    "/sessions/current/vms/{vm_id}/nics",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PlaygroundAcceptedOut,
)
def attach_nic_endpoint(vm_id: str, payload: PlaygroundAttachNicRequest) -> PlaygroundAcceptedOut:
    with db_mod.session_scope() as s:
        from ...db.models import JobNetwork, JobVM, JobVMNic

        job = pr.active_playground_session(s)
        if job is None:
            raise HTTPException(status_code=404, detail="no active playground session")
        _raise_if_authoring_closed(s, job.id)
        if job.status not in NON_DESTROY_ACTIVE:
            raise HTTPException(status_code=404, detail="no active playground session")
        job_id = job.id
        vm = s.query(JobVM).filter_by(id=vm_id, session_id=job_id).one_or_none()
        if vm is None:
            raise HTTPException(status_code=404, detail="VM not found")
        net = s.query(JobNetwork).filter_by(id=payload.network_id, session_id=job_id).one_or_none()
        if net is None:
            raise HTTPException(status_code=404, detail="network not found")
        if vm.state != "running" or net.state != "active":
            raise HTTPException(status_code=409, detail="VM or network is not eligible for NIC attachment")
        if payload.nic_id is not None:
            existing = s.query(JobVMNic).filter_by(id=payload.nic_id).one_or_none()
            if existing is not None:
                if (
                    existing.job_vm_id == vm_id
                    and existing.job_network_id == payload.network_id
                    and existing.state in {"pending", "attaching", "active"}
                ):
                    return PlaygroundAcceptedOut(resource_id=vm_id, job_id=job_id, session_id=job_id, task_id=None)
                raise HTTPException(status_code=409, detail="NIC id is already bound to another resource attempt")
    task_id = enqueue_or_raise(
        po.attach_nic,
        job_id=job_id,
        vm_id=vm_id,
        network_id=payload.network_id,
        nic_id=payload.nic_id,
    )
    return PlaygroundAcceptedOut(resource_id=vm_id, job_id=job_id, session_id=job_id, task_id=task_id)


@router.delete(
    "/sessions/current/vms/{vm_id}/nics/{nic_id}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PlaygroundAcceptedOut,
)
def detach_nic_endpoint(vm_id: str, nic_id: str, force: bool = False):
    with db_mod.session_scope() as s:
        from ...db.models import JobVM, JobVMNic

        pr.lock_resource_transitions(s)
        job = pr.active_playground_session(s)
        if job is None:
            raise HTTPException(status_code=404, detail="no active playground session")
        _raise_if_authoring_closed(s, job.id)
        job_id = job.id
        vm = s.query(JobVM).filter_by(id=vm_id, session_id=job_id).with_for_update().one_or_none()
        if vm is None:
            raise HTTPException(status_code=404, detail="VM not found")
        nic = s.query(JobVMNic).filter_by(id=nic_id, job_vm_id=vm_id).with_for_update().one_or_none()
        if nic is None:
            raise HTTPException(status_code=404, detail="NIC not found")
        if nic.state == "detach_claimed":
            claimed_at = nic.detach_claimed_at
            if claimed_at is not None and claimed_at.tzinfo is None:
                claimed_at = claimed_at.replace(tzinfo=UTC)
            stale_before = datetime.now(UTC) - timedelta(seconds=pr.NIC_DETACH_CLAIM_SECONDS)
            if claimed_at is not None and claimed_at > stale_before:
                return PlaygroundAcceptedOut(resource_id=nic_id, job_id=job_id, session_id=job_id, task_id=None)
            detach_token = secrets.token_hex(16)
            if not pr.reopen_nic_detach_claim(
                s,
                nic_id,
                vm_id=vm_id,
                detach_token=nic.detach_token,
                claimed_at=nic.detach_claimed_at,
                new_token=detach_token,
            ):
                return PlaygroundAcceptedOut(resource_id=nic_id, job_id=job_id, session_id=job_id, task_id=None)
        else:
            if nic.state not in {"active", "detaching"}:
                raise HTTPException(status_code=409, detail="NIC is not eligible for detachment")
            detach_token = nic.detach_token or secrets.token_hex(16)
            if nic.state == "active" or nic.detach_token is None:
                # Commit custody before enqueue so closing authoring observes
                # an in-flight detach, including legacy tokenless queued rows.
                nic.state = "detaching"
                nic.detach_token = detach_token
                nic.detach_claimed_at = None
    try:
        task_id = enqueue_or_raise(
            po.detach_nic,
            job_id=job_id,
            vm_id=vm_id,
            nic_id=nic_id,
            detach_token=detach_token,
        )
    except HTTPException:
        with db_mod.session_scope() as s:
            pr.compensate_nic_detach_enqueue_failure(s, nic_id, vm_id=vm_id, detach_token=detach_token, force=force)
        if not force:
            raise
        return Response(status_code=status.HTTP_200_OK)
    return PlaygroundAcceptedOut(resource_id=nic_id, job_id=job_id, session_id=job_id, task_id=task_id)
