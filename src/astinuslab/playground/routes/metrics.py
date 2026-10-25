from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from ... import db as db_mod
from ...api.schemas import PlaygroundVMMetricsOut
from .. import metrics as _metrics_mod
from .. import repositories as pr

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/vms/{vm_id}/metrics", response_model=PlaygroundVMMetricsOut)
def get_vm_metrics_endpoint(vm_id: str) -> PlaygroundVMMetricsOut:
    with db_mod.session_scope() as s:
        from ...db.models import JobVM

        active = pr.active_playground_session(s)
        vm = s.query(JobVM).filter_by(id=vm_id).one_or_none()
        if vm is None or active is None or vm.session_id != active.id:
            raise HTTPException(status_code=404, detail="vm not found")
        domain_name = vm.domain_name
    try:
        m = _metrics_mod.get_sampler().sample(vm_id, domain_name=domain_name)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"metrics unavailable: {exc}") from exc
    return PlaygroundVMMetricsOut(**m)


@router.get("/sessions/current/metrics", response_model=dict[str, PlaygroundVMMetricsOut])
def batch_metrics_endpoint() -> dict[str, PlaygroundVMMetricsOut]:
    with db_mod.session_scope() as s:
        job = pr.active_playground_session(s)
        if job is None:
            raise HTTPException(status_code=404, detail="no active playground session")
        vms = [(vm.id, vm.domain_name) for vm in pr.list_session_vms(s, job.id) if vm.state == "running"]

    sampler = _metrics_mod.get_sampler()
    result: dict[str, PlaygroundVMMetricsOut] = {}
    for vm_id, domain_name in vms:
        try:
            result[vm_id] = PlaygroundVMMetricsOut(**sampler.sample(vm_id, domain_name=domain_name))
        except Exception as exc:
            logger.warning("batch_metrics: sample failed for %s: %s", vm_id, exc)
    return result
