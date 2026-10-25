from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from astinuslab.jobs.celery_worker import celery_app
from astinuslab.main import app
from tests.python._support.helpers import autospec_setattr, configure_csrf_client, isolated_playground_database


@pytest.fixture(autouse=True)
def _eager(monkeypatch, tmp_path):
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: MagicMock())
    with isolated_playground_database(monkeypatch, tmp_path, globals()):
        yield


def _client():
    return configure_csrf_client(TestClient(app))


def test_seamless_network_creates_session_and_returns_state():
    c = _client()
    res = c.post("/api/v1/playground/networks", json={"name": "lab", "subnet_cidr": "10.42.1.0/24", "isolated": True})
    assert res.status_code == 202
    body = res.json()
    assert body["job_id"].startswith("pg-")
    assert body["status"] == "running"
    assert len(body["networks"]) == 1
    assert body["networks"][0]["name"] == "lab"


def test_seamless_network_reuses_existing_session():
    c = _client()
    first = c.post(
        "/api/v1/playground/networks", json={"name": "n1", "subnet_cidr": "10.42.1.0/24", "isolated": True}
    ).json()
    second = c.post(
        "/api/v1/playground/networks", json={"name": "n2", "subnet_cidr": "10.42.2.0/24", "isolated": True}
    ).json()
    assert first["job_id"] == second["job_id"]
    assert {n["name"] for n in second["networks"]} == {"n1", "n2"}


def test_seamless_network_partial_failure_retains_failure_cleanup_custody(monkeypatch):
    from astinuslab.playground import orchestrator as po

    def boom(**kw):
        raise RuntimeError("libvirt exploded")

    monkeypatch.setattr(po.create_network, "delay", boom)

    c = _client()
    res = c.post("/api/v1/playground/networks", json={"name": "lab", "subnet_cidr": "10.42.1.0/24", "isolated": True})
    assert res.status_code == 500
    assert res.json()["detail"] == "libvirt exploded"

    # The failed network still needs durable reconciliation ownership, so the
    # parent remains current until failure cleanup publishes a terminal state.
    cur = c.get("/api/v1/playground/sessions/current")
    assert cur.status_code == 200
    body = cur.json()
    assert body["status"] == "failure_cleanup"
    assert len(body["networks"]) == 1
    assert body["networks"][0]["state"] == "failed"


def test_jobs_list_excludes_playground_sessions():
    c = _client()
    c.post("/api/v1/playground/networks", json={"name": "lab", "subnet_cidr": "10.42.1.0/24", "isolated": True})
    res = c.get("/api/v1/jobs")
    assert res.status_code == 200
    ids = [j["id"] for j in res.json()]
    assert all(not jid.startswith("pg-") for jid in ids)


def test_job_detail_404_for_playground_id():
    c = _client()
    body = c.post(
        "/api/v1/playground/networks", json={"name": "lab", "subnet_cidr": "10.42.1.0/24", "isolated": True}
    ).json()
    pg_id = body["job_id"]
    assert c.get(f"/api/v1/jobs/{pg_id}").status_code == 404
    assert c.get(f"/jobs/{pg_id}").status_code == 404
