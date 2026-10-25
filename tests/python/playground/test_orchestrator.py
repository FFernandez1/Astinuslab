from __future__ import annotations

import errno
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from astinuslab.core import fake_mode
from astinuslab.db import SessionLocal
from astinuslab.db.models import JobNetwork, JobVM, PlaygroundSession
from astinuslab.jobs.celery_worker import celery_app
from astinuslab.playground import repositories as pr
from astinuslab.playground.resource_custody import LostResourceCustody, ResourceClaimLease
from tests.python._support.helpers import autospec_setattr, isolated_playground_database


@pytest.fixture(autouse=True)
def _eager(monkeypatch, tmp_path):
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    original_is_file = Path.is_file

    def is_file_with_placeholder_images(self):
        path = str(self)
        if path == "/x" or path.startswith("/var/lib/astinuslab/images/"):
            return True
        return original_is_file(self)

    monkeypatch.setattr(Path, "is_file", is_file_with_placeholder_images)
    with isolated_playground_database(monkeypatch, tmp_path, globals()):
        yield


def test_create_session_inserts_running_job():
    from astinuslab.playground.orchestrator import create_session

    create_session.delay(job_id="pg-1").get()

    with SessionLocal() as s:
        active = pr.active_playground_session(s)
        assert active is not None
        assert active.id == "pg-1"


def test_create_session_rejects_when_active_session_exists():
    from astinuslab.playground.orchestrator import create_session

    create_session.delay(job_id="pg-1").get()
    with pytest.raises(RuntimeError):
        create_session.delay(job_id="pg-2").get()


def test_destroy_session_marks_cancelled():
    from astinuslab.playground.orchestrator import (
        create_session,
        destroy_session,
    )

    create_session.delay(job_id="pg-1").get()
    destroy_session.delay(job_id="pg-1").get()

    with SessionLocal() as s:
        assert pr.active_playground_session(s) is None
        from astinuslab.db.models import PlaygroundSession

        playground_session = s.query(PlaygroundSession).filter_by(id="pg-1").one()
        assert playground_session.status == "cancelled"


def test_create_session_rejects_when_forensic_job_is_waiting_for_capture():
    from astinuslab.db import SessionLocal
    from astinuslab.db.models import Job
    from astinuslab.playground.orchestrator import create_session

    # Insert an active forensic job using a real forensic lifecycle status.
    with SessionLocal() as s:
        forensic = Job(
            id="forensic-1",
            target_id="ubuntu-22.04",
            scenario_id="immediate-default",
            execution_mode="immediate",
            status="waiting_for_capture",
        )
        s.add(forensic)
        s.commit()

    with pytest.raises(Exception):
        create_session.delay(job_id="pg-1").get()


def test_create_network_inserts_active_row(monkeypatch):
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground.orchestrator import create_network, create_session

    fake_pn = MagicMock()
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: fake_pn)

    create_session.delay(job_id="pg-1").get()
    create_network.delay(
        job_id="pg-1",
        network_id="n1",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
    ).get()

    fake_pn.create_network.assert_called_once_with(
        libvirt_net_name="astinuslab-pg-pg-1-ab-cdf2",
        network_id="n1",
        cidr="10.42.1.0/24",
        isolated=True,
    )
    with SessionLocal() as s:
        nets = pr.list_session_networks(s, "pg-1")
        assert len(nets) == 1
        assert nets[0].state == "active"


def test_queued_network_task_cannot_claim_after_session_is_frozen(monkeypatch):
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_network, create_session

    helper = MagicMock()
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: helper)
    create_session.delay(job_id="pg-frozen-network").get()
    with SessionLocal() as session:
        pr.register_network(
            session,
            network_id="net-frozen",
            job_id="pg-frozen-network",
            name="isolated",
            subnet_cidr="10.42.1.0/24",
            isolated=True,
            libvirt_net_name=po.libvirt_net_name("pg-frozen-network", "isolated"),
        )
        authored = session.get(PlaygroundSession, "pg-frozen-network")
        assert authored is not None
        authored.status = pr.AUTHORING_CLOSED
        session.commit()

    with pytest.raises(pr.AuthoringClosed):
        create_network(
            job_id="pg-frozen-network",
            network_id="net-frozen",
            name="isolated",
            subnet_cidr="10.42.1.0/24",
            isolated=True,
        )

    helper.create_network.assert_not_called()
    with SessionLocal() as session:
        network = session.get(JobNetwork, "net-frozen")
        assert network is not None
        assert network.state == "pending"


def test_queued_vm_task_cannot_claim_after_session_is_frozen(monkeypatch, tmp_path):
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_session, spawn_vm

    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda *_args, **_kwargs: pytest.fail("frozen task must stop before resolving its image"),
    )
    monkeypatch.setattr(po, "settings", _workload_cache_settings(tmp_path))
    create_session.delay(job_id="pg-frozen-vm").get()
    with SessionLocal() as session:
        pr.register_vm(
            session,
            vm_id="vm-frozen",
            job_id="pg-frozen-vm",
            label="target",
            image_id="ubuntu-24.04",
            domain_name="astinuslab-pg-pg-frozen-vm-target",
            disk_path=str(tmp_path / "disks" / "pg-frozen-vm" / "target.qcow2"),
        )
        authored = session.get(PlaygroundSession, "pg-frozen-vm")
        assert authored is not None
        authored.status = pr.AUTHORING_CLOSED
        session.commit()

    with pytest.raises(pr.AuthoringClosed):
        spawn_vm(
            job_id="pg-frozen-vm",
            vm_id="vm-frozen",
            label="target",
            image_id="ubuntu-24.04",
            nic_specs=[],
        )

    with SessionLocal() as session:
        vm = session.get(JobVM, "vm-frozen")
        assert vm is not None
        assert vm.state == "pending"


def test_create_network_fences_at_host_mutation_boundary_if_frozen(monkeypatch):
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground.orchestrator import create_network, create_session

    helper = MagicMock()

    def _reap(*, session_scope):
        with SessionLocal() as s:
            authored = s.get(PlaygroundSession, "pg-fence-net")
            assert authored is not None
            authored.status = pr.AUTHORING_CLOSED
            s.commit()
        return 0

    helper.reap_orphans.side_effect = _reap
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: helper)
    create_session.delay(job_id="pg-fence-net").get()

    with pytest.raises(pr.AuthoringClosed):
        create_network(
            job_id="pg-fence-net",
            network_id="net-fence",
            name="isolated",
            subnet_cidr="10.42.1.0/24",
            isolated=True,
        )

    helper.create_network.assert_not_called()


def test_spawn_vm_fences_at_host_mutation_boundary_if_frozen(monkeypatch, tmp_path):
    from astinuslab.playground import cloud_init_seed as cis
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_session, spawn_vm

    fake_lifecycle = MagicMock()
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)
    monkeypatch.setattr(po, "settings", _workload_cache_settings(tmp_path))

    disk = tmp_path / "img.qcow2"
    disk.write_bytes(b"dummy")
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda *_args, **_kwargs: {"path": str(disk)},
    )

    def _fake_cloud_init(*args, **kwargs):
        with SessionLocal() as s:
            authored = s.get(PlaygroundSession, "pg-fence-vm")
            assert authored is not None
            authored.status = pr.AUTHORING_CLOSED
            s.commit()
        return {}

    autospec_setattr(monkeypatch, cis, "_write_playground_cloud_init", _fake_cloud_init)
    autospec_setattr(monkeypatch, cis, "_playground_cloud_init_enabled", lambda _img: True)

    create_session.delay(job_id="pg-fence-vm").get()

    with pytest.raises(pr.AuthoringClosed):
        spawn_vm(
            job_id="pg-fence-vm",
            vm_id="vm-fence",
            label="target",
            image_id="ubuntu-24.04",
            nic_specs=[],
        )

    fake_lifecycle.boot_playground.assert_not_called()


def test_create_network_reaps_orphans_before_defining(monkeypatch):
    """A leaked host network from a dead session squats its subnet and makes
    libvirt reject the new network. create_network must reap orphans before
    defining the network. Regression for the "Network is already in use"
    playground capture failure."""
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground.orchestrator import create_network, create_session

    events: list[str] = []

    class _RecordingHelper:
        def reap_orphans(self, *, session_scope) -> int:
            events.append("reap")
            return 0

        def create_network(self, *, libvirt_net_name, network_id, cidr, isolated) -> None:
            events.append("create")

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: _RecordingHelper())

    create_session.delay(job_id="pg-1").get()
    create_network.delay(
        job_id="pg-1",
        network_id="n1",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
    ).get()

    assert events == ["reap", "create"]
    with SessionLocal() as s:
        assert pr.list_session_networks(s, "pg-1")[0].state == "active"


@pytest.mark.parametrize("failure_point", ["setup", "reap"])
def test_create_network_setup_failures_release_the_creating_claim(monkeypatch, failure_point):
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground.orchestrator import create_network, create_session

    class _ReapFailure:
        def reap_orphans(self, *, session_scope) -> int:
            raise RuntimeError("reap failed")

    def helper_factory():
        if failure_point == "setup":
            raise RuntimeError("setup failed")
        return _ReapFailure()

    autospec_setattr(monkeypatch, nl, "_networks_helper", helper_factory)
    create_session.delay(job_id="pg-1").get()

    with pytest.raises(RuntimeError, match=f"{failure_point} failed"):
        create_network(
            job_id="pg-1",
            network_id="n1",
            name="ab",
            subnet_cidr="10.42.1.0/24",
            isolated=True,
        )

    with SessionLocal() as session:
        network = pr.list_session_networks(session, "pg-1")[0]
        assert network.state == "failed"
        assert network.error_summary == f"{failure_point} failed"


def test_create_network_retry_rejects_mismatched_immutable_custody(monkeypatch):
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground.orchestrator import create_network, create_session

    helper = MagicMock()
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: helper)
    create_session.delay(job_id="pg-1").get()
    with SessionLocal() as session:
        pr.register_network(
            session,
            network_id="n1",
            job_id="pg-1",
            name="original",
            subnet_cidr="10.42.1.0/24",
            isolated=True,
            libvirt_net_name="astinuslab-pg-pg-1-original-0000",
        )
        pr.update_network_state(session, "n1", "failed", error="legacy failure")
        session.commit()

    with pytest.raises(RuntimeError, match="network retry identity mismatch"):
        create_network(
            job_id="pg-1",
            network_id="n1",
            name="replacement",
            subnet_cidr="10.42.1.0/24",
            isolated=True,
        )

    helper.create_network.assert_not_called()
    with SessionLocal() as session:
        from astinuslab.db.models import JobNetwork

        network = session.query(JobNetwork).filter_by(id="n1").one()
        assert network.state == "failed"
        assert network.name == "original"


def test_create_network_rejects_when_forensic_job_is_active(monkeypatch):
    from astinuslab.db.models import Job, JobNetwork
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground.orchestrator import create_network, create_session

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    create_session.delay(job_id="pg-1").get()
    with SessionLocal() as s:
        s.add(
            Job(
                id="forensic-1",
                target_id="ubuntu-22.04",
                scenario_id="immediate-default",
                execution_mode="immediate",
                status="waiting_for_capture",
            )
        )
        s.commit()

    with pytest.raises(RuntimeError, match="forensic job"):
        create_network.delay(
            job_id="pg-1",
            network_id="n1",
            name="ab",
            subnet_cidr="10.42.1.0/24",
            isolated=True,
        ).get()

    with SessionLocal() as s:
        assert s.query(JobNetwork).filter_by(id="n1").one_or_none() is None


def test_create_network_rejects_overlapping_cidr_before_registering(monkeypatch):
    from astinuslab.db.models import JobNetwork
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground.orchestrator import create_network, create_session

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    create_session.delay(job_id="pg-1").get()
    create_network.delay(
        job_id="pg-1",
        network_id="n1",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
    ).get()

    with pytest.raises(RuntimeError, match="overlap"):
        create_network.delay(
            job_id="pg-1",
            network_id="n2",
            name="cd",
            subnet_cidr="10.42.1.128/25",
            isolated=True,
        ).get()

    with SessionLocal() as s:
        assert s.query(JobNetwork).filter_by(id="n2").one_or_none() is None
        assert [n.id for n in pr.list_session_networks(s, "pg-1")] == ["n1"]


def test_create_network_rejects_duplicate_name_before_registering(monkeypatch):
    from astinuslab.db.models import JobNetwork
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground.orchestrator import create_network, create_session

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    create_session.delay(job_id="pg-1").get()
    create_network.delay(
        job_id="pg-1",
        network_id="n1",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
    ).get()

    with pytest.raises(RuntimeError, match="already exists"):
        create_network.delay(
            job_id="pg-1",
            network_id="n2",
            name="ab",
            subnet_cidr="10.42.2.0/24",
            isolated=True,
        ).get()

    with SessionLocal() as s:
        assert s.query(JobNetwork).filter_by(id="n2").one_or_none() is None
        assert [n.id for n in pr.list_session_networks(s, "pg-1")] == ["n1"]


def test_destroy_network_rejects_when_attached_vms_exist(monkeypatch):
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground.orchestrator import (
        create_network,
        create_session,
        destroy_network,
    )

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    create_session.delay(job_id="pg-1").get()
    create_network.delay(
        job_id="pg-1",
        network_id="n1",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
    ).get()

    # Simulate a NIC attached to net n1
    with SessionLocal() as s:
        pr.register_vm(s, vm_id="v1", job_id="pg-1", label="x", image_id="img", domain_name="d", disk_path="/p")
        pr.attach_nic(s, nic_id="nic1", vm_id="v1", network_id="n1", mac="52:54:00:aa:bb:01")
        s.commit()

    with pytest.raises(RuntimeError):
        destroy_network.delay(network_id="n1").get()


def test_destroy_network_worker_restores_a_queued_network_when_a_vm_attaches(monkeypatch):
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground.orchestrator import create_network, create_session, destroy_network

    helper = MagicMock()
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: helper)
    create_session.delay(job_id="pg-1").get()
    create_network.delay(
        job_id="pg-1",
        network_id="n1",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
    ).get()
    with SessionLocal() as session:
        assert pr.queue_network_for_destroy(session, "n1") is True
        pr.register_vm(
            session,
            vm_id="v1",
            job_id="pg-1",
            label="x",
            image_id="img",
            domain_name="d",
            disk_path="/p",
        )
        pr.attach_nic(session, nic_id="nic1", vm_id="v1", network_id="n1", mac="52:54:00:aa:bb:01")
        session.commit()

    with pytest.raises(RuntimeError, match="has attached VMs"):
        destroy_network(network_id="n1")

    helper.destroy_network.assert_not_called()
    with SessionLocal() as session:
        network = pr.list_session_networks(session, "pg-1")[0]
        assert network.state == "active"
        assert "VM attached" in (network.error_summary or "")


def test_wait_for_active_networks_rejects_a_queued_destroy_immediately():
    from astinuslab.playground.orchestrator import create_session
    from astinuslab.playground.vm_lifecycle import _wait_for_active_networks

    create_session.delay(job_id="pg-1").get()
    with SessionLocal() as session:
        pr.register_network(
            session,
            network_id="n1",
            job_id="pg-1",
            name="ab",
            subnet_cidr="10.42.1.0/24",
            isolated=True,
            libvirt_net_name="astinuslab-pg-pg-1-ab-cdf2",
        )
        pr.update_network_state(session, "n1", "active")
        assert pr.queue_network_for_destroy(session, "n1") is True
        session.commit()

    with pytest.raises(RuntimeError, match="destroy_queued"):
        _wait_for_active_networks(job_id="pg-1", network_ids={"n1"}, timeout_seconds=60, poll_seconds=1)


def test_destroy_network_tears_down_helper_and_marks_destroyed(monkeypatch):
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground.orchestrator import (
        create_network,
        create_session,
        destroy_network,
    )

    fake_pn = MagicMock()
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: fake_pn)

    create_session.delay(job_id="pg-1").get()
    create_network.delay(
        job_id="pg-1",
        network_id="n1",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
    ).get()

    result = destroy_network.delay(network_id="n1").get()

    assert result == "n1"
    fake_pn.destroy_network.assert_called_once_with("astinuslab-pg-pg-1-ab-cdf2")
    with SessionLocal() as s:
        nets = pr.list_session_networks(s, "pg-1")
        assert len(nets) == 1
        assert nets[0].state == "destroyed"


def test_destroy_network_marks_failed_when_helper_raises(monkeypatch):
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground.orchestrator import (
        create_network,
        create_session,
        destroy_network,
    )

    fake_pn = MagicMock()
    fake_pn.destroy_network.side_effect = RuntimeError("teardown failed")
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: fake_pn)

    create_session.delay(job_id="pg-1").get()
    create_network.delay(
        job_id="pg-1",
        network_id="n1",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
    ).get()

    with pytest.raises(RuntimeError, match="teardown failed"):
        destroy_network.delay(network_id="n1").get()

    with SessionLocal() as s:
        nets = pr.list_session_networks(s, "pg-1")
        assert len(nets) == 1
        assert nets[0].state == "failed"
        assert nets[0].error_summary == "teardown failed"


def test_public_network_destroy_cannot_steal_create_retry_custody(monkeypatch):
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground.orchestrator import create_session, destroy_network

    helper = MagicMock()
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: helper)
    create_session.delay(job_id="pg-1").get()
    with SessionLocal() as session:
        pr.register_network(
            session,
            network_id="n1",
            job_id="pg-1",
            name="ab",
            subnet_cidr="10.42.1.0/24",
            isolated=True,
            libvirt_net_name="astinuslab-pg-pg-1-ab-cdf2",
        )
        pr.update_network_state(session, "n1", "failed")
        assert pr.claim_network_for_create(session, "n1") is True
        session.commit()

    with pytest.raises(RuntimeError, match="busy"):
        destroy_network(network_id="n1")

    helper.destroy_network.assert_not_called()
    with SessionLocal() as session:
        from astinuslab.db.models import JobNetwork

        network = session.get(JobNetwork, "n1")
        assert network is not None
        assert network.state == "creating"


def test_spawn_vm_clones_image_renders_multi_nic_domain_and_records_state(monkeypatch, tmp_path):
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import (
        create_network,
        create_session,
        spawn_vm,
    )

    fake_lifecycle = MagicMock()
    fake_lifecycle.boot_playground.return_value = None
    fake_lifecycle.get_ip.return_value = "10.42.1.10"
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)

    fake_catalog_image = {
        "id": "ubuntu-22.04",
        "path": "/var/lib/astinuslab/images/ubuntu-22.04.qcow2",
        "format": "qcow2",
        "architecture": "x86_64",
        "credentials": {"username": "ubuntu", "password": "ubuntu"},
    }
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda image_id, *, role="target": fake_catalog_image if image_id == "ubuntu-22.04" else None,
    )
    storage_root = tmp_path / "storage"
    playground_root = storage_root / "playground"
    playground_disk_root = playground_root / "disks"
    storage_root.mkdir(mode=0o750)
    playground_disk_root.mkdir(parents=True)
    playground_root.chmod(0o775)
    playground_disk_root.chmod(0o775)
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(
            playground_disk_dir=playground_disk_root,
            playground_default_vcpus=2,
            playground_default_memory_mb=2048,
            libvirt_uri="qemu:///system",
            base_dir=tmp_path,
            data_dir=tmp_path,
            storage_dir=storage_root,
        ),
    )
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())

    create_session.delay(job_id="pg-1").get()
    create_network.delay(
        job_id="pg-1",
        network_id="n1",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
    ).get()

    spawn_vm.delay(
        job_id="pg-1",
        vm_id="v1",
        label="web-1",
        image_id="ubuntu-22.04",
        nic_specs=[{"network_id": "n1", "nic_id": "nic1"}],
    ).get()

    fake_lifecycle.boot_playground.assert_called_once()
    boot_kwargs = fake_lifecycle.boot_playground.call_args.kwargs
    assert boot_kwargs["domain_name"] == "astinuslab-pg-pg-1-web-1"
    assert str(boot_kwargs["base_image_path"]) == fake_catalog_image["path"]
    assert boot_kwargs["networks"][0]["libvirt_net"] == "astinuslab-pg-pg-1-ab-cdf2"
    assert boot_kwargs["networks"][0]["mac"].startswith("52:54:")
    for helper_walked_dir in (playground_root, playground_disk_root):
        assert helper_walked_dir.stat().st_mode & 0o022 == 0
    assert (playground_disk_root / "pg-1").stat().st_mode & 0o777 == 0o700
    with SessionLocal() as s:
        from astinuslab.db.models import JobVM, JobVMNic

        vm = s.query(JobVM).filter_by(id="v1").one()
        assert vm.state == "running"
        nics = s.query(JobVMNic).filter_by(job_vm_id="v1").all()
        assert len(nics) == 1
        assert nics[0].ip == "10.42.1.10"


def test_spawn_vm_uses_network_node_resource_defaults(monkeypatch, tmp_path):
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_network, create_session, spawn_vm

    fake_lifecycle = MagicMock()
    fake_lifecycle.get_ip.return_value = "10.42.1.10"
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda image_id, *, role="target": (
            {
                "id": image_id,
                "path": "/var/lib/astinuslab/images/openwrt.qcow2",
                "format": "qcow2",
                "architecture": "x86_64",
                "username": "root",
                "cloud_init": False,
                "defaults": {"vcpus": 1, "memory_mb": 512, "disk_gb": 4},
            }
            if role == "network" and image_id == "openwrt"
            else None
        ),
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
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())

    create_session.delay(job_id="pg-1").get()
    create_network.delay(
        job_id="pg-1",
        network_id="n1",
        name="wan",
        subnet_cidr="10.42.10.0/24",
        isolated=True,
    ).get()

    spawn_vm.delay(
        job_id="pg-1",
        vm_id="v1",
        label="edge-1",
        image_id="openwrt",
        role="network",
        nic_specs=[{"network_id": "n1", "nic_id": "nic1"}],
    ).get()

    boot_kwargs = fake_lifecycle.boot_playground.call_args.kwargs
    assert boot_kwargs["vcpus"] == 1
    assert boot_kwargs["memory_mb"] == 512
    assert boot_kwargs["disk_gb"] == 4
    assert "seed_iso_path" not in boot_kwargs


def testlookup_playground_image_finds_network_catalog_image(monkeypatch):
    from astinuslab import catalog
    from astinuslab.playground.orchestrator import lookup_playground_image

    autospec_setattr(
        monkeypatch,
        catalog,
        "list_playground_images_for_role",
        lambda role: (
            [
                {
                    "id": "openwrt",
                    "role": "network",
                    "path": "/images/openwrt.qcow2",
                    "cloud_init": False,
                }
            ]
            if role == "network"
            else []
        ),
    )

    image = lookup_playground_image("openwrt", role="network")

    assert image is not None
    assert image["role"] == "network"
    assert image["cloud_init"] is False


def test_spawn_vm_writes_cloud_init_seed_for_cloud_init_images(monkeypatch, tmp_path):
    from astinuslab.playground import cloud_init_seed as cis
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_network, create_session, spawn_vm

    fake_lifecycle = MagicMock()
    fake_lifecycle.get_ip.return_value = "10.42.1.10"
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)
    public_key = tmp_path / "id_ed25519.pub"
    public_key.write_text("ssh-ed25519 AAAATEST astinuslab-test\n", encoding="utf-8")
    autospec_setattr(monkeypatch, cis, "get_active_key_paths", lambda: (tmp_path / "id_ed25519", public_key))
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda image_id, *, role="target": {
            "id": image_id,
            "path": "/var/lib/astinuslab/images/img.qcow2",
            "format": "qcow2",
            "architecture": "x86_64",
            "username": "ubuntu",
            "distro": "ubuntu",
            "family": "debian",
            "cloud_init": True,
        },
    )
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(
            playground_disk_dir=tmp_path / "disks",
            playground_default_vcpus=2,
            playground_default_memory_mb=2048,
            libvirt_uri="qemu:///system",
            base_dir=tmp_path,
            data_dir=tmp_path,
        ),
    )

    create_session.delay(job_id="pg-1").get()
    create_network.delay(
        job_id="pg-1",
        network_id="n1",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
    ).get()

    spawn_vm.delay(
        job_id="pg-1",
        vm_id="v1",
        label="web-1",
        image_id="img",
        nic_specs=[{"network_id": "n1", "nic_id": "nic1", "mac": "52:54:00:aa:bb:cc"}],
    ).get()

    boot_kwargs = fake_lifecycle.boot_playground.call_args.kwargs
    assert boot_kwargs["seed_iso_path"] == tmp_path / "disks" / "pg-1" / "web-1-seed.iso"
    user_data = boot_kwargs["user_data_path"].read_text(encoding="utf-8")
    meta_data = boot_kwargs["meta_data_path"].read_text(encoding="utf-8")
    network_config = boot_kwargs["network_config_path"].read_text(encoding="utf-8")
    assert "#cloud-config" in user_data
    assert "name: ubuntu" in user_data
    assert "ssh-ed25519 AAAATEST astinuslab-test" in user_data
    assert "instance-id: astinuslab-pg-1-v1" in meta_data
    assert "local-hostname: web-1" in meta_data
    assert "dhcp4: true" in network_config


def test_spawn_vm_with_workload_enters_provisioning_state(monkeypatch, tmp_path):
    from astinuslab import catalog
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_network, create_session, spawn_vm

    fake_lifecycle = MagicMock()
    fake_lifecycle.get_ip.return_value = "10.42.1.10"
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda image_id, *, role="target": {
            "id": image_id,
            "path": "/var/lib/astinuslab/images/img.qcow2",
            "format": "qcow2",
            "architecture": "x86_64",
            "distro": "ubuntu",
            "version": "22.04",
            "family": "debian",
        },
    )
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(
            playground_disk_dir=tmp_path / "disks",
            playground_default_vcpus=2,
            playground_default_memory_mb=2048,
            libvirt_uri="qemu:///system",
            base_dir=tmp_path,
            data_dir=tmp_path,
        ),
    )
    autospec_setattr(
        monkeypatch,
        catalog,
        "workload_by_id",
        lambda workload_id: {"id": workload_id, "display_name": "DVWA", "services": []},
    )
    calls = []
    autospec_setattr(monkeypatch, po.provision_vm_workload, "delay", lambda **kwargs: calls.append(kwargs))

    create_session.delay(job_id="pg-1").get()
    create_network.delay(job_id="pg-1", network_id="n1", name="ab", subnet_cidr="10.42.1.0/24", isolated=True).get()

    spawn_vm.delay(
        job_id="pg-1",
        vm_id="v1",
        label="web-1",
        image_id="img",
        nic_specs=[{"network_id": "n1", "nic_id": "nic1"}],
        workload_id="dvwa",
    ).get()

    assert calls == [{"vm_id": "v1"}]
    with SessionLocal() as s:
        from astinuslab.db.models import JobVM

        vm = s.query(JobVM).filter_by(id="v1").one()
        assert vm.state == "provisioning_workload"
        assert vm.workload_id == "dvwa"
        assert vm.workload_state == "provisioning"


def test_spawn_vm_cleans_up_when_workload_enqueue_fails(monkeypatch, tmp_path):
    from astinuslab import catalog
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_network, create_session, spawn_vm

    fake_lifecycle = MagicMock()
    fake_lifecycle.get_ip.return_value = "10.42.1.10"
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda image_id, *, role="target": {
            "id": image_id,
            "path": "/var/lib/astinuslab/images/img.qcow2",
            "format": "qcow2",
            "architecture": "x86_64",
            "distro": "ubuntu",
            "version": "22.04",
            "family": "debian",
        },
    )
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(
            playground_disk_dir=tmp_path / "disks",
            playground_default_vcpus=2,
            playground_default_memory_mb=2048,
            libvirt_uri="qemu:///system",
            base_dir=tmp_path,
            data_dir=tmp_path,
        ),
    )
    autospec_setattr(
        monkeypatch,
        catalog,
        "workload_by_id",
        lambda workload_id: {"id": workload_id, "display_name": "DVWA", "services": []},
    )
    autospec_setattr(
        monkeypatch,
        po.provision_vm_workload,
        "delay",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("broker down")),
    )

    create_session.delay(job_id="pg-1").get()
    create_network.delay(
        job_id="pg-1",
        network_id="n1",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
    ).get()

    with pytest.raises(Exception, match="broker down"):
        spawn_vm.delay(
            job_id="pg-1",
            vm_id="v1",
            label="web-1",
            image_id="img",
            nic_specs=[{"network_id": "n1", "nic_id": "nic1"}],
            workload_id="dvwa",
        ).get()

    fake_lifecycle.destroy.assert_called_once()
    with SessionLocal() as session:
        from astinuslab.db.models import JobVM, JobVMNic

        vm = session.get(JobVM, "v1")
        assert vm is not None
        assert vm.state == "failed", "successful strict enqueue cleanup leaves no host residue"
        assert "broker down" in (vm.error_summary or "")
        assert session.query(JobVMNic).filter_by(job_vm_id="v1").count() == 0


def test_provision_vm_workload_marks_ready(monkeypatch):
    from astinuslab import catalog
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground import workload_provisioning as wp
    from astinuslab.playground.orchestrator import create_network, create_session, provision_vm_workload

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())

    create_session.delay(job_id="pg-1").get()
    create_network.delay(job_id="pg-1", network_id="n1", name="ab", subnet_cidr="10.42.1.0/24", isolated=True).get()
    with SessionLocal() as s:
        pr.register_vm(
            s,
            vm_id="v1",
            job_id="pg-1",
            label="web-1",
            image_id="img",
            domain_name="dom",
            disk_path="/tmp/disk.qcow2",
            role="target",
            workload_id="dvwa",
        )
        pr.update_vm_state(s, "v1", "provisioning_workload")
        pr.update_vm_workload_state(s, "v1", "provisioning")
        pr.attach_nic(s, nic_id="nic1", vm_id="v1", network_id="n1", mac="52:54:00:aa:bb:cc")
        pr.update_nic_ip(s, "nic1", "10.42.1.10")
        s.commit()

    autospec_setattr(
        monkeypatch,
        catalog,
        "workload_by_id",
        lambda workload_id: {"id": workload_id, "display_name": "DVWA", "services": []},
    )
    autospec_setattr(monkeypatch, po, "lookup_playground_image", lambda image_id, *, role="target": {"id": image_id})
    autospec_setattr(monkeypatch, wp, "_provision_playground_workload", lambda **kwargs: None)

    provision_vm_workload.delay(vm_id="v1").get()

    with SessionLocal() as s:
        from astinuslab.db.models import JobVM

        vm = s.query(JobVM).filter_by(id="v1").one()
        assert vm.state == "running"
        assert vm.workload_state == "ready"
        assert vm.workload_error is None


def test_provision_vm_workload_marks_failed(monkeypatch):
    from astinuslab import catalog
    from astinuslab.core.config import settings
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground import workload_provisioning as wp
    from astinuslab.playground.orchestrator import create_network, create_session, provision_vm_workload

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())

    create_session.delay(job_id="pg-1").get()
    create_network.delay(job_id="pg-1", network_id="n1", name="ab", subnet_cidr="10.42.1.0/24", isolated=True).get()
    with SessionLocal() as s:
        pr.register_vm(
            s,
            vm_id="v1",
            job_id="pg-1",
            label="web-1",
            image_id="img",
            domain_name="dom",
            disk_path="/tmp/disk.qcow2",
            role="target",
            workload_id="dvwa",
        )
        pr.update_vm_state(s, "v1", "provisioning_workload")
        pr.update_vm_workload_state(s, "v1", "provisioning")
        pr.attach_nic(s, nic_id="nic1", vm_id="v1", network_id="n1", mac="52:54:00:aa:bb:cc")
        pr.update_nic_ip(s, "nic1", "10.42.1.10")
        s.commit()

    destroy_calls: list[tuple[str, str]] = []
    artifact_cleanup_calls: list[Path] = []

    class FakeLifecycle:
        def destroy(self, uri, name):
            destroy_calls.append((uri, name))

    autospec_setattr(
        monkeypatch,
        catalog,
        "workload_by_id",
        lambda workload_id: {"id": workload_id, "display_name": "DVWA", "services": []},
    )
    autospec_setattr(monkeypatch, po, "lookup_playground_image", lambda image_id, *, role="target": {"id": image_id})
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: FakeLifecycle())
    autospec_setattr(monkeypatch, po, "_remove_playground_artifacts", artifact_cleanup_calls.append)
    autospec_setattr(
        monkeypatch,
        wp,
        "_provision_playground_workload",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("install failed")),
    )

    with pytest.raises(RuntimeError, match="install failed"):
        provision_vm_workload.delay(vm_id="v1").get()

    with SessionLocal() as s:
        from astinuslab.db.models import JobVM

        vm = s.query(JobVM).filter_by(id="v1").one()
        assert vm.state == "failed"
        assert vm.workload_state == "failed"
        assert vm.workload_error == "install failed"

    assert destroy_calls == [(settings.libvirt_uri, "dom")]
    assert artifact_cleanup_calls == [Path("/tmp/disk.qcow2")]


def test_provision_vm_workload_marks_cleanup_failure_retryable(monkeypatch, tmp_path):
    from astinuslab import catalog
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground import workload_provisioning as wp
    from astinuslab.playground.orchestrator import create_network, create_session, provision_vm_workload

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    create_session.delay(job_id="pg-cleanup-retry").get()
    create_network.delay(
        job_id="pg-cleanup-retry",
        network_id="n-cleanup-retry",
        name="ab",
        subnet_cidr="10.42.9.0/24",
        isolated=True,
    ).get()
    playground_root = tmp_path / "playground"
    disk_path = playground_root / "pg-cleanup-retry" / "web-1.qcow2"
    disk_path.parent.mkdir(parents=True)
    disk_path.write_bytes(b"live overlay")
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(libvirt_uri="qemu:///system", playground_disk_dir=playground_root),
    )
    with SessionLocal() as s:
        pr.register_vm(
            s,
            vm_id="vm-cleanup-retry",
            job_id="pg-cleanup-retry",
            label="web-1",
            image_id="img",
            domain_name="dom-cleanup-retry",
            disk_path=str(disk_path),
            role="target",
            workload_id="dvwa",
        )
        pr.update_vm_state(s, "vm-cleanup-retry", "provisioning_workload")
        pr.update_vm_workload_state(s, "vm-cleanup-retry", "provisioning")
        pr.attach_nic(
            s,
            nic_id="nic-cleanup-retry",
            vm_id="vm-cleanup-retry",
            network_id="n-cleanup-retry",
            mac="52:54:00:aa:bb:09",
        )
        pr.update_nic_ip(s, "nic-cleanup-retry", "10.42.9.10")
        s.commit()

    lifecycle = MagicMock()
    lifecycle.destroy.side_effect = RuntimeError("libvirt unavailable")
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: lifecycle)
    autospec_setattr(
        monkeypatch,
        catalog,
        "workload_by_id",
        lambda workload_id: {"id": workload_id, "display_name": "DVWA", "services": []},
    )
    autospec_setattr(monkeypatch, po, "lookup_playground_image", lambda image_id, *, role="target": {"id": image_id})
    autospec_setattr(
        monkeypatch,
        wp,
        "_provision_playground_workload",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("install failed")),
    )

    with pytest.raises(RuntimeError, match="install failed"):
        provision_vm_workload(vm_id="vm-cleanup-retry")

    with SessionLocal() as s:
        from astinuslab.db.models import JobVM

        vm = s.query(JobVM).filter_by(id="vm-cleanup-retry").one()
        assert vm.state == "teardown_failed"
        assert vm.workload_state == "failed"
        assert "cleanup retry required" in (vm.error_summary or "")
        assert "libvirt unavailable" in (vm.error_summary or "")
    assert disk_path.read_bytes() == b"live overlay", "a running domain must keep its backing overlay"


def _workload_cache_settings(tmp_path):
    return SimpleNamespace(
        playground_disk_dir=tmp_path / "disks",
        playground_default_vcpus=2,
        playground_default_memory_mb=2048,
        libvirt_uri="qemu:///system",
        base_dir=tmp_path,
        data_dir=tmp_path,
        storage_dir=tmp_path / "storage",
    )


def test_spawn_vm_prebaked_cache_hit_boots_on_cached_qcow2(monkeypatch, tmp_path):
    from astinuslab import catalog
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_network, create_session, spawn_vm

    fake_lifecycle = MagicMock()
    fake_lifecycle.get_ip.return_value = "10.42.1.10"
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    acl_calls = []
    autospec_setattr(monkeypatch, po, "_apply_workload_cache_acl", lambda path: acl_calls.append(path))
    monkeypatch.setattr(po, "settings", _workload_cache_settings(tmp_path))

    base_image = tmp_path / "images" / "noble.img"
    base_image.parent.mkdir(parents=True, exist_ok=True)
    base_image.write_bytes(b"base cloudimg\n")
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda image_id, *, role="target": {"id": image_id, "path": str(base_image), "format": "qcow2"},
    )
    autospec_setattr(
        monkeypatch,
        catalog,
        "workload_by_id",
        lambda workload_id: {"id": workload_id, "display_name": "DVWA", "services": []},
    )
    autospec_setattr(monkeypatch, po.provision_vm_workload, "delay", lambda **kwargs: None)

    # Pre-create the cache entry at the exact path the real key resolves to, so
    # the spawn lookup hits it (mirrors a prior Jobs bake).
    key = po._playground_workload_cache_key("ubuntu-24.04", "dvwa", base_image)
    cache_file = po._playground_workload_cache_path("ubuntu-24.04", "dvwa", key)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_bytes(b"prebaked overlay\n")

    create_session.delay(job_id="pg-1").get()
    create_network.delay(job_id="pg-1", network_id="n1", name="ab", subnet_cidr="10.42.1.0/24", isolated=True).get()

    spawn_vm.delay(
        job_id="pg-1",
        vm_id="v1",
        label="web-1",
        image_id="ubuntu-24.04",
        nic_specs=[{"network_id": "n1", "nic_id": "nic1"}],
        workload_id="dvwa",
    ).get()

    boot_kwargs = fake_lifecycle.boot_playground.call_args.kwargs
    assert acl_calls == [cache_file]
    assert Path(boot_kwargs["base_image_path"]) == cache_file
    with SessionLocal() as s:
        from astinuslab.db.models import JobVM

        vm = s.query(JobVM).filter_by(id="v1").one()
        assert vm.state == "provisioning_workload"
        assert vm.workload_state == "prebaked"


def test_spawn_vm_cache_miss_boots_on_base_image(monkeypatch, tmp_path):
    from astinuslab import catalog
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_network, create_session, spawn_vm

    fake_lifecycle = MagicMock()
    fake_lifecycle.get_ip.return_value = "10.42.1.10"
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    acl_calls = []
    autospec_setattr(monkeypatch, po, "_apply_workload_cache_acl", lambda path: acl_calls.append(path))
    monkeypatch.setattr(po, "settings", _workload_cache_settings(tmp_path))

    base_image = tmp_path / "images" / "noble.img"
    base_image.parent.mkdir(parents=True, exist_ok=True)
    base_image.write_bytes(b"base cloudimg\n")
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda image_id, *, role="target": {"id": image_id, "path": str(base_image), "format": "qcow2"},
    )
    autospec_setattr(
        monkeypatch,
        catalog,
        "workload_by_id",
        lambda workload_id: {"id": workload_id, "display_name": "DVWA", "services": []},
    )
    autospec_setattr(monkeypatch, po.provision_vm_workload, "delay", lambda **kwargs: None)

    # No cache file exists → miss → boot on the plain base image, live-install path.
    create_session.delay(job_id="pg-1").get()
    create_network.delay(job_id="pg-1", network_id="n1", name="ab", subnet_cidr="10.42.1.0/24", isolated=True).get()

    spawn_vm.delay(
        job_id="pg-1",
        vm_id="v1",
        label="web-1",
        image_id="ubuntu-24.04",
        nic_specs=[{"network_id": "n1", "nic_id": "nic1"}],
        workload_id="dvwa",
    ).get()

    boot_kwargs = fake_lifecycle.boot_playground.call_args.kwargs
    assert Path(boot_kwargs["base_image_path"]) == base_image
    with SessionLocal() as s:
        from astinuslab.db.models import JobVM

        vm = s.query(JobVM).filter_by(id="v1").one()
        assert vm.workload_state == "provisioning"


def test_provision_vm_workload_prebaked_skips_install(monkeypatch):
    from astinuslab import catalog
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground import workload_provisioning as wp
    from astinuslab.playground import workloads as pw
    from astinuslab.playground.orchestrator import create_network, create_session, provision_vm_workload

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())

    create_session.delay(job_id="pg-1").get()
    create_network.delay(job_id="pg-1", network_id="n1", name="ab", subnet_cidr="10.42.1.0/24", isolated=True).get()
    with SessionLocal() as s:
        pr.register_vm(
            s,
            vm_id="v1",
            job_id="pg-1",
            label="web-1",
            image_id="img",
            domain_name="dom",
            disk_path="/tmp/disk.qcow2",
            role="target",
            workload_id="dvwa",
        )
        pr.update_vm_state(s, "v1", "provisioning_workload")
        pr.update_vm_workload_state(s, "v1", "prebaked")
        pr.attach_nic(s, nic_id="nic1", vm_id="v1", network_id="n1", mac="52:54:00:aa:bb:cc")
        pr.update_nic_ip(s, "nic1", "10.42.1.10")
        s.commit()

    autospec_setattr(
        monkeypatch,
        catalog,
        "workload_by_id",
        lambda workload_id: {"id": workload_id, "display_name": "DVWA", "services": []},
    )
    autospec_setattr(monkeypatch, po, "lookup_playground_image", lambda image_id, *, role="target": {"id": image_id})

    def _fail_if_installed(**kwargs):
        raise AssertionError("prebaked provision must not run the live install")

    autospec_setattr(monkeypatch, wp, "_provision_playground_workload", _fail_if_installed)
    health_calls = []
    autospec_setattr(
        monkeypatch,
        pw,
        "best_effort_health_check",
        lambda workload, guest_ip: health_calls.append((workload["id"], guest_ip)),
    )

    provision_vm_workload.delay(vm_id="v1").get()

    assert health_calls == [("dvwa", "10.42.1.10")]
    with SessionLocal() as s:
        from astinuslab.db.models import JobVM

        vm = s.query(JobVM).filter_by(id="v1").one()
        assert vm.state == "running"
        assert vm.workload_state == "ready"
        assert vm.workload_error is None


def test_spawn_vm_unknown_image_raises_before_vm_row(monkeypatch, tmp_path):
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_session, spawn_vm

    autospec_setattr(monkeypatch, po, "lookup_playground_image", lambda _, *, role="target": None)
    disk_root = tmp_path / "playground"
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(playground_disk_dir=disk_root, libvirt_uri="qemu:///system", base_dir=tmp_path),
    )

    create_session.delay(job_id="pg-1").get()
    with pytest.raises(Exception):
        spawn_vm.delay(
            job_id="pg-1",
            vm_id="v1",
            label="x",
            image_id="missing",
            nic_specs=[],
        ).get()

    with SessionLocal() as s:
        from astinuslab.db.models import JobVM

        assert s.query(JobVM).filter_by(id="v1").one_or_none() is None
    assert not (disk_root / "pg-1").exists()


def test_spawn_vm_retry_rejects_mismatched_immutable_custody(monkeypatch, tmp_path):
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_session, spawn_vm

    disk_root = tmp_path / "playground"
    base_image = tmp_path / "base.qcow2"
    base_image.write_bytes(b"base")
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(
            playground_disk_dir=disk_root,
            playground_default_vcpus=2,
            playground_default_memory_mb=2048,
            libvirt_uri="qemu:///system",
            base_dir=tmp_path,
        ),
    )
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda _image_id, *, role="target": {"id": "img", "path": str(base_image)},
    )
    create_session.delay(job_id="pg-1").get()
    with SessionLocal() as session:
        pr.register_vm(
            session,
            vm_id="v1",
            job_id="pg-1",
            label="original",
            image_id="img",
            domain_name="astinuslab-pg-pg-1-original",
            disk_path=str(disk_root.resolve() / "pg-1" / "original.qcow2"),
        )
        pr.update_vm_state(session, "v1", "teardown_failed")
        session.commit()

    with pytest.raises(RuntimeError, match="VM retry identity mismatch"):
        spawn_vm(
            job_id="pg-1",
            vm_id="v1",
            label="replacement",
            image_id="img",
            nic_specs=[],
        )

    with SessionLocal() as session:
        from astinuslab.db.models import JobVM

        vm = session.query(JobVM).filter_by(id="v1").one()
        assert vm.state == "teardown_failed"
        assert vm.label == "original"
    assert not (disk_root / "pg-1").exists()


def test_spawn_vm_uses_verified_immutable_backing_without_catalog_or_cloud_init(monkeypatch, tmp_path):
    from astinuslab.playground import cloud_init_seed as cis
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_network, create_session, spawn_vm

    disk_root = tmp_path / "playground"
    immutable_layer = tmp_path / "immutable.qcow2"
    immutable_layer.write_bytes(b"immutable")
    fake_lifecycle = MagicMock()
    fake_lifecycle.get_ip.return_value = "10.42.1.10"
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(
            playground_disk_dir=disk_root,
            playground_default_vcpus=2,
            playground_default_memory_mb=2048,
            libvirt_uri="qemu:///system",
            base_dir=tmp_path,
            data_dir=tmp_path,
            storage_dir=tmp_path / "storage",
        ),
    )
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("locked boot must not consult catalog")),
    )
    monkeypatch.setattr(
        cis,
        "_write_playground_cloud_init",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("locked boot must not generate cloud-init")),
    )
    create_session.delay(job_id="pg-locked").get()
    create_network.delay(
        job_id="pg-locked",
        network_id="net-locked",
        name="lab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
    ).get()

    spawn_vm(
        job_id="pg-locked",
        vm_id="vm-locked",
        label="target",
        image_id="immutable-target",
        nic_specs=[{"network_id": "net-locked", "nic_id": "nic-locked", "mac": "52:54:00:00:00:05"}],
        vcpus=1,
        memory_mb=512,
        disk_gb=1,
        resources_resolved=True,
        immutable_backing_path=str(immutable_layer),
    )

    assert fake_lifecycle.boot_playground.call_args.kwargs["base_image_path"] == immutable_layer


def test_spawn_vm_retry_rejects_mismatched_persisted_disk_without_rewriting(monkeypatch, tmp_path):
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_session, spawn_vm

    disk_root = tmp_path / "playground"
    base_image = tmp_path / "base.qcow2"
    base_image.write_bytes(b"base")
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(
            playground_disk_dir=disk_root,
            playground_default_vcpus=2,
            playground_default_memory_mb=2048,
            libvirt_uri="qemu:///system",
            base_dir=tmp_path,
        ),
    )
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda _image_id, *, role="target": {"id": "img", "path": str(base_image), "disk_gb": 24},
    )
    create_session.delay(job_id="pg-1").get()
    with SessionLocal() as session:
        pr.register_vm(
            session,
            vm_id="v1",
            job_id="pg-1",
            label="web",
            image_id="img",
            domain_name="astinuslab-pg-pg-1-web",
            disk_path=str(disk_root.resolve() / "pg-1" / "web.qcow2"),
            vcpus=2,
            memory_mb=2048,
            disk_gb=None,
        )
        pr.update_vm_state(session, "v1", "teardown_failed")
        session.commit()

    with pytest.raises(RuntimeError, match="VM retry identity mismatch"):
        spawn_vm(
            job_id="pg-1",
            vm_id="v1",
            label="web",
            image_id="img",
            nic_specs=[],
            disk_gb=24,
        )

    with SessionLocal() as session:
        from astinuslab.db.models import JobVM

        vm = session.query(JobVM).filter_by(id="v1").one()
        assert (vm.vcpus, vm.memory_mb, vm.disk_gb) == (2, 2048, None)


def test_marked_spawn_vm_rejects_invalid_resources_and_marks_pending_failed(monkeypatch, tmp_path):
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_network, create_session, spawn_vm

    disk_root = tmp_path / "playground"
    base_image = tmp_path / "base.qcow2"
    base_image.write_bytes(b"base")
    fake_lifecycle = MagicMock()
    fake_lifecycle.get_ip.return_value = None
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(
            playground_disk_dir=disk_root,
            playground_default_vcpus=2,
            playground_default_memory_mb=2048,
            libvirt_uri="qemu:///system",
            base_dir=tmp_path,
            data_dir=tmp_path,
            storage_dir=tmp_path / "storage",
        ),
    )
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda _image_id, *, role="target": {"id": "img", "path": str(base_image)},
    )
    create_session.delay(job_id="pg-1").get()
    create_network.delay(
        job_id="pg-1",
        network_id="n1",
        name="lab",
        subnet_cidr="10.42.10.0/24",
        isolated=True,
    ).get()
    with SessionLocal() as session:
        pr.register_vm(
            session,
            vm_id="v1",
            job_id="pg-1",
            label="web",
            image_id="img",
            domain_name="astinuslab-pg-pg-1-web",
            disk_path=str(disk_root.resolve() / "pg-1" / "web.qcow2"),
            vcpus=2,
            memory_mb=2048,
            disk_gb=4096,
        )
        pr.attach_nic(session, nic_id="nic1", vm_id="v1", network_id="n1", mac="52:54:00:00:00:01")
        session.commit()

    with pytest.raises(RuntimeError, match="VM resource"):
        spawn_vm(
            job_id="pg-1",
            vm_id="v1",
            label="web",
            image_id="img",
            nic_specs=[{"network_id": "n1", "nic_id": "nic1", "mac": "52:54:00:00:00:01"}],
            vcpus=2,
            memory_mb=2048,
            disk_gb=4096,
            resources_resolved=True,
        )

    with SessionLocal() as session:
        from astinuslab.db.models import JobVM

        vm = session.query(JobVM).filter_by(id="v1").one()
        assert vm.state == "failed"
        assert (vm.vcpus, vm.memory_mb, vm.disk_gb) == (2, 2048, 4096)
    fake_lifecycle.boot_playground.assert_not_called()


@pytest.mark.parametrize("prior_state", ["destroy_queued", "starting", "teardown_failed"])
def test_invalid_marked_spawn_does_not_overwrite_non_pending_state(monkeypatch, tmp_path, prior_state):
    from astinuslab.db.models import JobVM
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_session, spawn_vm

    disk_root = tmp_path / "playground"
    base_image = tmp_path / "base.qcow2"
    base_image.write_bytes(b"base")
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(
            playground_disk_dir=disk_root,
            playground_default_vcpus=2,
            playground_default_memory_mb=2048,
            libvirt_uri="qemu:///system",
            base_dir=tmp_path,
            data_dir=tmp_path,
            storage_dir=tmp_path / "storage",
        ),
    )
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda _image_id, *, role="target": {"id": "img", "path": str(base_image)},
    )
    create_session.delay(job_id="pg-1").get()
    with SessionLocal() as session:
        pr.register_vm(
            session,
            vm_id="v1",
            job_id="pg-1",
            label="web",
            image_id="img",
            domain_name="astinuslab-pg-pg-1-web",
            disk_path=str(disk_root.resolve() / "pg-1" / "web.qcow2"),
            vcpus=2,
            memory_mb=2048,
            disk_gb=4096,
        )
        pr.update_vm_state(session, "v1", prior_state, error="keep-diagnostic")
        session.commit()

    with pytest.raises(RuntimeError, match="VM resource"):
        spawn_vm(
            job_id="pg-1",
            vm_id="v1",
            label="web",
            image_id="img",
            nic_specs=[],
            vcpus=2,
            memory_mb=2048,
            disk_gb=4096,
            resources_resolved=True,
        )

    with SessionLocal() as session:
        vm = session.query(JobVM).filter_by(id="v1").one()
        assert vm.state == prior_state
        assert vm.error_summary == "keep-diagnostic"
        assert (vm.vcpus, vm.memory_mb, vm.disk_gb) == (2, 2048, 4096)


def test_spawn_vm_preserves_unconfirmed_prior_artifacts_when_image_file_disappears(monkeypatch, tmp_path):
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_network, create_session, spawn_vm

    missing_image = tmp_path / "deleted.qcow2"
    fake_lifecycle = MagicMock()
    fake_lifecycle.boot_playground.side_effect = AssertionError("boot should not run")
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda _, *, role="target": {
            "id": "img",
            "path": str(missing_image),
            "format": "qcow2",
            "architecture": "x86_64",
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

    create_session.delay(job_id="pg-1").get()
    create_network.delay(
        job_id="pg-1",
        network_id="n1",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
    ).get()
    prior_overlay = tmp_path / "playground" / "pg-1" / "web-1.qcow2"
    prior_overlay.parent.mkdir(parents=True)
    prior_overlay.write_bytes(b"unconfirmed-prior-domain-overlay")
    with SessionLocal() as s:
        pr.register_vm(
            s,
            vm_id="v1",
            job_id="pg-1",
            label="web-1",
            image_id="img",
            domain_name="astinuslab-pg-pg-1-web-1",
            disk_path=str(tmp_path / "playground" / "pg-1" / "web-1.qcow2"),
        )
        s.commit()

    with pytest.raises(Exception, match="image not available"):
        spawn_vm.delay(
            job_id="pg-1",
            vm_id="v1",
            label="web-1",
            image_id="img",
            nic_specs=[{"network_id": "n1", "nic_id": "nic1"}],
        ).get()

    fake_lifecycle.boot_playground.assert_not_called()
    with SessionLocal() as s:
        from astinuslab.db.models import JobVM

        vm = s.query(JobVM).filter_by(id="v1").one()
        assert vm.state == "teardown_failed"
        assert "image not available" in (vm.error_summary or "")
        assert "lifecycle unavailable" in (vm.error_summary or "")
    assert prior_overlay.read_bytes() == b"unconfirmed-prior-domain-overlay"


def test_spawn_vm_marks_teardown_failed_when_exception_cleanup_needs_retry(monkeypatch, tmp_path):
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_session, spawn_vm

    missing_image = tmp_path / "deleted.qcow2"
    playground_root = tmp_path / "playground"
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(
            playground_disk_dir=playground_root,
            playground_default_vcpus=2,
            playground_default_memory_mb=2048,
            libvirt_uri="qemu:///system",
            base_dir=tmp_path,
            data_dir=tmp_path,
        ),
    )
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda _, *, role="target": {
            "id": "img",
            "path": str(missing_image),
            "format": "qcow2",
            "architecture": "x86_64",
        },
    )
    autospec_setattr(
        monkeypatch,
        po,
        "_cleanup_spawn_artifacts",
        lambda **_: "artifact cleanup failed: replacement detected",
    )

    create_session.delay(job_id="pg-1").get()
    with SessionLocal() as s:
        pr.register_vm(
            s,
            vm_id="v1",
            job_id="pg-1",
            label="web-1",
            image_id="img",
            domain_name="astinuslab-pg-pg-1-web-1",
            disk_path=str(playground_root / "pg-1" / "web-1.qcow2"),
        )
        s.commit()

    with pytest.raises(Exception, match="image not available"):
        spawn_vm.delay(
            job_id="pg-1",
            vm_id="v1",
            label="web-1",
            image_id="img",
            nic_specs=[],
        ).get()

    with SessionLocal() as s:
        from astinuslab.db.models import JobVM

        vm = s.query(JobVM).filter_by(id="v1").one()
        assert vm.state == "teardown_failed"
        assert "replacement detected" in (vm.error_summary or "")


def test_spawn_vm_marks_failed_when_nic_setup_fails(monkeypatch, tmp_path):
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_session, spawn_vm

    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda _, *, role="target": {
            "id": "img",
            "path": "/var/lib/astinuslab/images/img.qcow2",
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

    create_session.delay(job_id="pg-1").get()

    with pytest.raises(Exception):
        spawn_vm.delay(
            job_id="pg-1",
            vm_id="v1",
            label="web-1",
            image_id="img",
            nic_specs=[{"network_id": "missing-net", "nic_id": "nic1"}],
        ).get()

    with SessionLocal() as s:
        from astinuslab.db.models import JobVM

        vm = s.query(JobVM).filter_by(id="v1").one()
        assert vm.state == "teardown_failed"


def test_spawn_vm_rejects_network_from_another_session(monkeypatch, tmp_path):
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_network, create_session, spawn_vm

    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda _, *, role="target": {
            "id": "img",
            "path": "/var/lib/astinuslab/images/img.qcow2",
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
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())

    create_session.delay(job_id="pg-1").get()
    create_network.delay(
        job_id="pg-1",
        network_id="n1",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
    ).get()
    with SessionLocal() as s:
        pr.mark_session_completed(s, "pg-1")
        pr.create_playground_session(s, job_id="pg-2")
        s.commit()

    with pytest.raises(Exception):
        spawn_vm.delay(
            job_id="pg-2",
            vm_id="v2",
            label="web-2",
            image_id="img",
            nic_specs=[{"network_id": "n1", "nic_id": "nic1"}],
        ).get()

    with SessionLocal() as s:
        from astinuslab.db.models import JobVM, JobVMNic

        vm = s.query(JobVM).filter_by(id="v2").one()
        assert vm.state == "teardown_failed"
        assert s.query(JobVMNic).filter_by(job_vm_id="v2").count() == 0


def test_spawn_vm_cleans_partial_boot_artifacts_when_boot_raises(monkeypatch, tmp_path):
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_network, create_session, spawn_vm

    destroy_calls = []

    class FakeLifecycle:
        def boot_playground(self, **kwargs):
            kwargs["disk_path"].write_bytes(b"overlay")
            kwargs["disk_path"].with_suffix(".iso").write_bytes(b"cloud-init")
            raise RuntimeError("define failed")

        def destroy(self, uri, domain_name):
            destroy_calls.append((uri, domain_name))

    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: FakeLifecycle())
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda _, *, role="target": {
            "id": "img",
            "path": "/var/lib/astinuslab/images/img.qcow2",
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

    create_session.delay(job_id="pg-1").get()
    create_network.delay(
        job_id="pg-1",
        network_id="n1",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
    ).get()

    with pytest.raises(RuntimeError, match="define failed"):
        spawn_vm.delay(
            job_id="pg-1",
            vm_id="v1",
            label="web-1",
            image_id="img",
            nic_specs=[{"network_id": "n1", "nic_id": "nic1"}],
        ).get()

    disk = tmp_path / "playground" / "pg-1" / "web-1.qcow2"
    assert not disk.exists()
    assert not disk.with_suffix(".iso").exists()
    assert destroy_calls == [("qemu:///system", "astinuslab-pg-pg-1-web-1")]
    with SessionLocal() as s:
        from astinuslab.db.models import JobVM

        vm = s.query(JobVM).filter_by(id="v1").one()
        assert vm.state == "failed"
        assert vm.error_summary == "define failed"


def test_spawn_vm_rejects_unsafe_label_before_disk_path(monkeypatch, tmp_path):
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_session, spawn_vm

    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda _, *, role="target": {
            "id": "img",
            "path": "/var/lib/astinuslab/images/img.qcow2",
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

    create_session.delay(job_id="pg-1").get()

    with pytest.raises(Exception, match="invalid playground label"):
        spawn_vm.delay(
            job_id="pg-1",
            vm_id="v1",
            label="../escape",
            image_id="img",
            nic_specs=[],
        ).get()

    with SessionLocal() as s:
        from astinuslab.db.models import JobVM

        assert s.query(JobVM).filter_by(id="v1").one_or_none() is None


def test_spawn_vm_cleans_domain_and_disk_when_ip_lookup_fails(monkeypatch, tmp_path):
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import (
        create_network,
        create_session,
        spawn_vm,
    )

    fake_lifecycle = MagicMock()

    def _boot_playground(**kwargs):
        kwargs["disk_path"].write_bytes(b"qcow2")

    fake_lifecycle.boot_playground.side_effect = _boot_playground
    fake_lifecycle.get_ip.side_effect = RuntimeError("ip timeout")
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda _, *, role="target": {
            "id": "img",
            "path": "/var/lib/astinuslab/images/img.qcow2",
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
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())

    create_session.delay(job_id="pg-1").get()
    create_network.delay(
        job_id="pg-1",
        network_id="n1",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
    ).get()

    # Phase 3: per-NIC IP failures are graceful — VM still boots, NIC IP stays null.
    spawn_vm.delay(
        job_id="pg-1",
        vm_id="v1",
        label="web-1",
        image_id="img",
        nic_specs=[{"network_id": "n1", "nic_id": "nic1"}],
    ).get()

    with SessionLocal() as s:
        from astinuslab.db.models import JobVM, JobVMNic

        vm = s.query(JobVM).filter_by(id="v1").one()
        assert vm.state == "running"
        nic = s.query(JobVMNic).filter_by(id="nic1").one()
        assert nic.ip is None


def test_destroy_vm_cleans_libvirt_and_disk_and_marks_destroyed(monkeypatch, tmp_path):
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import (
        create_network,
        create_session,
        destroy_vm,
        spawn_vm,
    )

    fake_lifecycle = MagicMock()
    fake_lifecycle.get_ip.return_value = "10.42.1.10"
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda _, *, role="target": {
            "id": "img",
            "path": "/var/lib/astinuslab/images/img.qcow2",
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

    create_session.delay(job_id="pg-1").get()
    create_network.delay(
        job_id="pg-1",
        network_id="n1",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
    ).get()
    spawn_vm.delay(
        job_id="pg-1",
        vm_id="v1",
        label="web-1",
        image_id="img",
        nic_specs=[{"network_id": "n1", "nic_id": "nic1"}],
    ).get()

    disk = tmp_path / "playground" / "pg-1" / "web-1.qcow2"
    disk.parent.mkdir(parents=True, exist_ok=True)
    disk.write_bytes(b"qcow2-data")
    legacy_seed = disk.with_suffix(".iso")
    legacy_seed.write_bytes(b"legacy-seed")
    seed = disk.with_name("web-1-seed.iso")
    seed.write_bytes(b"seed")
    cloud_init_dir = disk.parent / "web-1-cloud-init"
    cloud_init_dir.mkdir()
    (cloud_init_dir / "user-data").write_text("#cloud-config\n", encoding="utf-8")
    capture = tmp_path / "captures" / "pg-1" / "v1" / "memory.raw"
    capture.parent.mkdir(parents=True)
    capture.write_bytes(b"evidence")

    result = destroy_vm.delay(vm_id="v1").get()

    assert result == "v1"
    fake_lifecycle.destroy.assert_called_once_with("qemu:///system", "astinuslab-pg-pg-1-web-1")
    assert not disk.exists()
    assert not legacy_seed.exists()
    assert not seed.exists()
    assert not cloud_init_dir.exists()
    assert not disk.parent.exists(), "an empty per-session staging directory should be removed"
    assert capture.read_bytes() == b"evidence"
    with SessionLocal() as s:
        from astinuslab.db.models import JobVM

        vm = s.query(JobVM).filter_by(id="v1").one()
        assert vm.state == "destroyed"


@pytest.mark.parametrize(
    "competing_owner", ["spawn", "teardown-retry", "workload", "powering_on", "powering_off", "rebooting"]
)
def test_public_vm_destroy_cannot_steal_active_custody(monkeypatch, competing_owner):
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_session, destroy_vm

    lifecycle = MagicMock()
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: lifecycle)
    create_session.delay(job_id="pg-1").get()
    with SessionLocal() as session:
        pr.register_vm(
            session,
            vm_id="v1",
            job_id="pg-1",
            label="web",
            image_id="img",
            domain_name="astinuslab-pg-pg-1-web",
            disk_path="/tmp/web.qcow2",
            workload_id="dvwa" if competing_owner == "workload" else None,
        )
        if competing_owner == "spawn":
            pr.update_vm_state(session, "v1", "failed")
            assert pr.claim_vm_for_spawn(session, "v1") is True
        elif competing_owner == "teardown-retry":
            pr.update_vm_state(session, "v1", "teardown_failed")
            assert pr.claim_vm_for_teardown_retry(session, "v1") is True
        elif competing_owner == "workload":
            pr.update_vm_state(session, "v1", "provisioning_workload")
            assert pr.claim_vm_for_workload(session, "v1") is True
        else:
            prior_state = "stopped" if competing_owner == "powering_on" else "running"
            pr.update_vm_state(session, "v1", prior_state)
            assert (
                pr.claim_vm_for_power_action(session, "v1", expected_state=prior_state, claim_state=competing_owner)
                is not None
            )
        assert pr.queue_vm_for_destroy(session, "v1") is False
        session.commit()

    with pytest.raises(RuntimeError, match="busy"):
        destroy_vm(vm_id="v1")

    lifecycle.destroy.assert_not_called()


@pytest.mark.parametrize("cleanup", ["vm", "session"])
def test_unresponsive_graceful_stop_can_be_explicitly_destroyed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cleanup: str
) -> None:
    from astinuslab.api.schemas import PlaygroundVMActionRequest
    from astinuslab.db.models import JobVMNic
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.routes import sessions, vm_actions

    disk = tmp_path / "playground" / "pg-1" / "web.qcow2"
    disk.parent.mkdir(parents=True)
    disk.write_bytes(b"owned overlay")
    monkeypatch.setattr(
        po, "settings", SimpleNamespace(playground_disk_dir=disk.parent.parent, libvirt_uri="qemu:///test")
    )
    po.create_session(job_id="pg-1")
    with SessionLocal() as session:
        pr.register_vm(
            session,
            vm_id="v1",
            job_id="pg-1",
            label="web",
            image_id="img",
            domain_name="astinuslab-pg-pg-1-web",
            disk_path=str(disk),
        )
        pr.update_vm_state(session, "v1", "running")
        net = pr.register_network(
            session,
            network_id="n1",
            job_id="pg-1",
            name="lan",
            subnet_cidr="10.42.1.0/24",
            isolated=True,
            libvirt_net_name="astinuslab-pg-pg-1-lan",
        )
        net.state = "active"
        pr.attach_nic(session, nic_id="nic1", vm_id="v1", network_id="n1", mac="52:54:00:aa:bb:01")
        session.commit()

    host = {"active": True}

    def destroy_domain(uri: str, name: str) -> None:
        assert (uri, name) == ("qemu:///test", "astinuslab-pg-pg-1-web")
        host["active"] = False

    def destroy_network(name: str) -> None:
        assert name == "astinuslab-pg-pg-1-lan"
        assert host["active"] is False
        with SessionLocal() as session:
            assert session.query(JobVM).filter_by(id="v1").one().state == "destroyed"
            assert session.get(JobVMNic, "nic1") is None

    lifecycle = SimpleNamespace(destroy=MagicMock(side_effect=destroy_domain))
    networks = SimpleNamespace(destroy_network=MagicMock(side_effect=destroy_network))
    monkeypatch.setattr(po, "_vm_lifecycle", lambda: lifecycle)
    monkeypatch.setattr(nl, "_networks_helper", lambda: networks)
    # virsh acknowledges the request; the guest ignores graceful shutdown.
    shutdown = MagicMock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(vm_actions.subprocess, "run", shutdown)
    vm_actions.vm_action_endpoint("v1", PlaygroundVMActionRequest(action="stop"))
    assert shutdown.call_args.args[0] == ["virsh", "-c", "qemu:///test", "shutdown", "astinuslab-pg-pg-1-web"]
    assert host["active"] is True
    with SessionLocal() as session:
        assert session.query(JobVM).filter_by(id="v1").one().state == "stopping"

    if cleanup == "vm":
        vm_actions.destroy_vm_endpoint("v1")
        networks.destroy_network.assert_not_called()
        sessions.destroy_session_endpoint("pg-1")
    else:
        sessions.destroy_session_endpoint("pg-1")
    lifecycle.destroy.assert_called_once()
    networks.destroy_network.assert_called_once()
    assert not disk.exists()
    with SessionLocal() as session:
        assert session.query(JobVM).filter_by(id="v1").one().state == "destroyed"
        assert session.query(PlaygroundSession).filter_by(id="pg-1").one().status == "cancelled"


@pytest.mark.parametrize(("final_state", "workload_state"), [("running", "ready"), ("failed", "failed")])
def test_workload_finish_and_destroy_claim_cannot_overwrite_each_other(final_state, workload_state):
    from astinuslab.db.models import JobVM

    with SessionLocal() as session:
        pr.create_playground_session(session, job_id="pg-1")
        pr.register_vm(
            session,
            vm_id="v1",
            job_id="pg-1",
            label="web",
            image_id="img",
            domain_name="astinuslab-pg-pg-1-web",
            disk_path="/tmp/web.qcow2",
            workload_id="dvwa",
        )
        pr.update_vm_state(session, "v1", "provisioning_workload")
        assert pr.claim_vm_for_workload(session, "v1") is True
        claimed_vm = session.get(JobVM, "v1")
        assert claimed_vm is not None
        session.refresh(claimed_vm)
        claim_token = claimed_vm.updated_at
        assert pr.claim_vm_for_destroy(session, "v1") is False
        assert (
            pr.finish_vm_workload(
                session,
                "v1",
                final_state,
                workload_state,
                expected_updated_at=claim_token,
            )
            is True
        )
        assert pr.claim_vm_for_destroy(session, "v1") is True
        assert (
            pr.finish_vm_workload(
                session,
                "v1",
                "running",
                "ready",
                expected_updated_at=claim_token,
            )
            is False
        )
        session.commit()

    with SessionLocal() as session:
        from astinuslab.db.models import JobVM

        vm = session.get(JobVM, "v1")
        assert vm is not None
        assert vm.state == "destroying"


def test_late_create_deliveries_cannot_resurrect_terminal_resources():
    from astinuslab.db.models import JobVM, PlaygroundSession

    with SessionLocal() as session:
        pr.create_playground_session(session, job_id="pg-terminal")
        pr.register_network(
            session,
            network_id="net-terminal",
            job_id="pg-terminal",
            name="lan",
            subnet_cidr="10.42.31.0/24",
            isolated=True,
            libvirt_net_name="astinuslab-pg-pg-terminal-lan",
        )
        pr.update_network_state(session, "net-terminal", "destroyed")
        pr.register_vm(
            session,
            vm_id="vm-terminal",
            job_id="pg-terminal",
            label="web",
            image_id="img",
            domain_name="astinuslab-pg-pg-terminal-web",
            disk_path="/tmp/terminal.qcow2",
        )
        pr.update_vm_state(session, "vm-terminal", "stopped")

        assert pr.claim_network_for_create(session, "net-terminal") is False
        assert pr.claim_vm_for_spawn(session, "vm-terminal") is False
        pr.update_vm_state(session, "vm-terminal", "destroyed")
        assert pr.claim_vm_for_spawn(session, "vm-terminal") is False

        vm = session.get(JobVM, "vm-terminal")
        assert vm is not None
        assert vm.state == "destroyed"

        playground_session = session.get(PlaygroundSession, "pg-terminal")
        assert playground_session is not None
        playground_session.status = "cancelled"
        pr.update_network_state(session, "net-terminal", "failed")
        pr.update_vm_state(session, "vm-terminal", "failed")
        assert pr.claim_network_for_create(session, "net-terminal") is False
        assert pr.claim_vm_for_spawn(session, "vm-terminal") is False
        vm = session.get(JobVM, "vm-terminal")
        assert vm is not None
        assert vm.state == "failed"


def test_artifact_cleanup_preserves_nonempty_session_directory(monkeypatch, tmp_path):
    from astinuslab.playground import orchestrator as po

    playground_disk_dir = tmp_path / "playground"
    disk = playground_disk_dir / "pg-1" / "web-1.qcow2"
    disk.parent.mkdir(parents=True)
    disk.write_bytes(b"disk")
    sibling = disk.parent / "operator-note.txt"
    sibling.write_text("preserve", encoding="utf-8")
    monkeypatch.setattr(po, "settings", SimpleNamespace(playground_disk_dir=playground_disk_dir))

    po._remove_playground_artifacts(disk)

    assert not disk.exists()
    assert sibling.read_text(encoding="utf-8") == "preserve"
    assert disk.parent.is_dir()


def test_artifact_cleanup_is_idempotent_when_session_directory_is_already_missing(monkeypatch, tmp_path):
    from astinuslab.playground import orchestrator as po

    playground_disk_dir = tmp_path / "playground"
    playground_disk_dir.mkdir()
    disk = playground_disk_dir / "already-removed" / "web-1.qcow2"
    monkeypatch.setattr(po, "settings", SimpleNamespace(playground_disk_dir=playground_disk_dir))

    po._remove_playground_artifacts(disk)

    assert not disk.parent.exists()


def test_destroy_vm_deletes_disk_and_marks_destroyed_when_lifecycle_setup_fails(monkeypatch, tmp_path):
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_session, destroy_vm

    playground_disk_dir = tmp_path / "playground"
    disk = playground_disk_dir / "pg-1" / "web-1.qcow2"
    disk.parent.mkdir(parents=True, exist_ok=True)
    disk.write_bytes(b"qcow2-data")
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(playground_disk_dir=playground_disk_dir, libvirt_uri="qemu:///system"),
    )
    monkeypatch.setattr(
        po,
        "_vm_lifecycle",
        MagicMock(side_effect=RuntimeError("libvirt unavailable")),
    )

    create_session.delay(job_id="pg-1").get()
    with SessionLocal() as s:
        pr.register_vm(
            s,
            vm_id="v1",
            job_id="pg-1",
            label="web-1",
            image_id="img",
            domain_name="astinuslab-pg-pg-1-web-1",
            disk_path=str(disk),
        )
        s.commit()

    with pytest.raises(Exception, match="teardown incomplete"):
        destroy_vm.delay(vm_id="v1").get()
    # Disk is preserved when libvirt teardown fails; reconciler retries later.
    assert disk.exists()
    with SessionLocal() as s:
        from astinuslab.db.models import JobVM

        vm = s.query(JobVM).filter_by(id="v1").one()
        assert vm.state == "teardown_failed"


def test_destroy_vm_refuses_disk_cleanup_outside_playground_disk_dir(monkeypatch, tmp_path):
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_session, destroy_vm

    playground_disk_dir = tmp_path / "playground"
    outside_disk = tmp_path / "outside.qcow2"
    outside_disk.write_bytes(b"outside-data")
    fake_lifecycle = MagicMock()
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(playground_disk_dir=playground_disk_dir, libvirt_uri="qemu:///system"),
    )
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)

    create_session.delay(job_id="pg-1").get()
    with SessionLocal() as s:
        pr.register_vm(
            s,
            vm_id="v1",
            job_id="pg-1",
            label="web-1",
            image_id="img",
            domain_name="astinuslab-pg-pg-1-web-1",
            disk_path=str(outside_disk),
        )
        s.commit()

    with pytest.raises(Exception, match="teardown incomplete"):
        destroy_vm.delay(vm_id="v1").get()
    fake_lifecycle.destroy.assert_called_once_with("qemu:///system", "astinuslab-pg-pg-1-web-1")
    assert outside_disk.exists()
    with SessionLocal() as s:
        from astinuslab.db.models import JobVM

        vm = s.query(JobVM).filter_by(id="v1").one()
        assert vm.state == "teardown_failed"


def test_destroy_vm_refuses_symlinked_cloud_init_without_touching_target(monkeypatch, tmp_path):
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_session, destroy_vm

    playground_disk_dir = tmp_path / "playground"
    disk = playground_disk_dir / "pg-1" / "web-1.qcow2"
    disk.parent.mkdir(parents=True, exist_ok=True)
    disk.write_bytes(b"qcow2-data")
    outside_dir = tmp_path / "outside-cloud-init"
    outside_dir.mkdir()
    sentinel = outside_dir / "do-not-delete"
    sentinel.write_bytes(b"preserve")
    cloud_init_link = disk.parent / "web-1-cloud-init"
    cloud_init_link.symlink_to(outside_dir, target_is_directory=True)

    fake_lifecycle = MagicMock()
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(playground_disk_dir=playground_disk_dir, libvirt_uri="qemu:///system"),
    )
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)

    create_session.delay(job_id="pg-1").get()
    with SessionLocal() as s:
        pr.register_vm(
            s,
            vm_id="v1",
            job_id="pg-1",
            label="web-1",
            image_id="img",
            domain_name="astinuslab-pg-pg-1-web-1",
            disk_path=str(disk),
        )
        s.commit()

    with pytest.raises(Exception, match="teardown incomplete"):
        destroy_vm.delay(vm_id="v1").get()

    assert sentinel.read_bytes() == b"preserve"
    assert cloud_init_link.is_symlink()
    with SessionLocal() as s:
        from astinuslab.db.models import JobVM

        vm = s.query(JobVM).filter_by(id="v1").one()
        assert vm.state == "teardown_failed"


def test_cleanup_accepts_an_unchanged_configured_disk_root_symlink(monkeypatch, tmp_path):
    from astinuslab.playground import orchestrator as po

    physical_root = tmp_path / "large-disk" / "playground"
    session_dir = physical_root / "pg-1"
    session_dir.mkdir(parents=True)
    disk = session_dir / "web.qcow2"
    disk.write_bytes(b"overlay")
    configured_root = tmp_path / "configured-playground"
    configured_root.symlink_to(physical_root, target_is_directory=True)
    monkeypatch.setattr(po, "settings", SimpleNamespace(playground_disk_dir=configured_root))

    po._remove_playground_artifacts(disk)

    assert not disk.exists()
    assert not session_dir.exists()
    assert physical_root.is_dir()


def test_regular_artifact_replacement_is_not_unlinked(monkeypatch, tmp_path):
    from astinuslab.playground import filesystem_safety as fs

    parent = tmp_path / "playground"
    parent.mkdir()
    artifact = parent / "disk.qcow2"
    artifact.write_bytes(b"original")
    displaced = parent / "original.qcow2"
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    real_open = os.open
    swapped = False

    def swap_before_descriptor_open(path, flags, *args, dir_fd=None, **kwargs):
        nonlocal swapped
        if path == artifact.name and dir_fd == parent_fd and not swapped:
            swapped = True
            artifact.rename(displaced)
            artifact.write_bytes(b"replacement")
        return real_open(path, flags, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(fs.os, "open", swap_before_descriptor_open)
    try:
        with pytest.raises(RuntimeError, match="replaced during cleanup"):
            fs._unlink_regular_no_follow(parent_fd, artifact.name)
    finally:
        os.close(parent_fd)

    assert artifact.read_bytes() == b"replacement"
    assert displaced.read_bytes() == b"original"


def test_spawn_cleanup_preserves_artifacts_when_domain_destroy_fails(monkeypatch, tmp_path):
    from astinuslab.playground import orchestrator as po

    lifecycle = MagicMock()
    lifecycle.destroy.side_effect = RuntimeError("libvirt down")
    artifact_cleanup = MagicMock()
    autospec_setattr(monkeypatch, po, "_remove_playground_artifacts", artifact_cleanup)
    monkeypatch.setattr(po, "settings", SimpleNamespace(libvirt_uri="qemu:///system", data_dir=tmp_path))

    error = po._cleanup_spawn_artifacts(
        lifecycle=lifecycle,
        domain_name="astinuslab-pg-pg-1-web-1",
        disk_path=tmp_path / "playground" / "pg-1" / "web-1.qcow2",
    )

    assert error is not None
    assert "libvirt down" in error
    artifact_cleanup.assert_not_called()
    assert not list(tmp_path.rglob("cleanup_backlog.txt"))


def test_spawn_retry_preserves_prior_artifacts_when_lifecycle_setup_fails(monkeypatch, tmp_path):
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_session, spawn_vm

    playground_root = tmp_path / "playground"
    disk = playground_root / "pg-1" / "web.qcow2"
    disk.parent.mkdir(parents=True)
    disk.write_bytes(b"prior-domain-overlay")
    image = tmp_path / "base.qcow2"
    image.write_bytes(b"base")
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(
            playground_disk_dir=playground_root,
            playground_default_vcpus=2,
            playground_default_memory_mb=2048,
            libvirt_uri="qemu:///system",
            base_dir=tmp_path,
            data_dir=tmp_path,
        ),
    )
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda _, *, role="target": {
            "id": "img",
            "path": str(image),
            "format": "qcow2",
            "architecture": "x86_64",
        },
    )
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: (_ for _ in ()).throw(RuntimeError("libvirt down")))

    create_session.delay(job_id="pg-1").get()
    with SessionLocal() as session:
        pr.register_vm(
            session,
            vm_id="v1",
            job_id="pg-1",
            label="web",
            image_id="img",
            domain_name="astinuslab-pg-pg-1-web",
            disk_path=str(disk),
        )
        pr.update_vm_state(session, "v1", "teardown_failed")
        session.commit()

    with pytest.raises(Exception, match="libvirt down"):
        spawn_vm.delay(
            job_id="pg-1",
            vm_id="v1",
            label="web",
            image_id="img",
            nic_specs=[],
        ).get()

    assert disk.read_bytes() == b"prior-domain-overlay"
    with SessionLocal() as session:
        from astinuslab.db.models import JobVM

        vm = session.get(JobVM, "v1")
        assert vm is not None
        assert vm.state == "teardown_failed"
        assert "cleanup retry required" in (vm.error_summary or "")


def test_capture_hashing_streams_in_chunks(tmp_path, monkeypatch):
    """A memory image is gigabytes; hashing must never read it whole.

    The helper this replaced lived in the orchestrator. Since Task 19 the
    publication service hashes through its own descriptor, so the guard follows
    it there.
    """
    import hashlib
    import os as os_module

    from astinuslab.storage import publication

    data = (b"a" * (1024 * 1024)) + b"tail"
    payload = tmp_path / "payload"
    payload.write_bytes(data)

    read_sizes = []
    real_read = os_module.read

    def recording_read(fd, size):
        read_sizes.append(size)
        return real_read(fd, size)

    monkeypatch.setattr(os_module, "read", recording_read)

    fd = os_module.open(payload, os_module.O_RDONLY)
    try:
        sha256, size_bytes = publication._hash_payload(fd, payload)
    finally:
        os_module.close(fd)

    assert sha256 == hashlib.sha256(data).hexdigest()
    assert size_bytes == len(data)
    assert read_sizes and max(read_sizes) == 1024 * 1024, "hashing must stay chunked"


def test_capture_memory_creates_acquisition_with_job_vm_id(monkeypatch, tmp_path):
    import hashlib

    from astinuslab.playground import acquisition as pa
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import (
        capture_memory,
        create_network,
        create_session,
        spawn_vm,
    )

    fake_lifecycle = MagicMock()
    fake_lifecycle.get_ip.return_value = "10.42.1.10"
    dump_bytes = b"playground memory bytes"

    def fake_dump(domain_name, output_path):
        output_path.write_bytes(dump_bytes)
        return {"path": str(output_path), "size": len(dump_bytes)}

    fake_dump = MagicMock(side_effect=fake_dump)
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    monkeypatch.setattr(pa, "_dump_memory", fake_dump)
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda _, *, role="target": {
            "id": "img",
            "path": "/var/lib/astinuslab/images/img.qcow2",
            "format": "qcow2",
            "architecture": "x86_64",
            "distro": "ubuntu",
            "version": "22.04",
            "credentials": {"username": "ubuntu", "password": "ubuntu"},
        },
    )
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(
            playground_disk_dir=tmp_path / "playground",
            playground_capture_dir=tmp_path / "captures",
            playground_default_vcpus=2,
            playground_default_memory_mb=2048,
            libvirt_uri="qemu:///system",
            base_dir=tmp_path,
            data_dir=tmp_path,
        ),
    )

    create_session.delay(job_id="pg-1").get()
    create_network.delay(
        job_id="pg-1",
        network_id="n1",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
    ).get()
    spawn_vm.delay(
        job_id="pg-1",
        vm_id="v1",
        label="web-1",
        image_id="img",
        nic_specs=[{"network_id": "n1", "nic_id": "nic1"}],
    ).get()

    result = capture_memory.delay(vm_id="v1").get()

    fake_dump.assert_called_once()
    domain_name, output_path = fake_dump.call_args.args
    assert domain_name == "astinuslab-pg-pg-1-web-1"
    # Since Task 18 the hypervisor writes into an attempt-owned directory inside
    # the capture directory; the published name is asserted through the
    # Acquisition below.
    assert output_path.parent.parent == tmp_path / "captures" / "pg-1" / "v1"
    assert output_path.parent.name.startswith(".publish-")
    with SessionLocal() as s:
        from astinuslab.db.models import Acquisition

        acq = s.query(Acquisition).filter_by(job_vm_id="v1").one()
        assert acq.status == "completed"
        assert acq.id == result
        assert acq.kind == "memory"
        assert acq.session_id == "pg-1"
        assert acq.vm_label == "web-1"
        assert acq.path is not None
        published = Path(acq.path)
        assert published.parent == tmp_path / "captures" / "pg-1" / "v1"
        assert published.name == f"mem-{acq.id}.elf"
        assert published.suffix == ".elf"
        assert published.exists() and not output_path.exists()
        assert acq.sha256 == hashlib.sha256(dump_bytes).hexdigest()
        assert acq.size_bytes == len(dump_bytes)


def test_capture_disk_creates_acquisition_with_job_vm_id(monkeypatch, tmp_path):
    import hashlib

    from astinuslab.playground import acquisition as pa
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import (
        capture_disk,
        create_network,
        create_session,
        spawn_vm,
    )

    fake_lifecycle = MagicMock()
    fake_lifecycle.get_ip.return_value = "10.42.1.10"
    disk_bytes = b"playground disk bytes"
    fake_disk_capture = MagicMock()
    fake_disk_capture.capture.side_effect = lambda **kwargs: kwargs["output_path"].write_bytes(disk_bytes)
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, pa, "_disk_capture", lambda: fake_disk_capture)
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda _, *, role="target": {
            "id": "img",
            "path": "/var/lib/astinuslab/images/img.qcow2",
            "format": "qcow2",
            "architecture": "x86_64",
            "distro": "ubuntu",
            "version": "22.04",
            "credentials": {"username": "ubuntu", "password": "ubuntu"},
        },
    )
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(
            playground_disk_dir=tmp_path / "playground",
            playground_capture_dir=tmp_path / "captures",
            playground_default_vcpus=2,
            playground_default_memory_mb=2048,
            libvirt_uri="qemu:///system",
            base_dir=tmp_path,
            data_dir=tmp_path,
        ),
    )

    create_session.delay(job_id="pg-1").get()
    create_network.delay(
        job_id="pg-1",
        network_id="n1",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
    ).get()
    spawn_vm.delay(
        job_id="pg-1",
        vm_id="v1",
        label="web-1",
        image_id="img",
        nic_specs=[{"network_id": "n1", "nic_id": "nic1"}],
    ).get()

    result = capture_disk.delay(vm_id="v1").get()

    fake_disk_capture.capture.assert_called_once()
    call_kwargs = fake_disk_capture.capture.call_args.kwargs
    assert call_kwargs["domain_name"] == "astinuslab-pg-pg-1-web-1"
    assert str(call_kwargs["source_disk_path"]).endswith(".qcow2")
    # Capture lands in an attempt-owned directory; the published name is checked
    # through the Acquisition below.
    assert call_kwargs["output_path"].parent.parent == tmp_path / "captures" / "pg-1" / "v1"
    assert call_kwargs["output_path"].parent.name.startswith(".publish-")
    with SessionLocal() as s:
        from astinuslab.db.models import Acquisition

        acq = s.query(Acquisition).filter_by(job_vm_id="v1").one()
        assert acq.status == "completed"
        assert acq.id == result
        assert acq.kind == "disk"
        assert acq.session_id == "pg-1"
        assert acq.vm_label == "web-1"
        assert acq.path is not None
        published = Path(acq.path)
        assert published.parent == tmp_path / "captures" / "pg-1" / "v1"
        assert published.name == f"disk-{acq.id}.qcow2"
        assert published.suffix == ".qcow2"
        assert published.exists() and not call_kwargs["output_path"].exists()
        assert acq.sha256 == hashlib.sha256(disk_bytes).hexdigest()
        assert acq.size_bytes == len(disk_bytes)


def test_capture_disk_uses_unique_output_paths_with_same_second(monkeypatch, tmp_path):
    from datetime import datetime as real_datetime

    from astinuslab.playground import acquisition as pa
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import (
        capture_disk,
        create_network,
        create_session,
        spawn_vm,
    )

    fake_lifecycle = MagicMock()
    fake_lifecycle.get_ip.return_value = "10.42.1.10"
    captured_paths = []
    fake_disk_capture = MagicMock()

    def capture_disk_bytes(**kwargs):
        captured_paths.append(kwargs["output_path"])
        kwargs["output_path"].write_bytes(b"disk")

    fake_disk_capture.capture.side_effect = capture_disk_bytes
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, pa, "_disk_capture", lambda: fake_disk_capture)
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda _, *, role="target": {
            "id": "img",
            "path": "/var/lib/astinuslab/images/img.qcow2",
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
            playground_capture_dir=tmp_path / "captures",
            playground_default_vcpus=2,
            playground_default_memory_mb=2048,
            libvirt_uri="qemu:///system",
            base_dir=tmp_path,
            data_dir=tmp_path,
        ),
    )

    create_session.delay(job_id="pg-1").get()
    create_network.delay(
        job_id="pg-1",
        network_id="n1",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
    ).get()
    spawn_vm.delay(
        job_id="pg-1",
        vm_id="v1",
        label="web-1",
        image_id="img",
        nic_specs=[{"network_id": "n1", "nic_id": "nic1"}],
    ).get()

    fixed_now = real_datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    class FixedDatetime:
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr(po, "datetime", FixedDatetime)
    monkeypatch.setattr(po.secrets, "token_hex", MagicMock(side_effect=["acq1", "acq2"]))

    first_id = capture_disk.delay(vm_id="v1").get()
    second_id = capture_disk.delay(vm_id="v1").get()

    assert first_id == "acq1"
    assert second_id == "acq2"
    assert len(captured_paths) == 2
    assert captured_paths[0] != captured_paths[1]
    with SessionLocal() as s:
        from astinuslab.db.models import Acquisition

        acq_ids = {acq.id for acq in s.query(Acquisition).filter_by(job_vm_id="v1").all()}
        assert acq_ids == {"acq1", "acq2"}


def test_capture_locked_disk_timeout_message_is_actionable(monkeypatch, tmp_path):
    from astinuslab.db.models import Acquisition, AcquisitionEvent
    from astinuslab.playground import acquisition as pa
    from astinuslab.playground import orchestrator as po

    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(playground_capture_dir=tmp_path / "captures", storage_dir=tmp_path / "storage"),
    )

    def timeout_capture(_out_path):
        raise subprocess.TimeoutExpired(cmd=["qemu-img", "convert"], timeout=12)

    with pytest.raises(RuntimeError, match="Disk capture timed out after 12s; VM resumed"):
        pa._capture_locked(
            kind="disk",
            extension="qcow2",
            do_capture=timeout_capture,
            vm_id="v1",
            acquisition_id="acq-timeout",
            job_id="pg-1",
            vm_label="web-1",
            domain_name="dom-v1",
        )

    with SessionLocal() as s:
        acq = s.query(Acquisition).filter_by(id="acq-timeout").one()
        events = s.query(AcquisitionEvent).filter_by(acquisition_id="acq-timeout").all()

    assert acq.status == "failed"
    assert any("Disk capture timed out after 12s; VM resumed" in event.message for event in events)


def test_capture_memory_rejects_non_running_vm_without_acquisition(monkeypatch, tmp_path):
    from astinuslab.playground import acquisition as pa
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import capture_memory, create_session

    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(playground_capture_dir=tmp_path / "captures"),
    )
    fake_dump = MagicMock()
    monkeypatch.setattr(pa, "_dump_memory", fake_dump)

    create_session.delay(job_id="pg-1").get()
    with SessionLocal() as s:
        pr.register_vm(
            s,
            vm_id="v1",
            job_id="pg-1",
            label="web-1",
            image_id="img",
            domain_name="astinuslab-pg-pg-1-web-1",
            disk_path="/tmp/web-1.qcow2",
        )
        s.commit()

    with pytest.raises(RuntimeError, match=r"VM v1 not running \(state=pending\)"):
        capture_memory.delay(vm_id="v1").get()

    fake_dump.assert_not_called()
    with SessionLocal() as s:
        from astinuslab.db.models import Acquisition

        assert s.query(Acquisition).filter_by(job_vm_id="v1").one_or_none() is None


def test_capture_memory_rejects_unsafe_vm_id_before_dump(monkeypatch, tmp_path):
    from astinuslab.playground import acquisition as pa
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import capture_memory, create_session

    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(playground_capture_dir=tmp_path / "captures"),
    )
    fake_dump = MagicMock()
    monkeypatch.setattr(pa, "_dump_memory", fake_dump)

    create_session.delay(job_id="pg-1").get()
    with SessionLocal() as s:
        pr.register_vm(
            s,
            vm_id="../escape",
            job_id="pg-1",
            label="web-1",
            image_id="img",
            domain_name="astinuslab-pg-pg-1-web-1",
            disk_path="/tmp/web-1.qcow2",
        )
        pr.update_vm_state(s, "../escape", "running")
        s.commit()

    with pytest.raises(RuntimeError, match="invalid playground vm_id"):
        capture_memory.delay(vm_id="../escape").get()

    fake_dump.assert_not_called()
    with SessionLocal() as s:
        from astinuslab.db.models import Acquisition

        assert s.query(Acquisition).filter_by(job_vm_id="../escape").one_or_none() is None


def test_dump_memory_brackets_dump_with_acl_helper(monkeypatch, tmp_path):
    """qemu:///system writes the dump as root, so the privileged ACL helper must
    run before and after the dump for the worker user to read it back in
    validate(). Regression for the playground capture PermissionError."""
    from pathlib import Path

    # Exercises the real privileged path: the suite runs under
    # ASTINUSLAB_FAKE_LIBVIRT=1, where the ACL helper is deliberately not invoked.
    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    from astinuslab.forensics import memory_dump as md
    from astinuslab.playground import acquisition as pa
    from astinuslab.playground import orchestrator as po

    events: list[str] = []

    class _FakeDumper:
        def __init__(self, *args, **kwargs):
            pass

        def dump(self, conn, name, path):
            events.append("dump")
            Path(path).write_bytes(b"\x7fELF")

        def validate(self, path):
            events.append("validate")

    fake_conn = MagicMock()
    monkeypatch.setattr(fake_mode, "FAKE_PLAYGROUND", False)
    monkeypatch.setattr(po, "libvirt", SimpleNamespace(open=lambda uri: fake_conn))
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(libvirt_uri="qemu:///system", storage_dir=tmp_path / "storage"),
    )
    monkeypatch.setattr(md, "MemoryDumper", _FakeDumper)
    monkeypatch.setattr(po, "fix_libvirt_path_acl_helper", lambda: Path("/helper.sh"))

    def _run(cmd, **kwargs):
        if cmd[:2] == ["sudo", "-n"]:
            events.append("acl")
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    monkeypatch.setattr(po.subprocess, "run", _run)

    out = tmp_path / "captures" / "vm-1" / "mem.elf"
    result = pa._dump_memory("astinuslab-pg-1-vm-1", out)

    assert events == ["acl", "dump", "acl", "validate"]
    assert result["path"] == str(out)
    fake_conn.close.assert_called_once()


def test_vm_lifecycle_runner_raises_on_failed_command(monkeypatch):
    from astinuslab.playground import orchestrator as po

    # Exercise the actual command adapter, with its entire process boundary
    # stubbed below, even when the surrounding suite enables fake Playground.
    monkeypatch.setattr(po.fake_mode, "FAKE_PLAYGROUND", False)

    def _run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args[0], returncode=1)

    monkeypatch.setattr(po.subprocess, "run", _run)

    lifecycle = po._vm_lifecycle()

    with pytest.raises(subprocess.CalledProcessError):
        lifecycle._run_command(["qemu-img", "create"], cwd=None)


def test_fake_playground_lifecycle_matches_vm_lifecycle_signatures():
    import inspect

    from astinuslab.playground.orchestrator import _FakePlaygroundLifecycle
    from astinuslab.virtualization.vm_lifecycle import VMLifecycle

    fake = _FakePlaygroundLifecycle()
    real = object.__new__(VMLifecycle)

    method_calls: dict[str, dict[str, object]] = {
        "boot_playground": {
            "domain_name": "dom",
            "memory_mb": 2048,
            "vcpus": 2,
            "base_image_path": Path("/base.qcow2"),
            "disk_path": Path("/overlay.qcow2"),
            "disk_gb": 20,
            "networks": [],
            "libvirt_uri": "qemu:///system",
            "machine_type": None,
            "cwd": Path("/repo"),
            "job_id": "pg-1",
            "seed_iso_path": Path("/seed.iso"),
            "user_data_path": Path("/user-data"),
            "meta_data_path": Path("/meta-data"),
            "network_config_path": Path("/network-config"),
        },
        "destroy": {"conn": "qemu:///system", "vm_name": "dom"},
        "get_ip": {
            "conn": "qemu:///system",
            "network_name": "net",
            "guest_mac": "52:54:00:00:00:01",
            "domain_name": "dom",
            "timeout_seconds": 1.0,
            "cancel_requested": None,
        },
        "attach_nic": {
            "conn": "qemu:///system",
            "domain_name": "dom",
            "network_name": "net",
            "mac": "52:54:00:00:00:01",
        },
        "detach_nic": {
            "conn": "qemu:///system",
            "domain_name": "dom",
            "mac": "52:54:00:00:00:01",
        },
    }

    for method_name, kwargs in method_calls.items():
        inspect.signature(getattr(real, method_name)).bind(**kwargs)
        inspect.signature(getattr(fake, method_name)).bind(**kwargs)


def test_destroy_session_drives_full_cleanup(monkeypatch, tmp_path):
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import (
        create_network,
        create_session,
        destroy_session,
        spawn_vm,
    )

    fake_lifecycle = MagicMock()
    fake_lifecycle.get_ip.return_value = "10.0.0.5"
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda _, *, role="target": {
            "id": "img",
            "path": "/x",
            "format": "qcow2",
            "architecture": "x86_64",
            "credentials": {"username": "u", "password": "p"},
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

    create_session.delay(job_id="pg-1").get()
    create_network.delay(job_id="pg-1", network_id="n1", name="ab", subnet_cidr="10.0.0.0/24", isolated=True).get()
    spawn_vm.delay(
        job_id="pg-1", vm_id="v1", label="x", image_id="img", nic_specs=[{"network_id": "n1", "nic_id": "nic1"}]
    ).get()

    destroy_session.delay(job_id="pg-1").get()

    with SessionLocal() as s:
        from astinuslab.db.models import JobNetwork, JobVM

        assert all(v.state == "destroyed" for v in s.query(JobVM).all())
        assert all(n.state == "destroyed" for n in s.query(JobNetwork).all())


def test_spawn_vm_resolves_ip_for_every_nic(monkeypatch, tmp_path):
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    # Return a distinct IP per call, in NIC order.
    fake_lifecycle = MagicMock()
    fake_lifecycle.get_ip.side_effect = ["10.42.1.10", "10.42.2.10"]

    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda _, *, role="target": {
            "id": "img",
            "path": "/x",
            "format": "qcow2",
            "architecture": "x86_64",
            "credentials": {"username": "u", "password": "p"},
        },
    )
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(
            playground_disk_dir=tmp_path / "disks",
            playground_capture_dir=tmp_path / "caps",
            playground_default_vcpus=1,
            playground_default_memory_mb=512,
            libvirt_uri="test://",
            base_dir=str,
            data_dir=str(tmp_path),
        ),
    )

    po.create_session.delay(job_id="pg-1").get()
    po.create_network.delay(job_id="pg-1", network_id="ab", name="ab", subnet_cidr="10.42.1.0/24", isolated=True).get()
    po.create_network.delay(job_id="pg-1", network_id="bc", name="bc", subnet_cidr="10.42.2.0/24", isolated=True).get()

    po.spawn_vm.delay(
        job_id="pg-1",
        vm_id="b1",
        label="b1",
        image_id="img",
        nic_specs=[
            {"network_id": "ab", "nic_id": "n1"},
            {"network_id": "bc", "nic_id": "n2"},
        ],
    ).get()

    with SessionLocal() as s:
        from astinuslab.db.models import JobVMNic

        nics = {n.id: n.ip for n in s.query(JobVMNic).filter_by(job_vm_id="b1")}
        assert nics == {"n1": "10.42.1.10", "n2": "10.42.2.10"}
    assert {call.kwargs["domain_name"] for call in fake_lifecycle.get_ip.call_args_list} == {"astinuslab-pg-pg-1-b1"}


def test_spawn_vm_resolves_multi_nic_ips_concurrently(monkeypatch, tmp_path):
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    fake_lifecycle = MagicMock()

    def slow_get_ip(_uri, libvirt_net, _mac, **_kwargs):
        time.sleep(0.3)
        return "10.42.1.10" if libvirt_net.endswith("ab-cdf2") else "10.42.2.10"

    fake_lifecycle.get_ip.side_effect = slow_get_ip
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda _, *, role="target": {
            "id": "img",
            "path": "/x",
            "format": "qcow2",
            "architecture": "x86_64",
            "credentials": {"username": "u", "password": "p"},
        },
    )
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(
            playground_disk_dir=tmp_path / "disks",
            playground_capture_dir=tmp_path / "caps",
            playground_default_vcpus=1,
            playground_default_memory_mb=512,
            libvirt_uri="test://",
            base_dir=str,
            data_dir=str(tmp_path),
        ),
    )

    po.create_session.delay(job_id="pg-1").get()
    po.create_network.delay(job_id="pg-1", network_id="ab", name="ab", subnet_cidr="10.42.1.0/24", isolated=True).get()
    po.create_network.delay(job_id="pg-1", network_id="bc", name="bc", subnet_cidr="10.42.2.0/24", isolated=True).get()

    started = time.monotonic()
    po.spawn_vm.delay(
        job_id="pg-1",
        vm_id="b1",
        label="b1",
        image_id="img",
        nic_specs=[
            {"network_id": "ab", "nic_id": "n1"},
            {"network_id": "bc", "nic_id": "n2"},
        ],
    ).get()
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert fake_lifecycle.get_ip.call_count == 2


def test_provision_vm_workload_prefers_routed_nic_for_health_check(monkeypatch):
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground import workload_provisioning as wp

    called: list[str] = []
    autospec_setattr(monkeypatch, po.catalog, "workload_by_id", lambda workload_id: {"id": workload_id, "services": []})
    autospec_setattr(monkeypatch, po, "lookup_playground_image", lambda image_id, *, role="target": {"id": image_id})
    autospec_setattr(
        monkeypatch,
        wp,
        "_provision_playground_workload",
        lambda *, workload, guest_ip, image, guest_user: called.append(guest_ip),
    )

    with SessionLocal() as s:
        pr.create_playground_session(s, job_id="pg-1")
        pr.register_network(
            s,
            network_id="isolated",
            job_id="pg-1",
            name="isolated",
            subnet_cidr="10.42.1.0/24",
            isolated=True,
            libvirt_net_name="astinuslab-pg-pg-1-isolated-a111",
        )
        pr.register_network(
            s,
            network_id="routed",
            job_id="pg-1",
            name="routed",
            subnet_cidr="10.42.2.0/24",
            isolated=False,
            libvirt_net_name="astinuslab-pg-pg-1-routed-b222",
        )
        pr.register_vm(
            s,
            vm_id="v1",
            job_id="pg-1",
            label="web",
            image_id="img",
            domain_name="domain",
            disk_path="/x",
            workload_id="dvwa",
        )
        pr.update_vm_state(s, "v1", "provisioning_workload")
        pr.update_vm_workload_state(s, "v1", "provisioning")
        pr.attach_nic(s, nic_id="nic-isolated", vm_id="v1", network_id="isolated", mac="52:54:00:aa:bb:01")
        pr.update_nic_ip(s, "nic-isolated", "10.42.1.10")
        pr.attach_nic(s, nic_id="nic-routed", vm_id="v1", network_id="routed", mac="52:54:00:aa:bb:02")
        pr.update_nic_ip(s, "nic-routed", "10.42.2.10")
        s.commit()

    po.provision_vm_workload(vm_id="v1")

    assert called == ["10.42.2.10"]


def test_delayed_workload_task_strictly_tears_down_after_session_destroy(monkeypatch, tmp_path):
    from astinuslab.db.models import JobVM
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground import workload_provisioning as wp

    playground_root = tmp_path / "playground"
    disk = playground_root / "pg-1" / "web.qcow2"
    disk.parent.mkdir(parents=True)
    disk.write_bytes(b"overlay")
    lifecycle = MagicMock()
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(playground_disk_dir=playground_root, libvirt_uri="qemu:///system"),
    )
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: lifecycle)
    workload_install = MagicMock()
    autospec_setattr(monkeypatch, wp, "_provision_playground_workload", workload_install)

    with SessionLocal() as session:
        pr.create_playground_session(session, job_id="pg-1")
        pr.register_vm(
            session,
            vm_id="v1",
            job_id="pg-1",
            label="web",
            image_id="img",
            domain_name="astinuslab-pg-pg-1-web",
            disk_path=str(disk),
            workload_id="dvwa",
        )
        pr.update_vm_state(session, "v1", "provisioning_workload")
        pr.mark_session_destroy_requested(session, "pg-1")
        session.commit()

    assert po.provision_vm_workload(vm_id="v1") == "v1"

    workload_install.assert_not_called()
    lifecycle.destroy.assert_called_once_with("qemu:///system", "astinuslab-pg-pg-1-web")
    assert not disk.exists()
    with SessionLocal() as session:
        vm = session.get(JobVM, "v1")
        assert vm is not None
        assert vm.state == "destroyed"


def test_active_worker_heartbeat_prevents_stale_claim_recovery_past_grace():
    from astinuslab.db.models import JobVM
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground import reconciliation

    with SessionLocal() as session:
        pr.create_playground_session(session, job_id="pg-lease")
        pr.register_vm(
            session,
            vm_id="vm-lease",
            job_id="pg-lease",
            label="web",
            image_id="img",
            domain_name="astinuslab-pg-pg-lease-web",
            disk_path="/tmp/lease.qcow2",
        )
        assert pr.claim_vm_for_spawn(session, "vm-lease") is True
        vm = session.get(JobVM, "vm-lease")
        assert vm is not None
        session.refresh(vm)
        token = vm.updated_at
        session.commit()

    lease = ResourceClaimLease(
        kind="vm",
        resource_id="vm-lease",
        state="starting",
        token=token,
        session_scope_factory=po.session_scope,
        grace_seconds=120,
    )
    lease._interval = 0.05
    lease.start()
    try:
        time.sleep(1.1)
        result = {"abandoned_vm_claims_recovered": 0, "abandoned_network_claims_recovered": 0}
        with SessionLocal() as session:
            reconciliation._recover_stale_resource_claims(
                session,
                result=result,
                now=datetime.now(UTC),
                grace_seconds=1,
            )
            vm = session.get(JobVM, "vm-lease")
            assert vm is not None
            assert vm.state == "starting"
        assert result["abandoned_vm_claims_recovered"] == 0
    finally:
        lease.stop()


@pytest.mark.parametrize(
    "name",
    [
        "create_session",
        "destroy_session",
        "create_network",
        "destroy_network",
        "spawn_vm",
        "provision_vm_workload",
        "destroy_vm",
        "capture_memory",
        "capture_disk",
        "attach_nic",
        "detach_nic",
    ],
)
def test_playground_task_registration_survives_custody_extraction(name):
    from astinuslab.playground import orchestrator as po

    task = getattr(po, name)
    assert task.name == f"playground.{name}"
    assert po.celery_app.tasks[task.name] is task._get_current_object()


def test_stale_network_worker_cannot_publish_after_same_state_reclaim(monkeypatch):
    from astinuslab.db.models import JobNetwork
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    helper = MagicMock()

    def replace_owner(**_kwargs):
        with SessionLocal() as session:
            network = session.get(JobNetwork, "net-aba")
            assert network is not None
            network.state = "failed"
            network.updated_at = datetime.now(UTC)
            session.flush()
            assert pr.claim_network_for_create(session, "net-aba") is True
            session.commit()
        raise RuntimeError("old host call failed")

    helper.create_network.side_effect = replace_owner
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: helper)
    po.create_session(job_id="pg-aba")

    with pytest.raises(LostResourceCustody):
        po.create_network(
            job_id="pg-aba",
            network_id="net-aba",
            name="lan",
            subnet_cidr="10.42.77.0/24",
            isolated=True,
        )

    with SessionLocal() as session:
        network = session.get(JobNetwork, "net-aba")
        assert network is not None
        assert network.state == "creating"
        assert network.error_summary is None


def test_celery_attach_nic(monkeypatch):
    import contextlib

    from astinuslab.db.models import JobVMNic
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_network, create_session

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())

    create_session.delay(job_id="j1").get()
    create_network.delay(
        job_id="j1",
        network_id="n1",
        name="net1",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
    ).get()

    with SessionLocal() as s:
        pr.register_vm(
            s,
            vm_id="v1",
            job_id="j1",
            label="vm1",
            image_id="img1",
            domain_name="fake-domain",
            disk_path="/tmp/fake.qcow2",
        )
        pr.update_vm_state(s, "v1", "running")
        s.commit()

    fake_lifecycle = MagicMock()
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)

    @contextlib.contextmanager
    def fake_libvirt_conn():
        yield "fake_conn"

    monkeypatch.setattr(po, "_libvirt_conn", fake_libvirt_conn)

    res = po.attach_nic(job_id="j1", vm_id="v1", network_id="n1", nic_id="nic1")

    assert res["nic_id"] == "nic1"
    assert res["mac"]
    with SessionLocal() as s:
        nics = s.query(JobVMNic).all()
        assert len(nics) == 1
        nic = nics[0]
        assert nic.id == "nic1"
        assert nic.job_vm_id == "v1"
        assert nic.job_network_id == "n1"
        assert nic.mac == res["mac"]
        assert nic.state == "active"
        libvirt_net_name = nic.network.libvirt_net_name
    fake_lifecycle.attach_nic.assert_called_once_with("fake_conn", "fake-domain", libvirt_net_name, res["mac"])


def _prepare_blocked_nic_attach(monkeypatch):
    import contextlib

    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_network, create_session

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    create_session(job_id="j1")
    create_network(
        job_id="j1",
        network_id="n1",
        name="net1",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
    )
    with SessionLocal() as session:
        pr.register_vm(
            session,
            vm_id="v1",
            job_id="j1",
            label="vm1",
            image_id="img1",
            domain_name="fake-domain",
            disk_path="/tmp/fake.qcow2",
        )
        pr.update_vm_state(session, "v1", "running")
        session.commit()

    entered_host_attach = Event()
    release_host_attach = Event()
    lifecycle = MagicMock()

    def blocked_attach(*_args):
        entered_host_attach.set()
        assert release_host_attach.wait(timeout=5)

    lifecycle.attach_nic.side_effect = blocked_attach
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: lifecycle)

    @contextlib.contextmanager
    def fake_libvirt_conn():
        yield "fake_conn"

    monkeypatch.setattr(po, "_libvirt_conn", fake_libvirt_conn)
    return po, lifecycle, entered_host_attach, release_host_attach


def test_vm_destroy_race_observes_committed_attaching_nic_and_refuses(monkeypatch):
    po, lifecycle, entered_host_attach, release_host_attach = _prepare_blocked_nic_attach(monkeypatch)

    with ThreadPoolExecutor(max_workers=1) as executor:
        attach = executor.submit(po.attach_nic, job_id="j1", vm_id="v1", network_id="n1", nic_id="nic-race")
        assert entered_host_attach.wait(timeout=5)
        try:
            with pytest.raises(RuntimeError, match="NIC attachment"):
                po.destroy_vm(vm_id="v1")
        finally:
            release_host_attach.set()
        assert attach.result(timeout=5)["nic_id"] == "nic-race"

    lifecycle.destroy.assert_not_called()
    with SessionLocal() as session:
        from astinuslab.db.models import JobVM, JobVMNic

        vm = session.get(JobVM, "v1")
        nic = session.get(JobVMNic, "nic-race")
        assert vm is not None
        assert nic is not None
        assert vm.state == "running"
        assert nic.state == "active"


def test_network_destroy_race_observes_committed_attaching_nic_and_refuses(monkeypatch):
    from astinuslab.playground import network_lifecycle as nl

    po, _lifecycle, entered_host_attach, release_host_attach = _prepare_blocked_nic_attach(monkeypatch)
    network_helper = MagicMock()
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: network_helper)

    with ThreadPoolExecutor(max_workers=1) as executor:
        attach = executor.submit(po.attach_nic, job_id="j1", vm_id="v1", network_id="n1", nic_id="nic-race")
        assert entered_host_attach.wait(timeout=5)
        try:
            with pytest.raises(RuntimeError, match="has attached VMs"):
                po.destroy_network(network_id="n1")
        finally:
            release_host_attach.set()
        assert attach.result(timeout=5)["nic_id"] == "nic-race"

    network_helper.destroy_network.assert_not_called()
    with SessionLocal() as session:
        from astinuslab.db.models import JobNetwork, JobVMNic

        network = session.get(JobNetwork, "n1")
        nic = session.get(JobVMNic, "nic-race")
        assert network is not None
        assert nic is not None
        assert network.state == "active"
        assert nic.state == "active"


def test_network_destroy_race_observes_spawn_reserved_nic_and_refuses(monkeypatch, tmp_path):
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_network, create_session

    network_helper = MagicMock()
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: network_helper)
    create_session(job_id="pg-race")
    create_network(
        job_id="pg-race",
        network_id="net-race",
        name="lan",
        subnet_cidr="10.42.77.0/24",
        isolated=True,
    )

    entered_boot = Event()
    release_boot = Event()
    lifecycle = MagicMock()

    def blocked_boot(**_kwargs):
        entered_boot.set()
        assert release_boot.wait(timeout=5)

    lifecycle.boot_playground.side_effect = blocked_boot
    lifecycle.get_ip.return_value = None
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: lifecycle)
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda _image_id, *, role="target": {
            "id": "img",
            "path": "/x",
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
            playground_default_vcpus=1,
            playground_default_memory_mb=512,
            libvirt_uri="qemu:///test",
            base_dir=tmp_path,
            data_dir=tmp_path,
        ),
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        spawn = executor.submit(
            po.spawn_vm,
            job_id="pg-race",
            vm_id="vm-race",
            label="web",
            image_id="img",
            nic_specs=[{"network_id": "net-race", "nic_id": "nic-race"}],
        )
        assert entered_boot.wait(timeout=5)
        try:
            with pytest.raises(RuntimeError, match="has attached VMs"):
                po.destroy_network(network_id="net-race")
        finally:
            release_boot.set()
        assert spawn.result(timeout=5) == "vm-race"

    network_helper.destroy_network.assert_not_called()
    with SessionLocal() as session:
        from astinuslab.db.models import JobNetwork, JobVM, JobVMNic

        network = session.get(JobNetwork, "net-race")
        vm = session.get(JobVM, "vm-race")
        nic = session.get(JobVMNic, "nic-race")
        assert network is not None
        assert vm is not None
        assert nic is not None
        assert network.state == "active"
        assert vm.state == "running"
        assert nic.job_network_id == "net-race"


def test_celery_detach_nic(monkeypatch):
    import contextlib

    from astinuslab.db.models import JobVMNic
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_network, create_session

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())

    create_session.delay(job_id="j1").get()
    create_network.delay(
        job_id="j1",
        network_id="n1",
        name="net1",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
    ).get()

    with SessionLocal() as s:
        pr.register_vm(
            s,
            vm_id="v1",
            job_id="j1",
            label="vm1",
            image_id="img1",
            domain_name="fake-domain",
            disk_path="/tmp/fake.qcow2",
        )
        pr.attach_nic(
            s,
            nic_id="nic1",
            vm_id="v1",
            network_id="n1",
            mac="52:54:00:11:22:33",
            state="active",
        )
        s.commit()

    fake_lifecycle = MagicMock()
    fake_lifecycle.is_missing_interface_error.return_value = False
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)

    @contextlib.contextmanager
    def fake_libvirt_conn():
        yield "fake_conn"

    monkeypatch.setattr(po, "_libvirt_conn", fake_libvirt_conn)

    res = po.detach_nic(job_id="j1", vm_id="v1", nic_id="nic1")

    assert res == "nic1"
    fake_lifecycle.detach_nic.assert_called_once_with("fake_conn", "fake-domain", "52:54:00:11:22:33")
    with SessionLocal() as s:
        assert s.query(JobVMNic).filter_by(id="nic1").one_or_none() is None


def _tx_discipline_harness(monkeypatch):
    """Returns (db_open, libvirt_during_db) trackers + installs fakes.

    B15: libvirt RPC must run with no DB transaction open. Wraps
    session_scope and _libvirt_conn so the test can assert that the
    libvirt block never enters while a session is held."""
    import contextlib
    from unittest.mock import MagicMock

    from astinuslab.playground import orchestrator as po

    db_open = [0]
    libvirt_during_db = [0]

    @contextlib.contextmanager
    def tracking_session_scope():
        db_open[0] += 1

        from types import SimpleNamespace

        from astinuslab.db.models import JobNetwork, JobVM, JobVMNic, PlaygroundSession

        class FakeQuery:
            def __init__(self, model):
                self.model = model

            def filter_by(self, **kwargs):
                return self

            def with_for_update(self):
                return self

            def one(self):
                if self.model is PlaygroundSession:
                    return SimpleNamespace(status="running")
                if self.model is JobVM:
                    return SimpleNamespace(domain_name="d", state="running")
                if self.model is JobNetwork:
                    return SimpleNamespace(libvirt_net_name="n", state="active")
                if self.model is JobVMNic:
                    return SimpleNamespace(mac="52:54:00:aa:bb:cc")
                raise AssertionError(f"unexpected query model: {self.model}")

            def one_or_none(self):
                return None

        class FakeSession:
            def query(self, model):
                return FakeQuery(model)

            def delete(self, _):
                pass

            def commit(self):
                pass

        try:
            yield FakeSession()
        finally:
            db_open[0] -= 1

    @contextlib.contextmanager
    def tracking_libvirt_conn():
        if db_open[0] > 0:
            libvirt_during_db[0] += 1
        yield "fake_conn"

    fake_lifecycle = MagicMock()
    fake_lifecycle.is_missing_interface_error.return_value = False
    monkeypatch.setattr(po, "session_scope", tracking_session_scope)
    monkeypatch.setattr(po.pr, "lock_resource_transitions", MagicMock())
    monkeypatch.setattr(po, "_libvirt_conn", tracking_libvirt_conn)
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)
    claimed_nic = SimpleNamespace(
        job_vm_id="v1",
        job_network_id="n1",
        mac="52:54:00:aa:bb:cc",
        created_at=datetime.now(UTC),
    )
    monkeypatch.setattr(po.pr, "attach_nic", MagicMock(return_value=claimed_nic))
    monkeypatch.setattr(po.pr, "mark_nic_active", MagicMock())
    monkeypatch.setattr(po.pr, "delete_nic_if_detach_claim", MagicMock(return_value=True))

    return db_open, libvirt_during_db


def test_attach_nic_does_not_hold_db_tx_during_libvirt(monkeypatch):
    from astinuslab.playground import orchestrator as po

    db_open, libvirt_during_db = _tx_discipline_harness(monkeypatch)

    po.attach_nic(job_id="j1", vm_id="v1", network_id="n1")

    assert libvirt_during_db[0] == 0
    assert db_open[0] == 0


def test_detach_nic_does_not_hold_db_tx_during_libvirt(monkeypatch):
    from astinuslab.playground import orchestrator as po

    db_open, libvirt_during_db = _tx_discipline_harness(monkeypatch)

    po.detach_nic(job_id="j1", vm_id="v1", nic_id="nic1")

    assert libvirt_during_db[0] == 0
    assert db_open[0] == 0


def test_fake_playground_memory_dump_logs_warning(tmp_path, caplog, monkeypatch):
    import logging

    from astinuslab.playground import acquisition as pa

    monkeypatch.setattr(fake_mode, "FAKE_PLAYGROUND", True)
    out = tmp_path / "captures" / "mem.elf"
    with caplog.at_level(logging.WARNING, logger="astinuslab.playground.orchestrator"):
        pa._dump_memory("dom-1", out)

    assert out.read_bytes().startswith(b"astinuslab fake memory dump")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "FAKE MODE" in r.getMessage()]
    assert warnings, "expected a FAKE MODE warning from _dump_memory"


def test_capture_directory_creation_refuses_symlinked_ancestor(tmp_path):
    """A symlinked component below the capture root must never be traversed.

    The containment check in _capture_locked already rejects a symlink that
    escapes the capture root. This guards the case it cannot see: a symlink whose
    target is *inside* the root still passes containment, so without O_NOFOLLOW
    the capture would be written somewhere other than the path recorded for the
    evidence.
    """
    from astinuslab.playground import orchestrator as po

    root = tmp_path / "captures"
    root.mkdir()
    elsewhere = root / "elsewhere"
    elsewhere.mkdir()
    (root / "job-1").symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(OSError) as excinfo:
        po._create_directory_no_follow(root, "job-1", "vm-1")
    assert excinfo.value.errno in (errno.ENOTDIR, errno.ELOOP)

    # The security property: nothing was written through the link.
    assert list(elsewhere.iterdir()) == []


def test_capture_directory_creation_allows_a_relocated_capture_root(tmp_path):
    """Relocating the capture root onto a bigger disk via symlink must keep working.

    _capture_locked resolves the configured directory before calling this, so the
    link is dereferenced and a real directory arrives. Passing the resolved path
    is what production does; a still-symlinked root is refused.
    """
    from astinuslab.playground import orchestrator as po

    big_disk = tmp_path / "mnt" / "big" / "captures"
    big_disk.mkdir(parents=True)
    configured = tmp_path / "var" / "astinuslab"
    configured.mkdir(parents=True)
    root = configured / "captures"
    root.symlink_to(big_disk, target_is_directory=True)

    po._create_directory_no_follow(root.resolve(), "job-1", "vm-1")
    assert (big_disk / "job-1" / "vm-1").is_dir()

    with pytest.raises(OSError) as excinfo:
        po._create_directory_no_follow(root, "job-2", "vm-2")
    assert excinfo.value.errno in (errno.ENOTDIR, errno.ELOOP)
    assert not (big_disk / "job-2").exists()


def test_capture_directory_creation_rejects_traversal_components(tmp_path):
    """Path separators and parent references must never reach the descriptor walk."""
    from astinuslab.playground import orchestrator as po

    root = tmp_path / "captures"
    root.mkdir()

    for bad in ("..", "", ".", "a/b"):
        with pytest.raises(RuntimeError, match="unsafe capture path component"):
            po._create_directory_no_follow(root, bad, "vm-1")


def test_capture_directory_creation_is_idempotent_for_real_directories(tmp_path):
    """Repeat creation of a genuine directory chain must still succeed."""
    from astinuslab.playground import orchestrator as po

    root = tmp_path / "captures"
    root.mkdir()

    po._create_directory_no_follow(root, "job-1", "vm-1")
    assert (root / "job-1" / "vm-1").is_dir()


def test_live_disk_job_directory_is_forced_owner_only(tmp_path):
    from astinuslab.playground import orchestrator as po

    root = tmp_path / "disks"
    root.mkdir()

    po._create_directory_no_follow(root, "job-1", final_mode=0o700)

    assert (root / "job-1").stat().st_mode & 0o777 == 0o700

    po._create_directory_no_follow(root, "job-1", "vm-1")
    assert (root / "job-1" / "vm-1").is_dir()


def test_capture_directory_creation_does_not_mkdir_existing_components(tmp_path, monkeypatch):
    """Components that already exist must be opened, never created.

    An unconditional mkdir raises EROFS on a read-only mount even when every
    directory already exists, failing a capture that needed no creation at all.
    EROFS cannot be provoked without root, so this asserts the property that
    prevents it: no mkdir attempt is made for a component that is already there.
    """
    from astinuslab.playground import filesystem_safety as fs
    from astinuslab.playground import orchestrator as po

    root = tmp_path / "captures"
    root.mkdir()
    (root / "job-1" / "vm-1").mkdir(parents=True)

    def read_only_mkdir(*args, **kwargs):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(fs.os, "mkdir", read_only_mkdir)

    # Succeeds because nothing needs creating -- the property that EROFS would break.
    po._create_directory_no_follow(root, "job-1", "vm-1")
    assert (root / "job-1" / "vm-1").is_dir()


def test_capture_directory_creation_still_creates_missing_components(tmp_path):
    """Open-first must not stop genuinely missing directories from being created."""
    from astinuslab.playground import orchestrator as po

    root = tmp_path / "captures"
    root.mkdir()

    po._create_directory_no_follow(root, "job-2", "vm-2")

    assert (root / "job-2" / "vm-2").is_dir()


# ---------------------------------------------------------------------------
# Losing the resource claim while cleaning up a failed spawn
# ---------------------------------------------------------------------------
#
# When a spawn fails, the task destroys what it created and records the outcome
# against the VM row. Both halves are guarded by the claim lease, because a
# slow spawn can have its row taken by teardown or by a retry. Every one of
# these paths must surface the custody loss rather than write to a row the task
# no longer owns -- a failure summary landing on someone else's boot is how a
# live VM gets reported as failed and reaped underneath its owner.


def _spawn_failure_fixture(monkeypatch, tmp_path):
    """A registered VM whose image is missing, so the spawn body raises."""
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_session

    playground_root = tmp_path / "playground"
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(
            playground_disk_dir=playground_root,
            playground_default_vcpus=2,
            playground_default_memory_mb=2048,
            libvirt_uri="qemu:///system",
            base_dir=tmp_path,
            data_dir=tmp_path,
        ),
    )
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda _, *, role="target": {
            "id": "img",
            "path": str(tmp_path / "deleted.qcow2"),
            "format": "qcow2",
            "architecture": "x86_64",
        },
    )
    create_session.delay(job_id="pg-1").get()
    with SessionLocal() as s:
        pr.register_vm(
            s,
            vm_id="v1",
            job_id="pg-1",
            label="web-1",
            image_id="img",
            domain_name="astinuslab-pg-pg-1-web-1",
            disk_path=str(playground_root / "pg-1" / "web-1.qcow2"),
        )
        s.commit()
    return playground_root


def _vm_row():
    from astinuslab.db.models import JobVM

    with SessionLocal() as s:
        return s.query(JobVM).filter_by(id="v1").one()


def test_spawn_failure_surfaces_a_lease_lost_before_cleanup(monkeypatch, tmp_path):
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground import vm_lifecycle as vml

    _spawn_failure_fixture(monkeypatch, tmp_path)
    cleanup_calls = []
    autospec_setattr(monkeypatch, po, "_cleanup_spawn_artifacts", lambda **kw: cleanup_calls.append(kw))
    monkeypatch.setattr(
        vml.ResourceClaimLease,
        "assert_owned",
        lambda self: (_ for _ in ()).throw(LostResourceCustody("claim taken")),
    )

    with pytest.raises(LostResourceCustody):
        po.spawn_vm.delay(job_id="pg-1", vm_id="v1", label="web-1", image_id="img", nic_specs=[]).get()

    # The lease is gone, so the artifacts now belong to whoever took the row.
    # Destroying them here would delete a disk under a live boot.
    assert cleanup_calls == []


def test_spawn_failure_surfaces_a_lease_lost_after_cleanup(monkeypatch, tmp_path):
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground import vm_lifecycle as vml

    _spawn_failure_fixture(monkeypatch, tmp_path)
    autospec_setattr(monkeypatch, po, "_cleanup_spawn_artifacts", lambda **_: None)
    monkeypatch.setattr(
        vml.ResourceClaimLease,
        "publication_token",
        lambda self: (_ for _ in ()).throw(LostResourceCustody("claim taken")),
    )

    with pytest.raises(LostResourceCustody):
        po.spawn_vm.delay(job_id="pg-1", vm_id="v1", label="web-1", image_id="img", nic_specs=[]).get()

    # Cleanup ran, but the row moved before the failure could be recorded, so
    # the task must not stamp its summary over the new owner's state.
    assert _vm_row().state != "failed"


def test_spawn_failure_surfaces_a_row_taken_before_the_failure_is_recorded(monkeypatch, tmp_path):
    from astinuslab.playground import orchestrator as po

    _spawn_failure_fixture(monkeypatch, tmp_path)
    autospec_setattr(monkeypatch, po, "_cleanup_spawn_artifacts", lambda **_: None)
    monkeypatch.setattr(pr, "finish_vm_spawn", lambda *a, **k: False)

    with pytest.raises(LostResourceCustody, match="lost spawn custody on failure"):
        po.spawn_vm.delay(job_id="pg-1", vm_id="v1", label="web-1", image_id="img", nic_specs=[]).get()


# ---------------------------------------------------------------------------
# Spawn argument validation
# ---------------------------------------------------------------------------


def test_spawn_refuses_an_immutable_backing_with_mutable_workload_provisioning(monkeypatch, tmp_path):
    from astinuslab.playground import orchestrator as po

    _spawn_failure_fixture(monkeypatch, tmp_path)

    # A locked backing file is shared and read-only. Provisioning a workload
    # into it would write through to every other lab using the same image.
    with pytest.raises(Exception, match="immutable backing cannot receive mutable workload provisioning"):
        po.spawn_vm.delay(
            job_id="pg-1",
            vm_id="v1",
            label="web-1",
            image_id="img",
            nic_specs=[],
            immutable_backing_path=str(tmp_path / "locked.qcow2"),
            workload_id="wl-1",
        ).get()


def test_spawn_refuses_an_immutable_backing_without_resolved_resources(monkeypatch, tmp_path):
    from astinuslab.playground import orchestrator as po

    _spawn_failure_fixture(monkeypatch, tmp_path)

    with pytest.raises(Exception, match="immutable backing requires resolved VM resources"):
        po.spawn_vm.delay(
            job_id="pg-1",
            vm_id="v1",
            label="web-1",
            image_id="img",
            nic_specs=[],
            immutable_backing_path=str(tmp_path / "locked.qcow2"),
        ).get()


# ---------------------------------------------------------------------------
# Retry identity
# ---------------------------------------------------------------------------
#
# A spawn can be delivered twice. The second delivery must either finish the
# same VM and the same NICs, or refuse -- never quietly adopt a row that now
# describes something else. These guards are what stop a duplicate delivery
# from re-pointing an existing interface at another network or MAC.


def test_a_spawn_whose_claim_is_already_taken_returns_without_touching_the_row(monkeypatch, tmp_path):
    from astinuslab.playground import orchestrator as po

    _spawn_failure_fixture(monkeypatch, tmp_path)
    booted = []
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: booted.append("boot"))
    monkeypatch.setattr(pr, "claim_vm_for_spawn", lambda *a, **k: False)

    result = po.spawn_vm.delay(job_id="pg-1", vm_id="v1", label="web-1", image_id="img", nic_specs=[]).get()

    # Another delivery owns the spawn. This one returns the id and does nothing
    # else -- no boot, and no failure recorded against a row it never claimed.
    assert result == "v1"
    assert booted == []
    assert _vm_row().state != "failed"


def _network_and_nic(*, nic_id, vm_id, network_id, mac, ordinal):
    from astinuslab.db.models import JobNetwork, JobVMNic

    with SessionLocal() as s:
        s.add(
            JobNetwork(
                id="net-1",
                session_id="pg-1",
                name="lan",
                subnet_cidr="10.42.0.0/24",
                isolated=True,
                libvirt_net_name="astinuslab-pg-pg-1-lan",
                state="active",
            )
        )
        s.add(
            JobVMNic(
                id=nic_id,
                job_vm_id=vm_id,
                job_network_id=network_id,
                mac=mac,
                ordinal=ordinal,
                state="active",
            )
        )
        s.commit()


@pytest.mark.parametrize(
    "nic_kwargs,spec_extra,expected",
    [
        (
            {"vm_id": "other-vm", "network_id": "net-1", "mac": "52:54:00:aa:bb:01", "ordinal": 0},
            {},
            "belongs to a different VM or network",
        ),
        (
            {"vm_id": "v1", "network_id": "net-1", "mac": "52:54:00:aa:bb:01", "ordinal": 0},
            {"mac": "52:54:00:ff:ff:ff"},
            "retry MAC identity mismatch",
        ),
        (
            {"vm_id": "v1", "network_id": "net-1", "mac": "52:54:00:aa:bb:01", "ordinal": 7},
            {},
            "retry ordinal identity mismatch",
        ),
    ],
)
def test_a_redelivered_spawn_refuses_a_nic_that_no_longer_matches(
    monkeypatch, tmp_path, nic_kwargs, spec_extra, expected
):
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground import vm_lifecycle as vml

    _spawn_failure_fixture(monkeypatch, tmp_path)
    # The NIC reservation runs after the image check, so this case needs an
    # image that exists; the identity guards under test are further in.
    (tmp_path / "deleted.qcow2").write_bytes(b"qcow2")
    _network_and_nic(nic_id="nic-1", **nic_kwargs)
    autospec_setattr(monkeypatch, vml, "_wait_for_active_networks", lambda *a, **k: None)

    spec = {"nic_id": "nic-1", "network_id": "net-1", **spec_extra}
    with pytest.raises(LostResourceCustody, match=expected):
        po.spawn_vm.delay(job_id="pg-1", vm_id="v1", label="web-1", image_id="img", nic_specs=[spec]).get()


# ---------------------------------------------------------------------------
# Network custody, before the network domain moves to a sibling module
#
# Every case below is a path where the task has already changed the host and
# then cannot record it. That is the failure mode worth pinning: a libvirt
# network left defined with no row claiming it is a leaked subnet, and the next
# create fails with "Network is already in use".
# ---------------------------------------------------------------------------


def _network_fixture(monkeypatch):
    """One session with one active network, and the libvirt helper faked."""
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground.orchestrator import create_network, create_session

    fake_pn = MagicMock()
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: fake_pn)
    create_session.delay(job_id="pg-1").get()
    create_network.delay(
        job_id="pg-1",
        network_id="n1",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
    ).get()
    return fake_pn


def _set_network_state(network_id: str, state: str) -> None:
    with SessionLocal() as s:
        s.query(JobNetwork).filter_by(id=network_id).one().state = state
        s.commit()


def _network_state(network_id: str = "n1") -> str:
    with SessionLocal() as s:
        return s.query(JobNetwork).filter_by(id=network_id).one().state


def test_create_network_surfaces_a_row_taken_before_activation_is_recorded(monkeypatch):
    """libvirt has the network; the row moved to a new owner before the task
    could mark it active. Reporting success would hand back a network this task
    no longer owns."""
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground.orchestrator import create_network, create_session

    fake_pn = MagicMock()
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: fake_pn)
    create_session.delay(job_id="pg-1").get()
    monkeypatch.setattr(pr, "finish_network_create", lambda *a, **k: False)

    with pytest.raises(RuntimeError, match="network n1 lost create custody before activation"):
        create_network.delay(
            job_id="pg-1",
            network_id="n1",
            name="ab",
            subnet_cidr="10.42.1.0/24",
            isolated=True,
        ).get()

    fake_pn.create_network.assert_called_once()


def test_create_network_surfaces_a_row_taken_before_the_failure_is_recorded(monkeypatch):
    """The create failed *and* the row moved, so the task cannot even record the
    failure. It must say custody was lost rather than stamp its own summary over
    the new owner's state."""
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground.orchestrator import create_network, create_session

    fake_pn = MagicMock()
    fake_pn.create_network.side_effect = RuntimeError("define failed")
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: fake_pn)
    create_session.delay(job_id="pg-1").get()
    monkeypatch.setattr(pr, "finish_network_create", lambda *a, **k: False)

    with pytest.raises(LostResourceCustody, match="network n1 lost create custody on failure"):
        create_network.delay(
            job_id="pg-1",
            network_id="n1",
            name="ab",
            subnet_cidr="10.42.1.0/24",
            isolated=True,
        ).get()


def test_create_network_refuses_a_retry_it_cannot_claim(monkeypatch):
    """A row that exists with matching identity but cannot be claimed belongs to
    a live worker. Proceeding would define the same network twice."""
    from astinuslab.playground.orchestrator import create_network

    _network_fixture(monkeypatch)
    with SessionLocal() as s:
        pr.claim_network_for_destroy(s, "n1")
        s.commit()
    monkeypatch.setattr(pr, "claim_network_for_create", lambda *a, **k: False)

    with pytest.raises(RuntimeError, match="network n1 is not available for create retry"):
        create_network.delay(
            job_id="pg-1",
            network_id="n1",
            name="ab",
            subnet_cidr="10.42.1.0/24",
            isolated=True,
        ).get()


def test_create_network_is_idempotent_for_an_already_active_network(monkeypatch):
    """A redelivered create must not touch libvirt again. The identity check
    passes, the state is already active, and the task returns."""
    from astinuslab.playground.orchestrator import create_network

    fake_pn = _network_fixture(monkeypatch)
    fake_pn.create_network.reset_mock()
    fake_pn.reap_orphans.reset_mock()

    result = create_network.delay(
        job_id="pg-1",
        network_id="n1",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
    ).get()

    assert result == "n1"
    fake_pn.create_network.assert_not_called()
    fake_pn.reap_orphans.assert_not_called()
    assert _network_state() == "active"


def test_destroy_network_is_idempotent_for_an_already_destroyed_network(monkeypatch):
    """A redelivered destroy returns without claiming or calling libvirt."""
    from astinuslab.playground.orchestrator import destroy_network

    fake_pn = _network_fixture(monkeypatch)
    destroy_network.delay(network_id="n1").get()
    fake_pn.destroy_network.reset_mock()

    assert destroy_network.delay(network_id="n1").get() == "n1"

    fake_pn.destroy_network.assert_not_called()
    assert _network_state() == "destroyed"


def test_destroy_network_surfaces_a_row_taken_before_completion_is_recorded(monkeypatch):
    """libvirt no longer has the network, but the row moved before the task
    could mark it destroyed."""
    from astinuslab.playground.orchestrator import destroy_network

    _network_fixture(monkeypatch)
    monkeypatch.setattr(pr, "finish_network_destroy", lambda *a, **k: False)

    with pytest.raises(RuntimeError, match="network n1 lost destroy custody before completion"):
        destroy_network.delay(network_id="n1").get()


def test_destroy_network_surfaces_a_row_taken_before_the_failure_is_recorded(monkeypatch):
    """Teardown failed and the row moved, so the failure cannot be recorded
    either. The network may still be defined on the host, which is exactly why
    this must not be reported as a plain teardown failure."""
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground.orchestrator import destroy_network

    fake_pn = _network_fixture(monkeypatch)
    fake_pn.destroy_network.side_effect = RuntimeError("teardown failed")
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: fake_pn)
    monkeypatch.setattr(pr, "finish_network_destroy", lambda *a, **k: False)

    with pytest.raises(LostResourceCustody, match="network n1 lost destroy custody on failure"):
        destroy_network.delay(network_id="n1").get()


def test_destroy_network_propagates_a_custody_loss_without_marking_failed(monkeypatch):
    """A lease that cannot prove ownership is not a teardown failure. Writing
    `failed` here would overwrite whatever the new owner has recorded."""
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground.orchestrator import destroy_network

    fake_pn = _network_fixture(monkeypatch)
    fake_pn.destroy_network.side_effect = LostResourceCustody("claim taken")
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: fake_pn)
    recorded: list[dict] = []

    def record_and_succeed(*args, **kwargs) -> bool:
        recorded.append(kwargs)
        return True

    monkeypatch.setattr(pr, "finish_network_destroy", record_and_succeed)

    with pytest.raises(LostResourceCustody, match="claim taken"):
        destroy_network.delay(network_id="n1").get()

    assert recorded == [], "a custody loss must not be recorded as a teardown outcome"


def test_destroy_network_reports_the_custody_loss_that_hides_a_failure(monkeypatch):
    """Teardown failed, and then the lease could not produce a publication token
    at all. The custody error is what the operator needs, chained from the
    teardown failure that exposed it."""
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground.orchestrator import destroy_network

    fake_pn = _network_fixture(monkeypatch)
    fake_pn.destroy_network.side_effect = RuntimeError("teardown failed")
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: fake_pn)
    monkeypatch.setattr(
        ResourceClaimLease,
        "publication_token",
        lambda self: (_ for _ in ()).throw(LostResourceCustody("token refused")),
    )

    with pytest.raises(LostResourceCustody, match="token refused") as raised:
        destroy_network.delay(network_id="n1").get()

    assert isinstance(raised.value.__cause__, RuntimeError)


def test_waiting_for_active_networks_times_out_instead_of_polling_forever(monkeypatch):
    """`spawn_vm` waits here before it defines a domain. Without the deadline a
    network stuck in `creating` would hang the spawn and hold its claim lease
    until the worker died."""
    from astinuslab.playground import vm_lifecycle as vml

    _network_fixture(monkeypatch)
    _set_network_state("n1", "creating")
    slept = []
    monkeypatch.setattr(vml.time, "sleep", lambda seconds: slept.append(seconds))

    with pytest.raises(RuntimeError, match=r"timed out waiting for active network\(s\): n1"):
        vml._wait_for_active_networks(job_id="pg-1", network_ids={"n1"}, timeout_seconds=0.0, poll_seconds=0.01)

    assert _network_state() == "creating", "the wait must not change what it is waiting for"


def test_networks_helper_wires_the_configured_libvirt_uri(monkeypatch):
    """Every other network test patches this factory out, so nothing checked what
    it builds. A wrong URI here would send every network operation to a
    different libvirt."""
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.networks import PlaygroundNetworks

    sentinel = object()
    monkeypatch.setattr(po, "libvirt", sentinel)
    monkeypatch.setattr(po, "settings", SimpleNamespace(libvirt_uri="qemu+tcp://elsewhere/system"))

    helper = nl._networks_helper()

    assert isinstance(helper, PlaygroundNetworks)
    assert helper._libvirt is sentinel
    assert helper._libvirt_uri == "qemu+tcp://elsewhere/system"


def test_waiting_for_active_networks_rereads_the_row_on_each_poll(monkeypatch):
    """The wait must re-query rather than decide once. A network is `creating` on
    the first look and active on the second, which is the ordinary case when
    `spawn_vm` runs while `create_network` is still finishing."""
    from astinuslab.playground import vm_lifecycle as vml

    _network_fixture(monkeypatch)
    _set_network_state("n1", "creating")

    def activate_on_sleep(seconds: float) -> None:
        _set_network_state("n1", "active")

    monkeypatch.setattr(vml.time, "sleep", activate_on_sleep)

    resolved = vml._wait_for_active_networks(job_id="pg-1", network_ids={"n1"}, timeout_seconds=30.0, poll_seconds=0.01)

    assert resolved == {"n1": "astinuslab-pg-pg-1-ab-cdf2"}


# ---------------------------------------------------------------------------
# Workload provisioning custody, before the workload domain moves out
#
# By the time provisioning runs, the VM is booted and its disk has been written
# to. Every path below is one where that work has happened and the task then
# cannot record what became of it. Leaving the row mid-transition is what makes
# a VM unrecoverable without a manual libvirt sweep.
# ---------------------------------------------------------------------------


def _provision_fixture(monkeypatch, *, workload_id="dvwa", workload=True, prebaked=False, guest_ip="10.42.1.10"):
    """One VM claimed for workload provisioning, with the install faked out."""
    from astinuslab import catalog
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_network, create_session

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    create_session.delay(job_id="pg-1").get()
    create_network.delay(job_id="pg-1", network_id="n1", name="ab", subnet_cidr="10.42.1.0/24", isolated=True).get()
    with SessionLocal() as s:
        pr.register_vm(
            s,
            vm_id="v1",
            job_id="pg-1",
            label="web-1",
            image_id="img",
            domain_name="dom",
            disk_path="/tmp/disk.qcow2",
            role="target",
            workload_id=workload_id,
        )
        pr.update_vm_state(s, "v1", "provisioning_workload")
        pr.update_vm_workload_state(s, "v1", "prebaked" if prebaked else "provisioning")
        pr.attach_nic(s, nic_id="nic1", vm_id="v1", network_id="n1", mac="52:54:00:aa:bb:cc")
        if guest_ip is not None:
            pr.update_nic_ip(s, "nic1", guest_ip)
        s.commit()

    autospec_setattr(
        monkeypatch,
        catalog,
        "workload_by_id",
        lambda wid: {"id": wid, "display_name": "DVWA", "services": []} if workload else None,
    )
    autospec_setattr(monkeypatch, po, "lookup_playground_image", lambda image_id, *, role="target": {"id": image_id})


def test_provision_is_a_no_op_for_a_vm_with_no_workload(monkeypatch):
    """Spawn queues this unconditionally. A VM without a workload must return
    without claiming the row, or an ordinary VM would be moved into a
    provisioning state it never leaves."""
    from astinuslab.playground import workload_provisioning as wp
    from astinuslab.playground.orchestrator import provision_vm_workload

    _provision_fixture(monkeypatch, workload_id=None)
    installed = []
    autospec_setattr(monkeypatch, wp, "_provision_playground_workload", lambda **k: installed.append(k))

    assert provision_vm_workload.delay(vm_id="v1").get() == "v1"

    assert installed == []
    assert _vm_row().state == "provisioning_workload", "an unclaimed row must not be moved"


def test_provision_returns_when_the_row_is_neither_claimable_nor_queued_for_destroy(monkeypatch):
    """A redelivered message whose row another worker owns, and which does not
    qualify for rejected-workload cleanup either. Doing anything here would act
    on someone else's VM."""
    from astinuslab.playground import workload_provisioning as wp
    from astinuslab.playground.orchestrator import provision_vm_workload

    _provision_fixture(monkeypatch)
    monkeypatch.setattr(pr, "claim_vm_for_workload", lambda *a, **k: False)
    monkeypatch.setattr(pr, "queue_rejected_workload_for_destroy", lambda *a, **k: False)
    installed = []
    autospec_setattr(monkeypatch, wp, "_provision_playground_workload", lambda **k: installed.append(k))

    assert provision_vm_workload.delay(vm_id="v1").get() == "v1"

    assert installed == []


def test_provision_cleans_up_and_fails_when_the_workload_is_gone_from_the_catalog(monkeypatch):
    """The catalog entry disappeared between spawn and provisioning. The disk is
    already written, so the VM is torn down rather than left running something
    nobody can describe."""
    from astinuslab.playground import workload_provisioning as wp
    from astinuslab.playground.orchestrator import provision_vm_workload

    _provision_fixture(monkeypatch, workload=False)
    cleaned = []
    autospec_setattr(monkeypatch, wp, "_cleanup_failed_provision", lambda **k: cleaned.append(k))

    with pytest.raises(RuntimeError, match="Workload not found: dvwa"):
        provision_vm_workload.delay(vm_id="v1").get()

    assert len(cleaned) == 1, "the booted VM must be torn down, not left running"
    vm = _vm_row()
    assert vm.state == "failed"
    assert vm.workload_state == "failed"
    assert vm.workload_error == "Workload not found: dvwa"


def test_provision_reports_a_cleanup_that_could_not_finish_as_retryable(monkeypatch):
    """Teardown itself failed, so libvirt may still hold the domain. That is
    `teardown_failed`, not `failed`: the difference is whether an operator has
    anything left to clean up."""
    from astinuslab.playground import workload_provisioning as wp
    from astinuslab.playground.orchestrator import provision_vm_workload

    _provision_fixture(monkeypatch, workload=False)
    autospec_setattr(monkeypatch, wp, "_cleanup_failed_provision", lambda **k: "domain still defined")

    with pytest.raises(RuntimeError, match="Workload not found: dvwa"):
        provision_vm_workload.delay(vm_id="v1").get()

    vm = _vm_row()
    assert vm.state == "teardown_failed"
    assert "cleanup retry required: domain still defined" in vm.error_summary


def test_provision_surfaces_a_row_taken_before_the_cleanup_is_recorded(monkeypatch):
    """The VM was torn down and then the row moved, so the outcome cannot be
    written. Reporting the original error would hide that this task no longer
    knows what state the VM is in."""
    from astinuslab.playground import workload_provisioning as wp
    from astinuslab.playground.orchestrator import provision_vm_workload

    _provision_fixture(monkeypatch, workload=False)
    autospec_setattr(monkeypatch, wp, "_cleanup_failed_provision", lambda **k: None)
    monkeypatch.setattr(pr, "finish_vm_workload", lambda *a, **k: False)

    with pytest.raises(LostResourceCustody, match="VM v1 lost workload custody during cleanup"):
        provision_vm_workload.delay(vm_id="v1").get()


def test_provision_propagates_a_custody_loss_from_the_install_without_recording_it(monkeypatch):
    """A lease that cannot prove ownership mid-install is not a workload
    failure. Writing `failed` here would overwrite the new owner's state."""
    from astinuslab.playground import workload_provisioning as wp
    from astinuslab.playground.orchestrator import provision_vm_workload

    _provision_fixture(monkeypatch)

    def lose_custody(**kwargs):
        raise LostResourceCustody("claim taken")

    autospec_setattr(monkeypatch, wp, "_provision_playground_workload", lose_custody)
    cleaned = []
    autospec_setattr(monkeypatch, wp, "_cleanup_failed_provision", lambda **k: cleaned.append(k))

    with pytest.raises(LostResourceCustody, match="claim taken"):
        provision_vm_workload.delay(vm_id="v1").get()

    assert cleaned == [], "a custody loss must not trigger teardown of a VM this task no longer owns"


def test_provision_surfaces_a_row_taken_after_a_failed_install(monkeypatch):
    """The install failed, the VM was torn down, and then the row moved. Both
    halves of the failure path have to survive the row being taken."""
    from astinuslab.playground import workload_provisioning as wp
    from astinuslab.playground.orchestrator import provision_vm_workload

    _provision_fixture(monkeypatch)

    def explode(**kwargs):
        raise RuntimeError("ssh refused")

    autospec_setattr(monkeypatch, wp, "_provision_playground_workload", explode)
    autospec_setattr(monkeypatch, wp, "_cleanup_failed_provision", lambda **k: None)
    monkeypatch.setattr(pr, "finish_vm_workload", lambda *a, **k: False)

    with pytest.raises(LostResourceCustody, match="VM v1 lost workload custody during cleanup") as raised:
        provision_vm_workload.delay(vm_id="v1").get()

    assert isinstance(raised.value.__cause__, RuntimeError)


def test_provision_surfaces_a_row_taken_before_success_is_recorded(monkeypatch):
    """The workload installed cleanly and the row moved before it could be
    marked ready. Returning success would report a VM this task no longer owns
    as ready."""
    from astinuslab.playground import workload_provisioning as wp
    from astinuslab.playground.orchestrator import provision_vm_workload

    _provision_fixture(monkeypatch)
    autospec_setattr(monkeypatch, wp, "_provision_playground_workload", lambda **k: None)
    monkeypatch.setattr(pr, "finish_vm_workload", lambda *a, **k: False)

    with pytest.raises(LostResourceCustody, match="VM v1 lost workload custody before completion"):
        provision_vm_workload.delay(vm_id="v1").get()


def test_cleanup_reports_a_libvirt_it_could_not_reach_instead_of_raising(monkeypatch):
    """Cleanup runs while another failure is already being handled. If libvirt
    cannot even be opened it must return that as text, because raising here
    would replace the original error with this one."""
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground import workload_provisioning as wp

    def no_libvirt():
        raise RuntimeError("connection refused")

    autospec_setattr(monkeypatch, po, "_vm_lifecycle", no_libvirt)

    result = wp._cleanup_failed_provision(domain_name="dom", disk_path=Path("/tmp/disk.qcow2"))

    assert result == "libvirt cleanup failed: connection refused"


def test_provision_playground_workload_passes_the_image_target_context(monkeypatch):
    """Every other provisioning test patches this out, so nothing checked what
    it hands the installer. The target context is what decides which guest
    commands run, so a wrong one would provision against the wrong OS."""
    from astinuslab.playground import workload_provisioning as wp

    calls = {}
    monkeypatch.setattr(wp.pw, "install_playground_workload", lambda **kwargs: calls.update(kwargs))
    monkeypatch.setattr(wp.pw, "target_context_from_playground_image", lambda image: {"os": image["id"]})

    wp._provision_playground_workload(
        workload={"id": "dvwa"},
        guest_ip="10.42.1.10",
        image={"id": "ubuntu-24"},
        guest_user="ubuntu",
    )

    assert calls == {
        "workload": {"id": "dvwa"},
        "guest_ip": "10.42.1.10",
        "guest_user": "ubuntu",
        "target_context": {"os": "ubuntu-24"},
    }


# ---------------------------------------------------------------------------
# VM lifecycle custody, before the domain moves to a sibling module
#
# This is the code that creates disks and defines domains. Every case below is a
# point where the host has already been changed and the row that records it can
# no longer be trusted to be ours. A VM left mid-transition here is the one
# outcome that needs a manual libvirt sweep to resolve.
# ---------------------------------------------------------------------------


def _destroy_fixture(monkeypatch, tmp_path, *, state: str = "running"):
    """A registered VM with a real disk file and libvirt faked out."""
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_session

    disk_dir = tmp_path / "playground"
    disk = disk_dir / "pg-1" / "web-1.qcow2"
    disk.parent.mkdir(parents=True, exist_ok=True)
    disk.write_bytes(b"qcow2-data")
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(playground_disk_dir=disk_dir, libvirt_uri="qemu:///system"),
    )
    lifecycle = MagicMock()
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: lifecycle)

    create_session.delay(job_id="pg-1").get()
    with SessionLocal() as s:
        pr.register_vm(
            s,
            vm_id="v1",
            job_id="pg-1",
            label="web-1",
            image_id="img",
            domain_name="astinuslab-pg-pg-1-web-1",
            disk_path=str(disk),
        )
        if state != "pending":
            pr.update_vm_state(s, "v1", state)
        s.commit()
    return disk, lifecycle


def test_destroy_is_idempotent_for_a_vm_already_destroyed(monkeypatch, tmp_path):
    """A redelivered destroy must not drive libvirt again, and must not delete a
    disk path a later VM could legitimately be reusing."""
    from astinuslab.playground.orchestrator import destroy_vm

    disk, lifecycle = _destroy_fixture(monkeypatch, tmp_path, state="destroyed")

    assert destroy_vm.delay(vm_id="v1").get() == "v1"

    lifecycle.destroy.assert_not_called()
    assert disk.exists(), "an already-destroyed row must not trigger another disk removal"


def test_destroy_releases_its_lease_when_libvirt_reports_lost_custody(monkeypatch, tmp_path):
    """A custody loss during host teardown is not a teardown failure. The lease
    is released and the error propagates, so the row keeps whatever the new
    owner has recorded rather than being stamped `teardown_failed`."""
    from astinuslab.playground.orchestrator import destroy_vm

    disk, lifecycle = _destroy_fixture(monkeypatch, tmp_path)
    lifecycle.destroy.side_effect = LostResourceCustody("claim taken")

    with pytest.raises(LostResourceCustody, match="claim taken"):
        destroy_vm.delay(vm_id="v1").get()

    assert _vm_row().state != "teardown_failed"
    assert disk.exists(), "artifacts must survive a destroy this task no longer owns"


def test_destroy_releases_its_lease_when_artifact_removal_reports_lost_custody(monkeypatch, tmp_path):
    """Same guarantee one stage later: libvirt is already down, but the row
    moved before the disk could be removed."""
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import destroy_vm

    _destroy_fixture(monkeypatch, tmp_path)

    def lose_custody(_disk_path):
        raise LostResourceCustody("claim taken during artifact cleanup")

    autospec_setattr(monkeypatch, po, "_remove_playground_artifacts", lose_custody)

    with pytest.raises(LostResourceCustody, match="claim taken during artifact cleanup"):
        destroy_vm.delay(vm_id="v1").get()

    assert _vm_row().state != "teardown_failed"


def test_destroy_surfaces_a_row_taken_before_completion_is_recorded(monkeypatch, tmp_path):
    """The domain is gone and the disk is removed, but the row moved before that
    could be written. Reporting success would leave an operator believing a row
    they can still see was cleaned up by this worker."""
    from astinuslab.playground.orchestrator import destroy_vm

    _destroy_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(pr, "finish_vm_destroy", lambda *a, **k: False)

    with pytest.raises(RuntimeError, match="VM v1 lost destroy custody before completion"):
        destroy_vm.delay(vm_id="v1").get()


def test_destroy_surfaces_a_row_taken_before_a_teardown_failure_is_recorded(monkeypatch, tmp_path):
    """Teardown failed *and* the row moved, so neither the failure nor the
    residue can be recorded. This is the worst case in the task and must not be
    reported as a plain teardown failure."""
    from astinuslab.playground.orchestrator import destroy_vm

    _disk, lifecycle = _destroy_fixture(monkeypatch, tmp_path)
    lifecycle.destroy.side_effect = RuntimeError("libvirt refused")
    monkeypatch.setattr(pr, "finish_vm_destroy", lambda *a, **k: False)

    with pytest.raises(LostResourceCustody, match="VM v1 lost destroy custody on failure"):
        destroy_vm.delay(vm_id="v1").get()


def test_workload_enqueue_failure_defers_to_a_worker_that_already_claimed_the_vm(monkeypatch, tmp_path):
    """Publishing the provisioning message can fail ambiguously: the broker may
    have taken it anyway. If another worker has already promoted the row, that
    worker owns the VM, so this one must leave the host alone."""
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground import vm_lifecycle as vml

    _destroy_fixture(monkeypatch, tmp_path)
    cleaned = []
    autospec_setattr(monkeypatch, po, "_cleanup_spawn_artifacts", lambda **kw: cleaned.append(kw))
    monkeypatch.setattr(pr, "claim_vm_for_workload_enqueue_cleanup", lambda *a, **k: False)

    def publish_fails(**_kwargs):
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr(po.provision_vm_workload, "delay", publish_fails)

    vml._dispatch_workload_provisioning(
        vm_id="v1",
        domain_name="astinuslab-pg-pg-1-web-1",
        disk_path=Path("/tmp/web-1.qcow2"),
        lifecycle=MagicMock(),
        claim_lease=MagicMock(),
    )

    assert cleaned == [], "the VM belongs to the worker that claimed it; do not tear it down"


def test_workload_enqueue_cleanup_reports_residue_and_a_row_taken_from_it(monkeypatch, tmp_path):
    """The enqueue failed, this worker claimed cleanup, the cleanup left residue,
    and then the row moved too. The custody error is what surfaces, with the
    original failure as its cause."""
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground import vm_lifecycle as vml

    # `provisioning_workload` is the only state the cleanup claim accepts -- it
    # exists precisely to catch a VM whose handoff no workload worker took. The
    # real CAS then moves it to `destroying`, which is what the cleanup lease
    # requires, so this cannot be faked with a patched claim.
    _destroy_fixture(monkeypatch, tmp_path, state="provisioning_workload")
    autospec_setattr(monkeypatch, po, "_cleanup_spawn_artifacts", lambda **kw: "disk still present")
    monkeypatch.setattr(pr, "finish_vm_destroy", lambda *a, **k: False)

    def publish_fails(**_kwargs):
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr(po.provision_vm_workload, "delay", publish_fails)

    with pytest.raises(RuntimeError, match="VM v1 lost workload-enqueue cleanup custody") as raised:
        vml._dispatch_workload_provisioning(
            vm_id="v1",
            domain_name="astinuslab-pg-pg-1-web-1",
            disk_path=Path("/tmp/web-1.qcow2"),
            lifecycle=MagicMock(),
            claim_lease=MagicMock(),
        )

    assert isinstance(raised.value.__cause__, RuntimeError)


def test_an_immutable_backing_that_is_not_there_is_refused(monkeypatch, tmp_path):
    """The package binding already checked the qcow metadata, but that was a
    different process at a different time. This is the worker's own boundary."""
    from astinuslab.playground import vm_lifecycle as vml

    with pytest.raises(RuntimeError, match="immutable backing is unavailable"):
        vml._checked_immutable_backing_path(str(tmp_path / "never-created.qcow2"))


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_an_immutable_backing_that_is_not_a_regular_file_is_refused(tmp_path, kind):
    """A symlink here would let the backing be redirected between the check and
    the qemu-img call; a directory is not a disk at all."""
    from astinuslab.playground import vm_lifecycle as vml

    target = tmp_path / "real.qcow2"
    target.write_bytes(b"qcow2")
    if kind == "symlink":
        candidate = tmp_path / "link.qcow2"
        candidate.symlink_to(target)
    else:
        candidate = tmp_path / "a-directory.qcow2"
        candidate.mkdir()

    with pytest.raises(RuntimeError, match="immutable backing is unsafe"):
        vml._checked_immutable_backing_path(str(candidate))


def test_nic_reservation_refuses_a_spawn_whose_claim_is_gone(monkeypatch, tmp_path):
    """NIC rows are reserved under the spawn claim. A token that no longer
    matches means the spawn was taken over, and reserving interfaces for it
    would attach them to another worker's VM."""
    from astinuslab.playground import vm_lifecycle as vml

    _destroy_fixture(monkeypatch, tmp_path, state="starting")

    with pytest.raises(LostResourceCustody, match="VM v1 lost spawn custody before NIC reservation"):
        vml._reserve_nic_rows(
            vm_id="v1",
            job_id="pg-1",
            nic_specs=[{"network_id": "n1"}],
            nic_publication_token=datetime.now(UTC),
        )


def test_nic_reservation_refuses_a_network_that_is_no_longer_active(monkeypatch, tmp_path):
    """The networks are locked and re-read here. One that is not active has no
    bridge, so a reserved NIC would name a network the VM cannot join."""
    from astinuslab.db.models import JobNetwork
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import vm_lifecycle as vml
    from astinuslab.playground.orchestrator import create_network

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    _destroy_fixture(monkeypatch, tmp_path, state="starting")
    create_network.delay(job_id="pg-1", network_id="n1", name="ab", subnet_cidr="10.42.1.0/24", isolated=True).get()
    with SessionLocal() as s:
        s.query(JobNetwork).filter_by(id="n1").one().state = "destroying"
        s.commit()
    token = _vm_row().updated_at

    with pytest.raises(LostResourceCustody, match="VM v1 lost active network custody before NIC reservation: n1"):
        vml._reserve_nic_rows(vm_id="v1", job_id="pg-1", nic_specs=[{"network_id": "n1"}], nic_publication_token=token)


def test_spawn_target_resolution_refuses_a_disk_path_outside_its_root(monkeypatch, tmp_path):
    """The label reaches this as a path component. A label that escapes the
    configured disk root would have the spawn create and later delete a file
    outside the playground tree."""
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground import vm_lifecycle as vml

    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(
            playground_disk_dir=tmp_path / "playground",
            playground_default_vcpus=2,
            playground_default_memory_mb=2048,
            libvirt_uri="qemu:///system",
        ),
    )
    (tmp_path / "playground").mkdir()

    with pytest.raises(RuntimeError, match="playground disk path escapes root"):
        vml._resolve_spawn_targets(
            job_id="pg-1",
            label="../../escaped",
            image_id="img",
            role="target",
            workload_id=None,
            immutable_backing_path=None,
            resources_resolved=True,
        )


def test_spawn_publication_surfaces_a_row_taken_before_completion(monkeypatch, tmp_path):
    """The disk exists and the domain is defined. If the row moved before the
    spawn could be recorded, reporting success would hand back a VM this task no
    longer owns."""
    from astinuslab.playground import vm_lifecycle as vml

    _destroy_fixture(monkeypatch, tmp_path, state="starting")
    monkeypatch.setattr(pr, "finish_vm_spawn", lambda *a, **k: False)

    with pytest.raises(RuntimeError, match="VM v1 lost spawn custody before completion"):
        vml._publish_spawn_result(
            vm_id="v1",
            workload_id=None,
            prebaked=False,
            ip_errors=[],
            claim_lease=SimpleNamespace(publication_token=lambda: datetime.now(UTC)),
        )


# ---------------------------------------------------------------------------
# The two seams the whole module hangs from
#
# `_libvirt_conn` is the only place a real libvirt connection is opened, and
# `_FakePlaygroundLifecycle` is the double that stands in for the host in every
# fake-mode test. Neither had coverage of its own: the first because tests
# replace it, the second because it *is* the replacement. A fake that disagrees
# with the real driver makes the fake-mode tier prove nothing, which is why its
# behaviour is pinned here rather than assumed.
# ---------------------------------------------------------------------------


def test_libvirt_conn_yields_none_and_stays_quiet_without_libvirt(monkeypatch):
    """The module imports libvirt optionally. Without it every caller must still
    work, with `conn` falsy, so host mutation is skipped rather than attempted."""
    from astinuslab.playground import orchestrator as po

    monkeypatch.setattr(po, "libvirt", None)

    with po._libvirt_conn() as conn:
        assert conn is None


def test_libvirt_conn_closes_the_connection_it_opened(monkeypatch):
    """A leaked connection accumulates per task on a long-running worker."""
    from astinuslab.playground import orchestrator as po

    closed = []
    conn = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(po, "libvirt", SimpleNamespace(open=lambda uri: conn))
    monkeypatch.setattr(po, "settings", SimpleNamespace(libvirt_uri="qemu+tcp://elsewhere/system"))

    with po._libvirt_conn() as opened:
        assert opened is conn
        assert closed == []

    assert closed == [True]


def test_libvirt_conn_closes_even_when_the_body_raises(monkeypatch):
    """Every caller of this does host work inside the block, and that work
    failing is the ordinary case rather than the exceptional one."""
    from astinuslab.playground import orchestrator as po

    closed = []
    conn = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(po, "libvirt", SimpleNamespace(open=lambda uri: conn))
    monkeypatch.setattr(po, "settings", SimpleNamespace(libvirt_uri="qemu:///system"))

    with pytest.raises(RuntimeError, match="host work failed"):
        with po._libvirt_conn():
            raise RuntimeError("host work failed")

    assert closed == [True]


def test_libvirt_conn_tolerates_a_connection_without_close(monkeypatch):
    """Fake and stub connections in this suite are not all libvirt objects, so
    the teardown must not assume the attribute exists."""
    from astinuslab.playground import orchestrator as po

    monkeypatch.setattr(po, "libvirt", SimpleNamespace(open=lambda uri: "not-a-connection-object"))
    monkeypatch.setattr(po, "settings", SimpleNamespace(libvirt_uri="qemu:///system"))

    with po._libvirt_conn() as conn:
        assert conn == "not-a-connection-object"


def test_the_fake_lifecycle_writes_a_disk_and_reports_the_domain_it_was_asked_for(tmp_path):
    """Fake mode still has to leave a file where a real boot would, because the
    spawn path hashes and cleans up that path afterwards."""
    from astinuslab.playground import orchestrator as po

    disk = tmp_path / "nested" / "web-1.qcow2"

    result = po._FakePlaygroundLifecycle().boot_playground(
        domain_name="astinuslab-pg-pg-1-web-1",
        memory_mb=2048,
        vcpus=2,
        base_image_path="/images/base.qcow2",
        disk_path=disk,
        disk_gb=10,
        networks=[],
        libvirt_uri="qemu:///system",
    )

    assert disk.read_bytes() == b"astinuslab fake playground disk\n"
    assert result.vm_name == "astinuslab-pg-pg-1-web-1"
    assert result.disk_path == disk


def test_the_fake_lifecycle_derives_a_guest_ip_from_the_network_it_is_asked_about(monkeypatch):
    """The fake returns an address inside the requested network's own subnet.

    That is not cosmetic. It is what lets a NIC-to-subnet wiring regression fail
    in the hermetic tier: a fake that always answered 10.42.1.10 would pass
    whichever network it was handed.
    """
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground.orchestrator import create_network, create_session

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    create_session.delay(job_id="pg-1").get()
    create_network.delay(job_id="pg-1", network_id="n2", name="cd", subnet_cidr="10.99.7.0/24", isolated=True).get()
    with SessionLocal() as s:
        libvirt_name = s.query(JobNetwork).filter_by(id="n2").one().libvirt_net_name

    from astinuslab.playground import orchestrator as po

    assert po._FakePlaygroundLifecycle().get_ip(None, libvirt_name, "52:54:00:aa:bb:cc") == "10.99.7.10"


@pytest.mark.parametrize(
    ("network_name", "why"),
    [
        pytest.param("astinuslab-pg-unknown", "no row matches, so there is no subnet to derive from", id="no-row"),
        pytest.param("", "an empty name cannot match a row either", id="empty-name"),
    ],
)
def test_the_fake_lifecycle_falls_back_to_its_historical_address(monkeypatch, network_name, why):
    """Without a matching network row the fake keeps the constant the older
    tests were written against, rather than raising inside a double."""
    from astinuslab.playground import orchestrator as po

    assert po._FakePlaygroundLifecycle().get_ip(None, network_name, "52:54:00:aa:bb:cc") == "10.42.1.10", why


def test_the_fake_lifecycle_host_operations_are_deliberate_no_ops():
    """destroy, attach_nic and detach_nic have nothing to do in fake mode, and
    must not raise: the tasks call them unconditionally."""
    from astinuslab.playground import orchestrator as po

    fake = po._FakePlaygroundLifecycle()

    # Nothing to assert but the absence of an exception: each is declared `-> None`
    # and the tasks call them without inspecting a result.
    fake.destroy(None, "astinuslab-pg-pg-1-web-1")
    fake.attach_nic(None, "dom", "net", "52:54:00:aa:bb:cc")
    fake.detach_nic(None, "dom", "52:54:00:aa:bb:cc")
