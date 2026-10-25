from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from astinuslab.db import SessionLocal
from astinuslab.db.models import JobVMNic
from astinuslab.jobs.celery_worker import celery_app
from astinuslab.playground import network_lifecycle as nl
from astinuslab.playground import orchestrator as po
from tests.python._support.helpers import autospec_setattr, isolated_playground_database


@pytest.fixture(autouse=True)
def _eager(monkeypatch, tmp_path):
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(
            playground_disk_dir=tmp_path / "disks",
            playground_capture_dir=tmp_path / "caps",
            playground_default_vcpus=2,
            playground_default_memory_mb=2048,
            libvirt_uri="qemu:///system",
            base_dir=tmp_path,
            data_dir=tmp_path,
        ),
    )
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: MagicMock(get_ip=lambda *a, **kw: "10.42.1.10"))
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
            "credentials": {"username": "u", "password": "p"},
        },
    )
    with isolated_playground_database(monkeypatch, tmp_path, globals()):
        yield


def test_pivot_topology_three_segments_four_vms():
    po.create_session.delay(job_id="pg-1").get()
    po.create_network.delay(job_id="pg-1", network_id="ab", name="ab", subnet_cidr="10.42.1.0/24", isolated=True).get()
    po.create_network.delay(job_id="pg-1", network_id="bc", name="bc", subnet_cidr="10.42.2.0/24", isolated=True).get()
    po.create_network.delay(job_id="pg-1", network_id="cd", name="cd", subnet_cidr="10.42.3.0/24", isolated=True).get()

    # a1: only on ab
    po.spawn_vm.delay(
        job_id="pg-1",
        vm_id="a1",
        label="a1",
        image_id="img",
        nic_specs=[{"network_id": "ab", "nic_id": "a1-ab"}],
    ).get()
    # b1: pivot between ab and bc
    po.spawn_vm.delay(
        job_id="pg-1",
        vm_id="b1",
        label="b1",
        image_id="img",
        nic_specs=[
            {"network_id": "ab", "nic_id": "b1-ab"},
            {"network_id": "bc", "nic_id": "b1-bc"},
        ],
    ).get()
    # c1: pivot between bc and cd
    po.spawn_vm.delay(
        job_id="pg-1",
        vm_id="c1",
        label="c1",
        image_id="img",
        nic_specs=[
            {"network_id": "bc", "nic_id": "c1-bc"},
            {"network_id": "cd", "nic_id": "c1-cd"},
        ],
    ).get()
    # d1: only on cd
    po.spawn_vm.delay(
        job_id="pg-1",
        vm_id="d1",
        label="d1",
        image_id="img",
        nic_specs=[{"network_id": "cd", "nic_id": "d1-cd"}],
    ).get()

    with SessionLocal() as s:
        nics_by_vm: dict[str, set[str]] = {}
        for nic in s.query(JobVMNic).all():
            nics_by_vm.setdefault(nic.job_vm_id, set()).add(nic.job_network_id)

        assert nics_by_vm["a1"] == {"ab"}
        assert nics_by_vm["b1"] == {"ab", "bc"}
        assert nics_by_vm["c1"] == {"bc", "cd"}
        assert nics_by_vm["d1"] == {"cd"}

        # Topology invariants
        assert nics_by_vm["a1"].isdisjoint(nics_by_vm["c1"])
        assert nics_by_vm["b1"] & nics_by_vm["a1"]
        assert nics_by_vm["b1"] & nics_by_vm["c1"]
