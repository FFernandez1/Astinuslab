from __future__ import annotations

import subprocess
import sys


def test_api_import_does_not_register_worker_ready_signal():
    code = """
from celery.signals import worker_ready
import astinuslab.main  # noqa: F401
print(len(worker_ready.receivers))
"""
    result = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)

    assert result.stdout.strip() == "0"


def test_celery_app_is_configured():
    from astinuslab.jobs.celery_worker import celery_app

    assert celery_app.main == "astinuslab"


def test_all_action_types_have_tasks():
    from astinuslab.jobs.celery_worker import TASK_MAP

    required = {
        "run_immediate_job",
        "run_post_script_job",
        "run_manual_trigger_job",
        "acquire_memory_dump",
        "control_runtime",
        "run_vol3_analysis",
    }
    assert required.issubset(set(TASK_MAP.keys()))


def test_reconciliation_task_is_registered_in_beat_schedule():
    from astinuslab.jobs.celery_worker import celery_app

    schedule = celery_app.conf.beat_schedule["reconcile-libvirt-resources"]

    assert schedule["task"] == "reconcile_libvirt_resources"
    assert schedule["schedule"] == 60


def test_dispatch_pending_actions_skips_non_queued(in_memory_session_scope, monkeypatch):
    """_dispatch_pending_actions should not re-dispatch completed actions."""
    import astinuslab.db as db_mod
    import astinuslab.jobs.celery_worker as worker_mod
    from astinuslab.api.schemas import JobCreate
    from astinuslab.db.repositories import JobRepository
    from astinuslab.jobs.job_services import create_job_service

    dispatched: list[str] = []

    class _FakeTask:
        def delay(self, *args, **kwargs):
            dispatched.append(args[0])
            return type("R", (), {"id": "fake-task-id"})()

    monkeypatch.setattr(worker_mod, "TASK_MAP", {"run_immediate_job": _FakeTask()})
    # Patch at db module level — _dispatch_pending_actions does `from .db import session_scope`
    monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)

    with in_memory_session_scope() as session:
        job = create_job_service(
            session,
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
            ),
        )
        repo = JobRepository(session)
        # Mark the existing queued action as completed so nothing gets re-dispatched
        for action in repo.list_actions(job.id):
            repo.update_action(action.id, status="completed")
        job_id = job.id

    worker_mod._dispatch_pending_actions(job_id)

    assert dispatched == []


def test_dispatch_pending_actions_persists_followup_task_id(in_memory_session_scope, monkeypatch):
    """Follow-up actions dispatched by the worker must update job.celery_task_id."""
    import astinuslab.db as db_mod
    import astinuslab.jobs.celery_worker as worker_mod
    from astinuslab.api.schemas import JobCreate
    from astinuslab.db.repositories import JobRepository
    from astinuslab.jobs.job_services import create_job_service

    class _FakeResult:
        id = "followup-task-id-123"

    class _FakeTask:
        def delay(self, *args, **kwargs):
            return _FakeResult()

    monkeypatch.setattr(worker_mod, "TASK_MAP", {"acquire_memory_dump": _FakeTask()})
    monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)

    with in_memory_session_scope() as session:
        job = create_job_service(
            session,
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
            ),
        )
        repo = JobRepository(session)
        for action in repo.list_actions(job.id):
            repo.update_action(action.id, status="completed")
        repo.enqueue_action(job.id, "acquire_memory_dump")
        repo.update_job(job.id, celery_task_id=None)
        job_id = job.id

    worker_mod._dispatch_pending_actions(job_id)

    with in_memory_session_scope() as session:
        stored = JobRepository(session).get_job(job_id)
        assert stored is not None
        assert stored.celery_task_id == "followup-task-id-123"
