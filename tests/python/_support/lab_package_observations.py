"""Read-only projections shared by native package acceptance and hermetic tests."""

from __future__ import annotations

import ipaddress
import os
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from astinuslab.lab_packages.models import LabDefinition, validate_checkpoint


def assert_native_wiring(
    vms: list[Any], networks: list[Any], domain_xml: dict[str, str], network_xml: dict[str, str]
) -> None:
    """Match the live hypervisor definition, not only intended database rows."""
    by_id = {network.id: network for network in networks}
    for vm in vms:
        root = ET.fromstring(domain_xml[vm.domain_name])
        disks = root.findall("./devices/disk[@device='disk']/source")
        assert [disk.get("file") for disk in disks] == [vm.disk_path], "live boot disk differs"
        interfaces = root.findall("./devices/interface")
        actual = []
        for node in interfaces:
            source = node.find("source")
            actual.append(source.get("network") if node.get("type") == "network" and source is not None else None)
        expected = [
            by_id[nic.job_network_id].libvirt_net_name for nic in sorted(vm.nics, key=lambda item: item.ordinal)
        ]
        assert actual == expected, "live NIC membership/order differs"
    for network in networks:
        root = ET.fromstring(network_xml[network.libvirt_net_name])
        subnets = []
        for node in root.findall("ip"):
            address, mask = node.get("address"), node.get("prefix") or node.get("netmask")
            assert address and mask, "live network subnet is missing"
            subnets.append(str(ipaddress.ip_network(f"{address}/{mask}", strict=False)))
        assert subnets == [network.subnet_cidr], "live network subnet differs"
        forwarding = root.find("forward")
        if network.isolated:
            assert forwarding is None, "live network is not isolated"
        else:
            assert forwarding is not None and forwarding.get("mode") == "nat", "live network is not NAT"


def logical_topology(vms: list[Any], networks: list[Any]) -> dict[str, Any]:
    by_id = {network.id: network for network in networks}
    assert len(by_id) == len(networks), "duplicate database network ID"
    assert len({network.name for network in networks}) == len(networks), "duplicate logical network"
    assert len({vm.label for vm in vms}) == len(vms), "duplicate logical VM"
    nics = []
    for vm in sorted(vms, key=lambda item: item.label):
        ordered = sorted(vm.nics, key=lambda nic: nic.ordinal)
        assert [nic.ordinal for nic in ordered] == list(range(len(ordered))), "ambiguous NIC ordinals"
        for nic in ordered:
            assert nic.job_network_id in by_id, "NIC refers to an unobserved network"
            nics.append({"vm_id": vm.label, "network_id": by_id[nic.job_network_id].name, "ordinal": nic.ordinal})
    return {
        "networks": [
            {"id": n.name, "subnet_cidr": n.subnet_cidr, "isolated": n.isolated}
            for n in sorted(networks, key=lambda item: item.name)
        ],
        "nics": nics,
    }


def observe_phase(
    definition: LabDefinition,
    vms: list[Any],
    networks: list[Any],
    *,
    backing_observer: Callable[[Path, str, int], tuple[str, int]],
    readiness_checker: Callable[..., bool],
    checkpoint_evaluator: Callable[..., bool],
) -> dict[str, Any]:
    """Observe every VM and declared guest operation; never zip unordered rows."""
    by_label = {vm.label: vm for vm in vms}
    assert len(by_label) == len(vms) and set(by_label) == {vm.id for vm in definition.vms}, "VM membership changed"
    topology = logical_topology(vms, networks)
    expected = {
        "networks": [
            {"id": n.id, "subnet_cidr": n.subnet_cidr, "isolated": n.isolated}
            for n in sorted(definition.networks, key=lambda item: item.id)
        ],
        "nics": [
            {"vm_id": vm.id, "network_id": network, "ordinal": ordinal}
            for vm in sorted(definition.vms, key=lambda item: item.id)
            for ordinal, network in enumerate(vm.network_ids)
        ],
    }
    assert topology == expected, "observed topology differs from definition"
    layers, readiness, checkpoints = [], [], []
    for contract in sorted(definition.vms, key=lambda item: item.id):
        layer = contract.effective_boot_layer
        assert layer is not None, "VM has no locked layer"
        digest, size = backing_observer(Path(by_label[contract.id].disk_path), layer.sha256, layer.size_bytes)
        assert digest == layer.sha256 and size == layer.size_bytes and size > 0, "boot layer differs"
        layers.append({"vm_id": contract.id, "sha256": digest, "size_bytes": size})
        probes = contract.workload.readiness_probes if contract.workload else ()
        passed = readiness_checker(contract.id, probes)
        assert passed is True, f"readiness failed: {contract.id}"
        readiness.append(
            {"vm_id": contract.id, "probes": [p.model_dump(mode="json") for p in probes], "passed": passed}
        )
    for raw in definition.experience.model_dump(mode="json")["checkpoints"]:
        operation = validate_checkpoint(raw)
        passed = checkpoint_evaluator(operation)
        assert passed is True, f"checkpoint failed: {operation.vm_id} {operation.path}"
        checkpoints.append({"operation": raw, "passed": passed})
    return {
        "state": "ready",
        "topology": topology,
        "boot_layers": layers,
        "readiness": readiness,
        "checkpoints": checkpoints,
    }


def resource_residue(
    *,
    domains: set[str],
    networks: set[str],
    overlays: Iterable[Path],
    temporary_paths: Iterable[Path],
    reservations: Iterable[str],
    lookup: Callable[[set[str], set[str]], tuple[set[str], set[str]]],
) -> dict[str, list[str]]:
    live_domains, live_networks = lookup(domains, networks)
    # lexists includes broken symlinks, which are still residual resources.
    return {
        "domains": sorted(live_domains),
        "networks": sorted(live_networks),
        "overlays": sorted({str(p) for p in overlays if os.path.lexists(p)}),
        "temporary_paths": sorted({str(p) for p in temporary_paths if os.path.lexists(p)}),
        "reservations": sorted(set(reservations)),
    }
