from unittest.mock import MagicMock

import pytest

from astinuslab.jobs.celery_worker import celery_app
from astinuslab.playground import network_lifecycle as nl
from astinuslab.playground import orchestrator as po
from tests.python._support.helpers import autospec_setattr


@pytest.fixture(autouse=True)
def _eager(monkeypatch, tmp_path, in_memory_session_scope):
    monkeypatch.setattr(po, "session_scope", in_memory_session_scope)
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setattr(
        po,
        "settings",
        po.settings.model_copy(
            update={
                "storage_dir": storage,
                "playground_disk_dir": storage / "playground/disks",
                "playground_capture_dir": storage / "playground/captures",
            }
        ),
    )
    image_file = tmp_path / "base.qcow2"
    image_file.write_bytes(b"placeholder image")
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    fake_lifecycle = MagicMock()
    fake_lifecycle.get_ip.return_value = None
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda image_id, *, role="target": {
            "id": image_id,
            "path": str(image_file),
            "distro": "ubuntu",
            "version": "22.04",
            "family": "debian",
        },
    )


def test_F01_vms_endpoint_honors_network_ids(client):
    response = client.post("/api/v1/playground/sessions", json={})
    assert response.status_code == 202, response.text
    first_network = client.post(
        "/api/v1/playground/networks",
        json={"name": "lan1", "subnet_cidr": "10.50.0.0/24", "isolated": True},
    )
    assert first_network.status_code == 202, first_network.text
    net1 = first_network.json()
    second_network = client.post(
        "/api/v1/playground/networks",
        json={"name": "lan2", "subnet_cidr": "10.51.0.0/24", "isolated": True},
    )
    assert second_network.status_code == 202, second_network.text
    net2 = second_network.json()
    n1 = net1["networks"][-1]["id"]
    n2 = net2["networks"][-1]["id"]
    res = client.post(
        "/api/v1/playground/vms",
        json={"label": "tgt", "image_id": "img", "role": "target", "network_ids": [n1, n2]},
    )
    assert res.status_code == 202, res.text
    body = res.json()
    assert [(nic["ordinal"], nic["job_network_id"]) for nic in body["nics"]] == [(0, n1), (1, n2)]
