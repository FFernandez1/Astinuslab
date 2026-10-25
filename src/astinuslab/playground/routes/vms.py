from __future__ import annotations

import logging
import secrets
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from ... import db as db_mod
from ...api.errors import ApiError
from ...api.schemas import (
    PlaygroundCreateNetworkRequest,
    PlaygroundNicSpec,
    PlaygroundSpawnVMRequest,
    PlaygroundVMOut,
    PlaygroundVMRole,
)
from .. import admission
from .. import orchestrator as po
from .. import repositories as pr
from .. import workloads as pw
from ..enqueue import ENQUEUE_FAILED_DETAIL as _ENQUEUE_FAILED_DETAIL
from ..enqueue import enqueue_or_raise
from ..resources import DISK_GB_MAX, DISK_GB_MIN, MEMORY_MB_MAX, MEMORY_MB_MIN, VCPUS_MAX, VCPUS_MIN
from ..serializers import NON_DESTROY_ACTIVE, playground_image_for, vm_out
from . import networks as networks_routes
from .sessions import _ensure_active_session_id, _integrity_as_409, _raise_if_authoring_closed

router = APIRouter()
logger = logging.getLogger(__name__)


class PlaygroundCreateIfNeededSpawnVMRequest(BaseModel):
    label: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    image_id: str = Field(min_length=1)
    vcpus: int | None = Field(default=None, ge=VCPUS_MIN, le=VCPUS_MAX)
    memory_mb: int | None = Field(default=None, ge=MEMORY_MB_MIN, le=MEMORY_MB_MAX)
    disk_gb: int | None = Field(default=None, ge=DISK_GB_MIN, le=DISK_GB_MAX)
    role: PlaygroundVMRole = "target"
    workload_id: str | None = None
    network_ids: list[str] | None = None
    new_networks: list[PlaygroundCreateNetworkRequest] | None = None


def _image_is_available(image: dict) -> bool:
    path = image.get("path")
    return bool(path and Path(path).is_file())


def _validate_spawn_image(payload: PlaygroundSpawnVMRequest | PlaygroundCreateIfNeededSpawnVMRequest) -> dict:
    image = playground_image_for(payload.role, payload.image_id)
    if image is None:
        raise HTTPException(status_code=400, detail=f"unknown or unavailable image: {payload.image_id}")
    if not _image_is_available(image):
        path = image.get("path", "")
        raise HTTPException(status_code=400, detail=f"image not available: {payload.image_id} ({path})")
    return image


def _validate_spawn_workload(
    payload: PlaygroundSpawnVMRequest | PlaygroundCreateIfNeededSpawnVMRequest,
    *,
    image: dict | None = None,
) -> None:
    if image is None:
        image = _validate_spawn_image(payload)
    validation = pw.validate_playground_workload_selection(
        role=payload.role,
        workload_id=payload.workload_id,
        image=image,
    )
    if not validation.ok:
        raise HTTPException(status_code=validation.status_code, detail=validation.detail)


def _validate_create_if_needed_spawn(job_id: str, payload: PlaygroundCreateIfNeededSpawnVMRequest) -> None:
    with db_mod.session_scope() as s:
        _raise_if_authoring_closed(s, job_id)
        existing_labels = {v.label for v in pr.list_session_vms(s, job_id)}
        if payload.label in existing_labels:
            raise ApiError(409, f"VM label '{payload.label}' already in use", "conflict")


def _safe_destroy_session(job_id: str) -> None:
    # Transfer the global exclusion lock into durable cleanup custody before
    # publishing. If the broker is unavailable, reconciliation can still drive
    # this row (including an empty, just-created session) to a terminal state.
    with db_mod.session_scope() as s:
        pr.mark_session_destroy_failed(s, job_id, "cleanup requested after failed session creation")
    try:
        po.destroy_session.delay(job_id=job_id)
    except Exception as exc:
        with db_mod.session_scope() as s:
            pr.mark_session_destroy_failed(s, job_id, f"cleanup enqueue failed: {exc}")
        logger.exception("safe_destroy_session failed for %s", job_id)


def _register_pending_vm(
    session,
    *,
    job_id: str,
    vm_id: str,
    payload: PlaygroundSpawnVMRequest,
    nic_specs: list[dict],
    resources: tuple[int, int, int | None],
):
    # Image removal holds this same writer reservation through unlink. The
    # earlier catalog/readiness check may predate that removal; validate again
    # before creating durable intent, including recipe imports.
    pr.lock_resource_transitions(session)
    _validate_spawn_image(payload)
    try:
        vm = pr.register_vm(
            session,
            vm_id=vm_id,
            job_id=job_id,
            label=payload.label,
            image_id=payload.image_id,
            domain_name=f"astinuslab-pg-{job_id}-{payload.label}",
            disk_path=str(po.settings.playground_disk_dir / job_id / f"{payload.label}.qcow2"),
            role=payload.role,
            workload_id=payload.workload_id,
            vcpus=resources[0],
            memory_mb=resources[1],
            disk_gb=resources[2],
        )
    except RuntimeError as exc:
        raise ApiError(409, str(exc), "conflict") from exc
    for spec in nic_specs:
        pr.attach_nic(
            session,
            nic_id=spec["nic_id"],
            vm_id=vm_id,
            network_id=spec["network_id"],
            mac=spec["mac"],
        )
    return vm


@router.post("/vms", status_code=status.HTTP_202_ACCEPTED, response_model=PlaygroundVMOut)
def spawn_vm_create_if_needed_endpoint(
    payload: PlaygroundCreateIfNeededSpawnVMRequest,
) -> PlaygroundVMOut:
    created_new_session = False
    job_id: str | None = None
    try:
        image = _validate_spawn_image(payload)
        _validate_spawn_workload(payload, image=image)
        job_id, created_new_session = _ensure_active_session_id()
        _validate_create_if_needed_spawn(job_id, payload)
        network_ids = list(payload.network_ids or [])
        if payload.new_networks:
            network_ids.extend(networks_routes._create_inline_networks(job_id, payload.new_networks))
        if network_ids:
            with db_mod.session_scope() as s:
                netset = {n.id: n for n in pr.list_session_networks(s, job_id)}
                new_ids = set(network_ids) - set(payload.network_ids or [])
                for nid in network_ids:
                    net = netset.get(nid)
                    if net is None:
                        raise HTTPException(status_code=400, detail=f"unknown network: {nid}")
                    if nid not in new_ids and net.state != "active":
                        raise ApiError(409, f"network not usable: {nid} state={net.state}", "conflict")
                    if nid in new_ids and net.state in {"destroy_queued", "destroying", "destroyed", "failed"}:
                        raise ApiError(409, f"network not usable: {nid} state={net.state}", "conflict")
            return spawn_vm_endpoint(
                job_id,
                PlaygroundSpawnVMRequest(
                    label=payload.label,
                    image_id=payload.image_id,
                    role=payload.role,
                    nics=[PlaygroundNicSpec(network_id=nid) for nid in network_ids],
                    vcpus=payload.vcpus,
                    memory_mb=payload.memory_mb,
                    disk_gb=payload.disk_gb,
                    workload_id=payload.workload_id,
                ),
            )
        # default path
        network_id = networks_routes._ensure_default_network(job_id)
        return spawn_vm_endpoint(
            job_id,
            PlaygroundSpawnVMRequest(
                label=payload.label,
                image_id=payload.image_id,
                role=payload.role,
                nics=[PlaygroundNicSpec(network_id=network_id)],
                vcpus=payload.vcpus,
                memory_mb=payload.memory_mb,
                disk_gb=payload.disk_gb,
                workload_id=payload.workload_id,
            ),
        )
    except (ApiError, HTTPException):
        if created_new_session and job_id is not None:
            _safe_destroy_session(job_id)
        raise
    except IntegrityError as exc:
        if created_new_session and job_id is not None:
            _safe_destroy_session(job_id)
        raise ApiError(409, "session, network, or VM already exists", "conflict") from exc
    except Exception as exc:
        if created_new_session and job_id is not None:
            _safe_destroy_session(job_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/sessions/{job_id}/vms", status_code=status.HTTP_202_ACCEPTED, response_model=PlaygroundVMOut)
def spawn_vm_endpoint(job_id: str, payload: PlaygroundSpawnVMRequest) -> PlaygroundVMOut:
    with _integrity_as_409(f"VM label '{payload.label}' already in use"):
        with db_mod.session_scope() as s:
            job = pr.active_playground_session(s)
            if job is None or job.id != job_id:
                raise HTTPException(status_code=404, detail="session not found or not active")
            _raise_if_authoring_closed(s, job.id)
            if job.status not in NON_DESTROY_ACTIVE:
                raise HTTPException(status_code=404, detail="session not found or not active")
            existing_labels = {v.label for v in pr.list_session_vms(s, job_id)}
            if payload.label in existing_labels:
                raise ApiError(409, f"VM label '{payload.label}' already in use", "conflict")
            admission.ensure_vm_capacity(s, requested=1)
            for nic in payload.nics:
                from ...db.models import JobNetwork

                net = s.query(JobNetwork).filter_by(id=nic.network_id, session_id=job_id).one_or_none()
                if net is None:
                    raise HTTPException(status_code=400, detail=f"unknown network: {nic.network_id}")
                if net.state in {"destroy_queued", "destroying", "destroyed", "failed"}:
                    raise ApiError(409, f"network not usable: {nic.network_id} state={net.state}", "conflict")
            image = _validate_spawn_image(payload)
            _validate_spawn_workload(payload, image=image)
            try:
                resources = po.resolve_vm_resources(
                    payload.role,
                    image,
                    vcpus=payload.vcpus,
                    memory_mb=payload.memory_mb,
                    disk_gb=payload.disk_gb,
                )
            except ValueError as exc:
                raise HTTPException(status_code=500, detail="invalid configured VM resources") from exc
            vm_id = f"vm-{secrets.token_hex(6)}"
            nic_specs = [
                {"network_id": n.network_id, "nic_id": f"nic-{secrets.token_hex(6)}", "mac": po.generate_mac()}
                for n in payload.nics
            ]
            _register_pending_vm(
                s, job_id=job_id, vm_id=vm_id, payload=payload, nic_specs=nic_specs, resources=resources
            )

    def _on_spawn_enqueue_failure(_exc: Exception) -> None:
        with db_mod.session_scope() as s:
            pr.update_vm_state(s, vm_id, "failed", error=_ENQUEUE_FAILED_DETAIL)

    enqueue_or_raise(
        po.spawn_vm,
        on_failure=_on_spawn_enqueue_failure,
        job_id=job_id,
        vm_id=vm_id,
        label=payload.label,
        image_id=payload.image_id,
        nic_specs=nic_specs,
        role=payload.role,
        vcpus=resources[0],
        memory_mb=resources[1],
        disk_gb=resources[2],
        resources_resolved=True,
        workload_id=payload.workload_id,
    )

    with db_mod.session_scope() as s:
        from ...db.models import JobVM

        vm = s.query(JobVM).filter_by(id=vm_id).one()
        active = pr.active_playground_session(s)
        session_active = active is not None and active.id == vm.session_id and active.status in NON_DESTROY_ACTIVE
        return vm_out(vm, session_active=session_active)
