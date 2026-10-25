from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from astinuslab.db.models import JobNetwork, JobVM, LabInstance, LabPackage, PlaygroundSession

pytestmark = pytest.mark.unit


def _package() -> LabPackage:
    return LabPackage(
        id="lp-restart",
        package_id="restart-lab",
        version="1.0.0",
        title="Restart lab",
        creator="Researcher",
        license="CC-BY-4.0",
        reproducibility="unlocked",
        definition_json="{}",
        manifest_json="{}",
        manifest_sha256="a" * 64,
        archive_sha256="b" * 64,
        archive_path="/nonexistent/restart.astinuslab",
        archive_size_bytes=1,
    )


@pytest.mark.parametrize(
    ("instance_state", "session_state"),
    [("creating", "queued"), ("ready", "running"), ("resetting", "running"), ("destroying", "destroying")],
)
def test_startup_sweep_preserves_package_owned_runtime(
    in_memory_session_scope, monkeypatch, tmp_path, instance_state: str, session_state: str
):
    from astinuslab.jobs import startup_recovery
    from astinuslab.playground import cleanup as playground_cleanup

    session_id = f"pg-{instance_state}"
    domain_name = f"astinuslab-pg-{session_id}-target"
    network_name = f"astinuslab-pg-{session_id}-isolated"
    disk_path = tmp_path / "playground" / session_id / "target.qcow2"
    disk_path.parent.mkdir(parents=True)
    disk_path.write_bytes(b"package overlay")

    domain = MagicMock()
    domain.name.return_value = domain_name
    domain.isActive.return_value = True
    network = MagicMock()
    network.name.return_value = network_name
    network.isActive.return_value = True
    connection = MagicMock()
    connection.listAllDomains.return_value = [domain]
    connection.listAllNetworks.return_value = [network]

    monkeypatch.setattr(startup_recovery, "_pg_session_scope", in_memory_session_scope)
    monkeypatch.setattr(playground_cleanup, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(startup_recovery, "_libvirt_open", lambda: connection)
    monkeypatch.setattr(
        startup_recovery,
        "settings",
        SimpleNamespace(
            playground_disk_dir=tmp_path / "playground",
            playground_capture_dir=tmp_path / "captures",
            storage_dir=tmp_path / "storage",
        ),
    )

    with in_memory_session_scope() as session:
        session.add(_package())
        session.add(
            PlaygroundSession(
                id=session_id,
                lab_package_record_id="lp-restart",
                status=session_state,
            )
        )
        session.add(
            JobNetwork(
                id=f"net-{instance_state}",
                session_id=session_id,
                name="isolated",
                subnet_cidr="192.0.2.0/29",
                isolated=True,
                libvirt_net_name=network_name,
                state="active",
            )
        )
        session.add(
            JobVM(
                id=f"vm-{instance_state}",
                session_id=session_id,
                label="target",
                image_id="ubuntu-24.04",
                domain_name=domain_name,
                disk_path=str(disk_path),
                state="running",
            )
        )
        session.add(
            LabInstance(
                id=f"lpi-{instance_state}",
                package_record_id="lp-restart",
                playground_session_id=session_id,
                idempotency_key=instance_state,
                state=instance_state,
            )
        )

    startup_recovery.cleanup_stale_playground_resources()

    domain.destroy.assert_not_called()
    domain.undefine.assert_not_called()
    network.destroy.assert_not_called()
    network.undefine.assert_not_called()
    assert disk_path.read_bytes() == b"package overlay"
    with in_memory_session_scope() as session:
        assert session.get(PlaygroundSession, session_id).status == session_state
        assert session.get(LabInstance, f"lpi-{instance_state}").state == instance_state


def test_startup_sweep_revalidates_package_ownership_after_initial_snapshot(
    in_memory_session_scope, monkeypatch, tmp_path
):
    from astinuslab.jobs import startup_recovery
    from astinuslab.playground import cleanup as playground_cleanup

    session_id = "pg-aabbccddeeff"
    domain_name = f"astinuslab-pg-{session_id}-target"
    network_name = f"astinuslab-pg-{session_id}-isolated"
    disk_dir = tmp_path / "playground" / session_id
    disk_path = disk_dir / "target.qcow2"
    tmp_dir = tmp_path / "storage" / "tmp" / session_id
    tmp_path_marker = tmp_dir / "package.tmp"
    disk_dir.mkdir(parents=True)
    tmp_dir.mkdir(parents=True)
    disk_path.write_bytes(b"package overlay")
    tmp_path_marker.write_bytes(b"package temp")

    domain = MagicMock()
    domain.name.return_value = domain_name
    domain.isActive.return_value = True
    network = MagicMock()
    network.name.return_value = network_name
    network.isActive.return_value = True
    connection = MagicMock()
    connection.listAllNetworks.return_value = [network]

    monkeypatch.setattr(startup_recovery, "_pg_session_scope", in_memory_session_scope)
    monkeypatch.setattr(playground_cleanup, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(startup_recovery, "_libvirt_open", lambda: connection)
    monkeypatch.setattr(
        startup_recovery,
        "settings",
        SimpleNamespace(
            playground_disk_dir=tmp_path / "playground",
            playground_capture_dir=tmp_path / "captures",
            storage_dir=tmp_path / "storage",
        ),
    )

    with in_memory_session_scope() as session:
        session.add(_package())

    activated = False

    def activate_package_session():
        nonlocal activated
        if not activated:
            with in_memory_session_scope() as session:
                session.add(
                    PlaygroundSession(
                        id=session_id,
                        lab_package_record_id="lp-restart",
                        status="running",
                    )
                )
                session.add(
                    JobNetwork(
                        id="net-race",
                        session_id=session_id,
                        name="isolated",
                        subnet_cidr="192.0.2.0/29",
                        isolated=True,
                        libvirt_net_name=network_name,
                        state="active",
                    )
                )
                session.add(
                    JobVM(
                        id="vm-race",
                        session_id=session_id,
                        label="target",
                        image_id="ubuntu-24.04",
                        domain_name=domain_name,
                        disk_path=str(disk_path),
                        state="running",
                    )
                )
                session.add(
                    LabInstance(
                        id="lpi-race",
                        package_record_id="lp-restart",
                        playground_session_id=session_id,
                        idempotency_key="race",
                        state="ready",
                    )
                )
            activated = True
        return [domain]

    connection.listAllDomains.side_effect = activate_package_session

    startup_recovery.cleanup_stale_playground_resources()

    domain.destroy.assert_not_called()
    domain.undefine.assert_not_called()
    network.destroy.assert_not_called()
    network.undefine.assert_not_called()
    assert disk_path.read_bytes() == b"package overlay"
    assert tmp_path_marker.read_bytes() == b"package temp"
    with in_memory_session_scope() as session:
        assert session.get(PlaygroundSession, session_id).status == "running"
        assert session.get(LabInstance, "lpi-race").state == "ready"


def test_startup_sweep_preserves_package_files_and_removes_stale_peer(in_memory_session_scope, monkeypatch, tmp_path):
    from astinuslab.jobs import startup_recovery
    from astinuslab.playground import cleanup as playground_cleanup

    package_session_id = "pg-aabbccddeeff"
    stale_session_id = "pg-deaddeaddead"
    package_domain_name = f"astinuslab-pg-{package_session_id}-target"
    stale_domain_name = f"astinuslab-pg-{stale_session_id}-target"
    package_network_name = f"astinuslab-pg-{package_session_id}-isolated"
    stale_network_name = f"astinuslab-pg-{stale_session_id}-isolated"

    package_disk = tmp_path / "playground" / package_session_id / "target.qcow2"
    stale_disk = tmp_path / "playground" / stale_session_id / "target.qcow2"
    package_tmp = tmp_path / "storage" / "tmp" / package_session_id / "package.tmp"
    stale_tmp = tmp_path / "storage" / "tmp" / stale_session_id / "stale.tmp"
    for path, contents in (
        (package_disk, b"package overlay"),
        (stale_disk, b"stale overlay"),
        (package_tmp, b"package temp"),
        (stale_tmp, b"stale temp"),
    ):
        path.parent.mkdir(parents=True)
        path.write_bytes(contents)

    package_domain = MagicMock()
    package_domain.name.return_value = package_domain_name
    package_domain.isActive.return_value = True
    stale_domain = MagicMock()
    stale_domain.name.return_value = stale_domain_name
    stale_domain.isActive.return_value = True
    package_network = MagicMock()
    package_network.name.return_value = package_network_name
    package_network.isActive.return_value = True
    stale_network = MagicMock()
    stale_network.name.return_value = stale_network_name
    stale_network.isActive.return_value = True
    connection = MagicMock()
    connection.listAllDomains.return_value = [package_domain, stale_domain]
    connection.listAllNetworks.return_value = [package_network, stale_network]

    monkeypatch.setattr(startup_recovery, "_pg_session_scope", in_memory_session_scope)
    monkeypatch.setattr(playground_cleanup, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(startup_recovery, "_libvirt_open", lambda: connection)
    monkeypatch.setattr(
        startup_recovery,
        "settings",
        SimpleNamespace(
            playground_disk_dir=tmp_path / "playground",
            playground_capture_dir=tmp_path / "captures",
            storage_dir=tmp_path / "storage",
        ),
    )

    with in_memory_session_scope() as session:
        session.add(_package())
        session.add(
            PlaygroundSession(
                id=package_session_id,
                lab_package_record_id="lp-restart",
                status="running",
            )
        )
        session.add(PlaygroundSession(id=stale_session_id, status="failed"))
        session.add_all(
            [
                JobNetwork(
                    id="net-package",
                    session_id=package_session_id,
                    name="isolated",
                    subnet_cidr="192.0.2.0/29",
                    isolated=True,
                    libvirt_net_name=package_network_name,
                    state="active",
                ),
                JobNetwork(
                    id="net-stale",
                    session_id=stale_session_id,
                    name="isolated",
                    subnet_cidr="198.51.100.0/29",
                    isolated=True,
                    libvirt_net_name=stale_network_name,
                    state="active",
                ),
                JobVM(
                    id="vm-package",
                    session_id=package_session_id,
                    label="target",
                    image_id="ubuntu-24.04",
                    domain_name=package_domain_name,
                    disk_path=str(package_disk),
                    state="running",
                ),
                JobVM(
                    id="vm-stale",
                    session_id=stale_session_id,
                    label="target",
                    image_id="ubuntu-24.04",
                    domain_name=stale_domain_name,
                    disk_path=str(stale_disk),
                    state="running",
                ),
                LabInstance(
                    id="lpi-package",
                    package_record_id="lp-restart",
                    playground_session_id=package_session_id,
                    idempotency_key="package",
                    state="ready",
                ),
            ]
        )

    startup_recovery.cleanup_stale_playground_resources()

    package_domain.destroy.assert_not_called()
    package_domain.undefine.assert_not_called()
    package_network.destroy.assert_not_called()
    package_network.undefine.assert_not_called()
    stale_domain.destroy.assert_called_once()
    stale_domain.undefine.assert_called_once()
    stale_network.destroy.assert_called_once()
    stale_network.undefine.assert_called_once()
    assert package_disk.read_bytes() == b"package overlay"
    assert package_tmp.read_bytes() == b"package temp"
    assert not stale_disk.parent.exists()
    assert not stale_tmp.parent.exists()
    with in_memory_session_scope() as session:
        assert session.get(PlaygroundSession, package_session_id).status == "running"
        assert session.get(LabInstance, "lpi-package").state == "ready"


def test_startup_sweep_refreshes_package_prefix_fallback_for_late_session(
    in_memory_session_scope, monkeypatch, tmp_path
):
    from astinuslab.jobs import startup_recovery
    from astinuslab.playground import cleanup as playground_cleanup

    session_id = "pg-bbccddeeffaa"
    domain_name = f"astinuslab-pg-{session_id}-target"
    network_name = f"astinuslab-pg-{session_id}-isolated"

    domain = MagicMock()
    domain.name.return_value = domain_name
    domain.isActive.return_value = True
    network = MagicMock()
    network.name.return_value = network_name
    network.isActive.return_value = True
    connection = MagicMock()
    connection.listAllNetworks.return_value = [network]

    monkeypatch.setattr(startup_recovery, "_pg_session_scope", in_memory_session_scope)
    monkeypatch.setattr(playground_cleanup, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(startup_recovery, "_libvirt_open", lambda: connection)
    monkeypatch.setattr(
        startup_recovery,
        "settings",
        SimpleNamespace(
            playground_disk_dir=tmp_path / "playground",
            playground_capture_dir=tmp_path / "captures",
            storage_dir=tmp_path / "storage",
        ),
    )

    with in_memory_session_scope() as session:
        session.add(_package())

    admitted = False

    def admit_package_session_without_child_rows():
        nonlocal admitted
        if not admitted:
            with in_memory_session_scope() as session:
                session.add(
                    PlaygroundSession(
                        id=session_id,
                        lab_package_record_id="lp-restart",
                        status="running",
                    )
                )
                session.add(
                    LabInstance(
                        id="lpi-prefix-race",
                        package_record_id="lp-restart",
                        playground_session_id=session_id,
                        idempotency_key="prefix-race",
                        state="creating",
                    )
                )
            admitted = True
        return [domain]

    connection.listAllDomains.side_effect = admit_package_session_without_child_rows

    startup_recovery.cleanup_stale_playground_resources()

    domain.destroy.assert_not_called()
    domain.undefine.assert_not_called()
    network.destroy.assert_not_called()
    network.undefine.assert_not_called()
    with in_memory_session_scope() as session:
        assert session.get(PlaygroundSession, session_id).status == "running"
        assert session.get(LabInstance, "lpi-prefix-race").state == "creating"
