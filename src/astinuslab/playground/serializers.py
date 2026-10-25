from __future__ import annotations

from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from .. import catalog
from ..api.schemas import (
    PlaygroundNetworkOut,
    PlaygroundNicOut,
    PlaygroundServiceOut,
    PlaygroundSessionOut,
    PlaygroundVMOut,
)
from . import orchestrator as po
from . import repositories as pr
from . import workloads as pw

NON_DESTROY_ACTIVE = {"queued", "running"}
VM_TERMINAL_STATES = {"destroyed", "destroy_queued", "destroying", "teardown_failed", "failed"}
NET_TERMINAL_STATES = {"destroyed", "destroy_queued", "destroying", "failed"}

__all__ = [
    "NON_DESTROY_ACTIVE",
    "VM_TERMINAL_STATES",
    "NET_TERMINAL_STATES",
    "vm_out",
    "network_out",
    "serialize_session",
    "count_attached_vms",
    "vm_action_flags",
    "network_action_flags",
    "playground_image_for",
    "PlaygroundAcceptedOut",
]


class PlaygroundAcceptedOut(BaseModel):
    status: str = "accepted"
    resource_id: str | None = None
    job_id: str | None = Field(
        default=None,
        description="Deprecated alias of session_id; will be removed in the next minor version.",
        json_schema_extra={"deprecated": True},
    )
    session_id: str | None = None
    task_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _mirror_session_id(cls, data):
        if isinstance(data, dict):
            if data.get("session_id") is None and data.get("job_id") is not None:
                data["session_id"] = data["job_id"]
            elif data.get("job_id") is None and data.get("session_id") is not None:
                data["job_id"] = data["session_id"]
        return data


def vm_action_flags(vm, *, session_active: bool) -> tuple[bool, bool, bool, bool, bool, bool, str | None]:
    if not session_active:
        return False, False, False, False, False, False, "session not active"
    if vm.state in VM_TERMINAL_STATES:
        return False, False, False, False, False, False, f"VM {vm.state}"
    if vm.role == "network":
        can_start = vm.state in {"stopped", "shutoff", "shutdown"}
        can_stop = vm.state == "running"
        can_reboot = vm.state == "running"
        return False, False, True, can_start, can_stop, can_reboot, "capture disabled for Network Nodes"
    can_capture = vm.state == "running"
    can_start = vm.state in {"stopped", "shutoff", "shutdown"}
    can_stop = vm.state == "running"
    can_reboot = vm.state == "running"
    can_destroy = True
    reason: str | None = None
    if not can_capture:
        reason = f"VM not running (state={vm.state})"
    return can_capture, can_capture, can_destroy, can_start, can_stop, can_reboot, reason


def network_action_flags(network, *, session_active: bool, attached_count: int) -> tuple[bool, str | None]:
    if not session_active:
        return False, "session not active"
    if network.state in NET_TERMINAL_STATES:
        return False, f"network {network.state}"
    if attached_count > 0:
        return False, f"{attached_count} attached VM(s)"
    return True, None


def playground_image_for(role: str, image_id: str) -> dict | None:
    try:
        return po.lookup_playground_image(image_id, role=role)
    except ValueError:
        return None


def _nic_diagnostic(vm, nic) -> str | None:
    if nic.ip:
        return None
    summary = vm.error_summary or ""
    if not summary:
        return None
    parts = [p.strip() for p in summary.split(";") if p.strip()]
    for part in parts:
        if nic.id in part:
            return part
    return summary if len(vm.nics or []) == 1 else None


def nic_out(vm, nic) -> PlaygroundNicOut:
    network = getattr(nic, "network", None)
    return PlaygroundNicOut(
        id=nic.id,
        job_network_id=nic.job_network_id,
        ordinal=nic.ordinal,
        network_id=nic.job_network_id,
        network_name=getattr(network, "name", None),
        network_state=getattr(network, "state", None),
        network_isolated=getattr(network, "isolated", None),
        mac=nic.mac,
        ip=nic.ip,
        state=getattr(nic, "state", None),
        diagnostic=_nic_diagnostic(vm, nic),
    )


def vm_out(vm, *, session_active: bool = False) -> PlaygroundVMOut:
    workload = catalog.workload_by_id(vm.workload_id) if vm.workload_id else None
    image = playground_image_for(vm.role, vm.image_id)
    ip = next((nic.ip for nic in vm.nics if nic.ip), None)
    workload_state = vm.workload_state
    raw_services = (
        pw.build_playground_service_outputs(workload, guest_ip=ip, status=workload_state or "planned")
        if workload
        else []
    )
    services = [PlaygroundServiceOut.model_validate(s) for s in raw_services]
    can_mem, can_disk, can_destroy, can_start, can_stop, can_reboot, reason = vm_action_flags(
        vm, session_active=session_active
    )
    return PlaygroundVMOut(
        id=vm.id,
        job_id=vm.session_id,
        session_id=vm.session_id,
        label=vm.label,
        image_id=vm.image_id,
        os=image.get("label") if image else vm.image_id,
        state=vm.state,
        domain_name=vm.domain_name,
        role=vm.role,
        # What a Network Node is for (firewall / router / nat-gateway). The
        # catalog has always carried it; without this the map cannot tell a
        # firewall from any other appliance.
        function_tags=list(image.get("function_tags") or []) if image else [],
        vcpus=vm.vcpus,
        memory_mb=vm.memory_mb,
        disk_gb=vm.disk_gb,
        created_at=vm.created_at,
        workload_id=vm.workload_id,
        workload_display_name=workload.get("display_name") if workload else None,
        workload_state=workload_state,
        workload_error=vm.workload_error,
        error_summary=vm.error_summary,
        services=services,
        nics=[nic_out(vm, n) for n in vm.nics],
        can_capture_memory=can_mem,
        can_capture_disk=can_disk,
        can_destroy=can_destroy,
        can_start=can_start,
        can_stop=can_stop,
        can_reboot=can_reboot,
        action_block_reason=reason,
    )


def network_out(n, *, session_active: bool = False, attached_vm_count: int = 0) -> PlaygroundNetworkOut:
    can_delete, reason = network_action_flags(n, session_active=session_active, attached_count=attached_vm_count)
    return PlaygroundNetworkOut(
        id=n.id,
        job_id=n.session_id,
        session_id=n.session_id,
        name=n.name,
        subnet_cidr=n.subnet_cidr,
        isolated=n.isolated,
        libvirt_net_name=n.libvirt_net_name,
        state=n.state,
        attached_vm_count=attached_vm_count,
        can_delete=can_delete,
        action_block_reason=reason,
    )


def count_attached_vms(session: Session, network_id: str) -> int:
    from ..db.models import JobVM, JobVMNic

    return (
        session.query(JobVMNic)
        .join(JobVM, JobVM.id == JobVMNic.job_vm_id)
        .filter(JobVMNic.job_network_id == network_id)
        .filter(JobVM.state != "destroyed")
        .count()
    )


def serialize_session(session: Session, job) -> PlaygroundSessionOut:
    vms = pr.list_session_vms(session, job.id)
    nets = [net for net in pr.list_session_networks(session, job.id) if net.state != "destroyed"]
    session_active = job.status in NON_DESTROY_ACTIVE
    return PlaygroundSessionOut(
        job_id=job.id,
        session_id=job.id,
        status=job.status,
        started_at=job.started_at,
        vms=[vm_out(vm, session_active=session_active) for vm in vms],
        networks=[
            network_out(
                n,
                session_active=session_active,
                attached_vm_count=count_attached_vms(session, n.id),
            )
            for n in nets
        ],
    )
