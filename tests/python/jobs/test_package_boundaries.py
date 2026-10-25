from __future__ import annotations

import importlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


JOB_MODULES = (
    "job_status",
    "job_state_support",
    "job_control",
    "job_services",
    "runtime_actions",
    "runtime_control",
    "runtime_support",
    "celery_app",
    "celery_main",
    "celery_worker",
    "celery_worker_signals",
    "startup_recovery",
)


def test_job_implementations_are_owned_by_the_jobs_package() -> None:
    for name in JOB_MODULES:
        module = importlib.import_module(f"astinuslab.jobs.{name}")
        assert module.__file__ is not None
        assert Path(module.__file__).parent.name == "jobs"


def test_root_job_imports_are_removed() -> None:
    for name in JOB_MODULES:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(f"astinuslab.{name}")


def test_celery_task_names_remain_stable_after_packaging() -> None:
    celery_app = importlib.import_module("astinuslab.jobs.celery_app").celery_app
    expected = {
        "run_immediate_job",
        "run_post_script_job",
        "run_manual_trigger_job",
        "acquire_memory_dump",
        "control_runtime",
        "run_vol3_analysis",
        "run_vol3_plugin",
        "run_retention",
        "reconcile_libvirt_resources",
        "teardown_job",
        "revert_lab_snapshot",
        "expire_waiting_captures",
    }
    assert expected <= set(celery_app.tasks)
