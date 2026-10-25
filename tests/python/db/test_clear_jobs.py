from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

import astinuslab.db as db
from astinuslab.db import Base, clear_job_records
from astinuslab.db.models import HostAction, Job, JobEvent, JobRuntime


def _engine(path):
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_connection, _record):  # mirrors astinuslab.db.set_sqlite_pragma
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return engine


def _seed(engine) -> None:
    now = datetime.now(UTC)
    with sessionmaker(bind=engine, future=True)() as session:
        session.add(
            Job(
                id="job-1",
                target_id="ubuntu",
                scenario_id="capture",
                execution_mode="manual",
                status="completed",
                created_at=now,
            )
        )
        session.flush()
        session.add(JobEvent(job_id="job-1", state="completed", message="done", created_at=now))
        session.add(HostAction(job_id="job-1", action_type="acquire_memory", status="completed", created_at=now))
        session.add(
            JobRuntime(
                job_id="job-1",
                role="target",
                vm_name="astinuslab-job-1",
                state="teardown_failed",
                power_state="unknown",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()


def test_clear_job_records_removes_every_job_table(tmp_path, monkeypatch):
    engine = _engine(tmp_path / "clear.db")
    _seed(engine)
    monkeypatch.setattr(db, "engine", engine)

    counts = clear_job_records()

    assert counts == {"job_events": 1, "job_runtimes": 1, "host_actions": 1, "jobs": 1}
    with engine.begin() as conn:
        for table in ("job_events", "job_runtimes", "host_actions", "jobs"):
            assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0


def test_clear_job_records_leaves_no_orphan_runtimes(tmp_path, monkeypatch):
    """The failure this replaces: runtime rows surviving the wipe of their job.

    Orphaned job_runtimes rows are what made has_active_non_playground_job()
    report a forensic job that no longer existed, blocking the Playground.
    """
    engine = _engine(tmp_path / "orphan.db")
    _seed(engine)
    monkeypatch.setattr(db, "engine", engine)

    clear_job_records()

    with engine.begin() as conn:
        orphans = conn.execute(
            text("SELECT count(*) FROM job_runtimes WHERE job_id NOT IN (SELECT id FROM jobs)")
        ).scalar()
    assert orphans == 0


def test_clear_job_records_is_idempotent_on_empty_db(tmp_path, monkeypatch):
    engine = _engine(tmp_path / "empty.db")
    monkeypatch.setattr(db, "engine", engine)

    assert clear_job_records() == {"job_events": 0, "job_runtimes": 0, "host_actions": 0, "jobs": 0}
