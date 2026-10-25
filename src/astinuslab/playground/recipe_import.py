"""Atomic admission for recipe import.

A recipe describes several networks and VMs that only make sense together, so
its admission decision has to be a single one. Reservation runs entirely inside
the caller's transaction: it resolves or creates the parent session, checks
every name, CIDR, capacity and resource limit, and registers every row. It
never commits and never dispatches. The caller commits, and only then hands the
returned plan to :func:`dispatch_plan`.

The plan carries plain values exactly so that boundary stays honest: nothing in
it is an attached ORM object, so no part of dispatch can read through to a
session that has already been closed.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .. import db as db_mod
from ..api.errors import ApiError
from ..api.schemas import PlaygroundNicSpec, PlaygroundRecipe, PlaygroundSpawnVMRequest
from ..db.models import JobNetwork, JobVM, PlaygroundTopologyLayout
from . import admission
from . import orchestrator as po
from . import repositories as pr
from .enqueue import enqueue_or_raise
from .routes import networks as networks_routes
from .routes import vms as vms_routes
from .routes.sessions import _raise_if_authoring_closed, _raise_if_non_playground_job_active
from .serializers import NON_DESTROY_ACTIVE

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlannedNic:
    nic_id: str
    network_id: str
    mac: str


@dataclass(frozen=True)
class PlannedNetwork:
    network_id: str
    name: str
    subnet_cidr: str
    isolated: bool
    reservation_token: str


@dataclass(frozen=True)
class PlannedVM:
    vm_id: str
    label: str
    image_id: str
    role: str
    vcpus: int
    memory_mb: int
    disk_gb: int | None
    workload_id: str | None
    reservation_token: str
    nics: tuple[PlannedNic, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RecipeImportPlan:
    """Everything dispatch needs, and nothing that is still attached to a session."""

    session_id: str
    created_new_session: bool
    networks: tuple[PlannedNetwork, ...] = field(default_factory=tuple)
    vms: tuple[PlannedVM, ...] = field(default_factory=tuple)


def _resolve_parent_session(session: Session, job_id_factory) -> tuple[str, bool]:
    """Resolve the active parent or create one, inside the caller's transaction."""
    parent = pr.active_playground_session(session)
    if parent is not None:
        _raise_if_authoring_closed(session, parent.id)
        if parent.status not in NON_DESTROY_ACTIVE:
            raise ApiError(409, "playground session is no longer accepting resources", "conflict")
        return parent.id, False
    _raise_if_non_playground_job_active(session)
    job_id = job_id_factory()
    pr.create_playground_session(session, job_id=job_id)
    return job_id, True


def reserve_recipe(
    session: Session,
    recipe: PlaygroundRecipe,
    *,
    images_by_label: dict[str, dict],
) -> RecipeImportPlan:
    """Reserve the whole recipe in the caller's transaction and return the plan.

    Raises before any row is dispatched if a single name, CIDR, resource or
    capacity check fails; the caller's rollback then removes every reservation,
    including a parent session created here.
    """
    # Take the writer lock before the first admission read so that no other
    # importer or endpoint can register a conflicting name between the check
    # and the insert.
    pr.lock_resource_transitions(session)

    job_id, created_new_session = _resolve_parent_session(session, lambda: f"pg-{secrets.token_hex(6)}")

    admission.ensure_vm_capacity(session, requested=len(recipe.vms))

    taken_network_names = {n.name for n in pr.list_live_session_networks(session, job_id)}
    taken_vm_labels = {vm.label for vm in pr.list_session_vms(session, job_id)}

    planned_networks: list[PlannedNetwork] = []
    network_ids_by_name: dict[str, str] = {}
    for network in recipe.networks:
        if network.name in taken_network_names:
            raise ApiError(409, f"network name {network.name!r} already exists", "conflict")
        network_id = f"net-{secrets.token_hex(6)}"
        final_cidr, _substituted_from = pr.resolve_cidr(session, job_id, network.subnet_cidr)
        try:
            registered_network = networks_routes._register_pending_network(
                session,
                job_id=job_id,
                network_id=network_id,
                name=network.name,
                subnet_cidr=final_cidr,
                isolated=network.isolated,
            )
        except RuntimeError as exc:
            raise ApiError(409, str(exc), "conflict") from exc
        taken_network_names.add(network.name)
        network_ids_by_name[network.name] = network_id
        planned_networks.append(
            PlannedNetwork(
                network_id=network_id,
                name=network.name,
                subnet_cidr=final_cidr,
                isolated=network.isolated,
                reservation_token=registered_network.updated_at.isoformat(),
            )
        )

    planned_vms: list[PlannedVM] = []
    for vm in recipe.vms:
        if vm.label in taken_vm_labels:
            raise ApiError(409, f"VM label {vm.label!r} already in use", "conflict")
        spawn_payload = PlaygroundSpawnVMRequest(
            label=vm.label,
            image_id=vm.image_id,
            role=vm.role,
            nics=[PlaygroundNicSpec(network_id=network_ids_by_name[name]) for name in vm.networks],
            vcpus=vm.vcpus,
            memory_mb=vm.memory_mb,
            disk_gb=vm.disk_gb,
            workload_id=vm.workload_id,
        )
        image = images_by_label[vm.label]
        try:
            resources = po.resolve_vm_resources(
                vm.role,
                image,
                vcpus=vm.vcpus,
                memory_mb=vm.memory_mb,
                disk_gb=vm.disk_gb,
            )
        except ValueError as exc:
            raise HTTPException(status_code=500, detail="invalid configured VM resources") from exc
        vm_id = f"vm-{secrets.token_hex(6)}"
        nic_specs = [
            {"network_id": nic.network_id, "nic_id": f"nic-{secrets.token_hex(6)}", "mac": po.generate_mac()}
            for nic in spawn_payload.nics
        ]
        registered_vm = vms_routes._register_pending_vm(
            session,
            job_id=job_id,
            vm_id=vm_id,
            payload=spawn_payload,
            nic_specs=nic_specs,
            resources=resources,
        )
        taken_vm_labels.add(vm.label)
        planned_vms.append(
            PlannedVM(
                vm_id=vm_id,
                label=vm.label,
                image_id=vm.image_id,
                role=vm.role,
                vcpus=resources[0],
                memory_mb=resources[1],
                disk_gb=resources[2],
                workload_id=vm.workload_id,
                reservation_token=registered_vm.updated_at.isoformat(),
                nics=tuple(
                    PlannedNic(nic_id=spec["nic_id"], network_id=spec["network_id"], mac=spec["mac"])
                    for spec in nic_specs
                ),
            )
        )

    vm_ids_by_label = {planned.label: planned.vm_id for planned in planned_vms}
    for node in recipe.topology:
        if node.node_type == "network":
            node_id = network_ids_by_name.get(node.node_id)
        else:
            node_id = vm_ids_by_label.get(node.node_id)
        if node_id is None:
            continue
        pr.save_topology_node(
            session,
            job_id,
            node_id,
            {"node_id": node_id, "node_type": node.node_type, "x": node.x, "y": node.y},
        )

    return RecipeImportPlan(
        session_id=job_id,
        created_new_session=created_new_session,
        networks=tuple(planned_networks),
        vms=tuple(planned_vms),
    )


def dispatch_plan(plan: RecipeImportPlan) -> None:
    """Publish a committed plan. Networks first, so the worker sees them ready.

    Only call this after the reservation transaction has committed: every row
    referenced here is already durable, so a task that arrives immediately
    finds its resource.
    """
    for network in plan.networks:
        try:
            networks_routes._enqueue_create_network(
                job_id=plan.session_id,
                network_id=network.network_id,
                name=network.name,
                subnet_cidr=network.subnet_cidr,
                isolated=network.isolated,
                reservation_token=network.reservation_token,
            )
        except Exception as exc:
            detail = str(exc)
            raise HTTPException(status_code=500, detail=detail) from exc

    for vm in plan.vms:
        enqueue_or_raise(
            po.spawn_vm,
            job_id=plan.session_id,
            vm_id=vm.vm_id,
            label=vm.label,
            image_id=vm.image_id,
            nic_specs=[{"network_id": nic.network_id, "nic_id": nic.nic_id, "mac": nic.mac} for nic in vm.nics],
            role=vm.role,
            vcpus=vm.vcpus,
            memory_mb=vm.memory_mb,
            disk_gb=vm.disk_gb,
            resources_resolved=True,
            workload_id=vm.workload_id,
            reservation_token=vm.reservation_token,
        )


def compensate_plan(plan: RecipeImportPlan, detail: str) -> None:
    """Persist cleanup custody before attempting any best-effort task delivery."""
    vm_ids = [vm.vm_id for vm in plan.vms]
    network_ids = [network.network_id for network in plan.networks]
    resource_groups: tuple[tuple[type[JobVM] | type[JobNetwork], list[str]], ...] = (
        (JobVM, vm_ids),
        (JobNetwork, network_ids),
    )
    with db_mod.session_scope() as session:
        pr.lock_resource_transitions(session)
        for model, ids in resource_groups:
            rows = cast(
                list[JobVM | JobNetwork],
                session.query(model).filter(model.id.in_(ids), model.session_id == plan.session_id).all(),
            )
            for row in rows:
                if row.state in {"destroying", "destroyed"}:
                    continue  # Preserve an existing teardown worker's token.
                session.query(model).filter(
                    model.id == row.id,
                    model.session_id == plan.session_id,
                    model.state == row.state,
                    model.updated_at == row.updated_at,
                ).update(
                    {
                        model.state: "teardown_failed",
                        model.updated_at: datetime.now(UTC),
                        model.error_summary: f"recipe import cleanup requested: {detail}"[:1000],
                    },
                    synchronize_session=False,
                )
        session.query(PlaygroundTopologyLayout).filter(
            PlaygroundTopologyLayout.session_id == plan.session_id,
            PlaygroundTopologyLayout.node_id.in_([*vm_ids, *network_ids]),
        ).delete(synchronize_session=False)
        if plan.created_new_session:
            pr.mark_session_destroy_failed(session, plan.session_id, "cleanup requested after failed recipe import")

    # Delivery only accelerates recovery; the persisted states above are enough
    # for reconciliation if the broker is unavailable or this process exits.
    if plan.created_new_session:
        try:
            po.destroy_session.delay(job_id=plan.session_id)
        except Exception:
            logger.exception("Could not dispatch recipe session cleanup for %s", plan.session_id)
        return
    for vm_id in vm_ids:
        try:
            po.destroy_vm.delay(vm_id=vm_id)
        except Exception:
            logger.exception("Could not dispatch recipe VM cleanup for %s", vm_id)
    for network_id in network_ids:
        with db_mod.session_scope() as session:
            if pr.network_has_attached_vms(session, network_id):
                continue  # Reconciliation retries after VM/NIC teardown.
        try:
            po.destroy_network.delay(network_id=network_id)
        except Exception:
            logger.exception("Could not dispatch recipe network cleanup for %s", network_id)
