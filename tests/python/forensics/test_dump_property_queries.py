from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import event

from astinuslab.api.schemas import JobOut
from astinuslab.db.models import Artifact, Job
from astinuslab.jobs.job_services import list_jobs_service


def _job(job_id: str) -> Job:
    return Job(
        id=job_id,
        target_id="ubuntu-22-04-default",
        scenario_id="immediate-capture",
        execution_mode="immediate",
        status="completed",
        capture_type="memory",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _artifact(job_id: str, kind: str, path: str, offset: int) -> Artifact:
    return Artifact(
        id=f"art-{job_id}-{kind}-{offset}",
        job_id=job_id,
        producer="job_capture",
        kind=kind,
        path=path,
        filename=path.rsplit("/", 1)[-1],
        sha256=f"sha-{job_id}-{kind}-{offset}",
        size_bytes=offset,
        created_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=offset),
    )


def test_list_jobs_prefetches_dump_artifacts_in_one_query(in_memory_session_scope):
    with in_memory_session_scope() as session:
        for idx in range(5):
            job = _job(f"job-{idx}")
            session.add(job)
            session.add(_artifact(job.id, "memory", f"/dumps/{job.id}/old.mem", idx * 10 + 1))
            session.add(_artifact(job.id, "memory", f"/dumps/{job.id}/new.mem", idx * 10 + 2))
            session.add(_artifact(job.id, "disk", f"/dumps/{job.id}/disk.img", idx * 10 + 3))
        session.add(_job("job-empty"))
        session.commit()

        statements: list[str] = []

        def _count(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(session.bind, "before_cursor_execute", _count)
        try:
            jobs = list_jobs_service(session)
            outputs = [JobOut.model_validate(job) for job in jobs]
        finally:
            event.remove(session.bind, "before_cursor_execute", _count)

    artifact_selects = [stmt for stmt in statements if " artifacts" in stmt.lower() or "from artifacts" in stmt.lower()]
    assert len(artifact_selects) == 1

    by_id = {job.id: job for job in outputs}
    for idx in range(5):
        job_id = f"job-{idx}"
        assert by_id[job_id].has_memory_dump is True
        assert by_id[job_id].dump_filename == "new.mem"
        assert by_id[job_id].has_disk_dump is True
        assert by_id[job_id].disk_dump_filename == "disk.img"
    assert by_id["job-empty"].has_memory_dump is False
    assert by_id["job-empty"].has_disk_dump is False


def test_dump_property_reuses_per_instance_memo(in_memory_session_scope):
    with in_memory_session_scope() as session:
        job = _job("job-one")
        session.add(job)
        session.add(_artifact(job.id, "memory", "/dumps/job-one/memory.dump", 1))
        session.commit()
        session.expire_all()
        loaded = session.get(Job, "job-one")
        assert loaded is not None

        statements: list[str] = []

        def _count(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(session.bind, "before_cursor_execute", _count)
        try:
            assert loaded.dump_path == "/dumps/job-one/memory.dump"
            first_count = len([stmt for stmt in statements if "from artifacts" in stmt.lower()])
            assert loaded.dump_filename == "memory.dump"
            second_count = len([stmt for stmt in statements if "from artifacts" in stmt.lower()])
        finally:
            event.remove(session.bind, "before_cursor_execute", _count)

    assert first_count == 1
    assert second_count == 1
