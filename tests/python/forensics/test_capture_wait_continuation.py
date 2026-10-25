from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from astinuslab.api.schemas import JobCreate
from astinuslab.db.repositories import JobRepository
from tests.python._support.helpers import autospec_setattr


def _create_manual_job(repo: JobRepository, *, workload_id: str | None = None, status: str = "mounting_output_share"):
    job = repo.create_job(
        JobCreate(
            target_id="ubuntu-22-04-default",
            scenario_id="lab-session",
            execution_mode="manual_trigger",
            workload_id=workload_id,
        )
    )
    repo.update_job(job.id, status=status, current_stage=status)
    return job


def test_wait_for_capture_handoff_persists_deadline_and_returns_without_polling(in_memory_session_scope):
    from astinuslab.live_session.support import wait_for_capture_handoff

    def complete_action(action_id: int | None) -> None:
        if action_id is None:
            return
        with in_memory_session_scope() as session:
            JobRepository(session).update_action(action_id, status="completed")

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job = _create_manual_job(repo)
        action = repo.enqueue_action(job.id, "run_manual_trigger_job")
        job_id = job.id
        action_id = action.id

    before = datetime.now(UTC)
    assert wait_for_capture_handoff(
        in_memory_session_scope,
        JobRepository,
        complete_action,
        job_id,
        {"trigger": {"max_wait_seconds": 90}},
        action_id,
    )
    after = datetime.now(UTC)

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        updated = repo.get_job(job_id)
        assert updated is not None
        assert updated.status == "waiting_for_capture"
        assert updated.current_stage == "waiting_for_capture"
        assert updated.capture_wait_deadline is not None
        deadline = updated.capture_wait_deadline.replace(tzinfo=UTC)
        assert before + timedelta(seconds=90) <= deadline <= after + timedelta(seconds=90)
        updated_action = repo.get_action(action_id)
        assert updated_action is not None
        assert updated_action.status == "completed"


def test_teardown_job_tears_down_target_and_attacker_then_marks_cancelled(in_memory_session_scope, monkeypatch):
    import astinuslab.db as db_mod
    import astinuslab.orchestration.lab_lifecycle as lab_lifecycle_mod
    from astinuslab.jobs import celery_app as celery_mod

    monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(celery_mod, "session_scope", in_memory_session_scope, raising=False)

    torn_down: list[str] = []
    autospec_setattr(monkeypatch, lab_lifecycle_mod, "teardown_vm", lambda vm_name: torn_down.append(vm_name))

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job = _create_manual_job(repo, status="waiting_for_capture")
        repo.transition_job_status(job.id, "cancel_requested")
        job_id = job.id

    celery_mod.teardown_job_task(job_id, "cancelled")

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        updated = repo.get_job(job_id)
        assert updated is not None
        assert updated.status == "cancelled"
        assert updated.finished_at is not None
        events = [(event.state, event.message) for event in repo.list_events(job_id)]

    assert torn_down == [job_id, f"{job_id}-attacker"]
    assert ("cancelled", "Job cancelled") in events


def test_cancel_waiting_job_dispatches_teardown_continuation(in_memory_session_scope, monkeypatch):
    import astinuslab.db as db_mod
    import astinuslab.jobs.job_control as job_control

    monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)

    dispatched: list[tuple[str, str]] = []
    monkeypatch.setattr(
        job_control,
        "_teardown_job_task",
        SimpleNamespace(delay=lambda job_id, final_status: dispatched.append((job_id, final_status))),
        raising=False,
    )

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job = _create_manual_job(repo, status="waiting_for_capture")
        action = repo.enqueue_action(job.id, "run_manual_trigger_job")
        repo.update_action(action.id, status="running")
        job_id = job.id
        action_id = action.id

    assert job_control.request_job_cancel(job_id) is True

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        updated = repo.get_job(job_id)
        assert updated is not None
        assert updated.status == "cancel_requested"
        updated_action = repo.get_action(action_id)
        assert updated_action is not None
        assert updated_action.status == "cancel_requested"
        events = [(event.state, event.message) for event in repo.list_events(job_id)]

    assert dispatched == [(job_id, "cancelled")]
    assert ("cancel_requested", "Cancellation request received") in events


def test_expire_waiting_captures_auto_captures_workload_jobs(in_memory_session_scope, monkeypatch):
    import astinuslab.db as db_mod
    from astinuslab.jobs import celery_app as celery_mod

    monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)

    delayed: list[tuple[str, int]] = []

    def _delay_capture(job_id: str, action_id: int):
        delayed.append((job_id, action_id))
        return SimpleNamespace(id="task-1")

    monkeypatch.setattr(
        celery_mod,
        "TASK_MAP",
        {"acquire_memory_dump": SimpleNamespace(delay=_delay_capture)},
    )

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job = _create_manual_job(repo, workload_id="wordpress", status="waiting_for_capture")
        repo.update_job(job.id, capture_wait_deadline=datetime.now(UTC) - timedelta(seconds=1))
        job_id = job.id

    assert celery_mod.expire_waiting_captures_task() == {"auto_captured": 1, "failed": 0, "skipped": 0}

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        updated = repo.get_job(job_id)
        assert updated is not None
        assert updated.status == "capture_requested"
        actions = [action for action in repo.list_actions(job_id) if action.action_type == "acquire_memory_dump"]
        action_ids = [action.id for action in actions]
        events = [(event.state, event.message) for event in repo.list_events(job_id)]

    assert len(actions) == 1
    assert delayed == [(job_id, action_ids[0])]
    assert ("capture_requested", "Lab session timer expired — auto-capturing memory") in events
    assert ("action_queued", "Host action queued: acquire_memory_dump") in events
    assert (
        "capture_requested",
        "Capture request received; waiting job will hand off to acquire_memory_dump",
    ) in events


def test_expire_waiting_captures_fails_non_workload_jobs_and_dispatches_teardown(in_memory_session_scope, monkeypatch):
    import astinuslab.db as db_mod
    from astinuslab.jobs import celery_app as celery_mod

    monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)
    dispatched: list[tuple[str, str]] = []
    monkeypatch.setattr(
        celery_mod,
        "teardown_job_task",
        SimpleNamespace(delay=lambda job_id, final_status: dispatched.append((job_id, final_status))),
    )

    deadline = datetime.now(UTC) - timedelta(seconds=121)
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job = _create_manual_job(repo, status="waiting_for_capture")
        event = repo.add_event(job.id, "waiting_for_capture", "Guest ready. Waiting for capture request (max 120s)")
        event.created_at = deadline - timedelta(seconds=120)
        repo.update_job(job.id, capture_wait_deadline=deadline)
        job_id = job.id

    assert celery_mod.expire_waiting_captures_task() == {"auto_captured": 0, "failed": 1, "skipped": 0}

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        updated = repo.get_job(job_id)
        assert updated is not None
        assert updated.status == "failed"
        events = [(event.state, event.message) for event in repo.list_events(job_id)]

    assert dispatched == [(job_id, "failed")]
    assert ("failed", "Timed out after 120s without receiving a capture request") in events


def test_expire_waiting_captures_skips_when_manual_trigger_holds_capture_lock(
    in_memory_session_scope, monkeypatch, tmp_path
):
    import astinuslab.db as db_mod
    from astinuslab.core.locks import capture_lock
    from astinuslab.jobs import celery_app as celery_mod

    monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)
    autospec_setattr(monkeypatch, celery_mod, "_capture_wait_lock_dir", lambda: tmp_path, raising=False)

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job = _create_manual_job(repo, workload_id="wordpress", status="waiting_for_capture")
        repo.update_job(job.id, capture_wait_deadline=datetime.now(UTC) - timedelta(seconds=1))
        job_id = job.id

    with capture_lock(job_id, base_dir=tmp_path):
        assert celery_mod.expire_waiting_captures_task() == {"auto_captured": 0, "failed": 0, "skipped": 1}

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        updated = repo.get_job(job_id)
        assert updated is not None
        assert updated.status == "waiting_for_capture"
        assert [a for a in repo.list_actions(job_id) if a.action_type == "acquire_memory_dump"] == []
