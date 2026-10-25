from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from astinuslab.api.errors import ApiError
from astinuslab.core.config import Settings
from astinuslab.db import Base
from astinuslab.jobs.celery_worker import celery_app
from astinuslab.main import app
from astinuslab.playground import admission
from astinuslab.playground import repositories as pr
from tests.python._support.helpers import autospec_setattr, configure_csrf_client, isolated_playground_database


@pytest.fixture(autouse=True)
def _eager(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    with isolated_playground_database(monkeypatch, tmp_path, globals()):
        yield


@pytest.fixture
def client() -> TestClient:
    return configure_csrf_client(TestClient(app))


def _register(s, vm_id: str, state: str = "pending", session_id: str = "pg-cap") -> None:
    pr.register_vm(
        s,
        vm_id=vm_id,
        job_id=session_id,
        label=vm_id,
        image_id="img",
        domain_name=f"astinuslab-pg-{session_id}-{vm_id}",
        disk_path=f"/tmp/{vm_id}.qcow2",
    )
    if state != "pending":
        pr.update_vm_state(s, vm_id, state)


def test_count_capacity_vms_by_state() -> None:
    from astinuslab.db import session_scope

    with session_scope() as s:
        pr.create_playground_session(s, job_id="pg-cap")
        _register(s, "vm-running", "running")
        _register(s, "vm-pending", "pending")
        _register(s, "vm-provisioning", "provisioning_workload")
        _register(s, "vm-teardown-failed", "teardown_failed")
        _register(s, "vm-stopped", "stopped")
        _register(s, "vm-destroyed", "destroyed")
        _register(s, "vm-failed", "failed")

        assert admission.count_capacity_vms(s) == 4


def test_ensure_vm_capacity_unlimited_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    from astinuslab.db import session_scope

    monkeypatch.setattr(admission, "settings", Settings())
    with session_scope() as s:
        pr.create_playground_session(s, job_id="pg-cap")
        for i in range(20):
            _register(s, f"vm-{i}", "running")

        admission.ensure_vm_capacity(s, requested=1)


def test_ensure_vm_capacity_rejects_at_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from astinuslab.db import session_scope

    monkeypatch.setattr(admission, "settings", Settings(max_concurrent_vms=2))
    with session_scope() as s:
        pr.create_playground_session(s, job_id="pg-cap")
        _register(s, "vm-1", "running")
        _register(s, "vm-2", "running")

        with pytest.raises(ApiError) as exc:
            admission.ensure_vm_capacity(s, requested=1)

    assert exc.value.status_code == 409
    assert exc.value.code == "vm_capacity"


def test_capacity_released_when_vm_reconciled_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    from astinuslab.db import session_scope

    monkeypatch.setattr(admission, "settings", Settings(max_concurrent_vms=1))
    with session_scope() as s:
        pr.create_playground_session(s, job_id="pg-cap")
        _register(s, "vm-1", "running")
        with pytest.raises(ApiError):
            admission.ensure_vm_capacity(s)

        pr.update_vm_state(s, "vm-1", "destroyed")
        admission.ensure_vm_capacity(s)


def test_concurrent_admission_admits_exactly_one(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admission, "settings", Settings(max_concurrent_vms=1))
    file_engine = create_engine(f"sqlite:///{tmp_path}/admission.db", connect_args={"timeout": 10})
    Base.metadata.create_all(file_engine)
    maker = sessionmaker(bind=file_engine)
    with maker() as s:
        pr.create_playground_session(s, job_id="pg-race")
        s.commit()

    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    guard = threading.Lock()

    def admit(vm_id: str) -> None:
        barrier.wait()
        with maker() as s:
            try:
                admission.ensure_vm_capacity(s, requested=1)
                _register(s, vm_id, session_id="pg-race")
                s.commit()
                with guard:
                    outcomes.append("admitted")
            except ApiError:
                s.rollback()
                with guard:
                    outcomes.append("rejected")

    threads = [threading.Thread(target=admit, args=(f"vm-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["admitted", "rejected"]


def test_spawn_endpoint_returns_409_at_capacity(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from astinuslab.db import session_scope
    from astinuslab.playground.routes import vms as vms_routes

    monkeypatch.setattr(admission, "settings", Settings(max_concurrent_vms=1))
    autospec_setattr(
        monkeypatch,
        vms_routes,
        "_validate_spawn_image",
        lambda payload: {"id": payload.image_id, "path": "/dev/null"},
    )
    autospec_setattr(
        monkeypatch,
        vms_routes,
        "_validate_spawn_workload",
        lambda payload, image=None: None,
    )
    autospec_setattr(
        monkeypatch,
        vms_routes,
        "enqueue_or_raise",
        lambda task, on_failure=None, **kw: "task-id",
    )

    with session_scope() as s:
        session = pr.create_playground_session(s, job_id="pg-api")
        pr.register_network(
            s,
            network_id="net-1",
            job_id="pg-api",
            name="lan",
            subnet_cidr="10.42.1.0/24",
            isolated=True,
            libvirt_net_name="pg-lan",
        )
        pr.update_network_state(s, "net-1", "active")
        session_id = session.id

    first = client.post(
        f"/api/v1/playground/sessions/{session_id}/vms",
        json={"label": "one", "image_id": "img", "nics": [{"network_id": "net-1"}]},
    )
    assert first.status_code == 202

    second = client.post(
        f"/api/v1/playground/sessions/{session_id}/vms",
        json={"label": "two", "image_id": "img", "nics": [{"network_id": "net-1"}]},
    )
    assert second.status_code == 409
    assert second.json()["code"] == "vm_capacity"
    assert "capacity" in second.json()["detail"].lower()


def test_create_job_rejects_attacker_pair_when_cap_is_one(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    from astinuslab.api.schemas import JobCreate
    from astinuslab.db import session_scope
    from astinuslab.jobs import job_services

    monkeypatch.setattr(job_services, "settings", Settings(max_concurrent_vms=1))
    payload = JobCreate(
        target_id="any",
        scenario_id="any",
        execution_mode="manual_trigger",
        capture_type="memory",
        attacker_vm_id="kali-default",
    )

    with session_scope() as s:
        with pytest.raises(HTTPException) as exc:
            job_services.create_job_service(s, payload)

    assert exc.value.status_code == 400
    assert "max_concurrent_vms" in exc.value.detail
