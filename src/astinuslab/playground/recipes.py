from __future__ import annotations

from collections.abc import Iterable
from typing import Literal, cast

from sqlalchemy.orm import Session

from ..api.schemas import (
    PlaygroundRecipe,
    PlaygroundRecipeNetwork,
    PlaygroundRecipeTopologyNode,
    PlaygroundRecipeVM,
)
from . import repositories as pr


class PlaygroundRecipeError(ValueError):
    pass


def _ensure_unique(values: Iterable[str], *, kind: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise PlaygroundRecipeError(f"duplicate {kind}: {value}")
        seen.add(value)


def validate_recipe(recipe: PlaygroundRecipe) -> None:
    _ensure_unique((n.name for n in recipe.networks), kind="network")
    _ensure_unique((v.label for v in recipe.vms), kind="VM")
    network_names = {n.name for n in recipe.networks}
    vm_labels = {v.label for v in recipe.vms}
    for vm in recipe.vms:
        if not vm.networks:
            raise PlaygroundRecipeError(f"VM {vm.label} must attach to at least one network")
        for network_name in vm.networks:
            if network_name not in network_names:
                raise PlaygroundRecipeError(f"VM {vm.label} references unknown network: {network_name}")
    for node in recipe.topology:
        if node.node_type == "network" and node.node_id not in network_names:
            raise PlaygroundRecipeError(f"topology references unknown network: {node.node_id}")
        if node.node_type == "vm" and node.node_id not in vm_labels:
            raise PlaygroundRecipeError(f"topology references unknown VM: {node.node_id}")


def export_recipe(session: Session, job_id: str, *, name: str = "playground") -> PlaygroundRecipe:
    live_networks = [n for n in pr.list_session_networks(session, job_id) if n.state != "destroyed"]
    live_vms = [vm for vm in pr.list_session_vms(session, job_id) if vm.state != "destroyed"]
    networks = [
        PlaygroundRecipeNetwork(name=n.name, subnet_cidr=n.subnet_cidr, isolated=n.isolated) for n in live_networks
    ]
    networks_by_id = {n.id: n.name for n in live_networks}
    vms_by_id = {vm.id: vm.label for vm in live_vms}
    vms = []
    for vm in live_vms:
        vms.append(
            PlaygroundRecipeVM(
                label=vm.label,
                role=cast(Literal["target", "attacker", "network"], vm.role),
                image_id=vm.image_id,
                workload_id=vm.workload_id,
                vcpus=vm.vcpus,
                memory_mb=vm.memory_mb,
                disk_gb=vm.disk_gb,
                networks=[
                    networks_by_id[nic.job_network_id] for nic in vm.nics if nic.job_network_id in networks_by_id
                ],
            )
        )
    topology = []
    for row in pr.list_topology_layout(session, job_id):
        by_id = networks_by_id if row.node_type == "network" else vms_by_id
        other_by_id = vms_by_id if row.node_type == "network" else networks_by_id
        node_id = by_id.get(row.node_id)
        if node_id is None:
            if row.node_id in other_by_id:
                raise PlaygroundRecipeError(f"topology {row.node_type} reference has the wrong type: {row.node_id}")
            continue
        topology.append(
            PlaygroundRecipeTopologyNode(
                node_id=node_id,
                node_type=cast(Literal["vm", "network"], row.node_type),
                x=row.x,
                y=row.y,
            )
        )
    recipe = PlaygroundRecipe(name=name, networks=networks, vms=vms, topology=topology)
    validate_recipe(recipe)
    return recipe
