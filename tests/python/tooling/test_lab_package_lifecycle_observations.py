"""Hermetic checks for the real-host observation projection; never native evidence."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace as Row

import pytest

import tests.python._support.lab_package_observations as observations

pytestmark = pytest.mark.unit


def _rows(prefix):
    networks = [
        Row(id=prefix + "b", name="second", subnet_cidr="198.51.100.0/29", isolated=True),
        Row(id=prefix + "a", name="first", subnet_cidr="192.0.2.0/29", isolated=True),
    ]
    vms = [
        Row(label="peer", disk_path="peer.qcow2", nics=[Row(ordinal=0, job_network_id=prefix + "b")]),
        Row(
            label="target",
            disk_path="target.qcow2",
            nics=[Row(ordinal=1, job_network_id=prefix + "a"), Row(ordinal=0, job_network_id=prefix + "b")],
        ),
    ]
    return vms, networks


def test_logical_projection_ignores_database_order_and_ids():
    vms, networks = _rows("host-a-")
    second_vms, second_networks = _rows("host-b-")
    actual = observations.logical_topology(vms, networks)
    assert actual == observations.logical_topology(list(reversed(second_vms)), list(reversed(second_networks)))
    assert actual["nics"] == [
        {"vm_id": "peer", "network_id": "second", "ordinal": 0},
        {"vm_id": "target", "network_id": "second", "ordinal": 0},
        {"vm_id": "target", "network_id": "first", "ordinal": 1},
    ]


@pytest.mark.parametrize(
    "damage", ["missing_network", "duplicate_vm", "duplicate_network", "duplicate_ordinal", "ordinal_gap"]
)
def test_ambiguous_or_missing_mapping_refuses_evidence(damage):
    vms, networks = _rows("a")
    if damage == "missing_network":
        networks.pop(0)
    elif damage == "duplicate_vm":
        vms.append(deepcopy(vms[0]))
    elif damage == "duplicate_network":
        networks.append(deepcopy(networks[0]))
    elif damage == "duplicate_ordinal":
        vms[0].nics.append(deepcopy(vms[0].nics[0]))
    else:
        vms[0].nics[0].ordinal = 1
    with pytest.raises(AssertionError):
        observations.logical_topology(vms, networks)


def test_residue_checks_both_session_snapshots_and_dangling_paths(tmp_path):
    old = tmp_path / "old.qcow2"
    new = tmp_path / "reset.qcow2"
    new.symlink_to(tmp_path / "missing")
    residue = observations.resource_residue(
        domains={"old-domain", "reset-domain"},
        networks={"old-net", "reset-net"},
        overlays=[old, new],
        temporary_paths=[tmp_path / "reset.seed"],
        reservations=["reset-session"],
        lookup=lambda domains, networks: (domains & {"reset-domain"}, networks & {"reset-net"}),
    )
    assert residue == {
        "domains": ["reset-domain"],
        "networks": ["reset-net"],
        "overlays": [str(new)],
        "temporary_paths": [],
        "reservations": ["reset-session"],
    }


def test_checkpoint_defaults_are_evaluated_without_changing_declared_evidence():
    from astinuslab.lab_packages.models import AssetReference, LabExperience
    from tests.python._support.lab_packages import prepare_definition

    definition = prepare_definition()
    layer = AssetReference(sha256="a" * 64, size_bytes=1, media_type="application/x-qcow2", license="CC-BY-4.0")
    operation = {"operation": "observe", "scope": "guest", "vm_id": "target", "path": "/etc/hostname"}
    definition = definition.model_copy(
        update={
            "experience": LabExperience(checkpoints=(operation,)),
            "vms": (definition.vms[0].model_copy(update={"boot_layer": layer}),),
        }
    )
    networks = [Row(id="db-id", name="isolated", subnet_cidr="192.0.2.0/29", isolated=True)]
    vms = [Row(label=vm.id, disk_path=vm.id, nics=[Row(ordinal=0, job_network_id="db-id")]) for vm in definition.vms]

    def evaluate(operation):
        return operation.timeout_seconds == 30 and operation.vm_id == "target"

    phase = observations.observe_phase(
        definition,
        vms,
        networks,
        backing_observer=lambda path, digest, size: (digest, size),
        readiness_checker=lambda vm, probes: True,
        checkpoint_evaluator=evaluate,
    )
    assert phase["checkpoints"] == [{"operation": operation, "passed": True}]


@pytest.mark.parametrize("damage", [None, "disk", "nic", "subnet", "forwarding"])
def test_live_libvirt_wiring_must_match_database_observations(damage):
    vm = Row(
        label="target",
        domain_name="vm-domain",
        disk_path="/owned/target.qcow2",
        nics=[Row(ordinal=0, job_network_id="db-net")],
    )
    network = Row(
        id="db-net", name="isolated", libvirt_net_name="native-network", subnet_cidr="192.0.2.0/29", isolated=True
    )
    disk = "/different.qcow2" if damage == "disk" else "/owned/target.qcow2"
    nic = "wrong-network" if damage == "nic" else "native-network"
    ip = "198.51.100.1" if damage == "subnet" else "192.0.2.1"
    forwarding = '<forward mode="nat"/>' if damage == "forwarding" else ""
    domain_xml = f'<domain><devices><disk device="disk"><source file="{disk}"/></disk><interface type="network"><source network="{nic}"/></interface></devices></domain>'
    network_xml = f'<network>{forwarding}<ip address="{ip}" netmask="255.255.255.248"/></network>'
    if damage:
        with pytest.raises(AssertionError):
            observations.assert_native_wiring(
                [vm], [network], {"vm-domain": domain_xml}, {"native-network": network_xml}
            )
    else:
        observations.assert_native_wiring([vm], [network], {"vm-domain": domain_xml}, {"native-network": network_xml})
