from __future__ import annotations

import pytest

from astinuslab.db import SessionLocal
from astinuslab.jobs.celery_worker import celery_app
from astinuslab.playground import repositories as pr
from astinuslab.playground.resource_custody import LostResourceCustody
from tests.python._support.helpers import autospec_setattr, configure_csrf_client, isolated_playground_database


@pytest.fixture(autouse=True)
def _eager(monkeypatch, tmp_path):
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    with isolated_playground_database(monkeypatch, tmp_path, globals()):
        yield


def _seed_session_vm_network(session_id: str = "pg-1", vm_id: str = "v1", network_id: str = "n1") -> None:
    from astinuslab.playground.orchestrator import create_network, create_session

    create_session.delay(job_id=session_id).get()
    create_network.delay(
        job_id=session_id,
        network_id=network_id,
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
    ).get()
    with SessionLocal() as s:
        pr.register_vm(
            s,
            vm_id=vm_id,
            job_id=session_id,
            label="web-1",
            image_id="img",
            domain_name=f"astinuslab-pg-{session_id}-web-1",
            disk_path="/tmp/disk.qcow2",
        )
        pr.update_vm_state(s, vm_id, "running")
        s.commit()


def _fake_seeded_host_network(network_id: str = "n1"):
    from unittest.mock import MagicMock

    from astinuslab.db.models import JobNetwork

    with SessionLocal() as s:
        network = s.get(JobNetwork, network_id)
        assert network is not None
        name = network.libvirt_net_name
    network = MagicMock()
    network.name.return_value = name
    network.isActive.return_value = True
    return network


def test_destroy_vm_deletes_nic_rows(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from astinuslab.db.models import JobVMNic
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import destroy_vm, spawn_vm

    fake_lifecycle = MagicMock()
    fake_lifecycle.get_ip.return_value = "10.42.1.10"
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    base_image = tmp_path / "base.qcow2"
    base_image.write_bytes(b"placeholder image")
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda _, *, role="target": {
            "id": "img",
            "path": str(base_image),
            "format": "qcow2",
            "architecture": "x86_64",
            "credentials": {"username": "ubuntu", "password": "ubuntu"},
        },
    )
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(
            playground_disk_dir=tmp_path / "playground",
            playground_default_vcpus=2,
            playground_default_memory_mb=2048,
            libvirt_uri="qemu:///system",
            base_dir=tmp_path,
            data_dir=tmp_path,
        ),
    )

    from astinuslab.playground.orchestrator import create_network, create_session

    create_session.delay(job_id="pg-1").get()
    create_network.delay(job_id="pg-1", network_id="n1", name="ab", subnet_cidr="10.42.1.0/24", isolated=True).get()
    spawn_vm.delay(
        job_id="pg-1",
        vm_id="v1",
        label="web-1",
        image_id="img",
        nic_specs=[{"network_id": "n1", "nic_id": "nic1"}],
    ).get()

    with SessionLocal() as s:
        assert s.query(JobVMNic).filter_by(job_vm_id="v1").count() == 1

    destroy_vm.delay(vm_id="v1").get()

    with SessionLocal() as s:
        assert s.query(JobVMNic).filter_by(job_vm_id="v1").count() == 0


def test_spawn_vm_boot_failure_purges_nic_rows(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from astinuslab.db.models import JobVMNic
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import (
        create_network,
        create_session,
        spawn_vm,
    )

    fake_lifecycle = MagicMock()
    fake_lifecycle.boot_playground.side_effect = RuntimeError("boot bombed")
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    base_image = tmp_path / "base.qcow2"
    base_image.write_bytes(b"placeholder image")
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda _, *, role="target": {
            "id": "img",
            "path": str(base_image),
            "format": "qcow2",
            "architecture": "x86_64",
            "credentials": {"username": "ubuntu", "password": "ubuntu"},
        },
    )
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(
            playground_disk_dir=tmp_path / "playground",
            playground_default_vcpus=2,
            playground_default_memory_mb=2048,
            libvirt_uri="qemu:///system",
            base_dir=tmp_path,
            data_dir=tmp_path,
            storage_dir=tmp_path / "storage",
        ),
    )

    create_session.delay(job_id="pg-1").get()
    create_network.delay(job_id="pg-1", network_id="n1", name="ab", subnet_cidr="10.42.1.0/24", isolated=True).get()

    with pytest.raises(RuntimeError, match="boot bombed"):
        spawn_vm.delay(
            job_id="pg-1",
            vm_id="v1",
            label="web-1",
            image_id="img",
            nic_specs=[{"network_id": "n1", "nic_id": "nic1"}],
        ).get()

    with SessionLocal() as s:
        from astinuslab.db.models import JobVM

        vm = s.query(JobVM).filter_by(id="v1").one()
        assert vm.state == "failed"
        assert s.query(JobVMNic).filter_by(job_vm_id="v1").count() == 0


def test_detach_nic_clears_db_when_libvirt_already_gone(monkeypatch):
    import contextlib
    from unittest.mock import MagicMock

    from astinuslab.db.models import JobVMNic
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    _seed_session_vm_network()
    with SessionLocal() as s:
        pr.attach_nic(s, nic_id="nic1", vm_id="v1", network_id="n1", mac="52:54:00:aa:bb:01")
        s.commit()

    class FakeLibvirtErr(Exception):
        def get_error_code(self):
            return 99

        def get_error_message(self):
            return "no matching network device with mac 52:54:00:aa:bb:01"

    fake_lifecycle = MagicMock()
    fake_lifecycle.detach_nic.side_effect = FakeLibvirtErr()
    fake_lifecycle.is_missing_interface_error.return_value = True
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)

    @contextlib.contextmanager
    def fake_libvirt_conn():
        yield "fake_conn"

    monkeypatch.setattr(po, "_libvirt_conn", fake_libvirt_conn)

    result = po.detach_nic(job_id="pg-1", vm_id="v1", nic_id="nic1")
    assert result == "nic1"

    with SessionLocal() as s:
        assert s.query(JobVMNic).filter_by(id="nic1").one_or_none() is None


def test_detach_nic_restores_active_state_after_host_failure(monkeypatch):
    import contextlib
    from unittest.mock import MagicMock

    from astinuslab.db.models import JobVMNic
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    _seed_session_vm_network()
    with SessionLocal() as s:
        pr.attach_nic(s, nic_id="nic1", vm_id="v1", network_id="n1", mac="52:54:00:aa:bb:01")
        s.commit()

    fake_lifecycle = MagicMock()
    fake_lifecycle.detach_nic.side_effect = RuntimeError("host refused detach")
    fake_lifecycle.is_missing_interface_error.return_value = False
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)

    @contextlib.contextmanager
    def fake_libvirt_conn():
        yield "fake_conn"

    monkeypatch.setattr(po, "_libvirt_conn", fake_libvirt_conn)

    with pytest.raises(RuntimeError, match="host refused detach"):
        po.detach_nic(job_id="pg-1", vm_id="v1", nic_id="nic1")

    with SessionLocal() as s:
        assert s.query(JobVMNic).filter_by(id="nic1").one().state == "active"


def test_reconciler_prunes_nic_rows_on_terminal_vm(monkeypatch):
    from unittest.mock import MagicMock

    from astinuslab.db import session_scope
    from astinuslab.db.models import JobVMNic
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    _seed_session_vm_network()
    with SessionLocal() as s:
        pr.attach_nic(s, nic_id="nic1", vm_id="v1", network_id="n1", mac="52:54:00:aa:bb:01")
        pr.update_vm_state(s, "v1", "destroyed")
        s.commit()

    fake_libvirt = MagicMock()
    fake_conn = MagicMock()
    fake_conn.listAllDomains.return_value = []
    fake_conn.listAllNetworks.return_value = []
    fake_libvirt.open.return_value = fake_conn

    result = reconcile_libvirt_resources(
        session_scope=session_scope,
        libvirt_module=fake_libvirt,
        libvirt_uri="qemu:///system",
        grace_seconds=0,
    )

    assert result.get("orphan_nic_rows_pruned") == 1
    with SessionLocal() as s:
        assert s.query(JobVMNic).filter_by(id="nic1").one_or_none() is None


def test_reconciler_detaches_host_orphan_nics(monkeypatch):
    from unittest.mock import MagicMock

    from astinuslab.db import session_scope
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    _seed_session_vm_network()
    with SessionLocal() as s:
        pr.attach_nic(s, nic_id="nic1", vm_id="v1", network_id="n1", mac="52:54:00:aa:bb:01")
        s.commit()

    domain = MagicMock()
    domain.name.return_value = "astinuslab-pg-pg-1-web-1"
    domain.isActive.return_value = True
    domain.XMLDesc.return_value = """
        <domain>
          <devices>
            <interface type='network'>
              <mac address='52:54:00:aa:bb:01'/>
            </interface>
            <interface type='network'>
              <mac address='52:54:00:DE:AD:99'/>
            </interface>
          </devices>
        </domain>
    """
    fake_libvirt = MagicMock()
    fake_libvirt.VIR_DOMAIN_AFFECT_LIVE = 1
    fake_libvirt.VIR_DOMAIN_AFFECT_CONFIG = 2
    fake_libvirt.libvirtError = type("FakeLibvirtError", (Exception,), {})
    fake_conn = MagicMock()
    fake_conn.listAllDomains.return_value = [domain]
    fake_conn.listAllNetworks.return_value = [_fake_seeded_host_network()]
    fake_conn.lookupByName.return_value = domain
    fake_libvirt.open.return_value = fake_conn

    result = reconcile_libvirt_resources(
        session_scope=session_scope,
        libvirt_module=fake_libvirt,
        libvirt_uri="qemu:///system",
        grace_seconds=0,
    )

    assert result.get("host_orphan_nics_detached") == 1
    domain.detachDeviceFlags.assert_called_once()
    detached_xml = domain.detachDeviceFlags.call_args[0][0]
    assert "52:54:00:DE:AD:99" in detached_xml.lower() or "52:54:00:de:ad:99" in detached_xml


def test_jobvmnic_state_defaults_to_active(monkeypatch):
    from unittest.mock import MagicMock

    from astinuslab.db.models import JobVMNic
    from astinuslab.playground import network_lifecycle as nl

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    _seed_session_vm_network()
    with SessionLocal() as s:
        pr.attach_nic(s, nic_id="nic1", vm_id="v1", network_id="n1", mac="52:54:00:aa:bb:01")
        s.commit()
    with SessionLocal() as s:
        nic = s.query(JobVMNic).filter_by(id="nic1").one()
        assert nic.state == "active"


def test_attach_nic_writes_pending_row_before_libvirt(monkeypatch):
    import contextlib
    from unittest.mock import MagicMock

    from astinuslab.db.models import JobVMNic
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    _seed_session_vm_network()

    observed_states: list[str | None] = []

    fake_lifecycle = MagicMock()

    def record_state(*_a, **_kw):
        with SessionLocal() as s:
            nic = s.query(JobVMNic).filter_by(job_vm_id="v1").one_or_none()
            observed_states.append(nic.state if nic else None)

    fake_lifecycle.attach_nic.side_effect = record_state
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)

    @contextlib.contextmanager
    def fake_libvirt_conn():
        yield "fake_conn"

    monkeypatch.setattr(po, "_libvirt_conn", fake_libvirt_conn)

    res = po.attach_nic(job_id="pg-1", vm_id="v1", network_id="n1")

    assert observed_states == ["attaching"]
    with SessionLocal() as s:
        nic = s.query(JobVMNic).filter_by(id=res["nic_id"]).one()
        assert nic.state == "active"


def test_attach_nic_leaves_pending_row_when_libvirt_fails(monkeypatch):
    import contextlib
    from unittest.mock import MagicMock

    from astinuslab.db.models import JobVMNic
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    _seed_session_vm_network()

    fake_lifecycle = MagicMock()
    fake_lifecycle.attach_nic.side_effect = RuntimeError("libvirt down")
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)

    @contextlib.contextmanager
    def fake_libvirt_conn():
        yield "fake_conn"

    monkeypatch.setattr(po, "_libvirt_conn", fake_libvirt_conn)

    with pytest.raises(RuntimeError, match="libvirt down"):
        po.attach_nic(job_id="pg-1", vm_id="v1", network_id="n1")

    with SessionLocal() as s:
        nic = s.query(JobVMNic).filter_by(job_vm_id="v1").one()
        assert nic.state == "attaching"


def test_attach_nic_cannot_activate_recreated_same_id_row(monkeypatch):
    import contextlib
    from unittest.mock import MagicMock

    from astinuslab.db.models import JobVMNic
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    _seed_session_vm_network()

    replacement_mac = "52:54:00:de:ad:02"
    original_macs: list[str] = []

    def replace_pending_row(_conn, _domain_name, _network_name, original_mac):
        assert original_mac != replacement_mac
        original_macs.append(original_mac)
        with SessionLocal() as s:
            original = s.query(JobVMNic).filter_by(id="nic-reused").one()
            s.delete(original)
            s.commit()
            pr.attach_nic(
                s,
                nic_id="nic-reused",
                vm_id="v1",
                network_id="n1",
                mac=replacement_mac,
                state="pending",
            )
            s.commit()

    fake_lifecycle = MagicMock()
    fake_lifecycle.attach_nic.side_effect = replace_pending_row
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)

    @contextlib.contextmanager
    def fake_libvirt_conn():
        yield "fake_conn"

    monkeypatch.setattr(po, "_libvirt_conn", fake_libvirt_conn)

    with pytest.raises(LostResourceCustody, match="NIC nic-reused lost attaching custody"):
        po.attach_nic(job_id="pg-1", vm_id="v1", network_id="n1", nic_id="nic-reused")

    fake_lifecycle.detach_nic.assert_called_once_with(
        "fake_conn",
        "astinuslab-pg-pg-1-web-1",
        original_macs[0],
    )

    with SessionLocal() as s:
        replacement = s.query(JobVMNic).filter_by(id="nic-reused").one()
        assert replacement.mac == replacement_mac
        assert replacement.state == "pending"


def test_attach_nic_rejects_same_id_bound_to_different_resources_before_host(monkeypatch):
    import contextlib
    from unittest.mock import MagicMock

    from astinuslab.db.models import JobVMNic
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    _seed_session_vm_network()
    po.create_network.delay(
        job_id="pg-1",
        network_id="n2",
        name="secondary",
        subnet_cidr="10.42.2.0/24",
        isolated=True,
    ).get()
    with SessionLocal() as s:
        pr.register_vm(
            s,
            vm_id="v2",
            job_id="pg-1",
            label="db-1",
            image_id="img",
            domain_name="astinuslab-pg-pg-1-db-1",
            disk_path="/tmp/db.qcow2",
        )
        pr.update_vm_state(s, "v2", "running")
        pr.attach_nic(
            s,
            nic_id="nic-collision",
            vm_id="v1",
            network_id="n1",
            mac="52:54:00:aa:bb:01",
            state="pending",
        )
        s.commit()

    fake_lifecycle = MagicMock()
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)

    @contextlib.contextmanager
    def fake_libvirt_conn():
        yield "fake_conn"

    monkeypatch.setattr(po, "_libvirt_conn", fake_libvirt_conn)

    with pytest.raises(LostResourceCustody, match="different VM or network"):
        po.attach_nic(job_id="pg-1", vm_id="v2", network_id="n2", nic_id="nic-collision")

    fake_lifecycle.attach_nic.assert_not_called()
    fake_lifecycle.detach_nic.assert_not_called()
    with SessionLocal() as s:
        original = s.get(JobVMNic, "nic-collision")
        assert original is not None
        assert original.job_vm_id == "v1"
        assert original.job_network_id == "n1"
        assert original.state == "pending"


def test_reconciler_promotes_pending_nic_when_libvirt_has_mac(monkeypatch):
    from unittest.mock import MagicMock

    from astinuslab.db import session_scope
    from astinuslab.db.models import JobVMNic
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    _seed_session_vm_network()
    with SessionLocal() as s:
        pr.attach_nic(
            s,
            nic_id="nic1",
            vm_id="v1",
            network_id="n1",
            mac="52:54:00:aa:bb:01",
            state="pending",
        )
        s.commit()

    domain = MagicMock()
    domain.name.return_value = "astinuslab-pg-pg-1-web-1"
    domain.isActive.return_value = True
    domain.XMLDesc.return_value = """
        <domain><devices>
          <interface type='network'><mac address='52:54:00:aa:bb:01'/></interface>
        </devices></domain>
    """
    fake_libvirt = MagicMock()
    fake_libvirt.VIR_DOMAIN_AFFECT_LIVE = 1
    fake_libvirt.VIR_DOMAIN_AFFECT_CONFIG = 2
    fake_libvirt.libvirtError = type("FakeLibvirtError", (Exception,), {})
    fake_conn = MagicMock()
    fake_conn.listAllDomains.return_value = [domain]
    fake_conn.listAllNetworks.return_value = [_fake_seeded_host_network()]
    fake_libvirt.open.return_value = fake_conn

    result = reconcile_libvirt_resources(
        session_scope=session_scope,
        libvirt_module=fake_libvirt,
        libvirt_uri="qemu:///system",
        grace_seconds=0,
    )

    assert result.get("pending_nics_promoted") == 1
    with SessionLocal() as s:
        assert s.query(JobVMNic).filter_by(id="nic1").one().state == "active"


def test_reconciler_drops_pending_nic_when_libvirt_lacks_mac(monkeypatch):
    from unittest.mock import MagicMock

    from astinuslab.db import session_scope
    from astinuslab.db.models import JobVMNic
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    _seed_session_vm_network()
    with SessionLocal() as s:
        pr.attach_nic(
            s,
            nic_id="nic1",
            vm_id="v1",
            network_id="n1",
            mac="52:54:00:aa:bb:01",
            state="pending",
        )
        s.commit()

    domain = MagicMock()
    domain.name.return_value = "astinuslab-pg-pg-1-web-1"
    domain.isActive.return_value = True
    domain.XMLDesc.return_value = "<domain><devices/></domain>"
    fake_libvirt = MagicMock()
    fake_libvirt.libvirtError = type("FakeLibvirtError", (Exception,), {})
    fake_conn = MagicMock()
    fake_conn.listAllDomains.return_value = [domain]
    fake_conn.listAllNetworks.return_value = [_fake_seeded_host_network()]
    fake_libvirt.open.return_value = fake_conn

    result = reconcile_libvirt_resources(
        session_scope=session_scope,
        libvirt_module=fake_libvirt,
        libvirt_uri="qemu:///system",
        grace_seconds=0,
    )

    assert result.get("pending_nics_dropped") == 1
    with SessionLocal() as s:
        assert s.query(JobVMNic).filter_by(id="nic1").one_or_none() is None


def test_api_attach_nic_with_nic_id_is_idempotent(monkeypatch):
    import contextlib
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from astinuslab.main import app
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    _seed_session_vm_network()

    fake_lifecycle = MagicMock()
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)

    @contextlib.contextmanager
    def fake_libvirt_conn():
        yield "fake_conn"

    monkeypatch.setattr(po, "_libvirt_conn", fake_libvirt_conn)

    client = configure_csrf_client(TestClient(app))
    payload = {"network_id": "n1", "nic_id": "nic-deadbeef"}
    r1 = client.post("/api/v1/playground/sessions/current/vms/v1/nics", json=payload)
    r2 = client.post("/api/v1/playground/sessions/current/vms/v1/nics", json=payload)

    assert r1.status_code == 202, r1.text
    assert r2.status_code == 202, r2.text
    assert fake_lifecycle.attach_nic.call_count == 1


def test_api_attach_nic_rejects_same_id_on_different_network_without_enqueue(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from astinuslab.main import app
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    _seed_session_vm_network()
    po.create_network.delay(
        job_id="pg-1",
        network_id="n2",
        name="secondary",
        subnet_cidr="10.42.2.0/24",
        isolated=True,
    ).get()
    with SessionLocal() as s:
        pr.attach_nic(
            s,
            nic_id="nic-collision",
            vm_id="v1",
            network_id="n1",
            mac="52:54:00:aa:bb:01",
            state="pending",
        )
        s.commit()

    enqueue = MagicMock(return_value=SimpleNamespace(id="task-1"))
    monkeypatch.setattr(po.attach_nic, "delay", enqueue)
    client = configure_csrf_client(TestClient(app))
    response = client.post(
        "/api/v1/playground/sessions/current/vms/v1/nics",
        json={"network_id": "n2", "nic_id": "nic-collision"},
    )

    assert response.status_code == 409, response.text
    enqueue.assert_not_called()


def test_api_attach_nic_rejects_cleanup_bound_session_without_enqueue(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from astinuslab.db.models import PlaygroundSession
    from astinuslab.main import app
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    _seed_session_vm_network()
    with SessionLocal() as s:
        parent = s.get(PlaygroundSession, "pg-1")
        assert parent is not None
        parent.status = "failure_cleanup"
        s.commit()

    enqueue = MagicMock(return_value=SimpleNamespace(id="task-1"))
    monkeypatch.setattr(po.attach_nic, "delay", enqueue)
    client = configure_csrf_client(TestClient(app))
    response = client.post(
        "/api/v1/playground/sessions/current/vms/v1/nics",
        json={"network_id": "n1", "nic_id": "nic-new"},
    )

    assert response.status_code == 404, response.text
    enqueue.assert_not_called()


def test_api_detach_nic_force_clears_db_when_task_fails(monkeypatch):
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from astinuslab.db.models import JobVMNic
    from astinuslab.main import app
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    _seed_session_vm_network()
    with SessionLocal() as s:
        pr.attach_nic(s, nic_id="nic1", vm_id="v1", network_id="n1", mac="52:54:00:aa:bb:01")
        s.commit()

    def boom(*_a, **_kw):
        raise RuntimeError("celery dead")

    monkeypatch.setattr(po.detach_nic, "delay", boom)

    client = configure_csrf_client(TestClient(app))
    response = client.delete("/api/v1/playground/sessions/current/vms/v1/nics/nic1?force=true")

    assert response.status_code == 200, response.text
    with SessionLocal() as s:
        assert s.query(JobVMNic).filter_by(id="nic1").one_or_none() is None


def test_delete_nics_for_vm_removes_all_nic_rows(monkeypatch):
    from unittest.mock import MagicMock

    from astinuslab.db.models import JobVMNic
    from astinuslab.playground import network_lifecycle as nl

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    _seed_session_vm_network()
    with SessionLocal() as s:
        pr.attach_nic(s, nic_id="nic1", vm_id="v1", network_id="n1", mac="52:54:00:aa:bb:01")
        pr.attach_nic(s, nic_id="nic2", vm_id="v1", network_id="n1", mac="52:54:00:aa:bb:02")
        s.commit()

    with SessionLocal() as s:
        deleted = pr.delete_nics_for_vm(s, "v1")
        s.commit()

    assert deleted == 2
    with SessionLocal() as s:
        assert s.query(JobVMNic).filter_by(job_vm_id="v1").count() == 0


@pytest.mark.parametrize("mismatch", [None, "id", "vm_id", "state", "token", "timestamp"])
@pytest.mark.parametrize("null_timestamp", [False, True])
def test_reopen_nic_detach_claim_requires_exact_observation(monkeypatch, mismatch, null_timestamp):
    from datetime import UTC, datetime, timedelta
    from unittest.mock import MagicMock

    from astinuslab.db.models import JobVMNic
    from astinuslab.playground import network_lifecycle as nl

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    _seed_session_vm_network()
    claimed_at = None if null_timestamp else datetime(2026, 9, 6, 11, 55, tzinfo=UTC)
    with SessionLocal() as session:
        nic = pr.attach_nic(session, nic_id="nic1", vm_id="v1", network_id="n1", mac="52:54:00:aa:bb:01")
        nic.state = "active" if mismatch == "state" else "detach_claimed"
        nic.detach_token = "old-token"
        nic.detach_claimed_at = claimed_at
        session.commit()

    with SessionLocal() as session:
        pr.lock_resource_transitions(session)
        reopened = pr.reopen_nic_detach_claim(
            session,
            "other-nic" if mismatch == "id" else "nic1",
            vm_id="other-vm" if mismatch == "vm_id" else "v1",
            detach_token="different-token" if mismatch == "token" else "old-token",
            claimed_at=(datetime(2026, 9, 6, 11, 54, tzinfo=UTC) + timedelta(seconds=1))
            if mismatch == "timestamp"
            else claimed_at,
            new_token="retry-token",
        )
        session.commit()

    assert reopened is (mismatch is None)
    with SessionLocal() as session:
        current = session.get(JobVMNic, "nic1")
        assert current is not None
        if mismatch is None:
            assert (current.state, current.detach_token, current.detach_claimed_at) == (
                "detaching",
                "retry-token",
                None,
            )
        else:
            assert current.state == ("active" if mismatch == "state" else "detach_claimed")
            assert current.detach_token == "old-token"
            expected_timestamp = None if claimed_at is None else claimed_at.replace(tzinfo=None)
            assert current.detach_claimed_at == expected_timestamp


def test_detach_nic_duplicate_delivery_cannot_call_host_twice(monkeypatch):
    from contextlib import nullcontext
    from unittest.mock import MagicMock

    from astinuslab.db.models import JobVMNic
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    _seed_session_vm_network()
    with SessionLocal() as session:
        nic = pr.attach_nic(session, nic_id="nic1", vm_id="v1", network_id="n1", mac="52:54:00:aa:bb:01")
        nic.state = "detaching"
        nic.detach_token = "queued-token"
        session.commit()

    lifecycle = MagicMock()
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: lifecycle)
    monkeypatch.setattr(po, "_libvirt_conn", lambda: nullcontext("fake-conn"))
    duplicate_rejected = []

    def deliver_duplicate(*_args):
        with pytest.raises(RuntimeError, match="already claimed"):
            po.detach_nic(job_id="pg-1", vm_id="v1", nic_id="nic1", detach_token="queued-token")
        duplicate_rejected.append(True)

    lifecycle.detach_nic.side_effect = deliver_duplicate
    assert po.detach_nic(job_id="pg-1", vm_id="v1", nic_id="nic1", detach_token="queued-token") == "nic1"
    assert duplicate_rejected == [True]
    lifecycle.detach_nic.assert_called_once()
    with SessionLocal() as session:
        assert session.get(JobVMNic, "nic1") is None


# ---------------------------------------------------------------------------
# NIC attach and detach custody, before the two tasks move to a sibling module
#
# A NIC is the one playground resource that exists in two places at once: a
# database row and a live interface on a running domain. Every case below is a
# point where those two can disagree, and the guard that stops the task acting
# on a row it no longer owns.
# ---------------------------------------------------------------------------


def _nic_harness(monkeypatch, *, conn="fake_conn"):
    """Seed a running VM on an active network, with libvirt faked out."""
    import contextlib
    from unittest.mock import MagicMock

    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    _seed_session_vm_network()
    lifecycle = MagicMock()
    lifecycle.is_missing_interface_error.return_value = False
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: lifecycle)

    @contextlib.contextmanager
    def fake_conn():
        yield conn

    monkeypatch.setattr(po, "_libvirt_conn", fake_conn)
    return lifecycle


@pytest.mark.parametrize(
    ("mutate", "why"),
    [
        pytest.param(("session", "closed"), "a closed session must not gain interfaces", id="session-closed"),
        pytest.param(("vm", "failed"), "a VM that is not running has no domain to attach to", id="vm-not-running"),
        pytest.param(("network", "destroyed"), "a destroyed network has no bridge to join", id="network-gone"),
    ],
)
def test_attach_refuses_when_a_parent_resource_is_no_longer_eligible(monkeypatch, mutate, why):
    """All three parents are locked and re-read inside the transaction. Any of
    them moving between enqueue and delivery means the attach would bind an
    interface to something that is being torn down."""
    from astinuslab.db.models import JobNetwork, JobVM, PlaygroundSession
    from astinuslab.playground import orchestrator as po

    lifecycle = _nic_harness(monkeypatch)
    kind, value = mutate
    with SessionLocal() as s:
        if kind == "session":
            s.query(PlaygroundSession).filter_by(id="pg-1").one().status = value
        elif kind == "vm":
            s.query(JobVM).filter_by(id="v1").one().state = value
        else:
            s.query(JobNetwork).filter_by(id="n1").one().state = value
        s.commit()

    with pytest.raises(LostResourceCustody, match="NIC attach lost eligible parent resource custody for VM v1"):
        po.attach_nic(job_id="pg-1", vm_id="v1", network_id="n1")

    lifecycle.attach_nic.assert_not_called(), why


def test_attach_is_idempotent_for_a_nic_that_is_already_active(monkeypatch):
    """A redelivered attach must not touch libvirt again: a second
    `attach_nic` on the same domain and MAC would add a duplicate interface."""
    from astinuslab.playground import orchestrator as po

    lifecycle = _nic_harness(monkeypatch)
    first = po.attach_nic(job_id="pg-1", vm_id="v1", network_id="n1", nic_id="nic-1")
    lifecycle.attach_nic.reset_mock()

    again = po.attach_nic(job_id="pg-1", vm_id="v1", network_id="n1", nic_id="nic-1")

    assert again == first
    lifecycle.attach_nic.assert_not_called()


def test_attach_refuses_a_row_it_cannot_claim_before_touching_the_host(monkeypatch):
    """The row exists and matches, but the claim failed, so another worker owns
    it. Refusing here is what stops two workers attaching the same NIC."""
    from astinuslab.playground import orchestrator as po

    lifecycle = _nic_harness(monkeypatch)
    with SessionLocal() as s:
        pr.attach_nic(s, nic_id="nic-1", vm_id="v1", network_id="n1", mac="52:54:00:aa:bb:cc", state="pending")
        s.commit()
    monkeypatch.setattr(pr, "claim_nic_for_attach", lambda *a, **k: False)

    with pytest.raises(LostResourceCustody, match="NIC nic-1 lost pending custody before host mutation"):
        po.attach_nic(job_id="pg-1", vm_id="v1", network_id="n1", nic_id="nic-1")

    lifecycle.attach_nic.assert_not_called(), "the host must not be touched for a row we do not own"


def test_attach_accepts_a_publication_that_another_worker_already_completed(monkeypatch):
    """`mark_nic_active` failed because someone else got there first, but the
    row is active with our exact identity. That is the same outcome, so it is
    reported as success rather than as lost custody.

    Reached through the retry path on purpose. The equivalence check compares
    `created_at` against the value the task observed, and on a fresh create that
    value is the in-memory tz-aware timestamp while the re-read row is naive, so
    the two can never compare equal. Adopting an existing row makes both sides
    the same loaded value, which is the case this branch exists for anyway: a
    redelivered attach whose row someone else published.
    """
    from astinuslab.db.models import JobVMNic
    from astinuslab.playground import orchestrator as po

    lifecycle = _nic_harness(monkeypatch)
    with SessionLocal() as s:
        pr.attach_nic(s, nic_id="nic-1", vm_id="v1", network_id="n1", mac="52:54:00:aa:bb:cc", state="pending")
        s.commit()

    def activate_but_report_failure(session, nic_id, **kwargs):
        # Promote in the task's own session, then report failure: the row is
        # active with the identity the task observed, but this delivery is not
        # the one that published it.
        session.query(JobVMNic).filter_by(id=nic_id).update({JobVMNic.state: "active"})
        session.commit()
        return False

    monkeypatch.setattr(pr, "mark_nic_active", activate_but_report_failure)

    result = po.attach_nic(job_id="pg-1", vm_id="v1", network_id="n1", nic_id="nic-1")

    assert result["nic_id"] == "nic-1"
    lifecycle.detach_nic.assert_not_called(), "an equivalent outcome must not be rolled back off the host"
    with SessionLocal() as s:
        assert s.query(JobVMNic).filter_by(id="nic-1").one().state == "active"


def test_attach_reports_a_host_rollback_it_could_not_complete(monkeypatch):
    """Custody was lost and the interface has to come back off the domain. If
    that detach fails for any reason other than the interface already being
    gone, the host is left holding an interface no row claims -- which must be
    reported, not swallowed behind the custody error."""
    from astinuslab.db.models import JobVMNic
    from astinuslab.playground import orchestrator as po

    lifecycle = _nic_harness(monkeypatch)
    lifecycle.detach_nic.side_effect = RuntimeError("libvirt refused")
    lifecycle.is_missing_interface_error.return_value = False

    def take_the_row(*args, **kwargs):
        # No row owns the MAC any more, so the interface on the domain is
        # unclaimed and has to come back off.
        with SessionLocal() as s:
            s.query(JobVMNic).filter_by(id="nic-1").delete()
            s.commit()
        return False

    monkeypatch.setattr(pr, "mark_nic_active", take_the_row)

    with pytest.raises(RuntimeError, match="NIC nic-1 lost custody and strict host cleanup failed"):
        po.attach_nic(job_id="pg-1", vm_id="v1", network_id="n1", nic_id="nic-1")


def test_attach_reports_lost_custody_when_the_interface_was_already_gone(monkeypatch):
    """Same rollback, but the interface had already vanished. That is not an
    extra failure, so the custody loss is what surfaces."""
    from astinuslab.db.models import JobVMNic
    from astinuslab.playground import orchestrator as po

    lifecycle = _nic_harness(monkeypatch)
    lifecycle.detach_nic.side_effect = RuntimeError("no such interface")
    lifecycle.is_missing_interface_error.return_value = True

    def take_the_row(*args, **kwargs):
        with SessionLocal() as s:
            s.query(JobVMNic).filter_by(id="nic-1").delete()
            s.commit()
        return False

    monkeypatch.setattr(pr, "mark_nic_active", take_the_row)

    with pytest.raises(LostResourceCustody, match="NIC nic-1 lost attaching custody"):
        po.attach_nic(job_id="pg-1", vm_id="v1", network_id="n1", nic_id="nic-1")

    lifecycle.detach_nic.assert_called_once(), "the unclaimed interface must still be taken off the domain"


def _seed_nic(state: str = "active", *, nic_id: str = "nic-1", token=None, claimed_at=None) -> None:
    from astinuslab.db.models import JobVMNic

    with SessionLocal() as s:
        pr.attach_nic(s, nic_id=nic_id, vm_id="v1", network_id="n1", mac="52:54:00:aa:bb:cc")
        nic = s.query(JobVMNic).filter_by(id=nic_id).one()
        nic.state = state
        if hasattr(nic, "detach_token"):
            nic.detach_token = token
            nic.detach_claimed_at = claimed_at
        s.commit()


def test_detach_refuses_when_the_parent_session_is_no_longer_eligible(monkeypatch):
    """Detach is the freeze barrier: it must not run while the session is being
    closed, or authoring could be sealed with a host change in flight."""
    from astinuslab.db.models import PlaygroundSession
    from astinuslab.playground import orchestrator as po

    lifecycle = _nic_harness(monkeypatch)
    _seed_nic()
    with SessionLocal() as s:
        s.query(PlaygroundSession).filter_by(id="pg-1").one().status = "closed"
        s.commit()

    with pytest.raises(LostResourceCustody, match="NIC detach lost eligible parent resource custody for VM v1"):
        po.detach_nic(job_id="pg-1", vm_id="v1", nic_id="nic-1")

    lifecycle.detach_nic.assert_not_called()


def test_detach_refuses_a_second_request_carrying_a_different_token(monkeypatch):
    """Two detach requests for one NIC. The second must not proceed, or both
    would drive libvirt against the same interface."""
    from astinuslab.playground import orchestrator as po

    lifecycle = _nic_harness(monkeypatch)
    _seed_nic("detaching", token="token-in-flight")

    with pytest.raises(LostResourceCustody, match="NIC nic-1 has a different detach request in flight"):
        po.detach_nic(job_id="pg-1", vm_id="v1", nic_id="nic-1", detach_token="a-different-token")

    lifecycle.detach_nic.assert_not_called()


def test_detach_adopts_a_detaching_row_that_has_no_token_yet(monkeypatch):
    """A row left `detaching` by an older release carries no token. It is
    recoverable rather than stuck, so this delivery adopts it."""
    from astinuslab.db.models import JobVMNic
    from astinuslab.playground import orchestrator as po

    _nic_harness(monkeypatch)
    _seed_nic("detaching", token=None)

    assert po.detach_nic(job_id="pg-1", vm_id="v1", nic_id="nic-1") == "nic-1"

    with SessionLocal() as s:
        assert s.query(JobVMNic).filter_by(id="nic-1").one_or_none() is None


def test_detach_refuses_a_claim_another_worker_holds_and_has_not_lost(monkeypatch):
    """`detach_claimed` inside the lease belongs to a live worker."""
    from datetime import UTC, datetime

    from astinuslab.playground import orchestrator as po

    lifecycle = _nic_harness(monkeypatch)
    _seed_nic("detach_claimed", token="held", claimed_at=datetime.now(UTC).replace(tzinfo=None))

    with pytest.raises(LostResourceCustody, match="NIC nic-1 is already claimed by another detach worker"):
        po.detach_nic(job_id="pg-1", vm_id="v1", nic_id="nic-1", detach_token="held")

    lifecycle.detach_nic.assert_not_called()


@pytest.mark.parametrize(
    ("stored", "requested"),
    [
        pytest.param("held", "someone-elses-token", id="mismatched-token"),
        pytest.param(None, "any-token", id="no-stored-token"),
    ],
)
def test_detach_refuses_a_claim_it_cannot_prove_is_its_own(monkeypatch, stored, requested):
    """`detach_claimed` without the matching token is someone else's claim.

    Staged with an *expired* lease on purpose. A live lease would be refused by
    the staleness check below anyway, with the same message, so it could not
    tell the two guards apart. Past the lease the claim is recoverable -- but
    only by a worker that can prove the token, which is what this pins.
    """
    from datetime import UTC, datetime, timedelta

    from astinuslab.playground import orchestrator as po

    lifecycle = _nic_harness(monkeypatch)
    expired = datetime.now(UTC) - timedelta(seconds=pr.NIC_DETACH_CLAIM_SECONDS + 60)
    _seed_nic("detach_claimed", token=stored, claimed_at=expired.replace(tzinfo=None))

    with pytest.raises(LostResourceCustody, match="NIC nic-1 is already claimed by another detach worker"):
        po.detach_nic(job_id="pg-1", vm_id="v1", nic_id="nic-1", detach_token=requested)

    lifecycle.detach_nic.assert_not_called()


def test_detach_recovers_a_claim_whose_worker_died(monkeypatch):
    """Past the bounded lease the claim is explicitly recoverable, otherwise a
    worker dying mid-detach would strand the NIC and block session freezing
    forever."""
    from datetime import UTC, datetime, timedelta

    from astinuslab.db.models import JobVMNic
    from astinuslab.playground import orchestrator as po

    lifecycle = _nic_harness(monkeypatch)
    stale = datetime.now(UTC) - timedelta(seconds=pr.NIC_DETACH_CLAIM_SECONDS + 60)
    _seed_nic("detach_claimed", token="held", claimed_at=stale.replace(tzinfo=None))

    assert po.detach_nic(job_id="pg-1", vm_id="v1", nic_id="nic-1", detach_token="held") == "nic-1"

    lifecycle.detach_nic.assert_called_once()
    with SessionLocal() as s:
        assert s.query(JobVMNic).filter_by(id="nic-1").one_or_none() is None


def test_detach_refuses_a_nic_in_a_state_it_cannot_act_on(monkeypatch):
    """A NIC still attaching has no interface to remove yet."""
    from astinuslab.playground import orchestrator as po

    lifecycle = _nic_harness(monkeypatch)
    _seed_nic("attaching")

    with pytest.raises(LostResourceCustody, match="NIC nic-1 is not eligible for detach"):
        po.detach_nic(job_id="pg-1", vm_id="v1", nic_id="nic-1")

    lifecycle.detach_nic.assert_not_called()


def test_detach_surfaces_a_row_taken_before_the_db_cleanup(monkeypatch):
    """The interface is off the domain and then the claim is gone, so the row
    cannot be deleted. Returning success would report a NIC as detached while
    its row still stands."""
    from astinuslab.playground import orchestrator as po

    lifecycle = _nic_harness(monkeypatch)
    _seed_nic()
    monkeypatch.setattr(pr, "delete_nic_if_detach_claim", lambda *a, **k: False)

    with pytest.raises(LostResourceCustody, match="NIC nic-1 lost detach custody before DB cleanup"):
        po.detach_nic(job_id="pg-1", vm_id="v1", nic_id="nic-1")

    lifecycle.detach_nic.assert_called_once(), "the host detach happened; only the row cleanup was lost"
