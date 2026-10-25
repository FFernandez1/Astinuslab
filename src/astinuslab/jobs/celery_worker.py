from __future__ import annotations

# Backward-compatible import shim. Worker-only signal registration lives in
# celery_worker_signals and is imported by celery_main, not by API paths.
from . import celery_app as _celery_app_mod

celery_app = _celery_app_mod.celery_app
TASK_MAP = _celery_app_mod.TASK_MAP


def _dispatch_pending_actions(job_id: str) -> None:
    # Preserve old monkeypatch behavior for tests/importers that patch
    # astinuslab.jobs.celery_worker.TASK_MAP.
    original = _celery_app_mod.TASK_MAP
    _celery_app_mod.TASK_MAP = TASK_MAP
    try:
        _celery_app_mod._dispatch_pending_actions(job_id)
    finally:
        _celery_app_mod.TASK_MAP = original


__all__ = ["TASK_MAP", "celery_app", "_dispatch_pending_actions"]
