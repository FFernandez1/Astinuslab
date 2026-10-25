"""SQLite fixtures for broker ambiguity; these do not exercise a real broker/host."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from astinuslab.api.schemas import JobCreate
from astinuslab.db.repositories import JobRepository, has_active_non_playground_job
from astinuslab.jobs import job_services


@pytest.fixture(autouse=True)
def _pinned_host_capacity(available_host_capacity):
    """Job admission in this module must not depend on the host's free RAM or disk."""


def make_intent(session, kind="run_manual_trigger_job", status="queued"):
    repo = JobRepository(session)
    job = repo.create_job(
        JobCreate(target_id="ubuntu-22-04-default", scenario_id="manual-trigger", execution_mode="manual_trigger")
    )
    repo.update_job(job.id, status=status)
    action = repo.enqueue_action(job.id, kind)
    return repo, job, action


@pytest.mark.parametrize(
    "kind,status,expected",
    [
        ("run_manual_trigger_job", "queued", "failed"),
        ("acquire_memory_dump", "capture_requested", "waiting_for_capture"),
        ("run_vol3_plugin", "completed", "completed"),
    ],
)
def test_failed_dispatch_compensates_parent_atomically(in_memory_session_scope, monkeypatch, kind, status, expected):
    def unavailable(*args):
        raise ConnectionError("fixture broker refused publish")

    monkeypatch.setitem(job_services.TASK_MAP, kind, SimpleNamespace(delay=unavailable))
    with in_memory_session_scope() as session:
        repo, job, action = make_intent(session, kind, status)
        with pytest.raises(HTTPException) as err:
            job_services._dispatch_action_after_commit(
                session, repo, job_id=job.id, action_id=action.id, action_type=kind
            )
        assert err.value.status_code == 503
        session.expire_all()
        assert _required(repo.get_job(job.id)).status == expected
        assert _required(repo.get_action(action.id)).status == "failed"
        assert repo.get_action(action.id).finished_at is not None
        if expected == "failed":
            assert not has_active_non_playground_job(session)


@pytest.mark.parametrize("status", ["running", "completed", "failed"])
def test_stale_dispatch_does_not_publish_nonqueued_action(in_memory_session_scope, status):
    published = []

    def publish(job_id, action_id):
        published.append((job_id, action_id))
        return SimpleNamespace(id="unexpected-delivery")

    with in_memory_session_scope() as session:
        repo, job, action = make_intent(session)
        repo.update_action(action.id, status=status, celery_task_id="original-delivery")
        session.commit()
        job_services._dispatch_action_after_commit(
            session,
            repo,
            job_id=job.id,
            action_id=action.id,
            action_type=action.action_type,
            task_fn=SimpleNamespace(delay=publish),
        )
        session.expire_all()
        assert published == []
        current = _required(repo.get_action(action.id))
        assert current.status == status
        assert current.celery_task_id == "original-delivery"
        assert current.dispatch_attempts == 0
        assert current.dispatched_at is None


def test_accepted_then_exception_does_not_revert_claimed_capture(in_memory_session_scope, monkeypatch):
    with in_memory_session_scope() as session:
        repo, job, action = make_intent(session, "acquire_memory_dump", "capture_requested")

        def accepted(job_id, action_id):
            with in_memory_session_scope() as worker:
                JobRepository(worker).update_action(action_id, status="running")
                JobRepository(worker).update_job(job_id, status="acquiring_memory")
            raise ConnectionError("fixture lost broker ack after worker accepted")

        monkeypatch.setitem(job_services.TASK_MAP, "acquire_memory_dump", SimpleNamespace(delay=accepted))
        job_services._dispatch_action_after_commit(
            session, repo, job_id=job.id, action_id=action.id, action_type="acquire_memory_dump"
        )
        session.expire_all()
        assert _required(repo.get_job(job.id)).status == "acquiring_memory"
        assert _required(repo.get_action(action.id)).status == "running"


def test_late_delivery_cannot_execute_compensated_action(in_memory_session_scope, monkeypatch):
    from astinuslab import db
    from astinuslab.jobs import celery_app
    from astinuslab.live_session import runner

    calls = []
    monkeypatch.setattr(db, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(runner, "run_live_session_job", lambda *a, **kw: calls.append(a))
    with in_memory_session_scope() as session:
        repo, job, action = make_intent(session)
        repo.compensate_dispatch_failure(job.id, action.id)
        job_id, action_id = job.id, action.id
    celery_app.run_live_session_job_task.run(job_id, action_id)
    assert calls == []


def test_duplicate_delivery_executes_only_once(in_memory_session_scope, monkeypatch):
    from astinuslab import db
    from astinuslab.jobs import celery_app
    from astinuslab.live_session import runner

    calls = []
    monkeypatch.setattr(db, "session_scope", in_memory_session_scope)

    def execute(job_id, action_id):
        calls.append(job_id)
        with in_memory_session_scope() as s:
            JobRepository(s).update_action(action_id, status="completed")

    monkeypatch.setattr(runner, "run_live_session_job", execute)
    with in_memory_session_scope() as session:
        _, job, action = make_intent(session)
        job_id, action_id = job.id, action.id
    celery_app.run_live_session_job_task.run(job_id, action_id)
    celery_app.run_live_session_job_task.run(job_id, action_id)
    assert calls == [job_id]


def test_analysis_recovery_independent_of_completed_parent_and_other_worker(in_memory_session_scope):
    from astinuslab.jobs.action_lifecycle import reconcile_interrupted_actions

    with in_memory_session_scope() as session:
        repo, job, interrupted = make_intent(session, "run_vol3_plugin", "completed")
        repo.update_action(interrupted.id, status="running", owner_token="dead-owner")
        live = repo.enqueue_action(job.id, "run_vol3_plugin")
        repo.update_action(live.id, status="running", owner_token="live-owner")
        capture = repo.enqueue_action(job.id, "acquire_memory_dump")
        repo.update_action(capture.id, status="running", owner_token="dead-owner")
        job_id, interrupted_id, live_id, capture_id = job.id, interrupted.id, live.id, capture.id
    count = reconcile_interrupted_actions(in_memory_session_scope, alive=lambda owner: owner == "live-owner")
    assert count == 1
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        assert _required(repo.get_job(job_id)).status == "completed"
        assert _required(repo.get_action(interrupted_id)).status == "failed"
        assert _required(repo.get_action(live_id)).status == "running"
        assert _required(repo.get_action(capture_id)).status == "running"


def test_analysis_startup_runs_independent_recovery(monkeypatch):
    from astinuslab.jobs import action_lifecycle, celery_worker_signals

    calls = []
    monkeypatch.setattr(action_lifecycle, "reconcile_interrupted_actions", lambda *a, **kw: calls.append(kw))
    celery_worker_signals._on_worker_ready(SimpleNamespace(hostname="analysis@fixture"))
    assert calls == [{"service_identity": "analysis"}]


@pytest.mark.parametrize("delete_files", [False, True])
def test_job_deletion_preserves_evidence_rows_and_bytes(in_memory_session_scope, tmp_path, delete_files):
    from astinuslab.api.errors import ApiError
    from astinuslab.db.repositories import ArtifactRepository

    evidence = tmp_path / "retained-report.json"
    evidence.write_bytes(b'{"fixture":"custody"}\n')
    with in_memory_session_scope() as session:
        repo, job, action = make_intent(session, "run_vol3_plugin", "completed")
        from astinuslab.db.models import Acquisition, Report

        session.add(Acquisition(id="sealed-fixture", name="sealed evidence", status="sealed"))
        session.flush()
        repo.update_job(job.id, acquisition_id="sealed-fixture")
        artifact = ArtifactRepository(session).create_artifact(
            kind="report",
            producer="vol3_plugin_run",
            path=str(evidence),
            filename=evidence.name,
            job_id=job.id,
            acquisition_id="sealed-fixture",
        )
        session.add(
            Report(
                id="report-fixture",
                report_type="volatility3_plugin",
                status="completed",
                path=str(evidence),
                job_id=job.id,
                acquisition_id="sealed-fixture",
                output_artifact_id=artifact.id,
            )
        )
        session.flush()
        artifact_id = artifact.id
        assert repo.delete_job(job.id) is False
        with pytest.raises(ApiError, match="Storage") as error:
            job_services.delete_job_service(session, job.id, delete_files=delete_files)
        assert error.value.status_code == 409
        from astinuslab.db.models import Artifact

        assert session.get(Artifact, artifact_id) is not None
        assert repo.get_job(job.id) is not None
        assert session.get(Report, "report-fixture") is not None
        assert evidence.read_bytes() == b'{"fixture":"custody"}\n'


def test_startup_retries_unclaimed_send_but_not_live_claim(in_memory_session_scope):
    from datetime import UTC, datetime

    from astinuslab.jobs.action_lifecycle import claim_action, process_owner, recover_queued_dispatches

    with in_memory_session_scope() as session:
        repo, job, action = make_intent(session)
        repo.update_action(
            action.id, dispatched_at=datetime.now(UTC), dispatch_attempts=1, celery_task_id="uncertain-old-delivery"
        )
        live = repo.enqueue_action(job.id, "run_manual_trigger_job")
        claim_action(session, job.id, live.id, owner=process_owner())
        action_id, live_id = action.id, live.id
    assert recover_queued_dispatches(in_memory_session_scope, service_identity="runner") == 1
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        assert _required(repo.get_action(action_id)).dispatched_at is None
        assert _required(repo.get_action(live_id)).status == "running"


def _required[T](value: T | None) -> T:
    assert value is not None
    return value
