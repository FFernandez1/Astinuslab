from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from astinuslab import db
from astinuslab.api.errors import ApiError
from astinuslab.api.schemas import JobCreate, PlaygroundCreateNetworkRequest
from astinuslab.db import repositories
from astinuslab.db.models import Job, PlaygroundSession
from astinuslab.jobs import job_services
from astinuslab.playground import orchestrator
from astinuslab.playground.routes import networks, sessions


@pytest.fixture
def sqlite_scope(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'admission.db'}")
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(engine)

    @contextmanager
    def scope():
        with factory() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    monkeypatch.setattr(db, "session_scope", scope)
    yield scope, engine
    engine.dispose()


def assert_other_writer_blocked(engine):
    """Observe the actual SQLite reservation through an independent connection."""
    with engine.connect() as contender:
        contender.exec_driver_sql("PRAGMA busy_timeout=0")
        with pytest.raises(OperationalError, match="database is locked"):
            contender.exec_driver_sql("BEGIN IMMEDIATE")


def job_payload():
    return JobCreate(target_id="ubuntu-22-04-default", scenario_id="immediate-capture", execution_mode="immediate")


def test_forensic_admission_holds_write_reservation_at_exclusion_check(sqlite_scope, monkeypatch):
    scope, engine = sqlite_scope
    original = job_services.has_active_playground_session

    def inspect_reservation(session):
        result = original(session)
        assert_other_writer_blocked(engine)
        return result

    monkeypatch.setattr(job_services, "has_active_playground_session", inspect_reservation)
    with scope() as session:
        job_services.create_job_service(session, job_payload())
    with pytest.raises(ApiError) as conflict:
        sessions.create_session_endpoint()
    assert conflict.value.status_code == 409


def test_concurrent_creators_admit_only_one_mode(sqlite_scope):
    scope, _ = sqlite_scope
    start = Barrier(2)

    def forensic():
        start.wait(timeout=10)
        try:
            with scope() as session:
                job_services.create_job_service(session, job_payload())
            return 201
        except ApiError as exc:
            return exc.status_code

    def playground():
        start.wait(timeout=10)
        try:
            sessions.create_session_endpoint()
            return 202
        except ApiError as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(forensic), pool.submit(playground)
        statuses = [a.result(timeout=15), b.result(timeout=15)]
    assert statuses.count(409) == 1
    assert len([status for status in statuses if status in {201, 202}]) == 1
    with scope() as session:
        assert session.query(Job).count() + session.query(PlaygroundSession).count() == 1


@pytest.mark.parametrize("create", [sessions.create_session_endpoint, sessions._ensure_active_session_id])
def test_playground_admission_holds_write_reservation_at_exclusion_check(sqlite_scope, monkeypatch, create):
    scope, engine = sqlite_scope
    original = repositories.has_active_non_playground_job

    def inspect_reservation(session):
        result = original(session)
        assert_other_writer_blocked(engine)
        return result

    monkeypatch.setattr(repositories, "has_active_non_playground_job", inspect_reservation)
    create()
    with scope() as session, pytest.raises(ApiError) as conflict:
        job_services.create_job_service(session, job_payload())
    assert conflict.value.status_code == 409


def test_playground_task_holds_write_reservation_before_admission_checks(sqlite_scope, monkeypatch):
    scope, engine = sqlite_scope
    original = orchestrator.pr.active_playground_session

    def inspect_reservation(session):
        assert_other_writer_blocked(engine)
        return original(session)

    monkeypatch.setattr(orchestrator, "session_scope", scope)
    monkeypatch.setattr(orchestrator.pr, "active_playground_session", inspect_reservation)
    assert orchestrator.create_session.run(job_id="pg-task-admission") == "pg-task-admission"
    with scope() as session:
        assert session.get(PlaygroundSession, "pg-task-admission").status == "running"
    with scope() as session, pytest.raises(ApiError) as conflict:
        job_services.create_job_service(session, job_payload())
    assert conflict.value.status_code == 409


def test_first_network_holds_write_reservation_at_session_admission(sqlite_scope, monkeypatch):
    scope, engine = sqlite_scope
    original = repositories.has_active_non_playground_job

    def inspect_reservation(session):
        assert_other_writer_blocked(engine)
        return original(session)

    monkeypatch.setattr(repositories, "has_active_non_playground_job", inspect_reservation)
    monkeypatch.setattr(networks, "_enqueue_create_network", lambda **kwargs: None)
    result = networks.ensure_session_and_create_network_endpoint(
        PlaygroundCreateNetworkRequest(name="ab", subnet_cidr="10.42.1.0/24", isolated=True)
    )
    assert len(result.networks) == 1
    with scope() as session, pytest.raises(ApiError) as conflict:
        job_services.create_job_service(session, job_payload())
    assert conflict.value.status_code == 409
