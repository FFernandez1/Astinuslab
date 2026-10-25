from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from astinuslab.db import Base
from astinuslab.db.models import JobNetwork, JobVM, JobVMNic, PlaygroundSession, PlaygroundTopologyLayout
from astinuslab.lab_packages.canonical import canonical_bytes
from astinuslab.lab_packages.models import HostResources, LabExperience, ResourceRecommendations
from astinuslab.lab_packages.playground_adapter import definition_from_session


@pytest.fixture
def resources() -> ResourceRecommendations:
    return ResourceRecommendations(
        minimum=HostResources(cpu_threads=2, memory_mb=1024, free_disk_bytes=10_000),
        recommended=HostResources(cpu_threads=4, memory_mb=2048, free_disk_bytes=20_000),
    )


def _session_with_lab(*, suffix: str, reverse_insertion: bool = False, colliding_row_ids: bool = False):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    session_id = f"pg-runtime-{suffix}"
    outside_row_id = f"db-shared-{suffix}" if colliding_row_ids else f"db-net-z-{suffix}"
    attacker_row_id = f"db-shared-{suffix}" if colliding_row_ids else f"db-vm-a-{suffix}"
    network_rows = [
        JobNetwork(
            id=outside_row_id,
            session_id=session_id,
            name="outside",
            subnet_cidr="10.42.2.0/24",
            isolated=False,
            libvirt_net_name=f"libvirt-net-outside-{suffix}",
            state="active",
        ),
        JobNetwork(
            id=f"db-net-a-{suffix}",
            session_id=session_id,
            name="inside",
            subnet_cidr="10.42.1.0/24",
            isolated=True,
            libvirt_net_name=f"libvirt-net-inside-{suffix}",
            state="active",
        ),
    ]
    vm_rows = [
        JobVM(
            id=f"db-vm-z-{suffix}",
            session_id=session_id,
            label="web",
            image_id="ubuntu-24.04",
            workload_id="dvwa",
            role="target",
            state="running",
            domain_name=f"libvirt-domain-web-{suffix}",
            disk_path=f"/runtime/{suffix}/web.qcow2",
            vcpus=2,
            memory_mb=1024,
            disk_gb=12,
        ),
        JobVM(
            id=attacker_row_id,
            session_id=session_id,
            label="attacker",
            image_id="kali-current",
            role="attacker",
            state="running",
            domain_name=f"libvirt-domain-attacker-{suffix}",
            disk_path=f"/runtime/{suffix}/attacker.qcow2",
            vcpus=4,
            memory_mb=2048,
            disk_gb=24,
        ),
    ]
    session.add(PlaygroundSession(id=session_id, status="running"))
    rows = [*network_rows, *vm_rows]
    session.add_all(list(reversed(rows)) if reverse_insertion else rows)
    session.flush()
    outside, inside = network_rows
    web, attacker = vm_rows
    session.add_all(
        [
            JobVMNic(
                id=f"db-nic-1-{suffix}",
                job_vm_id=web.id,
                job_network_id=inside.id,
                mac="52:54:00:aa:bb:01",
                ip="10.42.1.10",
                ordinal=0,
            ),
            JobVMNic(
                id=f"db-nic-2-{suffix}",
                job_vm_id=attacker.id,
                job_network_id=outside.id,
                mac="52:54:00:aa:bb:02",
                ip="10.42.2.10",
                ordinal=0,
            ),
            PlaygroundTopologyLayout(
                session_id=session_id,
                node_id=web.id,
                node_type="vm",
                x=300,
                y=100,
            ),
            PlaygroundTopologyLayout(
                session_id=session_id,
                node_id=inside.id,
                node_type="network",
                x=100,
                y=100,
            ),
        ]
    )
    session.commit()
    return session, session_id


def _definition(session, session_id: str, resources: ResourceRecommendations):
    return definition_from_session(
        session,
        session_id=session_id,
        package_id="org.example.playground",
        version="1.0.0",
        title="Portable Playground",
        description="Exported from a live session",
        creator="Example Creator",
        license="CC-BY-4.0",
        resources=resources,
        experience=LabExperience(
            objectives=("Find the service",),
            checkpoints=({"id": "service-found", "required": True},),
        ),
    )


@pytest.mark.parametrize("node_type", ["vm", "network"])
@pytest.mark.parametrize("exporter", ["recipe", "package"])
def test_exports_omit_destroyed_positioned_resources(resources, node_type, exporter):
    from astinuslab.playground.recipes import export_recipe, validate_recipe

    session, session_id = _session_with_lab(suffix="destroyed-layout")
    try:
        removed: JobNetwork | JobVM
        if node_type == "network":
            removed = JobNetwork(
                id="removed-network",
                session_id=session_id,
                name="removed",
                subnet_cidr="10.42.3.0/24",
                isolated=True,
                libvirt_net_name="removed-network",
                state="active",
            )
        else:
            removed = JobVM(
                id="removed-vm",
                session_id=session_id,
                label="removed",
                image_id="ubuntu-24.04",
                role="target",
                domain_name="removed-vm",
                disk_path="/runtime/removed.qcow2",
                state="running",
            )
        session.add(removed)
        session.flush()
        layout = PlaygroundTopologyLayout(
            session_id=session_id,
            node_id=removed.id,
            node_type=node_type,
            x=500,
            y=200,
        )
        session.add(layout)
        session.commit()
        removed.state = "destroyed"
        session.commit()

        if exporter == "recipe":
            result = export_recipe(session, session_id)
            validate_recipe(result)
        else:
            result = _definition(session, session_id, resources)
        assert {(node.node_type, node.node_id, node.x, node.y) for node in result.topology} == {
            ("vm", "web", 300, 100),
            ("network", "inside", 100, 100),
        }
        session.refresh(layout)
        assert layout.node_id == removed.id  # Historical layout is retained.
    finally:
        session.close()


def test_adapter_uses_logical_ids_effective_resources_and_no_runtime_identifiers(resources):
    session, session_id = _session_with_lab(suffix="one")
    try:
        definition = _definition(session, session_id, resources)
    finally:
        session.close()

    assert definition.reproducibility == "unlocked"
    assert [network.id for network in definition.networks] == ["inside", "outside"]
    assert [(network.id, network.reachability, network.guest_egress) for network in definition.networks] == [
        ("inside", "isolated", False),
        ("outside", "nat", True),
    ]
    assert [vm.id for vm in definition.vms] == ["attacker", "web"]
    assert [(vm.id, vm.vcpus, vm.memory_mb, vm.disk_gb) for vm in definition.vms] == [
        ("attacker", 4, 2048, 24),
        ("web", 2, 1024, 12),
    ]
    assert definition.vms[0].network_ids == ("outside",)
    assert definition.vms[1].network_ids == ("inside",)
    assert all(vm.clean_layer is None for vm in definition.vms)
    assert [(node.node_id, node.node_type) for node in definition.topology] == [
        ("inside", "network"),
        ("web", "vm"),
    ]

    payload = canonical_bytes(definition).decode("utf-8")
    for runtime_identifier in (
        session_id,
        "db-net-z-one",
        "db-net-a-one",
        "db-vm-z-one",
        "db-vm-a-one",
        "db-nic-1-one",
        "db-nic-2-one",
        "libvirt-net-outside-one",
        "libvirt-net-inside-one",
        "libvirt-domain-web-one",
        "libvirt-domain-attacker-one",
        "52:54:00:aa:bb:01",
        "52:54:00:aa:bb:02",
        "/runtime/one",
        "running",
        "active",
    ):
        assert runtime_identifier not in payload


def test_adapter_is_canonical_stable_across_persistence_identity_and_row_order(resources):
    first_session, first_id = _session_with_lab(suffix="first")
    second_session, second_id = _session_with_lab(suffix="second", reverse_insertion=True)
    try:
        first = _definition(first_session, first_id, resources)
        second = _definition(second_session, second_id, resources)
    finally:
        first_session.close()
        second_session.close()

    assert canonical_bytes(first) == canonical_bytes(second)


def test_adapter_fails_clearly_when_effective_vm_resources_are_absent(resources):
    session, session_id = _session_with_lab(suffix="missing-resource")
    session.query(JobVM).filter_by(label="web").one().memory_mb = None
    session.commit()
    try:
        with pytest.raises(ValueError, match="VM web is missing effective resources: memory_mb"):
            _definition(session, session_id, resources)
    finally:
        session.close()


def test_adapter_resolves_a_missing_persisted_disk_size_from_its_source_image(resources, monkeypatch):
    from astinuslab.lab_packages import playground_adapter

    session, session_id = _session_with_lab(suffix="resolved-disk")
    session.query(JobVM).filter_by(label="web").one().disk_gb = None
    session.commit()
    monkeypatch.setattr(
        playground_adapter.playground_orchestrator,
        "lookup_playground_image",
        lambda image_id, *, role: {"id": image_id, "disk_gb": 20},
    )
    monkeypatch.setattr(
        playground_adapter.playground_orchestrator,
        "resolve_vm_resources",
        lambda role, image, **kwargs: (kwargs["vcpus"], kwargs["memory_mb"], 20),
    )
    try:
        definition = _definition(session, session_id, resources)
    finally:
        session.close()

    assert next(vm for vm in definition.vms if vm.id == "web").disk_gb == 20


def test_adapter_derives_missing_disk_size_from_the_backing_image_virtual_size(resources, monkeypatch):
    from astinuslab.lab_packages import playground_adapter

    session, session_id = _session_with_lab(suffix="backing-virtual-size")
    session.query(JobVM).filter_by(label="web").one().disk_gb = None
    session.commit()
    monkeypatch.setattr(
        playground_adapter.playground_orchestrator,
        "lookup_playground_image",
        lambda image_id, *, role: {"id": image_id, "path": "/images/ubuntu.qcow2"},
    )
    monkeypatch.setattr(
        playground_adapter.playground_orchestrator,
        "resolve_vm_resources",
        lambda role, image, **kwargs: (kwargs["vcpus"], kwargs["memory_mb"], None),
    )
    monkeypatch.setattr(playground_adapter, "backing_virtual_size_gb", lambda **kwargs: 37, raising=False)
    try:
        definition = _definition(session, session_id, resources)
    finally:
        session.close()

    assert next(vm for vm in definition.vms if vm.id == "web").disk_gb == 37


@pytest.mark.parametrize(
    ("label", "role", "expected_disk_gb"),
    [("web", "target", 31), ("attacker", "attacker", 23), ("attacker", "network", 7)],
)
def test_adapter_resolves_missing_disk_size_for_each_playground_role(
    resources, monkeypatch, label, role, expected_disk_gb
):
    from astinuslab.lab_packages import playground_adapter

    session, session_id = _session_with_lab(suffix=f"disk-{role}")
    vm = session.query(JobVM).filter_by(label=label).one()
    vm.role = role
    vm.disk_gb = None
    session.commit()
    monkeypatch.setattr(
        playground_adapter.playground_orchestrator,
        "lookup_playground_image",
        lambda image_id, *, role: {"id": image_id, "path": f"/images/{image_id}.qcow2", "defaults": {}},
    )
    monkeypatch.setattr(playground_adapter, "backing_virtual_size_gb", lambda **kwargs: expected_disk_gb, raising=False)
    monkeypatch.setattr(
        playground_adapter.playground_orchestrator,
        "resolve_vm_resources",
        lambda role, image, **kwargs: (kwargs["vcpus"], kwargs["memory_mb"], None),
    )
    try:
        definition = _definition(session, session_id, resources)
    finally:
        session.close()

    assert next(vm for vm in definition.vms if vm.id == label).disk_gb == expected_disk_gb


def test_adapter_rejects_missing_disk_when_backing_virtual_size_is_undeterminable(resources, monkeypatch):
    from astinuslab.lab_packages import playground_adapter

    session, session_id = _session_with_lab(suffix="undeterminable-disk")
    session.query(JobVM).filter_by(label="web").one().disk_gb = None
    session.commit()
    monkeypatch.setattr(
        playground_adapter.playground_orchestrator,
        "lookup_playground_image",
        lambda image_id, *, role: {"id": image_id, "path": "/images/ubuntu.qcow2"},
    )
    monkeypatch.setattr(
        playground_adapter.playground_orchestrator,
        "resolve_vm_resources",
        lambda role, image, **kwargs: (kwargs["vcpus"], kwargs["memory_mb"], None),
    )
    monkeypatch.setattr(playground_adapter, "backing_virtual_size_gb", lambda **kwargs: None, raising=False)
    try:
        with pytest.raises(ValueError, match="VM web source image virtual size is undeterminable: ubuntu-24.04"):
            _definition(session, session_id, resources)
    finally:
        session.close()


def test_adapter_missing_disk_fails_only_when_the_source_image_is_unavailable(resources, monkeypatch):
    from astinuslab.lab_packages import playground_adapter

    session, session_id = _session_with_lab(suffix="unavailable-disk")
    session.query(JobVM).filter_by(label="web").one().disk_gb = None
    session.commit()
    monkeypatch.setattr(
        playground_adapter.playground_orchestrator,
        "lookup_playground_image",
        lambda image_id, *, role: None,
    )
    try:
        with pytest.raises(ValueError, match="VM web source image is unavailable: ubuntu-24.04"):
            _definition(session, session_id, resources)
    finally:
        session.close()


def test_adapter_preserves_persisted_nic_order_instead_of_sorting_network_names(resources):
    session, session_id = _session_with_lab(suffix="nic-order")
    web = session.query(JobVM).filter_by(label="web").one()
    outside = session.query(JobNetwork).filter_by(name="outside").one()
    session.add(
        JobVMNic(
            id="db-nic-order-second",
            job_vm_id=web.id,
            job_network_id=outside.id,
            mac="52:54:00:aa:bb:03",
            ordinal=1,
        )
    )
    session.commit()
    try:
        definition = _definition(session, session_id, resources)
    finally:
        session.close()

    assert next(vm for vm in definition.vms if vm.id == "web").network_ids == ("inside", "outside")


def test_adapter_fails_clearly_when_topology_reference_cannot_be_mapped(resources):
    session, session_id = _session_with_lab(suffix="missing-topology")
    session.add(
        PlaygroundTopologyLayout(
            session_id=session_id,
            node_id="missing-db-row",
            node_type="vm",
            x=0,
            y=0,
        )
    )
    session.commit()
    try:
        with pytest.raises(ValueError, match="topology reference cannot be mapped: missing-db-row"):
            _definition(session, session_id, resources)
    finally:
        session.close()


def test_adapter_rejects_topology_reference_whose_type_disagrees_with_source_table(resources):
    session, session_id = _session_with_lab(suffix="topology-type")
    attacker = session.query(JobVM).filter_by(label="attacker").one()
    outside = session.query(JobNetwork).filter_by(name="outside").one()
    outside.name = "attacker"
    session.add(
        PlaygroundTopologyLayout(
            session_id=session_id,
            node_id=attacker.id,
            node_type="network",
            x=0,
            y=0,
        )
    )
    session.commit()
    try:
        with pytest.raises(ValueError, match=f"topology network reference cannot be mapped: {attacker.id}"):
            _definition(session, session_id, resources)
    finally:
        session.close()


def test_adapter_rejects_inverse_topology_table_type_mismatch(resources):
    session, session_id = _session_with_lab(suffix="topology-type-inverse")
    inside = session.query(JobNetwork).filter_by(name="inside").one()
    web = session.query(JobVM).filter_by(label="web").one()
    web.label = "inside"
    network_layout = session.query(PlaygroundTopologyLayout).filter_by(node_id=inside.id).one()
    network_layout.node_type = "vm"
    session.delete(session.query(PlaygroundTopologyLayout).filter_by(node_id=web.id).one())
    session.commit()
    try:
        with pytest.raises(ValueError, match=f"topology vm reference cannot be mapped: {inside.id}"):
            _definition(session, session_id, resources)
    finally:
        session.close()


def test_adapter_rejects_colliding_persisted_vm_and_network_row_ids(resources):
    session, session_id = _session_with_lab(suffix="identity-collision", colliding_row_ids=True)
    try:
        with pytest.raises(
            ValueError,
            match="persisted VM and network IDs collide: db-shared-identity-collision",
        ):
            _definition(session, session_id, resources)
    finally:
        session.close()


def test_adapter_output_is_json_serializable_without_none_values(resources):
    session, session_id = _session_with_lab(suffix="json")
    try:
        payload = json.loads(canonical_bytes(_definition(session, session_id, resources)))
    finally:
        session.close()

    assert payload["reproducibility"] == "unlocked"
    assert all("clean_layer" not in vm for vm in payload["vms"])
