from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from astinuslab.jobs.celery_worker import celery_app
from astinuslab.main import app
from astinuslab.playground import acquisition as pa
from astinuslab.playground import network_lifecycle as nl
from astinuslab.playground import orchestrator as po
from tests.python._support.helpers import autospec_setattr, configure_csrf_client, isolated_playground_database


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

    def fake_dump_memory(dom, out):
        data = b"e2e-memory"
        out.write_bytes(data)
        return {"path": str(out), "size": len(data), "checksum": "abc"}

    monkeypatch.setattr(pa, "_dump_memory", fake_dump_memory)
    autospec_setattr(
        monkeypatch,
        pa,
        "_disk_capture",
        lambda: MagicMock(capture=lambda **kw: kw["output_path"].write_bytes(b"e2e-disk")),
    )
    with isolated_playground_database(monkeypatch, tmp_path, globals()):
        yield


def test_full_api_roundtrip():
    c = configure_csrf_client(TestClient(app))
    job = c.post("/api/v1/playground/sessions", json={}).json()
    assert job["job_id"].startswith("pg-")
    net = c.post(
        f"/api/v1/playground/sessions/{job['job_id']}/networks",
        json={"name": "ab", "subnet_cidr": "10.42.1.0/24", "isolated": True},
    ).json()
    vm = c.post(
        f"/api/v1/playground/sessions/{job['job_id']}/vms",
        json={"label": "t1", "image_id": "img", "nics": [{"network_id": net["id"]}]},
    ).json()
    assert vm["state"] == "running"

    mem = c.post(f"/api/v1/playground/vms/{vm['id']}/captures", json={"kind": "memory"})
    assert mem.status_code == 202
    disk = c.post(f"/api/v1/playground/vms/{vm['id']}/captures", json={"kind": "disk"})
    assert disk.status_code == 202

    caps = c.get(f"/api/v1/playground/vms/{vm['id']}/captures").json()
    assert len(caps) == 2

    assert c.delete(f"/api/v1/playground/sessions/{job['job_id']}").status_code == 202
    assert c.get("/api/v1/playground/sessions/current").status_code == 404
