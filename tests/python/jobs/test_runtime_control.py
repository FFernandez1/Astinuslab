from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest


class _FakeRuntimeRepo:
    action = SimpleNamespace(
        job_id="job-1",
        action_type="control_runtime",
        status="queued",
        started_at=None,
        payload=json.dumps({"role": "target", "action": "reboot"}),
    )
    job = SimpleNamespace(status="waiting_for_capture")
    runtime = SimpleNamespace(vm_name="astinuslab-job-1")
    updates: list[tuple[int, dict]] = []
    events: list[tuple[str, str, str]] = []

    def __init__(self, session):
        self.session = session

    def get_action(self, action_id: int):
        if action_id == 404:
            return None
        return self.action

    def get_job(self, job_id: str):
        return self.job

    def get_runtime(self, job_id: str, role: str):
        return self.runtime

    def list_actions(self, job_id: str | None = None):
        return []

    def add_event(self, job_id: str, state: str, message: str):
        self.events.append((job_id, state, message))

    def update_action(self, action_id: int, **kwargs):
        self.updates.append((action_id, kwargs))


@contextmanager
def _fake_session_scope():
    yield object()


@pytest.fixture(autouse=True)
def _patch_runtime_repo(monkeypatch, tmp_path):
    from astinuslab.core import config
    from astinuslab.jobs import runtime_control as runtime_mod

    monkeypatch.setattr(config, "settings", config.settings.model_copy(update={"storage_dir": tmp_path / "storage"}))
    _FakeRuntimeRepo.updates = []
    _FakeRuntimeRepo.events = []
    _FakeRuntimeRepo.action = SimpleNamespace(
        job_id="job-1",
        action_type="control_runtime",
        status="queued",
        started_at=None,
        payload=json.dumps({"role": "target", "action": "reboot"}),
    )
    _FakeRuntimeRepo.job = SimpleNamespace(status="waiting_for_capture")
    _FakeRuntimeRepo.runtime = SimpleNamespace(vm_name="astinuslab-job-1")
    monkeypatch.setattr(runtime_mod, "session_scope", _fake_session_scope)
    monkeypatch.setattr(runtime_mod, "JobRepository", _FakeRuntimeRepo)
    yield _FakeRuntimeRepo


def test_run_control_runtime_action_marks_action_completed(monkeypatch):
    from astinuslab.jobs import runtime_control as runtime_mod

    calls: list[tuple[str, str, str, str | None]] = []

    def fake_control_runtime(job_id: str, role: str, action: str, *, vm_name: str | None = None) -> bool:
        calls.append((job_id, role, action, vm_name))
        return True

    monkeypatch.setattr(runtime_mod, "control_runtime", fake_control_runtime)

    runtime_mod.run_control_runtime_action("job-1", 123)

    # The VM acted on comes from the Job's own runtime row, not a rebuilt name.
    assert calls == [("job-1", "target", "reboot", "astinuslab-job-1")]
    assert _FakeRuntimeRepo.updates[0][0] == 123
    assert _FakeRuntimeRepo.updates[0][1]["status"] == "running"
    assert "started_at" in _FakeRuntimeRepo.updates[0][1]
    assert _FakeRuntimeRepo.updates[1][0] == 123
    assert _FakeRuntimeRepo.updates[1][1]["status"] == "completed"
    assert _FakeRuntimeRepo.updates[1][1]["active_pid"] is None
    assert "finished_at" in _FakeRuntimeRepo.updates[1][1]


def test_run_control_runtime_action_marks_action_failed_on_exception(monkeypatch):
    from astinuslab.jobs import runtime_control as runtime_mod

    def fake_control_runtime(job_id: str, role: str, action: str, *, vm_name: str | None = None) -> bool:
        raise RuntimeError("guest refused")

    monkeypatch.setattr(runtime_mod, "control_runtime", fake_control_runtime)

    with pytest.raises(RuntimeError, match="guest refused"):
        runtime_mod.run_control_runtime_action("job-1", 123)

    assert _FakeRuntimeRepo.updates[0][1]["status"] == "running"
    assert _FakeRuntimeRepo.updates[1][0] == 123
    assert _FakeRuntimeRepo.updates[1][1]["status"] == "failed"
    assert _FakeRuntimeRepo.updates[1][1]["error_summary"] == "guest refused"
    assert "finished_at" in _FakeRuntimeRepo.updates[1][1]


def test_run_control_runtime_action_raises_for_missing_action():
    from astinuslab.jobs import runtime_control as runtime_mod

    with pytest.raises(RuntimeError, match="Action 404 not found"):
        runtime_mod.run_control_runtime_action("job-1", 404)

    assert _FakeRuntimeRepo.updates == []


def test_run_control_runtime_action_refuses_a_foreign_vm_identity(monkeypatch):
    """A runtime row naming another domain must not be powered off by this Job."""
    from astinuslab.jobs import runtime_control as runtime_mod

    _FakeRuntimeRepo.runtime = SimpleNamespace(vm_name="astinuslab-someone-elses-vm")
    host_call = []

    def record_call(*args, **kwargs):
        host_call.append(args)
        return True

    monkeypatch.setattr(
        runtime_mod,
        "control_runtime",
        record_call,
    )

    with pytest.raises(runtime_mod.RuntimeControlIdentityError, match="not this job's VM identity"):
        runtime_mod.run_control_runtime_action("job-1", 123)

    assert host_call == []
    assert _FakeRuntimeRepo.updates[1][1]["status"] == "failed"


def test_run_control_runtime_action_rejects_an_unsupported_payload_action(monkeypatch):
    """Pause/resume stay disabled even if an old row still carries them."""
    from astinuslab.jobs import runtime_control as runtime_mod

    _FakeRuntimeRepo.action = SimpleNamespace(
        job_id="job-1",
        action_type="control_runtime",
        status="queued",
        started_at=None,
        payload=json.dumps({"role": "target", "action": "pause"}),
    )
    host_call = []

    def record_call(*args, **kwargs):
        host_call.append(args)
        return True

    monkeypatch.setattr(
        runtime_mod,
        "control_runtime",
        record_call,
    )
    with pytest.raises(RuntimeError, match="unsupported runtime control payload"):
        runtime_mod.run_control_runtime_action("job-1", 123)

    assert host_call == []
    assert _FakeRuntimeRepo.updates[1][1]["status"] == "failed"
