from __future__ import annotations

import io
import json
import subprocess
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from astinuslab.db import engine
from astinuslab.jobs.celery_worker import celery_app
from astinuslab.main import app
from tests.python._support.helpers import autospec_setattr, configure_csrf_client, isolated_playground_database


@pytest.fixture(autouse=True)
def _eager(monkeypatch, tmp_path):
    # Eager execution still acquires a producer to serialize task arguments.
    # Keep this fixture independent of other suites' broker configuration.
    monkeypatch.setattr(celery_app.conf, "broker_url", "memory://")
    monkeypatch.setattr(celery_app.conf, "result_backend", "cache+memory://")
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    with isolated_playground_database(monkeypatch, tmp_path, globals()):
        yield


@pytest.fixture
def client():
    return configure_csrf_client(TestClient(app))


def test_create_session_returns_session_payload(client, monkeypatch):
    from unittest.mock import MagicMock

    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: MagicMock())

    res = client.post("/api/v1/playground/sessions", json={})
    assert res.status_code == 202
    body = res.json()
    assert body["status"] == "running"
    assert body["job_id"].startswith("pg-")
    assert body["vms"] == []
    assert body["networks"] == []


def test_get_current_returns_active_session(client, monkeypatch):
    from unittest.mock import MagicMock

    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: MagicMock())

    create = client.post("/api/v1/playground/sessions", json={}).json()
    res = client.get("/api/v1/playground/sessions/current")
    assert res.status_code == 200
    assert res.json()["job_id"] == create["job_id"]


def test_get_current_returns_404_when_idle(client):
    res = client.get("/api/v1/playground/sessions/current")
    assert res.status_code == 404


def test_get_current_or_null_returns_null_when_idle(client):
    res = client.get("/api/v1/playground/sessions/current-or-null")
    assert res.status_code == 200
    assert res.json() is None


@pytest.mark.parametrize("coordinate", ["NaN", "Infinity", "-Infinity"])
def test_topology_layout_rejects_non_finite_coordinates_without_persisting(client, monkeypatch, coordinate):
    from unittest.mock import MagicMock

    from astinuslab.db import session_scope
    from astinuslab.db.models import PlaygroundTopologyLayout
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: MagicMock())
    client.post("/api/v1/playground/sessions", json={})

    response = client.patch(
        "/api/v1/playground/sessions/current/topology-layout/node-1",
        json={"node_id": "node-1", "node_type": "vm", "x": coordinate, "y": 5.0},
    )

    assert response.status_code == 422
    with session_scope() as session:
        assert session.query(PlaygroundTopologyLayout).count() == 0


def test_topology_layout_finite_coordinates_round_trip(client, monkeypatch):
    from unittest.mock import MagicMock

    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: MagicMock())
    client.post("/api/v1/playground/sessions", json={})

    saved = client.patch(
        "/api/v1/playground/sessions/current/topology-layout/node-1",
        json={"node_id": "node-1", "node_type": "vm", "x": 12.5, "y": -3.25},
    )
    loaded = client.get("/api/v1/playground/sessions/current/topology-layout")

    assert saved.status_code == 200
    assert saved.json()["x"] == 12.5
    assert saved.json()["y"] == -3.25
    assert loaded.json()["nodes"] == [saved.json()]


@pytest.mark.parametrize("coordinate", ["NaN", "Infinity", "-Infinity"])
def test_recipe_import_rejects_non_finite_topology_before_creating_a_session(client, coordinate):
    response = client.post(
        "/api/v1/playground/recipes/import",
        json={
            "name": "invalid-layout",
            "networks": [],
            "vms": [],
            "topology": [{"node_id": "node-1", "node_type": "vm", "x": coordinate, "y": 5.0}],
        },
    )

    assert response.status_code == 422
    assert client.get("/api/v1/playground/sessions/current").status_code == 404


def test_create_session_409_when_one_active(client, monkeypatch):
    from unittest.mock import MagicMock

    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: MagicMock())

    client.post("/api/v1/playground/sessions", json={})
    second = client.post("/api/v1/playground/sessions", json={})
    assert second.status_code == 409


def test_create_session_reports_authoring_closed_while_verification_owns_the_lab(client):
    from astinuslab.db import SessionLocal
    from astinuslab.playground import repositories as pr

    with SessionLocal() as session:
        pr.create_playground_session(session, job_id="pg-frozen-create")
        pr.close_authored_session(session, "pg-frozen-create")
        session.commit()

    response = client.post("/api/v1/playground/sessions", json={})

    assert response.status_code == 409
    assert response.json()["code"] == "authoring_closed"


def test_authoring_closed_session_rejects_all_vm_and_network_mutations(client, tmp_path):
    from astinuslab.api.errors import ApiError
    from astinuslab.db import SessionLocal
    from astinuslab.playground import repositories as pr
    from astinuslab.playground.routes import networks as network_routes

    with SessionLocal() as session:
        pr.create_playground_session(session, job_id="pg-frozen-api")
        pr.register_network(
            session,
            network_id="net-frozen-api",
            job_id="pg-frozen-api",
            name="isolated",
            subnet_cidr="10.42.90.0/24",
            isolated=True,
            libvirt_net_name="astinuslab-pg-frozen-api-isolated",
        )
        pr.update_network_state(session, "net-frozen-api", "active")
        pr.register_vm(
            session,
            vm_id="vm-frozen-api",
            job_id="pg-frozen-api",
            label="target",
            image_id="img",
            domain_name="astinuslab-pg-frozen-api-target",
            disk_path=str(tmp_path / "target.qcow2"),
        )
        pr.update_vm_state(session, "vm-frozen-api", "running")
        pr.attach_nic(
            session,
            nic_id="nic-frozen-api",
            vm_id="vm-frozen-api",
            network_id="net-frozen-api",
            mac="52:54:00:00:00:90",
        )
        pr.close_authored_session(session, "pg-frozen-api")
        session.commit()

    requests = [
        client.post(
            "/api/v1/playground/networks",
            json={"name": "other", "subnet_cidr": "10.42.91.0/24", "isolated": True},
        ),
        client.post(
            "/api/v1/playground/sessions/pg-frozen-api/networks",
            json={"name": "other", "subnet_cidr": "10.42.91.0/24", "isolated": True},
        ),
        client.post(
            "/api/v1/playground/sessions/pg-frozen-api/vms",
            json={
                "label": "other",
                "image_id": "missing",
                "nics": [{"network_id": "net-frozen-api"}],
            },
        ),
        client.delete("/api/v1/playground/networks/net-frozen-api"),
        client.delete("/api/v1/playground/vms/vm-frozen-api"),
        client.post("/api/v1/playground/vms/vm-frozen-api/actions", json={"action": "reboot"}),
        client.post(
            "/api/v1/playground/sessions/current/vms/vm-frozen-api/nics",
            json={"network_id": "net-frozen-api"},
        ),
        client.delete("/api/v1/playground/sessions/current/vms/vm-frozen-api/nics/nic-frozen-api"),
        client.post("/api/v1/playground/vms/vm-frozen-api/captures", json={"kind": "disk"}),
        client.patch(
            "/api/v1/playground/sessions/current/topology-layout/vm-frozen-api",
            json={"node_id": "vm-frozen-api", "node_type": "vm", "x": 1, "y": 2},
        ),
        client.delete("/api/v1/playground/sessions/pg-frozen-api"),
    ]

    for response in requests:
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "authoring_closed"

    with pytest.raises(ApiError) as inline_error:
        network_routes._create_inline_networks(
            "pg-frozen-api",
            [network_routes.PlaygroundCreateNetworkRequest(name="inline", subnet_cidr="10.42.92.0/24", isolated=True)],
        )
    assert inline_error.value.code == "authoring_closed"
    with pytest.raises(ApiError) as default_error:
        network_routes._ensure_default_network("pg-frozen-api")
    assert default_error.value.code == "authoring_closed"


def test_destroy_session_returns_204(client, monkeypatch):
    from unittest.mock import MagicMock

    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: MagicMock())

    job_id = client.post("/api/v1/playground/sessions", json={}).json()["job_id"]
    res = client.delete(f"/api/v1/playground/sessions/{job_id}")
    assert res.status_code == 202
    assert client.get("/api/v1/playground/sessions/current").status_code == 404


def test_destroy_session_exposes_destroy_requested_state_during_cleanup(client, monkeypatch):
    """When destroy is enqueued but cleanup has not yet run, the session must
    still be visible in destroy_requested state so the UI can show progress
    and normal jobs remain blocked."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: MagicMock())

    job_id = client.post("/api/v1/playground/sessions", json={}).json()["job_id"]

    # Defer destroy_session so we observe pre-cleanup state.
    autospec_setattr(monkeypatch, po.destroy_session, "delay", lambda **kw: SimpleNamespace(id="deferred"))

    res = client.delete(f"/api/v1/playground/sessions/{job_id}")
    assert res.status_code == 202

    current = client.get("/api/v1/playground/sessions/current")
    assert current.status_code == 200
    assert current.json()["status"] == "destroy_requested"


def test_create_session_blocked_while_previous_is_destroying(client, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: MagicMock())

    job_id = client.post("/api/v1/playground/sessions", json={}).json()["job_id"]
    autospec_setattr(monkeypatch, po.destroy_session, "delay", lambda **kw: SimpleNamespace(id="deferred"))
    client.delete(f"/api/v1/playground/sessions/{job_id}")

    second = client.post("/api/v1/playground/sessions", json={})
    assert second.status_code == 409


def test_create_network_returns_active_network(client, monkeypatch):
    from unittest.mock import MagicMock

    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: MagicMock())

    job_id = client.post("/api/v1/playground/sessions", json={}).json()["job_id"]
    res = client.post(
        f"/api/v1/playground/sessions/{job_id}/networks",
        json={"name": "ab", "subnet_cidr": "10.42.1.0/24", "isolated": True},
    )
    assert res.status_code == 202
    body = res.json()
    assert body["name"] == "ab"
    assert body["state"] == "active"


def test_create_network_returns_promptly_without_waiting_for_celery_result(client, monkeypatch):
    from unittest.mock import MagicMock

    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: MagicMock())

    job_id = client.post("/api/v1/playground/sessions", json={}).json()["job_id"]
    get_calls = []
    delay_calls = []

    class NeverReturningResult:
        def get(self, timeout=None):
            get_calls.append(timeout)
            raise AssertionError("playground API must not wait for Celery result")

    def fake_delay(**kwargs):
        delay_calls.append(kwargs)
        return NeverReturningResult()

    monkeypatch.setattr(po.create_network, "delay", fake_delay)

    started = time.perf_counter()
    res = client.post(
        f"/api/v1/playground/sessions/{job_id}/networks",
        json={"name": "slow", "subnet_cidr": "10.42.2.0/24", "isolated": True},
    )
    elapsed = time.perf_counter() - started

    assert elapsed < 1
    assert res.status_code == 202
    assert res.json()["name"] == "slow"
    assert res.json()["state"] == "pending"
    assert delay_calls == [
        {
            "job_id": job_id,
            "network_id": res.json()["id"],
            "name": "slow",
            "subnet_cidr": "10.42.2.0/24",
            "isolated": True,
        }
    ]
    assert get_calls == []


def test_create_network_without_session_creates_session_payload(client, monkeypatch):
    from unittest.mock import MagicMock

    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: MagicMock())

    res = client.post(
        "/api/v1/playground/networks",
        json={"name": "ab", "subnet_cidr": "10.42.1.0/24", "isolated": True},
    )

    assert res.status_code == 202
    body = res.json()
    assert body["status"] == "running"
    assert body["job_id"].startswith("pg-")
    assert body["vms"] == []
    assert len(body["networks"]) == 1
    assert body["networks"][0]["name"] == "ab"
    assert body["networks"][0]["state"] == "active"


def test_create_network_without_session_rejects_too_small_cidr_without_side_effect(client, monkeypatch):
    from unittest.mock import MagicMock

    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: MagicMock())

    res = client.post(
        "/api/v1/playground/networks",
        json={"name": "ab", "subnet_cidr": "10.42.1.0/31", "isolated": True},
    )

    assert res.status_code == 422
    assert client.get("/api/v1/playground/sessions/current").status_code == 404


def test_create_network_without_session_reuses_active_session_and_validates(client, monkeypatch):
    from unittest.mock import MagicMock

    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: MagicMock())

    first = client.post(
        "/api/v1/playground/networks",
        json={"name": "ab", "subnet_cidr": "10.42.1.0/24", "isolated": True},
    ).json()
    second = client.post(
        "/api/v1/playground/networks",
        json={"name": "cd", "subnet_cidr": "10.42.2.0/24", "isolated": True},
    )
    duplicate = client.post(
        "/api/v1/playground/networks",
        json={"name": "ab", "subnet_cidr": "10.42.3.0/24", "isolated": True},
    )
    overlap = client.post(
        "/api/v1/playground/networks",
        json={"name": "ef", "subnet_cidr": "10.42.2.128/25", "isolated": True},
    )

    assert second.status_code == 202
    body = second.json()
    assert body["job_id"] == first["job_id"]
    assert [n["name"] for n in body["networks"]] == ["ab", "cd"]
    assert duplicate.status_code == 409
    assert overlap.status_code == 202
    ef = next(n for n in overlap.json()["networks"] if n["name"] == "ef")
    assert ef["subnet_cidr"] == "10.42.3.0/24"
    assert ef["cidr_substituted_from"] == "10.42.2.128/25"


def test_concurrent_overlapping_network_creates_one_and_conflicts_cleanly(client, monkeypatch):
    from unittest.mock import MagicMock

    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.routes import networks as networks_routes

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: MagicMock())
    autospec_setattr(monkeypatch, po.create_network, "delay", lambda **kwargs: MagicMock(id="fake-celery-id"))

    job_id = client.post("/api/v1/playground/sessions", json={}).json()["job_id"]
    original_register_pending_network = networks_routes._register_pending_network
    barrier = Barrier(2)

    def synchronized_register_pending_network(*args, **kwargs):
        barrier.wait(timeout=5)
        return original_register_pending_network(*args, **kwargs)

    monkeypatch.setattr(networks_routes, "_register_pending_network", synchronized_register_pending_network)

    def create_network(name: str, cidr: str) -> int:
        with TestClient(app, raise_server_exceptions=False) as thread_client:
            configure_csrf_client(thread_client)
            response = thread_client.post(
                f"/api/v1/playground/sessions/{job_id}/networks",
                json={"name": name, "subnet_cidr": cidr, "isolated": True},
            )
            return response.status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = sorted(
            executor.map(
                lambda args: create_network(*args),
                [("ab", "10.42.1.0/24"), ("cd", "10.42.1.128/25")],
            )
        )

    assert statuses == [202, 409]


def test_create_network_without_session_409_when_non_playground_job_active(client, monkeypatch):
    from unittest.mock import MagicMock

    from astinuslab.db import SessionLocal
    from astinuslab.db.models import Job
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: MagicMock())

    with SessionLocal() as s:
        s.add(
            Job(
                id="job-active",
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
                status="queued",
            )
        )
        s.commit()

    res = client.post(
        "/api/v1/playground/networks",
        json={"name": "ab", "subnet_cidr": "10.42.1.0/24", "isolated": True},
    )

    assert res.status_code == 409
    assert "active" in res.json()["detail"].lower()


def test_create_network_without_session_409_when_non_playground_job_waiting_for_capture(client, monkeypatch):
    from unittest.mock import MagicMock

    from astinuslab.db import SessionLocal
    from astinuslab.db.models import Job
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground import repositories as pr

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: MagicMock())

    with SessionLocal() as s:
        s.add(
            Job(
                id="job-waiting",
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
                status="waiting_for_capture",
            )
        )
        s.commit()

    res = client.post(
        "/api/v1/playground/networks",
        json={"name": "ab", "subnet_cidr": "10.42.1.0/24", "isolated": True},
    )

    assert res.status_code == 409
    assert "active" in res.json()["detail"].lower()
    with SessionLocal() as s:
        assert pr.active_playground_session(s) is None


def test_create_network_with_existing_session_409_when_non_playground_job_active(client, monkeypatch):
    from unittest.mock import MagicMock

    from astinuslab.db import SessionLocal
    from astinuslab.db.models import Job
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground import repositories as pr

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: MagicMock())

    first = client.post(
        "/api/v1/playground/networks",
        json={"name": "ab", "subnet_cidr": "10.42.1.0/24", "isolated": True},
    ).json()
    with SessionLocal() as s:
        s.add(
            Job(
                id="job-waiting",
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
                status="waiting_for_capture",
            )
        )
        s.commit()

    res = client.post(
        "/api/v1/playground/networks",
        json={"name": "cd", "subnet_cidr": "10.42.2.0/24", "isolated": True},
    )

    assert res.status_code == 409
    assert "active" in res.json()["detail"].lower()
    with SessionLocal() as s:
        assert [n.name for n in pr.list_session_networks(s, first["job_id"])] == ["ab"]


def test_create_network_legacy_endpoint_409_when_non_playground_job_active(client, monkeypatch):
    from unittest.mock import MagicMock

    from astinuslab.db import SessionLocal
    from astinuslab.db.models import Job
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground import repositories as pr

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: MagicMock())

    job_id = client.post("/api/v1/playground/sessions", json={}).json()["job_id"]
    with SessionLocal() as s:
        s.add(
            Job(
                id="job-waiting",
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
                status="waiting_for_capture",
            )
        )
        s.commit()

    res = client.post(
        f"/api/v1/playground/sessions/{job_id}/networks",
        json={"name": "ab", "subnet_cidr": "10.42.1.0/24", "isolated": True},
    )

    assert res.status_code == 409
    assert "active" in res.json()["detail"].lower()
    with SessionLocal() as s:
        assert pr.list_session_networks(s, job_id) == []


def test_create_network_substitutes_overlapping_cidr(client, monkeypatch):
    from unittest.mock import MagicMock

    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: MagicMock())

    job_id = client.post("/api/v1/playground/sessions", json={}).json()["job_id"]
    client.post(
        f"/api/v1/playground/sessions/{job_id}/networks",
        json={"name": "ab", "subnet_cidr": "10.42.1.0/24", "isolated": True},
    )
    res = client.post(
        f"/api/v1/playground/sessions/{job_id}/networks",
        json={"name": "cd", "subnet_cidr": "10.42.1.128/25", "isolated": True},
    )
    assert res.status_code == 202
    body = res.json()
    assert body["subnet_cidr"] == "10.42.2.0/24"
    assert body["cidr_substituted_from"] == "10.42.1.128/25"


def test_delete_network_returns_204(client, monkeypatch):
    from unittest.mock import MagicMock

    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    network_helper = MagicMock()
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: network_helper)
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: MagicMock())

    job_id = client.post("/api/v1/playground/sessions", json={}).json()["job_id"]
    net = client.post(
        f"/api/v1/playground/sessions/{job_id}/networks",
        json={"name": "ab", "subnet_cidr": "10.42.1.0/24", "isolated": True},
    ).json()
    res = client.delete(f"/api/v1/playground/networks/{net['id']}")
    assert res.status_code == 202
    network_helper.destroy_network.assert_called_once_with(net["libvirt_net_name"])
    from astinuslab.db import SessionLocal
    from astinuslab.db.models import JobNetwork

    with SessionLocal() as session:
        network = session.get(JobNetwork, net["id"])
        assert network is not None
        assert network.state == "destroyed"


def _common_monkeypatch(monkeypatch, tmp=None):
    import pathlib
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    if tmp is None:
        import tempfile

        tmp = pathlib.Path(tempfile.mkdtemp())
    base_image = tmp / "base.qcow2"
    base_image.write_bytes(b"placeholder image")
    network_helper = MagicMock()
    lifecycle = MagicMock(get_ip=lambda *a, **kw: "10.42.1.10")
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: network_helper)
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: lifecycle)
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda _image_id, *, role="target": {
            "id": "img",
            "path": str(base_image),
            "format": "qcow2",
            "architecture": "x86_64",
            "credentials": {"username": "u", "password": "p"},
        },
    )
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(
            playground_disk_dir=tmp / "disks",
            playground_capture_dir=tmp / "caps",
            playground_default_vcpus=2,
            playground_default_memory_mb=2048,
            libvirt_uri="qemu:///system",
            base_dir=tmp,
            data_dir=tmp,
            storage_dir=tmp / "storage",
        ),
    )
    return network_helper, lifecycle


def _make_session_with_network(client):
    job_id = client.post("/api/v1/playground/sessions", json={}).json()["job_id"]
    net = client.post(
        f"/api/v1/playground/sessions/{job_id}/networks",
        json={"name": "ab", "subnet_cidr": "10.42.1.0/24", "isolated": True},
    ).json()
    return job_id, net


def test_spawn_target_accepts_workload_and_returns_services(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    from astinuslab import catalog
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground import workloads as pw

    workload = {
        "id": "dvwa",
        "display_name": "DVWA",
        "services": [{"name": "http", "port": 80, "url_path": "/", "credentials": {"user": "admin"}}],
        "service_labels": ["http"],
        "tags": ["web"],
        "cve_ids": [],
        "is_stack": False,
    }
    autospec_setattr(
        monkeypatch, catalog, "workload_by_id", lambda workload_id: workload if workload_id == "dvwa" else None
    )
    autospec_setattr(
        monkeypatch,
        catalog,
        "workload_compatibility_for_target",
        lambda workload, target, workload_map=None: {
            "compatibility_status": "unknown",
            "compatibility_reason": "not declared",
        },
    )
    autospec_setattr(
        monkeypatch,
        pw,
        "annotated_workloads_for_playground_image",
        lambda image: [{**workload, "compatibility_status": "unknown", "compatibility_reason": "not declared"}],
    )
    autospec_setattr(monkeypatch, po.provision_vm_workload, "delay", lambda **kwargs: None)

    res = client.post(
        "/api/v1/playground/vms",
        json={"label": "web", "image_id": "img", "role": "target", "workload_id": "dvwa"},
    )

    assert res.status_code == 202
    body = res.json()
    assert body["workload_id"] == "dvwa"
    assert body["workload_display_name"] == "DVWA"
    assert body["workload_state"] in {"planned", "provisioning", "ready"}
    assert body["services"] == [
        {
            "name": "http",
            "port": 80,
            "url_path": "/",
            "url": "http://10.42.1.10:80/",
            "credentials": {"user": "admin"},
            "status": body["workload_state"],
        }
    ]


def test_spawn_attacker_rejects_workload_id(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)

    res = client.post(
        "/api/v1/playground/vms",
        json={"label": "atk", "image_id": "img", "role": "attacker", "workload_id": "dvwa"},
    )

    assert res.status_code == 400
    assert "targets only" in res.json()["detail"]


def test_spawn_network_rejects_workload_id(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)

    res = client.post(
        "/api/v1/playground/vms",
        json={"label": "edge", "image_id": "img", "role": "network", "workload_id": "dvwa"},
    )

    assert res.status_code == 400
    assert "targets only" in res.json()["detail"]


def test_spawn_network_rejects_unknown_image(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, po, "lookup_playground_image", lambda _image_id, *, role="target": None)

    res = client.post(
        "/api/v1/playground/vms",
        json={"label": "edge", "image_id": "missing", "role": "network"},
    )

    assert res.status_code == 400
    assert "unknown or unavailable image" in res.json()["detail"]


def test_spawn_target_rejects_unknown_workload(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    from astinuslab import catalog

    autospec_setattr(monkeypatch, catalog, "workload_by_id", lambda workload_id: None)

    res = client.post(
        "/api/v1/playground/vms",
        json={"label": "web", "image_id": "img", "role": "target", "workload_id": "missing"},
    )

    assert res.status_code == 400
    assert "not found" in res.json()["detail"]


def test_list_playground_workloads_for_image(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    from astinuslab.playground import workloads as pw

    autospec_setattr(
        monkeypatch,
        pw,
        "annotated_workloads_for_playground_image",
        lambda image: [
            {
                "id": "dvwa",
                "display_name": "DVWA",
                "tags": ["web"],
                "service_labels": ["http"],
                "services": [{"name": "http", "port": 80, "url_path": "/", "credentials": None}],
                "cve_ids": [],
                "is_stack": False,
                "compatibility_status": "unknown",
                "compatibility_reason": "not declared",
            }
        ],
    )

    res = client.get("/api/v1/playground/catalog/workloads?image_id=img")

    assert res.status_code == 200
    assert res.json()[0]["id"] == "dvwa"
    assert res.json()[0]["compatibility_status"] == "unknown"


def test_create_if_needed_spawn_creates_session_default_network_and_vm(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)

    res = client.post(
        "/api/v1/playground/vms",
        json={"label": "web", "image_id": "img", "role": "target"},
    )

    assert res.status_code == 202
    body = res.json()
    assert body["label"] == "web"
    assert body["role"] == "target"
    assert len(body["nics"]) == 1

    session = client.get("/api/v1/playground/sessions/current")
    assert session.status_code == 200
    session_body = session.json()
    assert len(session_body["networks"]) == 1
    default_network = session_body["networks"][0]
    assert default_network["name"] == "default"
    assert default_network["subnet_cidr"] == "10.42.1.0/24"
    assert default_network["isolated"] is True
    assert body["nics"][0]["job_network_id"] == default_network["id"]


def test_create_if_needed_rejects_unavailable_image_without_creating_session(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    from astinuslab.playground import orchestrator as po

    missing_image = tmp_path / "missing.qcow2"
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda _image_id, *, role="target": {
            "id": "missing",
            "path": str(missing_image),
            "format": "qcow2",
            "architecture": "x86_64",
        },
    )

    def fail_delay(**kwargs):
        raise AssertionError("unavailable image must be rejected before enqueue")

    monkeypatch.setattr(po.create_network, "delay", fail_delay)
    monkeypatch.setattr(po.spawn_vm, "delay", fail_delay)

    res = client.post(
        "/api/v1/playground/vms",
        json={"label": "web", "image_id": "missing", "role": "target"},
    )

    assert res.status_code == 400
    assert "image not available" in res.json()["detail"]
    assert client.get("/api/v1/playground/sessions/current-or-null").json() is None


def test_create_if_needed_cleanup_survives_destroy_broker_failure(client, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from astinuslab.db import SessionLocal, session_scope
    from astinuslab.db.models import PlaygroundSession
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground import repositories as pr
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    _common_monkeypatch(monkeypatch, tmp=tmp_path)

    def broker_down(**_kwargs):
        raise RuntimeError("destroy broker down")

    monkeypatch.setattr(po.destroy_session, "delay", broker_down)

    response = client.post(
        "/api/v1/playground/vms",
        json={"label": "web", "image_id": "img", "role": "target", "network_ids": ["bogus"]},
    )

    assert response.status_code == 400
    with SessionLocal() as session:
        cleanup = session.query(PlaygroundSession).one()
        cleanup_id = cleanup.id
        assert cleanup.status == "destroy_failed"
        assert "destroy broker down" in (cleanup.error_summary or "")

    fake_conn = MagicMock()
    fake_conn.listAllDomains.return_value = []
    fake_conn.listAllNetworks.return_value = []
    fake_libvirt = SimpleNamespace(
        open=MagicMock(return_value=fake_conn),
        libvirtError=type("FakeLibvirtError", (Exception,), {}),
    )
    reconcile_libvirt_resources(
        session_scope=session_scope,
        libvirt_module=fake_libvirt,
        libvirt_uri="qemu:///test",
        grace_seconds=0,
    )

    with SessionLocal() as session:
        reconciled = session.get(PlaygroundSession, cleanup_id)
        assert reconciled is not None
        assert reconciled.status == "cancelled"
        assert pr.active_playground_session(session) is None


def test_create_if_needed_spawn_accepts_network_role_multi_nic_and_resources(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    job_id = client.post("/api/v1/playground/sessions", json={}).json()["job_id"]
    net1 = client.post(
        f"/api/v1/playground/sessions/{job_id}/networks",
        json={"name": "wan", "subnet_cidr": "10.42.10.0/24", "isolated": True},
    ).json()
    net2 = client.post(
        f"/api/v1/playground/sessions/{job_id}/networks",
        json={"name": "lan", "subnet_cidr": "10.42.20.0/24", "isolated": True},
    ).json()

    res = client.post(
        "/api/v1/playground/vms",
        json={
            "label": "edge",
            "image_id": "img",
            "role": "network",
            "network_ids": [net1["id"], net2["id"]],
            "vcpus": 1,
            "memory_mb": 512,
            "disk_gb": 4,
        },
    )

    assert res.status_code == 202
    body = res.json()
    assert body["role"] == "network"
    assert body["workload_id"] is None
    assert len(body["nics"]) == 2


def test_create_if_needed_spawn_creates_inline_network_with_vm(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)

    res = client.post(
        "/api/v1/playground/vms",
        json={
            "label": "web",
            "image_id": "img",
            "role": "target",
            "new_networks": [
                {"name": "labnet", "subnet_cidr": "10.42.2.0/24", "isolated": True},
            ],
        },
    )

    assert res.status_code == 202
    body = res.json()
    assert body["label"] == "web"
    assert len(body["nics"]) == 1

    session = client.get("/api/v1/playground/sessions/current")
    assert session.status_code == 200
    session_body = session.json()
    assert len(session_body["networks"]) == 1
    assert session_body["networks"][0]["name"] == "labnet"
    assert session_body["networks"][0]["subnet_cidr"] == "10.42.2.0/24"
    assert body["nics"][0]["job_network_id"] == session_body["networks"][0]["id"]


def test_create_if_needed_spawn_inline_network_rolls_back_before_duplicate_vm(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    first = client.post(
        "/api/v1/playground/vms",
        json={"label": "web", "image_id": "img", "role": "target"},
    )
    assert first.status_code == 202

    res = client.post(
        "/api/v1/playground/vms",
        json={
            "label": "web",
            "image_id": "img",
            "role": "target",
            "new_networks": [
                {"name": "should-not-exist", "subnet_cidr": "10.42.2.0/24", "isolated": True},
            ],
        },
    )

    assert res.status_code == 409
    session = client.get("/api/v1/playground/sessions/current").json()
    assert [network["name"] for network in session["networks"]] == ["default"]


def test_create_if_needed_spawn_reuses_default_network(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)

    first = client.post(
        "/api/v1/playground/vms",
        json={"label": "one", "image_id": "img", "role": "target"},
    )
    second = client.post(
        "/api/v1/playground/vms",
        json={"label": "two", "image_id": "img", "role": "target"},
    )

    assert first.status_code == 202
    assert second.status_code == 202
    session = client.get("/api/v1/playground/sessions/current").json()
    assert len(session["vms"]) == 2
    assert len(session["networks"]) == 1
    assert {vm["label"] for vm in session["vms"]} == {"one", "two"}


def test_create_if_needed_spawn_rejects_incompatible_default_network(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    job_id = client.post("/api/v1/playground/sessions", json={}).json()["job_id"]
    network = client.post(
        f"/api/v1/playground/sessions/{job_id}/networks",
        json={"name": "default", "subnet_cidr": "10.42.2.0/24", "isolated": False},
    )
    assert network.status_code == 202

    res = client.post(
        "/api/v1/playground/vms",
        json={"label": "web", "image_id": "img", "role": "target"},
    )

    assert res.status_code == 409
    detail = res.json()["detail"].lower()
    assert "default" in detail
    assert "incompatible" in detail


def test_spawn_vm_returns_running_vm(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    job_id, net = _make_session_with_network(client)

    res = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "web-1", "image_id": "img", "nics": [{"network_id": net["id"]}]},
    )
    assert res.status_code == 202
    body = res.json()
    assert body["label"] == "web-1"
    assert body["state"] == "running"
    assert body["nics"][0]["ip"] == "10.42.1.10"


def test_spawn_existing_session_rejects_unavailable_image_without_registering_vm(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    from astinuslab.playground import orchestrator as po

    job_id, net = _make_session_with_network(client)
    missing_image = tmp_path / "missing.qcow2"
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda _image_id, *, role="target": {
            "id": "missing",
            "path": str(missing_image),
            "format": "qcow2",
            "architecture": "x86_64",
        },
    )

    def fail_delay(**kwargs):
        raise AssertionError("unavailable image must be rejected before spawn enqueue")

    monkeypatch.setattr(po.spawn_vm, "delay", fail_delay)

    res = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "web-1", "image_id": "missing", "nics": [{"network_id": net["id"]}]},
    )

    assert res.status_code == 400
    assert "image not available" in res.json()["detail"]
    current = client.get("/api/v1/playground/sessions/current").json()
    assert current["vms"] == []


def test_spawn_attacker_uses_attacker_catalog_image(client, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from astinuslab import catalog
    from astinuslab.playground import cloud_init_seed as cis
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    fake_lifecycle = MagicMock(get_ip=lambda *a, **kw: "10.42.1.20")
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)
    public_key = tmp_path / "id_ed25519.pub"
    public_key.write_text("ssh-ed25519 AAAATEST astinuslab-test\n", encoding="utf-8")
    autospec_setattr(monkeypatch, cis, "get_active_key_paths", lambda: (tmp_path / "id_ed25519", public_key))
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
            storage_dir=tmp_path / "storage",
        ),
    )
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    (images_dir / "kali.qcow2").write_bytes(b"placeholder image")
    monkeypatch.setattr(catalog, "settings", SimpleNamespace(images_dir=images_dir))
    autospec_setattr(monkeypatch, catalog, "list_playground_usable_images", lambda: [])

    autospec_setattr(
        monkeypatch,
        catalog,
        "load_attackers",
        lambda: [
            {
                "id": "kali",
                "display_name": "Kali",
                "image_ref": "kali.qcow2",
                "username": "kali",
                "image_available": True,
                "cloud_init": True,
            }
        ],
    )

    job_id, net = _make_session_with_network(client)
    res = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={
            "label": "atk",
            "image_id": "kali",
            "role": "attacker",
            "nics": [{"network_id": net["id"]}],
        },
    )

    assert res.status_code == 202
    body = res.json()
    assert body["role"] == "attacker"
    assert body["image_id"] == "kali"

    boot_kwargs = fake_lifecycle.boot_playground.call_args.kwargs
    assert boot_kwargs["seed_iso_path"] == tmp_path / "disks" / job_id / "atk-seed.iso"
    user_data = boot_kwargs["user_data_path"].read_text(encoding="utf-8")
    network_config = boot_kwargs["network_config_path"].read_text(encoding="utf-8")
    assert "name: kali" in user_data
    assert "ssh-ed25519 AAAATEST astinuslab-test" in user_data
    assert "dhcp4: true" in network_config


def test_spawn_vm_duplicate_label_returns_409(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    job_id, net = _make_session_with_network(client)
    payload = {"label": "x", "image_id": "img", "nics": [{"network_id": net["id"]}]}
    client.post(f"/api/v1/playground/sessions/{job_id}/vms", json=payload)
    res = client.post(f"/api/v1/playground/sessions/{job_id}/vms", json=payload)
    assert res.status_code == 409


def test_spawn_vm_unknown_network_returns_400(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    job_id, _ = _make_session_with_network(client)
    res = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "x", "image_id": "img", "nics": [{"network_id": "bogus"}]},
    )
    assert res.status_code == 400


def test_destroy_vm_returns_204(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    job_id, net = _make_session_with_network(client)
    vm = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "x", "image_id": "img", "nics": [{"network_id": net["id"]}]},
    ).json()
    res = client.delete(f"/api/v1/playground/vms/{vm['id']}")
    assert res.status_code == 202


def test_destroy_last_vm_keeps_playground_session(client, monkeypatch, tmp_path):
    """Operator contract: deleting the last VM must NOT destroy the session.
    Networks, layout, and any captures must remain. Session only terminates
    via explicit DELETE /sessions/{job_id}."""
    _network_helper, lifecycle = _common_monkeypatch(monkeypatch, tmp=tmp_path)
    vm = client.post(
        "/api/v1/playground/vms",
        json={"label": "solo", "image_id": "img", "role": "target"},
    ).json()

    res = client.delete(f"/api/v1/playground/vms/{vm['id']}")

    assert res.status_code == 202
    lifecycle.destroy.assert_called_once()
    session = client.get("/api/v1/playground/sessions/current")
    assert session.status_code == 200
    assert [v["label"] for v in session.json()["vms"] if v["state"] != "destroyed"] == []


def test_destroy_non_last_vm_keeps_playground_session(client, monkeypatch, tmp_path):
    _network_helper, lifecycle = _common_monkeypatch(monkeypatch, tmp=tmp_path)
    first = client.post(
        "/api/v1/playground/vms",
        json={"label": "one", "image_id": "img", "role": "target"},
    ).json()
    client.post(
        "/api/v1/playground/vms",
        json={"label": "two", "image_id": "img", "role": "target"},
    )

    res = client.delete(f"/api/v1/playground/vms/{first['id']}")

    assert res.status_code == 202
    lifecycle.destroy.assert_called_once()
    session = client.get("/api/v1/playground/sessions/current")
    assert session.status_code == 200
    assert [vm["label"] for vm in session.json()["vms"] if vm["state"] != "destroyed"] == ["two"]


def test_delete_network_rejects_cross_session_id(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    from astinuslab.db import SessionLocal
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground import repositories as pr

    def fail_destroy(**kwargs):
        pytest.fail("destroy_network must not be queued for another session's network")

    monkeypatch.setattr(po.destroy_network, "delay", fail_destroy)
    with SessionLocal() as s:
        pr.create_playground_session(s, job_id="pg-old")
        pr.register_network(
            s,
            network_id="net-old",
            job_id="pg-old",
            name="old",
            subnet_cidr="10.42.9.0/24",
            isolated=True,
            libvirt_net_name="astinuslab-pg-old-old",
        )
        pr.mark_session_completed(s, "pg-old")
        pr.create_playground_session(s, job_id="pg-active")
        s.commit()

    res = client.delete("/api/v1/playground/networks/net-old")

    assert res.status_code == 404
    with SessionLocal() as s:
        assert pr.list_session_networks(s, "pg-old")[0].id == "net-old"


def test_delete_vm_rejects_cross_session_id(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    from astinuslab.db import SessionLocal
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground import repositories as pr

    def fail_destroy(**kwargs):
        pytest.fail("destroy_vm must not be queued for another session's VM")

    monkeypatch.setattr(po.destroy_vm, "delay", fail_destroy)
    with SessionLocal() as s:
        pr.create_playground_session(s, job_id="pg-old")
        pr.register_vm(
            s,
            vm_id="vm-old",
            job_id="pg-old",
            label="old",
            image_id="img",
            domain_name="astinuslab-pg-old-old",
            disk_path=str(tmp_path / "old.qcow2"),
        )
        # Keep the historical VM row, with cleanup complete, so a new session
        # can start while this test still exercises cross-session rejection.
        pr.update_vm_state(s, "vm-old", "destroyed")
        pr.mark_session_completed(s, "pg-old")
        pr.create_playground_session(s, job_id="pg-active")
        s.commit()

    res = client.delete("/api/v1/playground/vms/vm-old")

    assert res.status_code == 404
    with SessionLocal() as s:
        assert pr.list_session_vms(s, "pg-old")[0].id == "vm-old"


def test_concurrent_spawns_are_allowed(client, monkeypatch, tmp_path):
    """Operator can queue multiple spawns without waiting for prior VMs
    to reach `running` (B20)."""
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    from astinuslab.db import SessionLocal
    from astinuslab.db.models import JobVM

    job_id, net = _make_session_with_network(client)

    first = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "one", "image_id": "img", "nics": [{"network_id": net["id"]}]},
    )
    assert first.status_code == 202

    with SessionLocal() as s:
        vm = s.query(JobVM).filter_by(id=first.json()["id"]).one()
        vm.state = "pending"
        s.commit()

    second = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "two", "image_id": "img", "nics": [{"network_id": net["id"]}]},
    )

    assert second.status_code == 202


def test_capture_memory_returns_acquisition_id(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    from astinuslab.playground import acquisition as pa

    autospec_setattr(
        monkeypatch, pa, "_dump_memory", lambda dom, out: {"path": str(out), "size": out.write_bytes(b"api-memory")}
    )

    job_id, net = _make_session_with_network(client)
    vm = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "x", "image_id": "img", "nics": [{"network_id": net["id"]}]},
    ).json()

    res = client.post(f"/api/v1/playground/vms/{vm['id']}/captures", json={"kind": "memory"})
    assert res.status_code == 202
    assert "acquisition_id" in res.json()


def test_capture_events_stream_returns_lifecycle_events(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    from astinuslab.playground import acquisition as pa

    autospec_setattr(
        monkeypatch, pa, "_dump_memory", lambda dom, out: {"path": str(out), "size": out.write_bytes(b"api-memory")}
    )

    job_id, net = _make_session_with_network(client)
    vm = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "x", "image_id": "img", "nics": [{"network_id": net["id"]}]},
    ).json()
    capture = client.post(f"/api/v1/playground/vms/{vm['id']}/captures", json={"kind": "memory"}).json()

    res = client.get(f"/api/v1/playground/captures/{capture['acquisition_id']}/events")

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    assert "event: queued" in res.text
    assert "event: running" in res.text
    assert "event: completed" in res.text


def test_capture_disk_returns_acquisition_id(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    from unittest.mock import MagicMock

    from astinuslab.playground import acquisition as pa

    autospec_setattr(
        monkeypatch,
        pa,
        "_disk_capture",
        lambda: MagicMock(capture=lambda **kw: kw["output_path"].write_bytes(b"api-disk")),
    )

    job_id, net = _make_session_with_network(client)
    vm = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "x", "image_id": "img", "nics": [{"network_id": net["id"]}]},
    ).json()

    res = client.post(f"/api/v1/playground/vms/{vm['id']}/captures", json={"kind": "disk"})
    assert res.status_code == 202


def test_list_captures_returns_acquisitions(client, monkeypatch, tmp_path):
    import hashlib

    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    from unittest.mock import MagicMock

    from astinuslab.playground import acquisition as pa

    memory_bytes = b"api-list-memory"
    disk_bytes = b"api-list-disk"
    autospec_setattr(
        monkeypatch, pa, "_dump_memory", lambda dom, out: {"path": str(out), "size": out.write_bytes(memory_bytes)}
    )
    autospec_setattr(
        monkeypatch,
        pa,
        "_disk_capture",
        lambda: MagicMock(capture=lambda **kw: kw["output_path"].write_bytes(disk_bytes)),
    )

    job_id, net = _make_session_with_network(client)
    vm = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "x", "image_id": "img", "nics": [{"network_id": net["id"]}]},
    ).json()
    client.post(f"/api/v1/playground/vms/{vm['id']}/captures", json={"kind": "memory"})
    client.post(f"/api/v1/playground/vms/{vm['id']}/captures", json={"kind": "disk"})

    res = client.get(f"/api/v1/playground/vms/{vm['id']}/captures")
    assert res.status_code == 200
    rows = res.json()
    assert len(rows) == 2
    by_kind = {row["kind"]: row for row in rows}
    for kind, marker in (("memory", "mem"), ("disk", "disk")):
        assert by_kind[kind]["name"] == f"{marker}-{by_kind[kind]['id']}"
    assert by_kind["memory"]["session_id"] == job_id
    assert by_kind["memory"]["vm_label"] == "x"
    assert by_kind["memory"]["path"].endswith(".elf")
    assert by_kind["memory"]["sha256"] == hashlib.sha256(memory_bytes).hexdigest()
    assert by_kind["memory"]["size_bytes"] == len(memory_bytes)
    assert by_kind["disk"]["session_id"] == job_id
    assert by_kind["disk"]["vm_label"] == "x"
    assert by_kind["disk"]["path"].endswith(".qcow2")
    assert by_kind["disk"]["sha256"] == hashlib.sha256(disk_bytes).hexdigest()
    assert by_kind["disk"]["size_bytes"] == len(disk_bytes)


def test_list_playground_usable_images_endpoint(client, monkeypatch):
    from astinuslab import catalog

    autospec_setattr(
        monkeypatch,
        catalog,
        "list_playground_usable_images",
        lambda: [
            {"id": "img-a", "architecture": "x86_64", "format": "qcow2"},
            {"id": "img-b", "architecture": "x86_64", "format": "qcow2"},
        ],
    )
    res = client.get("/api/v1/playground/catalog/images")
    assert res.status_code == 200
    ids = {img["id"] for img in res.json()}
    assert ids == {"img-a", "img-b"}


def test_list_playground_images_endpoint_defaults_to_target(client, monkeypatch):
    from astinuslab import catalog

    autospec_setattr(
        monkeypatch,
        catalog,
        "list_playground_usable_images",
        lambda: [
            {
                "id": "ubuntu",
                "path": "/images/ubuntu.qcow2",
                "format": "qcow2",
                "architecture": "ubuntu",
                "image_ref": "ubuntu.qcow2",
                "display_name": "Ubuntu 22.04",
            }
        ],
    )

    res = client.get("/api/v1/playground/catalog/images")

    assert res.status_code == 200
    body = res.json()
    assert body == [
        {
            "id": "ubuntu",
            "label": "Ubuntu 22.04",
            "role": "target",
            "image_ref": "ubuntu.qcow2",
            "path": "/images/ubuntu.qcow2",
            "format": "qcow2",
            "architecture": "ubuntu",
            "username": "",
            "distro": "unknown",
            "version": "unknown",
            "family": "generic",
            "disk_gb": None,
        }
    ]


def test_list_playground_images_endpoint_filters_attackers(client, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    from astinuslab import catalog

    monkeypatch.setattr(catalog, "settings", SimpleNamespace(images_dir=Path("images")))
    autospec_setattr(
        monkeypatch,
        catalog,
        "load_attackers",
        lambda: [
            {
                "id": "kali",
                "display_name": "Kali Linux",
                "image_ref": "kali.qcow2",
                "username": "kali",
                "os_variant": "generic",
                "image_available": True,
            },
            {
                "id": "parrot",
                "display_name": "Parrot OS",
                "image_ref": "parrot.qcow2",
                "username": "user",
                "os_variant": "generic",
                "image_available": False,
            },
            {
                "id": "default-included",
                "display_name": "Default Included",
                "image_ref": "default-included.qcow2",
                "username": "analyst",
                "os_variant": "generic",
            },
        ],
    )

    res = client.get("/api/v1/playground/catalog/images?role=attacker")

    assert res.status_code == 200
    body = res.json()
    assert body == [
        {
            "id": "kali",
            "label": "Kali Linux",
            "role": "attacker",
            "image_ref": "kali.qcow2",
            "path": "images/kali.qcow2",
            "format": "qcow2",
            "architecture": "x86_64",
            "username": "kali",
            "cloud_init": False,
        },
        {
            "id": "default-included",
            "label": "Default Included",
            "role": "attacker",
            "image_ref": "default-included.qcow2",
            "path": "images/default-included.qcow2",
            "format": "qcow2",
            "architecture": "x86_64",
            "username": "analyst",
            "cloud_init": False,
        },
    ]


def test_list_playground_images_endpoint_filters_network_nodes(client, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    from astinuslab import catalog

    monkeypatch.setattr(catalog, "settings", SimpleNamespace(images_dir=Path("images")))
    autospec_setattr(
        monkeypatch,
        catalog,
        "load_network_nodes",
        lambda: [
            {
                "id": "openwrt",
                "display_name": "OpenWrt Network Node",
                "image_ref": "openwrt.qcow2",
                "username": "root",
                "os_variant": "generic",
                "cloud_init": False,
                "function_tags": ["firewall", "router"],
                "defaults": {"vcpus": 1, "memory_mb": 512, "disk_gb": 4},
                "image_available": True,
            },
            {
                "id": "missing",
                "display_name": "Missing Node",
                "image_ref": "missing.qcow2",
                "image_available": False,
            },
        ],
    )

    res = client.get("/api/v1/playground/catalog/images?role=network")

    assert res.status_code == 200
    assert res.json() == [
        {
            "id": "openwrt",
            "label": "OpenWrt Network Node",
            "role": "network",
            "image_ref": "openwrt.qcow2",
            "path": "images/openwrt.qcow2",
            "format": "qcow2",
            "architecture": "x86_64",
            "username": "root",
            "os_variant": "generic",
            "cloud_init": False,
            "function_tags": ["firewall", "router"],
            "defaults": {"vcpus": 1, "memory_mb": 512, "disk_gb": 4},
            "disk_gb": 4,
        }
    ]


def test_list_playground_images_endpoint_rejects_unknown_role(client):
    res = client.get("/api/v1/playground/catalog/images?role=sensor")

    assert res.status_code == 400
    assert "role" in res.json()["detail"].lower()


def test_list_playground_images_for_role_rejects_unknown_role():
    from astinuslab import catalog

    with pytest.raises(ValueError):
        catalog.list_playground_images_for_role("sensor")


def test_create_network_duplicate_name_returns_409(client, monkeypatch):
    from unittest.mock import MagicMock

    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: MagicMock())

    job_id = client.post("/api/v1/playground/sessions", json={}).json()["job_id"]
    client.post(
        f"/api/v1/playground/sessions/{job_id}/networks",
        json={"name": "ab", "subnet_cidr": "10.42.1.0/24", "isolated": True},
    )
    res = client.post(
        f"/api/v1/playground/sessions/{job_id}/networks",
        json={"name": "ab", "subnet_cidr": "10.42.2.0/24", "isolated": True},
    )
    assert res.status_code == 409


def test_delete_network_with_attached_vm_returns_409(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    job_id, net = _make_session_with_network(client)
    # spawn a VM attached to the network
    client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "x", "image_id": "img", "nics": [{"network_id": net["id"]}]},
    )
    res = client.delete(f"/api/v1/playground/networks/{net['id']}")
    assert res.status_code == 409
    assert "attached" in res.json()["detail"].lower()


def test_capture_non_running_vm_returns_409(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    job_id, net = _make_session_with_network(client)
    vm = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "x", "image_id": "img", "nics": [{"network_id": net["id"]}]},
    ).json()
    # Force VM into a non-running state
    from astinuslab.db import SessionLocal
    from astinuslab.playground import repositories as pr

    with SessionLocal() as s:
        pr.update_vm_state(s, vm["id"], "failed")
        s.commit()

    res = client.post(f"/api/v1/playground/vms/{vm['id']}/captures", json={"kind": "memory"})
    assert res.status_code == 409
    assert "not running" in res.json()["detail"].lower()


def test_vm_serializer_emits_action_flags_for_running_vm(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    job_id, net = _make_session_with_network(client)
    vm = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "x", "image_id": "img", "nics": [{"network_id": net["id"]}]},
    ).json()

    session = client.get("/api/v1/playground/sessions/current").json()
    vm_row = next(v for v in session["vms"] if v["id"] == vm["id"])
    assert vm_row["can_capture_memory"] is True
    assert vm_row["can_capture_disk"] is True
    assert vm_row["can_destroy"] is True


def test_vm_serializer_disables_capture_for_network_nodes(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    job_id, net = _make_session_with_network(client)
    vm = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "edge", "image_id": "img", "role": "network", "nics": [{"network_id": net["id"]}]},
    ).json()

    session = client.get("/api/v1/playground/sessions/current").json()
    vm_row = next(v for v in session["vms"] if v["id"] == vm["id"])
    assert vm_row["can_capture_memory"] is False
    assert vm_row["can_capture_disk"] is False
    assert vm_row["can_destroy"] is True
    assert vm_row["action_block_reason"] == "capture disabled for Network Nodes"


def test_capture_network_node_returns_409(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    job_id, net = _make_session_with_network(client)
    vm = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "edge", "image_id": "img", "role": "network", "nics": [{"network_id": net["id"]}]},
    ).json()

    res = client.post(f"/api/v1/playground/vms/{vm['id']}/captures", json={"kind": "memory"})

    assert res.status_code == 409
    assert res.json()["detail"] == "capture disabled for Network Nodes"


def test_vm_serializer_includes_nic_network_status_fields(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    job_id, net = _make_session_with_network(client)
    vm = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "x", "image_id": "img", "nics": [{"network_id": net["id"]}]},
    ).json()

    from astinuslab.db import SessionLocal
    from astinuslab.db.models import JobVMNic
    from astinuslab.playground import repositories as pr

    with SessionLocal() as s:
        nic = s.query(JobVMNic).filter_by(job_vm_id=vm["id"]).one()
        nic.ip = None
        pr.update_vm_state(s, vm["id"], "running", error=f"IP lookup failed for {nic.id}: DHCP lease timed out")
        s.commit()

    session = client.get("/api/v1/playground/sessions/current").json()
    vm_row = next(v for v in session["vms"] if v["id"] == vm["id"])
    nic_row = vm_row["nics"][0]
    assert nic_row["network_id"] == net["id"]
    assert nic_row["network_name"] == net["name"]
    assert nic_row["network_state"] == "active"
    assert nic_row["state"] == "active"
    assert "DHCP lease timed out" in nic_row["diagnostic"]


def test_vm_serializer_includes_vm_error_summary(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    job_id, net = _make_session_with_network(client)
    vm = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "x", "image_id": "img", "nics": [{"network_id": net["id"]}]},
    ).json()

    from astinuslab.db import SessionLocal
    from astinuslab.playground import repositories as pr

    with SessionLocal() as s:
        pr.update_vm_state(s, vm["id"], "running", error="IP lookup failed: DHCP lease timed out")
        s.commit()

    session = client.get("/api/v1/playground/sessions/current").json()
    vm_row = next(v for v in session["vms"] if v["id"] == vm["id"])
    assert vm_row["error_summary"] == "IP lookup failed: DHCP lease timed out"


def test_vm_serializer_blocks_capture_when_not_running(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    job_id, net = _make_session_with_network(client)
    vm = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "x", "image_id": "img", "nics": [{"network_id": net["id"]}]},
    ).json()

    from astinuslab.db import SessionLocal
    from astinuslab.playground import repositories as pr

    with SessionLocal() as s:
        pr.update_vm_state(s, vm["id"], "pending")
        s.commit()

    session = client.get("/api/v1/playground/sessions/current").json()
    vm_row = next(v for v in session["vms"] if v["id"] == vm["id"])
    assert vm_row["can_capture_memory"] is False
    assert vm_row["can_capture_disk"] is False
    assert vm_row["action_block_reason"]


def test_network_serializer_blocks_delete_when_attached_vms_exist(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    job_id, net = _make_session_with_network(client)
    client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "x", "image_id": "img", "nics": [{"network_id": net["id"]}]},
    )

    session = client.get("/api/v1/playground/sessions/current").json()
    net_row = next(n for n in session["networks"] if n["id"] == net["id"])
    assert net_row["can_delete"] is False
    assert net_row["attached_vm_count"] >= 1
    assert "attached" in (net_row["action_block_reason"] or "").lower()


def test_network_serializer_can_delete_when_no_vms_attached(client, monkeypatch):
    from unittest.mock import MagicMock

    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: MagicMock())

    job_id = client.post("/api/v1/playground/sessions", json={}).json()["job_id"]
    net = client.post(
        f"/api/v1/playground/sessions/{job_id}/networks",
        json={"name": "ab", "subnet_cidr": "10.42.1.0/24", "isolated": True},
    ).json()

    session = client.get("/api/v1/playground/sessions/current").json()
    net_row = next(n for n in session["networks"] if n["id"] == net["id"])
    assert net_row["can_delete"] is True
    assert net_row["attached_vm_count"] == 0


def test_capture_on_vm_from_inactive_session_returns_404(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    from astinuslab.db import SessionLocal
    from astinuslab.playground import repositories as pr

    job_id, net = _make_session_with_network(client)
    vm = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "x", "image_id": "img", "nics": [{"network_id": net["id"]}]},
    ).json()

    with SessionLocal() as s:
        pr.mark_session_completed(s, job_id)
        s.commit()

    res = client.post(f"/api/v1/playground/vms/{vm['id']}/captures", json={"kind": "memory"})
    assert res.status_code == 404


def test_metrics_on_vm_from_inactive_session_returns_404(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    from astinuslab.db import SessionLocal
    from astinuslab.playground import repositories as pr

    job_id, net = _make_session_with_network(client)
    vm = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "x", "image_id": "img", "nics": [{"network_id": net["id"]}]},
    ).json()

    with SessionLocal() as s:
        pr.mark_session_completed(s, job_id)
        s.commit()

    res = client.get(f"/api/v1/playground/vms/{vm['id']}/metrics")
    assert res.status_code == 404


def test_spawn_vm_broker_failure_returns_503_and_marks_vm_failed(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    from astinuslab.db import SessionLocal
    from astinuslab.db.models import JobVM
    from astinuslab.playground import orchestrator as po

    job_id, net = _make_session_with_network(client)

    def broker_down(**kwargs):
        raise RuntimeError("broker down")

    monkeypatch.setattr(po.spawn_vm, "delay", broker_down)

    res = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "broken", "image_id": "img", "nics": [{"network_id": net["id"]}]},
    )

    assert res.status_code == 503
    assert "queue" in res.json()["detail"].lower()
    with SessionLocal() as s:
        vms = s.query(JobVM).filter_by(session_id=job_id, label="broken").all()
        assert len(vms) == 1
        assert vms[0].state == "failed"


def test_destroy_vm_broker_failure_returns_503_and_restores_state(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    from astinuslab.db import SessionLocal
    from astinuslab.db.models import JobVM
    from astinuslab.playground import orchestrator as po

    job_id, net = _make_session_with_network(client)
    vm = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "victim", "image_id": "img", "nics": [{"network_id": net["id"]}]},
    ).json()

    with SessionLocal() as s:
        prior_state = s.query(JobVM).filter_by(id=vm["id"]).one().state

    def broker_down(**kwargs):
        raise RuntimeError("broker down")

    monkeypatch.setattr(po.destroy_vm, "delay", broker_down)

    res = client.delete(f"/api/v1/playground/vms/{vm['id']}")
    assert res.status_code == 503
    with SessionLocal() as s:
        assert s.query(JobVM).filter_by(id=vm["id"]).one().state == prior_state


def test_destroy_network_broker_failure_returns_503_and_restores_state(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    from astinuslab.db import SessionLocal
    from astinuslab.db.models import JobNetwork
    from astinuslab.playground import orchestrator as po

    job_id, net = _make_session_with_network(client)
    with SessionLocal() as session:
        network = session.get(JobNetwork, net["id"])
        assert network is not None
        prior_state = network.state

    def broker_down(**kwargs):
        raise RuntimeError("broker down")

    monkeypatch.setattr(po.destroy_network, "delay", broker_down)

    res = client.delete(f"/api/v1/playground/networks/{net['id']}")
    assert res.status_code == 503
    with SessionLocal() as session:
        network = session.get(JobNetwork, net["id"])
        assert network is not None
        assert network.state == prior_state
        assert "queue" in (network.error_summary or "").lower()


def test_capture_broker_failure_returns_503_and_marks_acquisition_failed(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    from astinuslab.db import SessionLocal
    from astinuslab.db.models import Acquisition
    from astinuslab.playground import orchestrator as po

    job_id, net = _make_session_with_network(client)
    vm = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "v", "image_id": "img", "nics": [{"network_id": net["id"]}]},
    ).json()

    def broker_down(**kwargs):
        raise RuntimeError("broker down")

    monkeypatch.setattr(po.capture_memory, "delay", broker_down)

    res = client.post(f"/api/v1/playground/vms/{vm['id']}/captures", json={"kind": "memory"})
    assert res.status_code == 503
    with SessionLocal() as s:
        acqs = s.query(Acquisition).filter_by(job_vm_id=vm["id"]).all()
        assert len(acqs) == 1
        assert acqs[0].status == "failed"


def test_attach_nic_broker_failure_returns_503(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    from astinuslab.playground import orchestrator as po

    job_id, net = _make_session_with_network(client)
    vm = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "v", "image_id": "img", "nics": [{"network_id": net["id"]}]},
    ).json()

    def broker_down(**kwargs):
        raise RuntimeError("broker down")

    monkeypatch.setattr(po.attach_nic, "delay", broker_down)

    res = client.post(
        f"/api/v1/playground/sessions/current/vms/{vm['id']}/nics",
        json={"network_id": net["id"]},
    )
    assert res.status_code == 503


def test_detach_nic_broker_failure_returns_503(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    from astinuslab.playground import orchestrator as po

    job_id, net = _make_session_with_network(client)
    vm = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "v", "image_id": "img", "nics": [{"network_id": net["id"]}]},
    ).json()
    nic_id = vm["nics"][0]["id"]

    def broker_down(**kwargs):
        raise RuntimeError("broker down")

    monkeypatch.setattr(po.detach_nic, "delay", broker_down)

    res = client.delete(f"/api/v1/playground/sessions/current/vms/{vm['id']}/nics/{nic_id}")
    assert res.status_code == 503


def test_attach_and_detach_nic_endpoints(client, monkeypatch, tmp_path):
    # Verify endpoints return 404 if session doesn't exist
    res1 = client.post("/api/v1/playground/sessions/current/vms/vm-123/nics", json={"network_id": "net-123"})
    assert res1.status_code == 404

    res2 = client.delete("/api/v1/playground/sessions/current/vms/vm-123/nics/nic-123")
    assert res2.status_code == 404

    # Now verify happy path and 409 error paths with an active session
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    job_id, net = _make_session_with_network(client)

    net2 = client.post(
        f"/api/v1/playground/sessions/{job_id}/networks",
        json={"name": "ab2", "subnet_cidr": "10.42.2.0/24", "isolated": True},
    ).json()

    vm = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "x", "image_id": "img", "nics": [{"network_id": net["id"]}]},
    ).json()

    from unittest.mock import MagicMock

    from astinuslab.playground import orchestrator as po

    mock_attach = MagicMock()
    mock_detach = MagicMock()

    get_calls = []

    class MockResult:
        id = "task-1"

        def get(self, timeout=None):
            get_calls.append(timeout)
            raise AssertionError("playground API must not wait for Celery result")

    mock_attach.return_value = MockResult()
    mock_detach.return_value = MockResult()

    monkeypatch.setattr(po.attach_nic, "delay", mock_attach)
    monkeypatch.setattr(po.detach_nic, "delay", mock_detach)

    # 1. Happy path: Attach NIC is accepted without waiting for the task result.
    res_attach = client.post(
        f"/api/v1/playground/sessions/current/vms/{vm['id']}/nics", json={"network_id": net2["id"]}
    )
    assert res_attach.status_code == 202
    mock_attach.assert_called_once_with(job_id=job_id, vm_id=vm["id"], network_id=net2["id"], nic_id=None)
    assert res_attach.json()["resource_id"] == vm["id"]

    # 2. Happy path: Detach NIC is accepted without waiting for the task result.
    nic_id = vm["nics"][0]["id"]
    res_detach = client.delete(f"/api/v1/playground/sessions/current/vms/{vm['id']}/nics/{nic_id}")
    assert res_detach.status_code == 202
    mock_detach.assert_called_once()
    detach_kwargs = mock_detach.call_args.kwargs
    assert detach_kwargs["job_id"] == job_id
    assert detach_kwargs["vm_id"] == vm["id"]
    assert detach_kwargs["nic_id"] == nic_id
    assert isinstance(detach_kwargs["detach_token"], str) and detach_kwargs["detach_token"]
    assert res_detach.json()["resource_id"] == nic_id
    assert get_calls == []


def test_suggest_cidr_without_job_returns_first_slot(client):
    res = client.get("/api/v1/playground/networks/suggest-cidr")
    assert res.status_code == 200
    assert res.json()["subnet_cidr"] == "10.42.1.0/24"


def test_suggest_cidr_with_job_skips_existing_networks(client, monkeypatch):
    from unittest.mock import MagicMock

    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: MagicMock())

    job_id = client.post("/api/v1/playground/sessions", json={}).json()["job_id"]
    client.post(
        f"/api/v1/playground/sessions/{job_id}/networks",
        json={"name": "ab", "subnet_cidr": "10.42.1.0/24", "isolated": True},
    )
    res = client.get(f"/api/v1/playground/networks/suggest-cidr?job_id={job_id}")
    assert res.status_code == 200
    assert res.json()["subnet_cidr"] == "10.42.2.0/24"


def test_suggest_cidr_excludes_extra_cidrs(client):
    res = client.get("/api/v1/playground/networks/suggest-cidr?extra=10.42.1.0/24&extra=10.42.2.0/24")
    assert res.status_code == 200
    assert res.json()["subnet_cidr"] == "10.42.3.0/24"


def test_suggest_cidr_returns_409_when_pool_exhausted(client, monkeypatch):
    from astinuslab.playground import repositories as pr

    def _exhausted(_taken):
        raise RuntimeError("playground CIDR pool exhausted")

    monkeypatch.setattr(pr, "next_free_cidr", _exhausted)
    res = client.get("/api/v1/playground/networks/suggest-cidr")
    assert res.status_code == 409
    assert "exhausted" in res.json()["detail"].lower()


def test_export_current_recipe_maps_ids_to_recipe_names(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    job_id, net = _make_session_with_network(client)
    vm = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "web", "image_id": "img", "role": "target", "nics": [{"network_id": net["id"]}]},
    ).json()
    client.patch(
        f"/api/v1/playground/sessions/current/topology-layout/{vm['id']}",
        json={"node_id": vm["id"], "node_type": "vm", "x": 10, "y": 20},
    )
    client.patch(
        f"/api/v1/playground/sessions/current/topology-layout/{net['id']}",
        json={"node_id": net["id"], "node_type": "network", "x": 30, "y": 40},
    )

    res = client.get("/api/v1/playground/sessions/current/recipe")

    assert res.status_code == 200
    body = res.json()
    assert body["schema_version"] == 1
    assert body["networks"] == [{"name": "ab", "subnet_cidr": "10.42.1.0/24", "isolated": True}]
    assert body["vms"] == [
        {
            "label": "web",
            "role": "target",
            "image_id": "img",
            "workload_id": None,
            "vcpus": 2,
            "memory_mb": 2048,
            "disk_gb": None,
            "networks": ["ab"],
        }
    ]
    assert {node["node_id"] for node in body["topology"]} == {"web", "ab"}


def test_export_recipe_preserves_effective_vm_resources(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    job_id, net = _make_session_with_network(client)
    client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={
            "label": "sized",
            "image_id": "img",
            "role": "target",
            "vcpus": 2,
            "memory_mb": 1536,
            "disk_gb": 24,
            "nics": [{"network_id": net["id"]}],
        },
    )

    res = client.get("/api/v1/playground/sessions/current/recipe")

    assert res.status_code == 200
    assert res.json()["vms"][0]["vcpus"] == 2
    assert res.json()["vms"][0]["memory_mb"] == 1536
    assert res.json()["vms"][0]["disk_gb"] == 24


def test_spawn_vm_persists_explicit_resources_before_enqueue(client, monkeypatch, tmp_path):
    from types import SimpleNamespace

    from astinuslab.db import SessionLocal
    from astinuslab.db.models import JobVM
    from astinuslab.playground import orchestrator as po

    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    _job_id, net = _make_session_with_network(client)
    captured = []

    def capture_spawn(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(id="deferred")

    monkeypatch.setattr(po.spawn_vm, "delay", capture_spawn)

    res = client.post(
        "/api/v1/playground/vms",
        json={
            "label": "deferred",
            "image_id": "img",
            "vcpus": 2,
            "memory_mb": 1536,
            "disk_gb": 24,
            "network_ids": [net["id"]],
        },
    )

    assert res.status_code == 202
    assert (res.json()["vcpus"], res.json()["memory_mb"], res.json()["disk_gb"]) == (2, 1536, 24)
    recipe = client.get("/api/v1/playground/sessions/current/recipe").json()
    assert (recipe["vms"][0]["vcpus"], recipe["vms"][0]["memory_mb"], recipe["vms"][0]["disk_gb"]) == (2, 1536, 24)
    with SessionLocal() as session:
        vm = session.query(JobVM).filter_by(label="deferred").one()
        assert (vm.vcpus, vm.memory_mb, vm.disk_gb) == (2, 1536, 24)
    assert (captured[0]["vcpus"], captured[0]["memory_mb"], captured[0]["disk_gb"]) == (2, 1536, 24)


def test_spawn_vm_persists_defaults_before_enqueue(client, monkeypatch, tmp_path):
    from types import SimpleNamespace

    from astinuslab.db import SessionLocal
    from astinuslab.db.models import JobVM
    from astinuslab.playground import orchestrator as po

    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    image_path = tmp_path / "default.qcow2"
    image_path.write_bytes(b"placeholder image")
    monkeypatch.setattr(
        po,
        "lookup_playground_image",
        lambda _image_id, *, role="target": {"id": "img", "path": str(image_path), "disk_gb": 24},
    )
    job_id, net = _make_session_with_network(client)
    captured = []

    def capture_spawn(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(id="deferred")

    monkeypatch.setattr(po.spawn_vm, "delay", capture_spawn)

    res = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "defaults", "image_id": "img", "nics": [{"network_id": net["id"]}]},
    )

    assert res.status_code == 202
    assert (res.json()["vcpus"], res.json()["memory_mb"], res.json()["disk_gb"]) == (2, 2048, 24)
    recipe = client.get("/api/v1/playground/sessions/current/recipe").json()
    assert (recipe["vms"][0]["vcpus"], recipe["vms"][0]["memory_mb"], recipe["vms"][0]["disk_gb"]) == (2, 2048, 24)
    with SessionLocal() as session:
        vm = session.query(JobVM).filter_by(id=res.json()["id"]).one()
        assert (vm.vcpus, vm.memory_mb, vm.disk_gb) == (2, 2048, 24)
    assert (captured[0]["vcpus"], captured[0]["memory_mb"], captured[0]["disk_gb"]) == (2, 2048, 24)


def test_spawn_vm_freezes_none_disk_for_delayed_worker(client, monkeypatch, tmp_path):
    from types import SimpleNamespace

    from astinuslab.db import SessionLocal
    from astinuslab.db.models import JobVM
    from astinuslab.playground import orchestrator as po

    _, lifecycle = _common_monkeypatch(monkeypatch, tmp=tmp_path)
    job_id, net = _make_session_with_network(client)
    captured = []

    def capture_spawn(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(id="deferred")

    monkeypatch.setattr(po.spawn_vm, "delay", capture_spawn)

    res = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "race", "image_id": "img", "nics": [{"network_id": net["id"]}]},
    )
    assert res.status_code == 202
    assert captured[0]["disk_gb"] is None

    base_image = tmp_path / "base.qcow2"
    monkeypatch.setattr(
        po,
        "lookup_playground_image",
        lambda _image_id, *, role="target": {
            "id": "img",
            "path": str(base_image),
            "format": "qcow2",
            "architecture": "x86_64",
            "disk_gb": 24,
        },
    )
    po.spawn_vm(**captured[0])

    assert captured[0]["resources_resolved"] is True
    with SessionLocal() as session:
        vm = session.query(JobVM).filter_by(id=res.json()["id"]).one()
        assert (vm.vcpus, vm.memory_mb, vm.disk_gb) == (2, 2048, None)
    boot = lifecycle.boot_playground
    assert boot.call_args.kwargs["disk_gb"] is None


@pytest.mark.parametrize("invalid_default", ["settings_vcpus", "image_disk"])
def test_spawn_vm_rejects_invalid_effective_defaults_before_enqueue(client, monkeypatch, tmp_path, invalid_default):
    from types import SimpleNamespace

    from astinuslab.db import SessionLocal
    from astinuslab.db.models import JobVM, JobVMNic
    from astinuslab.playground import orchestrator as po

    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    job_id, net = _make_session_with_network(client)
    if invalid_default == "settings_vcpus":
        po.settings.playground_default_vcpus = 0
    else:
        image_path = tmp_path / "bad-default.qcow2"
        image_path.write_bytes(b"placeholder image")
        monkeypatch.setattr(
            po,
            "lookup_playground_image",
            lambda _image_id, *, role="target": {"id": "img", "path": str(image_path), "disk_gb": 4096},
        )
    captured = []

    def capture_spawn(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(id="deferred")

    monkeypatch.setattr(po.spawn_vm, "delay", capture_spawn)

    res = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "invalid", "image_id": "img", "nics": [{"network_id": net["id"]}]},
    )

    assert res.status_code == 500
    assert res.json()["detail"] == "invalid configured VM resources"
    with SessionLocal() as session:
        assert session.query(JobVM).filter_by(session_id=job_id).count() == 0
        assert session.query(JobVMNic).count() == 0
    assert captured == []


def test_import_recipe_creates_session_resources_and_topology(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)

    res = client.post(
        "/api/v1/playground/recipes/import",
        json={
            "schema_version": 1,
            "name": "dvwa-lab",
            "networks": [{"name": "lab", "subnet_cidr": "10.42.1.0/24", "isolated": True}],
            "vms": [{"label": "web", "role": "target", "image_id": "img", "networks": ["lab"]}],
            "topology": [
                {"node_id": "lab", "node_type": "network", "x": 320, "y": 66},
                {"node_id": "web", "node_type": "vm", "x": 40, "y": 54},
            ],
        },
    )

    assert res.status_code == 202
    body = res.json()
    assert [n["name"] for n in body["networks"]] == ["lab"]
    assert [v["label"] for v in body["vms"]] == ["web"]
    topology = client.get("/api/v1/playground/sessions/current/topology-layout").json()["nodes"]
    assert len(topology) == 2
    assert {node["node_type"] for node in topology} == {"network", "vm"}


def test_import_recipe_passes_machine_resource_overrides_to_spawn(client, monkeypatch, tmp_path):
    from types import SimpleNamespace

    from astinuslab.playground import orchestrator as po

    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    spawn_calls = []

    def fake_spawn_delay(**kwargs):
        spawn_calls.append(kwargs)
        return SimpleNamespace(id="fake-spawn")

    monkeypatch.setattr(po.spawn_vm, "delay", fake_spawn_delay)

    res = client.post(
        "/api/v1/playground/recipes/import",
        json={
            "schema_version": 1,
            "name": "sized-lab",
            "networks": [{"name": "lab", "subnet_cidr": "10.42.1.0/24", "isolated": True}],
            "vms": [
                {
                    "label": "web",
                    "role": "target",
                    "image_id": "img",
                    "networks": ["lab"],
                    "vcpus": 4,
                    "memory_mb": 8192,
                    "disk_gb": 80,
                }
            ],
        },
    )

    assert res.status_code == 202
    assert spawn_calls[0]["vcpus"] == 4
    assert spawn_calls[0]["memory_mb"] == 8192
    assert spawn_calls[0]["disk_gb"] == 80


def test_scenario_package_export_current_returns_zip(client, monkeypatch, tmp_path):
    import yaml

    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    job_id, net = _make_session_with_network(client)
    client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "web", "image_id": "img", "role": "target", "nics": [{"network_id": net["id"]}]},
    )

    res = client.get("/api/v1/scenario-packages/playground/current")

    assert res.status_code == 200
    assert res.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(res.content)) as archive:
        assert {"metadata.yaml", "scenario.yaml"}.issubset(archive.namelist())
        metadata = yaml.safe_load(archive.read("metadata.yaml"))
        recipe = yaml.safe_load(archive.read("scenario.yaml"))
    assert metadata["schema_version"] == 1
    assert metadata["package_type"] == "astinuslab.playground.scenario"
    assert recipe["vms"][0]["label"] == "web"


def test_scenario_package_import_applies_resource_overrides(client, monkeypatch, tmp_path):
    from types import SimpleNamespace

    from astinuslab import catalog
    from astinuslab.playground import orchestrator as po

    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    package = catalog.build_scenario_package(
        {
            "schema_version": 1,
            "name": "sized-lab",
            "networks": [{"name": "lab", "subnet_cidr": "10.42.1.0/24", "isolated": True}],
            "vms": [
                {
                    "label": "web",
                    "role": "target",
                    "image_id": "img",
                    "networks": ["lab"],
                    "vcpus": 1,
                    "memory_mb": 512,
                    "disk_gb": 20,
                }
            ],
            "topology": [],
        },
        metadata={"name": "Sized Lab"},
    )
    spawn_calls = []

    def fake_spawn_delay(**kwargs):
        spawn_calls.append(kwargs)
        return SimpleNamespace(id="fake-spawn")

    monkeypatch.setattr(po.spawn_vm, "delay", fake_spawn_delay)

    res = client.post(
        "/api/v1/scenario-packages/import",
        files={"file": ("sized-lab.zip", package, "application/zip")},
        data={"resource_overrides": json.dumps({"web": {"vcpus": 4, "memory_mb": 8192, "disk_gb": 80}})},
    )

    assert res.status_code == 202
    assert [v["label"] for v in res.json()["vms"]] == ["web"]
    assert spawn_calls[0]["vcpus"] == 4
    assert spawn_calls[0]["memory_mb"] == 8192
    assert spawn_calls[0]["disk_gb"] == 80


def test_scenario_package_import_rejects_malformed_zip(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)

    res = client.post(
        "/api/v1/scenario-packages/import",
        files={"file": ("bad.zip", b"not a zip", "application/zip")},
    )

    assert res.status_code == 400


def test_scenario_package_import_rejects_oversized_upload_before_parsing(client, monkeypatch, tmp_path):
    from astinuslab import catalog

    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    monkeypatch.setattr(catalog, "PACKAGE_MAX_UPLOAD_BYTES", 10)

    res = client.post(
        "/api/v1/scenario-packages/import",
        files={"file": ("large.zip", b"x" * 11, "application/zip")},
    )

    assert res.status_code == 413
    assert "too large" in res.json()["detail"]


def test_scenario_package_import_rejects_invalid_resource_overrides(client, monkeypatch, tmp_path):
    from astinuslab import catalog

    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    package = catalog.build_scenario_package(
        {
            "schema_version": 1,
            "name": "sized-lab",
            "networks": [],
            "vms": [{"label": "web", "image_id": "img", "networks": []}],
        },
        metadata={"name": "Sized Lab"},
    )

    res = client.post(
        "/api/v1/scenario-packages/import",
        files={"file": ("sized-lab.zip", package, "application/zip")},
        data={"resource_overrides": json.dumps({"missing": {"vcpus": 4}})},
    )

    assert res.status_code == 400
    assert "unknown VM" in res.json()["detail"]


def test_import_recipe_rejects_unknown_network_reference(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)

    res = client.post(
        "/api/v1/playground/recipes/import",
        json={
            "schema_version": 1,
            "networks": [{"name": "lab", "subnet_cidr": "10.42.1.0/24"}],
            "vms": [{"label": "web", "image_id": "img", "networks": ["missing"]}],
        },
    )

    assert res.status_code == 400
    assert "unknown network" in res.json()["detail"]


def test_vm_lifecycle_action_runs_virsh_and_updates_state(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))

    from astinuslab.db import session_scope
    from astinuslab.db.models import JobVM
    from astinuslab.playground.routes import vm_actions as vm_actions_routes

    monkeypatch.setattr(vm_actions_routes.subprocess, "run", fake_run)
    job_id, net = _make_session_with_network(client)
    vm = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "web", "image_id": "img", "nics": [{"network_id": net["id"]}]},
    ).json()
    with session_scope() as s:
        row = s.query(JobVM).filter_by(id=vm["id"]).one()
        row.state = "running"

    res = client.post(f"/api/v1/playground/vms/{vm['id']}/actions", json={"action": "reboot"})

    assert res.status_code == 202
    assert calls[0][0] == ["virsh", "-c", "qemu:///system", "reboot", vm["domain_name"]]
    with session_scope() as s:
        assert s.query(JobVM).filter_by(id=vm["id"]).one().state == "booting"


def _register_power_action_vm(client, *, state="running"):
    from astinuslab import db as db_mod
    from astinuslab.playground import repositories as pr

    job_id = client.post("/api/v1/playground/sessions").json()["session_id"]
    with db_mod.session_scope() as session:
        pr.register_vm(
            session,
            vm_id="vm-power-race",
            job_id=job_id,
            label="web",
            image_id="img",
            domain_name="astinuslab-pg-power-race-web",
            disk_path="/tmp/power-race.qcow2",
        )
        pr.update_vm_state(session, "vm-power-race", state)
    return "vm-power-race"


def test_vm_power_action_never_runs_virsh_without_exact_claim(client, monkeypatch):
    from unittest.mock import MagicMock

    from astinuslab.playground.routes import vm_actions

    vm_id = _register_power_action_vm(client)
    run = MagicMock()
    monkeypatch.setattr(vm_actions.subprocess, "run", run)
    monkeypatch.setattr(vm_actions.pr, "claim_vm_for_power_action", lambda *_args, **_kwargs: None)

    response = client.post(f"/api/v1/playground/vms/{vm_id}/actions", json={"action": "reboot"})

    assert response.status_code == 409
    run.assert_not_called()


def test_vm_destroy_request_during_virsh_cannot_steal_power_claim(client, monkeypatch):
    from astinuslab.api.errors import ApiError
    from astinuslab.playground.routes import vm_actions

    vm_id = _register_power_action_vm(client)

    def request_destroy_during_virsh(*_args, **_kwargs):
        with pytest.raises(ApiError) as exc_info:
            vm_actions.destroy_vm_endpoint(vm_id)
        assert exc_info.value.status_code == 409
        assert "powering_off" in exc_info.value.detail

    monkeypatch.setattr(vm_actions.subprocess, "run", request_destroy_during_virsh)

    response = client.post(f"/api/v1/playground/vms/{vm_id}/actions", json={"action": "stop"})

    assert response.status_code == 202
    with vm_actions.db_mod.session_scope() as session:
        from astinuslab.db.models import JobVM

        vm = session.get(JobVM, vm_id)
        assert vm is not None
        assert vm.state == "stopping"


@pytest.mark.parametrize("virsh_fails", [False, True])
def test_power_publication_never_overwrites_destroy_custody(client, monkeypatch, virsh_fails):
    from datetime import UTC, datetime

    from astinuslab.db.models import JobVM
    from astinuslab.playground.routes import vm_actions

    vm_id = _register_power_action_vm(client)

    def transfer_to_destroy(*_args, **_kwargs):
        with vm_actions.db_mod.session_scope() as session:
            session.query(JobVM).filter_by(id=vm_id).update(
                {JobVM.state: "destroying", JobVM.updated_at: datetime.now(UTC)},
                synchronize_session=False,
            )
        if virsh_fails:
            raise subprocess.CalledProcessError(1, ["virsh"], stderr="host failure")

    monkeypatch.setattr(vm_actions.subprocess, "run", transfer_to_destroy)

    response = client.post(f"/api/v1/playground/vms/{vm_id}/actions", json={"action": "reboot"})

    assert response.status_code == 409
    with vm_actions.db_mod.session_scope() as session:
        vm = session.get(JobVM, vm_id)
        assert vm is not None
        assert vm.state == "destroying"


def test_vm_power_action_times_out_and_restores_prior_state(client, monkeypatch):
    from astinuslab.db.models import JobVM
    from astinuslab.playground.routes import vm_actions

    vm_id = _register_power_action_vm(client)
    with vm_actions.db_mod.session_scope() as session:
        prior_vm = session.get(JobVM, vm_id)
        assert prior_vm is not None
        prior_state = prior_vm.state
    calls: list[dict] = []

    def hung_virsh(*args, **kwargs):
        calls.append(kwargs)
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(vm_actions.subprocess, "run", hung_virsh)

    response = client.post(f"/api/v1/playground/vms/{vm_id}/actions", json={"action": "reboot"})

    assert response.status_code == 500
    assert "timed out" in response.json()["detail"]
    assert calls[0]["timeout"] == vm_actions._VIRSH_POWER_ACTION_TIMEOUT_SECONDS
    with vm_actions.db_mod.session_scope() as session:
        vm = session.get(JobVM, vm_id)
        assert vm is not None
        assert vm.state == prior_state


def test_vm_start_uses_probe_synchronized_booting_state(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    from astinuslab.db import session_scope
    from astinuslab.db.models import JobVM
    from astinuslab.playground.routes import vm_actions as vm_actions_routes

    monkeypatch.setattr(vm_actions_routes.subprocess, "run", lambda *args, **kwargs: None)
    job_id, net = _make_session_with_network(client)
    vm = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "web", "image_id": "img", "nics": [{"network_id": net["id"]}]},
    ).json()
    with session_scope() as session:
        record = session.get(JobVM, vm["id"])
        assert record is not None
        record.state = "stopped"

    response = client.post(f"/api/v1/playground/vms/{vm['id']}/actions", json={"action": "start"})

    assert response.status_code == 202
    with session_scope() as session:
        record = session.get(JobVM, vm["id"])
        assert record is not None
        assert record.state == "booting"


def test_vm_lifecycle_action_rejects_wrong_state(client, monkeypatch, tmp_path):
    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    job_id, net = _make_session_with_network(client)
    vm = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "web", "image_id": "img", "nics": [{"network_id": net["id"]}]},
    ).json()

    from astinuslab.db import session_scope
    from astinuslab.db.models import JobVM

    with session_scope() as s:
        s.query(JobVM).filter_by(id=vm["id"]).one().state = "pending"

    res = client.post(f"/api/v1/playground/vms/{vm['id']}/actions", json={"action": "stop"})

    assert res.status_code == 409
    assert "not running" in res.json()["detail"]


def test_stop_action_sets_stopping_not_stopped(client, monkeypatch):
    from astinuslab import db as db_mod
    from astinuslab.db.models import JobVM
    from astinuslab.playground import repositories as pr
    from astinuslab.playground.routes import vm_actions

    monkeypatch.setattr(
        vm_actions.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout="", stderr=""),
    )
    sess = client.post("/api/v1/playground/sessions").json()
    job_id = sess["session_id"]
    with db_mod.session_scope() as s:
        pr.register_network(
            s,
            network_id="net-1",
            job_id=job_id,
            name="default",
            subnet_cidr="10.42.1.0/24",
            isolated=True,
            libvirt_net_name="astinuslab-pg-x",
        )
        pr.update_network_state(s, "net-1", "active")
        pr.register_vm(
            s,
            vm_id="vm-1",
            job_id=job_id,
            label="tgt",
            image_id="ubuntu-22.04",
            domain_name="astinuslab-pg-x-tgt",
            disk_path="/x.qcow2",
            role="target",
        )
        pr.update_vm_state(s, "vm-1", "running")

    r = client.post("/api/v1/playground/vms/vm-1/actions", json={"action": "stop"})

    assert r.status_code == 202
    with db_mod.session_scope() as s:
        assert s.query(JobVM).filter_by(id="vm-1").one().state == "stopping"


def test_recent_failure_endpoint_reports_destroy_failed(client):
    from astinuslab import db as db_mod
    from astinuslab.playground import repositories as pr

    assert client.get("/api/v1/playground/sessions/recent-failure").json() is None

    with db_mod.session_scope() as s:
        pr.create_playground_session(s, job_id="pg-dead")
        pr.mark_session_destroy_failed(s, "pg-dead", "vm vm-1: libvirt down")

    body = client.get("/api/v1/playground/sessions/recent-failure").json()
    assert body["session_id"] == "pg-dead"
    assert body["status"] == "destroy_failed"
    assert "libvirt down" in body["error_summary"]


def test_recent_failure_ignores_clean_sessions(client):
    from astinuslab import db as db_mod
    from astinuslab.playground import repositories as pr

    with db_mod.session_scope() as s:
        pr.create_playground_session(s, job_id="pg-ok")
        pr.mark_session_completed(s, "pg-ok")

    assert client.get("/api/v1/playground/sessions/recent-failure").json() is None


@pytest.mark.parametrize(
    "state",
    [
        "destroy_requested",
        "destroying",
        "failure_cleanup",
        "cancelled",
        "completed",
        "destroy_failed",
        "failed",
        "running",
    ],
)
def test_named_cleanup_status_is_exact_safe_and_read_only(client, state):
    from datetime import datetime

    from astinuslab import db as db_mod
    from astinuslab.playground import repositories as pr

    with db_mod.session_scope() as s:
        record = pr.create_playground_session(s, job_id="pg-cleanup")
        record.status = state
        record.finished_at = datetime(2026, 9, 6, 10)
        record.error_summary = "/private/runner TOKEN_SECRET_DO_NOT_EXPOSE"
    response = client.get("/api/v1/playground/sessions/pg-cleanup/cleanup-status")
    assert response.status_code == 200
    assert response.json() == {
        "session_id": "pg-cleanup",
        "status": state,
        "finished_at": "2026-09-06T10:00:00Z",
        "error_code": {"destroy_failed": "cleanup_incomplete", "failed": "session_failed"}.get(state),
    }
    with db_mod.session_scope() as s:
        from astinuslab.db.models import PlaygroundSession

        retained_record = s.get(PlaygroundSession, "pg-cleanup")
        assert retained_record is not None
        assert retained_record.status == state
        assert retained_record.error_summary == "/private/runner TOKEN_SECRET_DO_NOT_EXPOSE"


def test_named_cleanup_status_missing_is_not_success(client):
    assert client.get("/api/v1/playground/sessions/missing/cleanup-status").status_code == 404


def test_named_cleanup_status_requires_admin_when_auth_is_enabled(client, monkeypatch):
    import hashlib
    from types import SimpleNamespace

    from astinuslab.core import security

    monkeypatch.setattr(
        security,
        "settings",
        SimpleNamespace(
            auth_enabled=True,
            admin_api_token_hash="sha256:" + hashlib.sha256(b"fixture-token").hexdigest(),
            api_port=8000,
            browser_allowed_origins=[],
        ),
    )
    assert client.get("/api/v1/playground/sessions/missing/cleanup-status").status_code == 401


def test_create_network_reuses_the_identity_of_a_destroyed_network(client, monkeypatch):
    """The route layer must release a torn-down network's name and subnet.

    Three checks guarded creation - the route's name comparison, resolve_cidr,
    and register_network - and each read every row in the session regardless of
    state. Recreating a destroyed network therefore failed with 409 "network
    name 'default' already exists", or silently landed on a substituted CIDR.
    """
    from unittest.mock import MagicMock

    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: MagicMock())

    job_id = client.post("/api/v1/playground/sessions", json={}).json()["job_id"]
    payload = {"name": "default", "subnet_cidr": "10.42.1.0/24", "isolated": True}
    first = client.post(f"/api/v1/playground/sessions/{job_id}/networks", json=payload).json()
    assert client.delete(f"/api/v1/playground/networks/{first['id']}").status_code == 202

    res = client.post(f"/api/v1/playground/sessions/{job_id}/networks", json=payload)

    assert res.status_code == 202, res.json()
    body = res.json()
    assert body["id"] != first["id"]
    assert body["name"] == "default"
    assert body["subnet_cidr"] == "10.42.1.0/24"
    assert body["cidr_substituted_from"] is None
    # A live namesake is still a conflict; only teardown releases the name.
    conflict = client.post(f"/api/v1/playground/sessions/{job_id}/networks", json=payload)
    assert conflict.status_code == 409


@pytest.fixture
def detach_claim_case(client, monkeypatch, tmp_path):
    from datetime import UTC, datetime

    from astinuslab.db import SessionLocal
    from astinuslab.db.models import JobVMNic
    from astinuslab.playground.routes import nics as nic_routes

    _common_monkeypatch(monkeypatch, tmp=tmp_path)
    job_id, network = _make_session_with_network(client)
    vm = client.post(
        f"/api/v1/playground/sessions/{job_id}/vms",
        json={"label": "detach-retry", "image_id": "img", "nics": [{"network_id": network["id"]}]},
    ).json()
    nic_id = vm["nics"][0]["id"]
    now = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(nic_routes, "datetime", FixedDatetime, raising=False)

    def set_claim(claimed_at, *, state="detach_claimed", token="old-detach-token"):
        with SessionLocal() as session:
            nic = session.get(JobVMNic, nic_id)
            assert nic is not None
            nic.state = state
            nic.detach_token = token
            nic.detach_claimed_at = claimed_at
            session.commit()

    return job_id, vm["id"], nic_id, now, set_claim


@pytest.mark.parametrize("age_seconds", [299.999999, 300, 301, None])
def test_detach_claim_retry_uses_five_minute_boundary(client, monkeypatch, detach_claim_case, age_seconds):
    from datetime import timedelta
    from types import SimpleNamespace

    from astinuslab.db import SessionLocal
    from astinuslab.db.models import JobVMNic
    from astinuslab.playground import orchestrator as po

    job_id, vm_id, nic_id, now, set_claim = detach_claim_case
    claimed_at = None if age_seconds is None else (now - timedelta(seconds=age_seconds)).replace(tzinfo=None)
    set_claim(claimed_at)
    calls = []

    def enqueue(**kwargs):
        with SessionLocal() as session:
            current = session.get(JobVMNic, nic_id)
            assert current is not None
            assert current.state == "detaching"
            assert current.detach_claimed_at is None
            assert current.detach_token == kwargs["detach_token"] != "old-detach-token"
        calls.append(kwargs)
        return SimpleNamespace(id="retry-task")

    monkeypatch.setattr(po.detach_nic, "delay", enqueue)
    response = client.delete(f"/api/v1/playground/sessions/current/vms/{vm_id}/nics/{nic_id}")

    assert response.status_code == 202, response.text
    if age_seconds is not None and age_seconds < 300:
        assert response.json()["task_id"] is None
        assert calls == []
        with SessionLocal() as session:
            current = session.get(JobVMNic, nic_id)
            assert current is not None
            assert (current.state, current.detach_token, current.detach_claimed_at) == (
                "detach_claimed",
                "old-detach-token",
                claimed_at,
            )
    else:
        assert response.json()["task_id"] == "retry-task"
        assert len(calls) == 1
        assert calls[0] == {
            "job_id": job_id,
            "vm_id": vm_id,
            "nic_id": nic_id,
            "detach_token": calls[0]["detach_token"],
        }


def test_detach_claim_retry_cas_loser_does_not_enqueue(client, monkeypatch, detach_claim_case):
    from datetime import timedelta
    from unittest.mock import Mock

    from sqlalchemy import event

    from astinuslab.db import SessionLocal
    from astinuslab.db.models import JobVMNic
    from astinuslab.playground import orchestrator as po

    _, vm_id, nic_id, now, set_claim = detach_claim_case
    set_claim(now - timedelta(seconds=300))
    enqueue = Mock()
    monkeypatch.setattr(po.detach_nic, "delay", enqueue)
    injected: list[bool] = []

    def replace_claim(conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.startswith("UPDATE job_vm_nics") and not injected:
            injected.append(True)
            conn.exec_driver_sql("UPDATE job_vm_nics SET detach_token = ? WHERE id = ?", ("successor-token", nic_id))

    event.listen(engine, "before_cursor_execute", replace_claim)
    try:
        response = client.delete(f"/api/v1/playground/sessions/current/vms/{vm_id}/nics/{nic_id}")
    finally:
        event.remove(engine, "before_cursor_execute", replace_claim)

    assert injected == [True], "stale retry must attempt its exact conditional claim"
    assert response.status_code == 202, response.text
    assert response.json()["task_id"] is None
    enqueue.assert_not_called()
    with SessionLocal() as session:
        current = session.get(JobVMNic, nic_id)
        assert current is not None
        assert current.state == "detach_claimed"
        assert current.detach_token == "successor-token"


@pytest.mark.parametrize("claim_state", ["active", "stale", "null_timestamp", "tokenless_detaching"])
def test_detach_nic_broker_failure_restores_own_token(client, monkeypatch, detach_claim_case, claim_state):
    from datetime import timedelta

    from astinuslab.db import SessionLocal
    from astinuslab.db.models import JobVMNic
    from astinuslab.playground import orchestrator as po

    _, vm_id, nic_id, now, set_claim = detach_claim_case
    state = "active" if claim_state == "active" else "detach_claimed"
    if claim_state == "tokenless_detaching":
        state = "detaching"
    set_claim(
        now - timedelta(seconds=300) if claim_state == "stale" else None,
        state=state,
        token=None if claim_state == "tokenless_detaching" else "old-detach-token",
    )
    attempted = []
    committed_tokens = []

    def broker_down(**kwargs):
        attempted.append(kwargs["detach_token"])
        with SessionLocal() as session:
            current = session.get(JobVMNic, nic_id)
            assert current is not None
            committed_tokens.append(current.detach_token)
            assert current.detach_token == kwargs["detach_token"]
        raise RuntimeError("broker down")

    monkeypatch.setattr(po.detach_nic, "delay", broker_down)
    response = client.delete(f"/api/v1/playground/sessions/current/vms/{vm_id}/nics/{nic_id}")

    assert response.status_code == 503, response.text
    assert len(attempted) == 1
    assert committed_tokens == attempted
    with SessionLocal() as session:
        current = session.get(JobVMNic, nic_id)
        assert current is not None
        assert (current.state, current.detach_token, current.detach_claimed_at) == ("active", None, None)


@pytest.mark.parametrize("force", [False, True])
def test_detach_nic_broker_failure_cannot_mutate_successor_at_sql_write(client, monkeypatch, detach_claim_case, force):
    from sqlalchemy import event

    from astinuslab.db import SessionLocal
    from astinuslab.db.models import JobVMNic
    from astinuslab.playground import orchestrator as po

    _, vm_id, nic_id, _, set_claim = detach_claim_case
    set_claim(None, state="active")
    broker_failed = []
    injected: list[bool] = []

    def broker_down(**kwargs):
        broker_failed.append(kwargs["detach_token"])
        raise RuntimeError("broker down")

    def replace_claim(conn, _cursor, statement, _parameters, _context, _executemany):
        if broker_failed and not injected and statement.startswith(("UPDATE job_vm_nics", "DELETE FROM job_vm_nics")):
            injected.append(True)
            conn.exec_driver_sql("UPDATE job_vm_nics SET detach_token = ? WHERE id = ?", ("successor-token", nic_id))

    monkeypatch.setattr(po.detach_nic, "delay", broker_down)
    event.listen(engine, "before_cursor_execute", replace_claim)
    try:
        response = client.delete(
            f"/api/v1/playground/sessions/current/vms/{vm_id}/nics/{nic_id}?force={str(force).lower()}"
        )
    finally:
        event.remove(engine, "before_cursor_execute", replace_claim)

    assert response.status_code == (200 if force else 503), response.text
    assert len(broker_failed) == 1
    assert injected == [True], "inject successor after observation and before the actual compensation mutation"
    with SessionLocal() as session:
        current = session.get(JobVMNic, nic_id)
        assert current is not None
        assert (current.state, current.detach_token, current.detach_claimed_at) == (
            "detaching",
            "successor-token",
            None,
        )
