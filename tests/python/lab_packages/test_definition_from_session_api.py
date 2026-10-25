from __future__ import annotations

import pytest

from astinuslab.db.models import (
    JobNetwork,
    JobVM,
    JobVMNic,
    PlaygroundSession,
    PlaygroundTopologyLayout,
)
from astinuslab.lab_packages.models import LabDefinition

_METADATA = {
    "package_id": "org.example.derived",
    "version": "1.0.0",
    "title": "Derived lab",
    "description": "Generated from the authored session",
    "creator": "Example Creator",
    "license": "CC-BY-4.0",
}


@pytest.fixture()
def authored_session(in_memory_session_scope) -> str:
    """Seed one active Playground session the creator could have built visually."""

    session_id = "pg-authored"
    with in_memory_session_scope() as session:
        session.add(PlaygroundSession(id=session_id, status="running"))
        network = JobNetwork(
            id="db-net-inside",
            session_id=session_id,
            name="inside",
            subnet_cidr="10.42.1.0/24",
            isolated=True,
            libvirt_net_name="libvirt-net-inside",
            state="active",
        )
        vm = JobVM(
            id="db-vm-web",
            session_id=session_id,
            label="web",
            image_id="ubuntu-24.04",
            workload_id="dvwa",
            role="target",
            state="running",
            domain_name="libvirt-domain-web",
            disk_path="/runtime/web.qcow2",
            vcpus=2,
            memory_mb=1024,
            disk_gb=12,
        )
        session.add_all([network, vm])
        session.flush()
        session.add_all(
            [
                JobVMNic(
                    id="db-nic-1",
                    job_vm_id=vm.id,
                    job_network_id=network.id,
                    mac="52:54:00:aa:bb:01",
                    ip="10.42.1.10",
                    ordinal=0,
                ),
                PlaygroundTopologyLayout(session_id=session_id, node_id=vm.id, node_type="vm", x=300, y=100),
                PlaygroundTopologyLayout(session_id=session_id, node_id=network.id, node_type="network", x=100, y=100),
            ]
        )
    return session_id


def test_active_session_yields_a_valid_definition_and_its_source_session_id(package_client, authored_session):
    response = package_client.post("/api/v1/lab-packages/definitions/from-session", json=dict(_METADATA))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source_session_id"] == authored_session
    assert body["resources_derived"] is True
    # The response body must be the canonical schema the creator submits back,
    # not an approximation that only looks like one.
    definition = LabDefinition.model_validate(body["definition"])
    assert definition.format_version == 1
    assert definition.reproducibility == "unlocked"
    assert definition.package_id == _METADATA["package_id"]
    assert definition.title == _METADATA["title"]
    assert [vm.id for vm in definition.vms] == ["web"]
    assert [network.id for network in definition.networks] == ["inside"]
    assert sorted(node.node_id for node in definition.topology) == ["inside", "web"]
    # Derived sizing is admissible for the VMs the session declares.
    assert definition.resources.minimum.cpu_threads == 2
    assert definition.resources.minimum.memory_mb == 1024
    assert definition.resources.recommended.free_disk_bytes >= definition.resources.minimum.free_disk_bytes


def test_generated_definition_is_exportable_as_a_package_archive(package_client, authored_session):
    from astinuslab.lab_packages.archive import inspect_archive, write_archive

    response = package_client.post(
        "/api/v1/lab-packages/definitions/from-session",
        json={**_METADATA, "session_id": authored_session},
    )
    assert response.status_code == 200, response.text

    definition = LabDefinition.model_validate(response.json()["definition"])
    inspected = inspect_archive(write_archive(definition, "# Runbook\n"))
    assert inspected.definition == definition


def test_supplied_resources_are_used_verbatim(package_client, authored_session):
    resources = {
        "minimum": {"cpu_threads": 4, "memory_mb": 8192, "free_disk_bytes": 40 * 1024**3},
        "recommended": {"cpu_threads": 8, "memory_mb": 16384, "free_disk_bytes": 80 * 1024**3},
    }

    response = package_client.post(
        "/api/v1/lab-packages/definitions/from-session",
        json={**_METADATA, "resources": resources},
    )

    assert response.status_code == 200, response.text
    assert response.json()["resources_derived"] is False
    assert response.json()["definition"]["resources"] == resources


def test_definition_handoff_performs_no_destructive_lifecycle_operation(
    package_client, authored_session, in_memory_session_scope, monkeypatch
):
    from astinuslab.playground import repositories as playground_repositories

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("definition handoff must not change session lifecycle state")

    for name in (
        "mark_session_destroying",
        "mark_session_destroy_requested",
        "mark_session_destroyed",
        "finalize_session",
    ):
        if hasattr(playground_repositories, name):
            monkeypatch.setattr(playground_repositories, name, _forbidden)

    response = package_client.post("/api/v1/lab-packages/definitions/from-session", json=dict(_METADATA))
    assert response.status_code == 200, response.text

    with in_memory_session_scope() as session:
        playground_session = session.get(PlaygroundSession, authored_session)
        assert playground_session is not None
        assert playground_session.status == "running"
        assert playground_session.finished_at is None
        assert [(vm.label, vm.state) for vm in session.query(JobVM).all()] == [("web", "running")]
        assert [(net.name, net.state) for net in session.query(JobNetwork).all()] == [("inside", "active")]
        assert session.query(JobVMNic).count() == 1
        assert session.query(PlaygroundTopologyLayout).count() == 2

    # The session is still authorable, so the same handoff repeats identically.
    repeated = package_client.post("/api/v1/lab-packages/definitions/from-session", json=dict(_METADATA))
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == response.json()


def test_unknown_session_is_rejected_without_falling_back_to_another_session(package_client, authored_session):
    response = package_client.post(
        "/api/v1/lab-packages/definitions/from-session",
        json={**_METADATA, "session_id": "pg-missing"},
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "session_not_found"


def test_absent_active_session_reports_no_active_session(package_client):
    response = package_client.post("/api/v1/lab-packages/definitions/from-session", json=dict(_METADATA))

    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "no_active_session"


def test_invalid_package_identity_is_rejected_before_reading_session_state(package_client, authored_session):
    response = package_client.post(
        "/api/v1/lab-packages/definitions/from-session",
        json={**_METADATA, "package_id": "Not A Package Id"},
    )

    assert response.status_code == 422, response.text


def test_session_without_portable_state_reports_a_definition_error(package_client, in_memory_session_scope):
    with in_memory_session_scope() as session:
        session.add(PlaygroundSession(id="pg-empty", status="running"))

    response = package_client.post(
        "/api/v1/lab-packages/definitions/from-session",
        json={**_METADATA, "session_id": "pg-empty"},
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "definition_not_derivable"
