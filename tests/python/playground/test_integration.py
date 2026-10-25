from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from astinuslab.db import SessionLocal
from astinuslab.db.models import Acquisition, JobNetwork, JobVM, PlaygroundSession
from astinuslab.jobs.celery_worker import celery_app
from astinuslab.playground import acquisition as pa
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

    networks_mock = MagicMock()
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: networks_mock)

    vm_mock = MagicMock()
    vm_mock.get_ip.return_value = "10.42.1.10"
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: vm_mock)

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

    autospec_setattr(monkeypatch, pa, "_dump_memory", lambda domain, out: out.write_bytes(b"memory"))

    disk_capture_mock = MagicMock()
    disk_capture_mock.capture.side_effect = lambda **kw: kw["output_path"].write_bytes(b"disk")
    autospec_setattr(monkeypatch, pa, "_disk_capture", lambda: disk_capture_mock)

    with isolated_playground_database(monkeypatch, tmp_path, globals()):
        yield


def test_full_session_roundtrip():
    po.create_session.delay(job_id="pg-1").get()
    po.create_network.delay(
        job_id="pg-1",
        network_id="n1",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
    ).get()
    po.spawn_vm.delay(
        job_id="pg-1",
        vm_id="v1",
        label="t1",
        image_id="img",
        nic_specs=[{"network_id": "n1", "nic_id": "nic1"}],
    ).get()
    mem_acq = po.capture_memory.delay(vm_id="v1").get()
    disk_acq = po.capture_disk.delay(vm_id="v1").get()
    po.destroy_session.delay(job_id="pg-1").get()

    with SessionLocal() as s:
        playground_session = s.query(PlaygroundSession).filter_by(id="pg-1").one()
        assert playground_session.status == "cancelled"
        assert all(v.state == "destroyed" for v in s.query(JobVM).all())
        assert all(n.state == "destroyed" for n in s.query(JobNetwork).all())
        acqs = {a.id for a in s.query(Acquisition).filter_by(job_vm_id="v1").all()}
        assert {mem_acq, disk_acq} == acqs
