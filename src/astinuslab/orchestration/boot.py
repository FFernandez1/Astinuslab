"""Entry-point shim: lazy imports here defer the runner import until a Celery task executes."""

from __future__ import annotations


def run_immediate_job(job_id: str, action_id: int | None = None) -> None:
    from .runners.boot_capture_runner import run_immediate_job as _run_immediate_job

    _run_immediate_job(job_id, action_id=action_id)


def run_post_script_job(job_id: str, action_id: int | None = None) -> None:
    from .runners.boot_capture_runner import run_post_script_job as _run_post_script_job

    _run_post_script_job(job_id, action_id=action_id)
