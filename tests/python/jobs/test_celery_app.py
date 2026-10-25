from __future__ import annotations

import os
import subprocess
import sys
from types import ModuleType, SimpleNamespace

from tests.python._support.helpers import autospec_setattr


def test_celery_eager_env_warning_emits_only_when_active():
    cmd = [
        sys.executable,
        "-c",
        "import logging; logging.basicConfig(level=logging.WARNING); import astinuslab.jobs.celery_app",
    ]
    eager_env = {**os.environ, "CELERY_TASK_ALWAYS_EAGER": "1"}
    eager = subprocess.run(cmd, env=eager_env, capture_output=True, text=True, check=True)
    assert "CELERY_TASK_ALWAYS_EAGER is active" in eager.stderr

    normal_env = {k: v for k, v in os.environ.items() if k != "CELERY_TASK_ALWAYS_EAGER"}
    normal = subprocess.run(cmd, env=normal_env, capture_output=True, text=True, check=True)
    assert "CELERY_TASK_ALWAYS_EAGER is active" not in normal.stderr


def test_task_map_contains_expected_action_names():
    from astinuslab.jobs import celery_app as celery_mod

    assert set(celery_mod.TASK_MAP) == {
        "run_immediate_job",
        "run_post_script_job",
        "run_manual_trigger_job",
        "acquire_memory_dump",
        "control_runtime",
        "run_vol3_analysis",
        "run_vol3_plugin",
        "cleanup_job_resources",
    }
    assert celery_mod.TASK_MAP["control_runtime"].name == "control_runtime"


def test_beat_schedule_contains_hourly_retention():
    from astinuslab.jobs import celery_app as celery_mod

    entry = celery_mod.celery_app.conf.beat_schedule["run-retention"]

    assert entry == {"task": "run_retention", "schedule": 3600}


def test_interactive_plugin_task_routes_to_analysis_queue():
    from astinuslab.jobs.celery_app import celery_app

    assert celery_app.conf.task_routes == {"run_vol3_plugin": {"queue": "analysis"}}


def test_run_retention_task_skips_when_disabled(monkeypatch):
    from astinuslab import app_settings as app_settings_mod
    from astinuslab.jobs import celery_app as celery_mod

    autospec_setattr(monkeypatch, app_settings_mod, "load_app_settings", lambda: {"retention": {"auto_enabled": False}})

    assert celery_mod.run_retention_task() == {"skipped": True}


def test_run_retention_task_runs_when_enabled(monkeypatch):
    from astinuslab import app_settings as app_settings_mod
    from astinuslab.jobs import celery_app as celery_mod
    from astinuslab.storage import retention as retention_mod

    autospec_setattr(monkeypatch, app_settings_mod, "load_app_settings", lambda: {"retention": {"auto_enabled": True}})
    autospec_setattr(monkeypatch, retention_mod, "run_retention", lambda: {"dumps_deleted": 2, "logs_deleted": 1})

    assert celery_mod.run_retention_task() == {"dumps_deleted": 2, "logs_deleted": 1}


def test_run_retention_task_swallows_errors(monkeypatch):
    from astinuslab import app_settings as app_settings_mod
    from astinuslab.jobs import celery_app as celery_mod
    from astinuslab.storage import retention as retention_mod

    autospec_setattr(monkeypatch, app_settings_mod, "load_app_settings", lambda: {"retention": {"auto_enabled": True}})

    def broken():
        raise RuntimeError("boom")

    monkeypatch.setattr(retention_mod, "run_retention", broken)

    assert celery_mod.run_retention_task() == {"error": True}


def test_control_runtime_task_calls_runtime_action(monkeypatch, in_memory_session_scope):
    from astinuslab.jobs import celery_app as celery_mod
    from astinuslab.jobs import runtime_control as runtime_mod

    calls: list[tuple[str, int]] = []

    def fake_run_control_runtime_action(job_id: str, action_id: int) -> None:
        calls.append((job_id, action_id))

    monkeypatch.setattr(runtime_mod, "run_control_runtime_action", fake_run_control_runtime_action)

    from astinuslab import db
    from astinuslab.api.schemas import JobCreate
    from astinuslab.db.repositories import JobRepository

    monkeypatch.setattr(db, "session_scope", in_memory_session_scope)
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(JobCreate(target_id="fixture", scenario_id="fixture", execution_mode="manual_trigger"))
        action = repo.enqueue_action(job.id, "control_runtime")
        job_id, action_id = job.id, action.id
    celery_mod.control_runtime_task(job_id, action_id)

    assert calls == [(job_id, action_id)]


def test_control_runtime_task_without_action_id_logs_and_returns(monkeypatch, caplog):
    from astinuslab.jobs import celery_app as celery_mod
    from astinuslab.jobs import runtime_control as runtime_mod

    calls: list[tuple[str, int]] = []
    autospec_setattr(
        monkeypatch,
        runtime_mod,
        "run_control_runtime_action",
        lambda job_id, action_id: calls.append((job_id, action_id)),
    )

    celery_mod.control_runtime_task("job-1", None)

    assert calls == []
    assert "control_runtime_task called without action_id" in caplog.text


def test_reconcile_libvirt_resources_task_returns_zero_summary_on_exception(monkeypatch):
    import astinuslab.playground.reconciliation as reconciliation_mod
    from astinuslab.jobs import celery_app as celery_mod

    fake_libvirt = ModuleType("libvirt")
    monkeypatch.setitem(sys.modules, "libvirt", fake_libvirt)

    def broken_reconcile_libvirt_resources(**kwargs):
        raise RuntimeError("libvirt connection failed")

    monkeypatch.setattr(reconciliation_mod, "reconcile_libvirt_resources", broken_reconcile_libvirt_resources)

    assert celery_mod.reconcile_libvirt_resources_task() == {
        "orphan_domains_destroyed": 0,
        "orphan_networks_destroyed": 0,
        "zombie_vms_failed": 0,
        "zombie_networks_failed": 0,
        "zombie_runtimes_failed": 0,
        "stale_runtimes_released": 0,
        "power_states_synced": 0,
    }


def test_dispatch_pending_actions_dispatches_known_actions_and_skips_unknown(monkeypatch, in_memory_session_scope):
    from astinuslab import db
    from astinuslab.api.schemas import JobCreate
    from astinuslab.db.repositories import JobRepository
    from astinuslab.jobs import celery_app as celery_mod

    delayed = []

    class FakeTask:
        def delay(self, job_id, action_id):
            delayed.append((job_id, action_id))
            return SimpleNamespace(id="task-123")

    monkeypatch.setattr(db, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(celery_mod, "TASK_MAP", {"control_runtime": FakeTask()})
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(JobCreate(target_id="fixture", scenario_id="fixture", execution_mode="manual_trigger"))
        action = repo.enqueue_action(job.id, "control_runtime")
        repo.enqueue_action(job.id, "missing_action")
        finished = repo.enqueue_action(job.id, "run_immediate_job")
        repo.update_action(finished.id, status="completed")
        job_id, action_id = job.id, action.id
    celery_mod._dispatch_pending_actions(job_id)
    assert delayed == [(job_id, action_id)]
    with in_memory_session_scope() as session:
        assert _required(JobRepository(session).get_action(action_id)).celery_task_id == "task-123"


def test_revert_lab_snapshot_task_registered_name():
    from astinuslab.jobs.celery_app import celery_app

    assert "revert_lab_snapshot" in celery_app.tasks


def test_revert_lab_snapshot_task_rechecks_state(in_memory_session_scope, monkeypatch):
    import astinuslab.db as db_mod
    import astinuslab.orchestration.lab_lifecycle as lab_lifecycle_mod
    from astinuslab.api.schemas import JobCreate
    from astinuslab.db.repositories import JobRepository
    from astinuslab.jobs import celery_app as celery_mod

    monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)
    reverted: list[str] = []
    autospec_setattr(monkeypatch, lab_lifecycle_mod, "revert_lab_snapshot", lambda job_id: reverted.append(job_id))

    def make_job(status: str, snapshot: str | None) -> str:
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job = repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="lab-session",
                    execution_mode="manual_trigger",
                    workload_id="wordpress-6.4",
                )
            )
            repo.update_job(job.id, status=status, lab_snapshot_name=snapshot)
            return job.id

    runnable = make_job("waiting_for_capture", "snap-clean")
    completed = make_job("completed", "snap-clean")
    no_snapshot = make_job("waiting_for_capture", None)

    celery_mod.revert_lab_snapshot_task(runnable)
    celery_mod.revert_lab_snapshot_task(completed)
    celery_mod.revert_lab_snapshot_task(no_snapshot)

    assert reverted == [runnable]


def _required[T](value: T | None) -> T:
    assert value is not None
    return value
