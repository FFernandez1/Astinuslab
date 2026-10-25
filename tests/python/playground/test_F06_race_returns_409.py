from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from astinuslab.db import Base
from astinuslab.jobs.celery_worker import celery_app
from astinuslab.playground import network_lifecycle as nl
from astinuslab.playground import orchestrator as po
from astinuslab.playground import repositories as pr
from tests.python._support.helpers import autospec_setattr


@pytest.fixture
def in_memory_session_scope(tmp_path):
    """Keep the shared client fixture API, with separate connections for races."""
    # A StaticPool in-memory DB shares one connection across the eight writers;
    # this disposable file exercises actual SQLite writer serialization.
    test_engine = create_engine(f"sqlite:///{tmp_path / 'race.sqlite'}")
    Base.metadata.create_all(test_engine)
    factory = sessionmaker(bind=test_engine, autoflush=False)

    @contextmanager
    def scope():
        with factory.begin() as session:
            yield session

    try:
        yield scope
    finally:
        test_engine.dispose()


@pytest.fixture(autouse=True)
def _eager(monkeypatch, tmp_path, in_memory_session_scope):
    monkeypatch.setattr(po, "session_scope", in_memory_session_scope)
    image_file = tmp_path / "base.qcow2"
    image_file.write_bytes(b"placeholder image")

    class _VM:
        def boot_playground(self, *args, **kwargs):
            return None

        def get_ip(self, *args, **kwargs):
            return None

    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    autospec_setattr(
        monkeypatch, nl, "_networks_helper", lambda: type("N", (), {"create_network": lambda *a, **k: None})()
    )
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: _VM())
    autospec_setattr(monkeypatch, po.create_network, "delay", lambda **kwargs: SimpleNamespace(id="create-network"))
    autospec_setattr(monkeypatch, po.spawn_vm, "delay", lambda **kwargs: SimpleNamespace(id="spawn-vm"))
    autospec_setattr(monkeypatch, po.destroy_session, "delay", lambda **kwargs: SimpleNamespace(id="destroy-session"))
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


def test_F06_concurrent_session_create_returns_either_202_or_409_not_500(client):
    barrier = Barrier(8)

    def attempt():
        barrier.wait()
        return client.post("/api/v1/playground/sessions", json={}).status_code

    with ThreadPoolExecutor(max_workers=8) as ex:
        statuses = list(ex.map(lambda _: attempt(), range(8)))
    assert all(s in (202, 409) for s in statuses), statuses
    assert statuses.count(202) == 1


def test_F06_concurrent_vms_create_if_needed_returns_202_or_409_not_500(client):
    barrier = Barrier(8)

    def attempt(i: int):
        barrier.wait()
        response = client.post(
            "/api/v1/playground/vms",
            json={"label": f"tgt-{i}", "image_id": "img", "role": "target"},
        )
        return response.status_code, response.text

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(attempt, range(8)))

    statuses = [status for status, _body in results]
    unexpected = [result for result in results if result[0] not in (202, 409)]
    assert not unexpected, unexpected
    assert 202 in statuses


def test_F06_vm_registration_state_race_returns_409(monkeypatch, client):
    def reject_registration(*args, **kwargs):
        raise RuntimeError("playground session pg-raced is not accepting new resources")

    autospec_setattr(monkeypatch, pr, "register_vm", reject_registration)

    response = client.post(
        "/api/v1/playground/vms",
        json={"label": "tgt-raced", "image_id": "img", "role": "target"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
