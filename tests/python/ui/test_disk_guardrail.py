"""Disk-pressure guardrail enforced at job creation."""

from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from astinuslab import app_settings as app_settings_mod
from astinuslab.ui import job_mutations


def _set_min_free(monkeypatch, gib: int) -> None:
    monkeypatch.setattr(app_settings_mod, "load_app_settings", lambda: {"disk_guardrail": {"min_free_gib": gib}})


def _set_free(monkeypatch, gib: float) -> None:
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: SimpleNamespace(free=int(gib * 1024**3)))


def test_disabled_guardrail_never_blocks(monkeypatch):
    _set_min_free(monkeypatch, 0)
    _set_free(monkeypatch, 1)  # almost full, but guardrail off
    job_mutations._enforce_disk_guardrail()  # does not raise


def test_blocks_when_below_threshold(monkeypatch):
    _set_min_free(monkeypatch, 50)
    _set_free(monkeypatch, 10)
    with pytest.raises(HTTPException) as excinfo:
        job_mutations._enforce_disk_guardrail()
    assert excinfo.value.status_code == 507
    assert "guardrail" in excinfo.value.detail.lower()


def test_allows_when_above_threshold(monkeypatch):
    _set_min_free(monkeypatch, 50)
    _set_free(monkeypatch, 120)
    job_mutations._enforce_disk_guardrail()  # does not raise


def test_probe_failure_does_not_block(monkeypatch):
    _set_min_free(monkeypatch, 50)

    def _boom(_p):
        raise OSError("cannot stat")

    monkeypatch.setattr(shutil, "disk_usage", _boom)
    job_mutations._enforce_disk_guardrail()  # swallows OSError, does not raise


def test_api_job_creation_respects_operator_disk_floor(in_memory_session_scope, available_host_capacity, monkeypatch):
    from astinuslab.api.routes import create_job
    from astinuslab.api.schemas import JobCreate
    from astinuslab.db.models import Job

    _set_min_free(monkeypatch, 20)
    _set_free(monkeypatch, 1)
    payload = JobCreate(target_id="ubuntu-22-04-default", scenario_id="immediate-capture", execution_mode="immediate")
    with in_memory_session_scope() as session:
        before = session.query(Job).count()
        with pytest.raises(HTTPException) as refused:
            create_job(payload, session=session)
        assert refused.value.status_code == 507
        assert session.query(Job).count() == before
