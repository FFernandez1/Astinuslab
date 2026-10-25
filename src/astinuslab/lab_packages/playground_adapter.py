"""Translate persisted Playground state into a portable logical definition."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Literal, cast

from sqlalchemy.orm import Session

from ..api.schemas import (
    PlaygroundRecipe,
    PlaygroundRecipeNetwork,
    PlaygroundRecipeTopologyNode,
    PlaygroundRecipeVM,
)
from ..db.models import PlaygroundSession
from ..playground import orchestrator as playground_orchestrator
from ..playground import repositories as playground_repositories
from ..virtualization.vm_lifecycle import backing_virtual_size_gb
from .models import (
    LabDefinition,
    LabExperience,
    LabNetwork,
    LabTopologyNode,
    LabVM,
    ResourceRecommendations,
)


class PackageLaunchError(ValueError):
    """Raised when a portable definition cannot use the current Playground path."""


def _qemu_img_info(command: list[str], **kwargs):
    return subprocess.run(command, check=True, **kwargs)


def _backing_image_disk_gb(*, vm_label: str, image_id: str, image: dict) -> int:
    image_path = image.get("path")
    if not isinstance(image_path, str) or not image_path:
        raise ValueError(f"VM {vm_label} source image is unavailable: {image_id}")
    disk_gb = backing_virtual_size_gb(backing_image=Path(image_path), run_command=_qemu_img_info)
    if disk_gb is None:
        raise ValueError(f"VM {vm_label} source image virtual size is undeterminable: {image_id}")
    return disk_gb


def recipe_from_definition(definition: LabDefinition) -> PlaygroundRecipe:
    """Translate an unlocked portable definition into the existing Playground recipe contract."""

    if definition.reproducibility != "unlocked":
        raise PackageLaunchError(
            "locked packages require clean-start layer materialization and cannot use recipe launch yet"
        )
    networks = []
    for network in definition.networks:
        if network.host_bridge is not None:
            raise PackageLaunchError(f"network {network.id} requires unsupported host bridge: {network.host_bridge}")
        networks.append(
            PlaygroundRecipeNetwork(
                name=network.id,
                subnet_cidr=network.subnet_cidr,
                isolated=network.isolated,
            )
        )
    vms = []
    for vm in definition.vms:
        if vm.architecture != "x86_64":
            raise PackageLaunchError(f"VM {vm.id} requires unsupported architecture: {vm.architecture}")
        if vm.firmware != "bios":
            raise PackageLaunchError(f"VM {vm.id} requires unsupported firmware: {vm.firmware}")
        vms.append(
            PlaygroundRecipeVM(
                label=vm.id,
                role=vm.role,
                image_id=vm.image_id,
                workload_id=vm.workload_id,
                vcpus=vm.vcpus,
                memory_mb=vm.memory_mb,
                disk_gb=vm.disk_gb,
                networks=list(vm.network_ids),
            )
        )
    topology = [
        PlaygroundRecipeTopologyNode(node_id=node.node_id, node_type=node.node_type, x=node.x, y=node.y)
        for node in definition.topology
    ]
    return PlaygroundRecipe(
        name=definition.title,
        networks=networks,
        vms=vms,
        topology=topology,
    )


def definition_from_session(
    session: Session,
    *,
    session_id: str,
    package_id: str,
    version: str,
    title: str,
    description: str,
    creator: str,
    license: str,
    resources: ResourceRecommendations,
    experience: LabExperience,
) -> LabDefinition:
    """Build an unlocked definition without carrying runtime identity or custody state."""
    if session.get(PlaygroundSession, session_id) is None:
        raise ValueError(f"playground session not found: {session_id}")

    all_networks = playground_repositories.list_session_networks(session, session_id)
    persisted_networks = [network for network in all_networks if network.state != "destroyed"]
    destroyed_network_ids = {network.id for network in all_networks if network.state == "destroyed"}
    network_name_by_row_id = {network.id: network.name for network in persisted_networks}
    networks = sorted(
        (
            LabNetwork(
                id=network.name,
                subnet_cidr=network.subnet_cidr,
                reachability="isolated" if network.isolated else "nat",
                isolated=network.isolated,
                guest_egress=not network.isolated,
                host_bridge=None,
            )
            for network in persisted_networks
        ),
        key=lambda network: network.id,
    )

    all_vms = playground_repositories.list_session_vms(session, session_id)
    persisted_vms = [vm for vm in all_vms if vm.state != "destroyed"]
    destroyed_vm_ids = {vm.id for vm in all_vms if vm.state == "destroyed"}
    vm_name_by_row_id = {vm.id: vm.label for vm in persisted_vms}
    colliding_row_ids = sorted(set(network_name_by_row_id) & set(vm_name_by_row_id))
    if colliding_row_ids:
        raise ValueError(f"persisted VM and network IDs collide: {', '.join(colliding_row_ids)}")
    vms: list[LabVM] = []
    for vm in persisted_vms:
        missing_resources = [name for name in ("vcpus", "memory_mb") if getattr(vm, name) is None]
        if missing_resources:
            raise ValueError(f"VM {vm.label} is missing effective resources: {', '.join(missing_resources)}")
        vcpus = cast(int, vm.vcpus)
        memory_mb = cast(int, vm.memory_mb)
        if vm.disk_gb is None:
            image = playground_orchestrator.lookup_playground_image(vm.image_id, role=vm.role)
            if image is None:
                raise ValueError(f"VM {vm.label} source image is unavailable: {vm.image_id}")
            _, _, disk_gb = playground_orchestrator.resolve_vm_resources(
                vm.role, image, vcpus=vcpus, memory_mb=memory_mb, disk_gb=None
            )
            if disk_gb is None:
                disk_gb = _backing_image_disk_gb(vm_label=vm.label, image_id=vm.image_id, image=image)
        else:
            disk_gb = vm.disk_gb
        network_ids = []
        for nic in sorted(vm.nics, key=lambda item: item.ordinal):
            network_id = network_name_by_row_id.get(nic.job_network_id)
            if network_id is None:
                raise ValueError(f"VM {vm.label} network reference cannot be mapped: {nic.job_network_id}")
            network_ids.append(network_id)
        if not network_ids:
            raise ValueError(f"VM {vm.label} has no portable network references")
        vms.append(
            LabVM(
                id=vm.label,
                role=cast(Literal["target", "attacker", "network"], vm.role),
                image_id=vm.image_id,
                workload_id=vm.workload_id,
                vcpus=vcpus,
                memory_mb=memory_mb,
                disk_gb=disk_gb,
                network_ids=tuple(network_ids),
                clean_layer=None,
            )
        )
    vms.sort(key=lambda vm: vm.id)

    topology: list[LabTopologyNode] = []
    for node in playground_repositories.list_topology_layout(session, session_id):
        if node.node_type == "vm":
            logical_id = vm_name_by_row_id.get(node.node_id)
            mismatched_reference = node.node_id in network_name_by_row_id
            destroyed = node.node_id in destroyed_vm_ids
        elif node.node_type == "network":
            logical_id = network_name_by_row_id.get(node.node_id)
            mismatched_reference = node.node_id in vm_name_by_row_id
            destroyed = node.node_id in destroyed_network_ids
        else:
            raise ValueError(f"unsupported topology node type: {node.node_type}")
        if logical_id is None:
            if mismatched_reference:
                raise ValueError(f"topology {node.node_type} reference cannot be mapped: {node.node_id}")
            if destroyed:
                continue
            raise ValueError(f"topology reference cannot be mapped: {node.node_id}")
        topology.append(
            LabTopologyNode(
                node_id=logical_id,
                node_type=cast(Literal["vm", "network"], node.node_type),
                x=node.x,
                y=node.y,
            )
        )
    topology.sort(key=lambda node: (node.node_id, node.node_type))

    return LabDefinition(
        package_id=package_id,
        version=version,
        title=title,
        description=description,
        creator=creator,
        license=license,
        reproducibility="unlocked",
        resources=resources,
        networks=tuple(networks),
        vms=tuple(vms),
        topology=tuple(topology),
        experience=experience,
    )
