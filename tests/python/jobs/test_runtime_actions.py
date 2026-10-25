"""Unit tests for runtime_actions — control_runtime and refresh_runtime."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from astinuslab.api.schemas import JobCreate
from astinuslab.db.repositories import JobRepository
from astinuslab.jobs.runtime_actions import control_runtime, refresh_runtime
from tests.python._support.helpers import autospec_setattr


def _create_job(scope):
    with scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
            )
        )
        return job.id


class FakeSettings:
    libvirt_uri = "qemu:///system"


class TestControlRuntime:
    def test_shutdown_calls_virsh_shutdown(self, in_memory_session_scope, monkeypatch):
        import time

        autospec_setattr(monkeypatch, time, "sleep", lambda _: None)
        job_id = _create_job(in_memory_session_scope)
        run_cmd = MagicMock()
        refresh_fn = MagicMock(return_value={"power_state": "off", "role": "target"})

        control_runtime(
            in_memory_session_scope,
            JobRepository,
            FakeSettings(),
            run_cmd,
            refresh_fn,
            job_id,
            "target",
            "shutdown",
        )

        expected_cmd = ["virsh", "-c", "qemu:///system", "shutdown", f"astinuslab-{job_id}"]
        run_cmd.assert_called_once_with(expected_cmd, job_id=job_id)

    def test_reboot_calls_virsh_reboot(self, in_memory_session_scope, monkeypatch):
        import time

        autospec_setattr(monkeypatch, time, "sleep", lambda _: None)
        job_id = _create_job(in_memory_session_scope)
        run_cmd = MagicMock()
        refresh_fn = MagicMock(return_value={"power_state": "on", "role": "target"})

        control_runtime(
            in_memory_session_scope,
            JobRepository,
            FakeSettings(),
            run_cmd,
            refresh_fn,
            job_id,
            "target",
            "reboot",
        )

        args = run_cmd.call_args[0][0]
        assert "reboot" in args

    def test_raises_for_unsupported_action(self, in_memory_session_scope):
        with pytest.raises(ValueError, match="Unsupported runtime action"):
            control_runtime(
                in_memory_session_scope,
                JobRepository,
                FakeSettings(),
                MagicMock(),
                MagicMock(),
                "job-1",
                "target",
                "explode",
            )

    def test_pause_raises_unsupported(self, in_memory_session_scope):
        with pytest.raises(ValueError, match="Unsupported runtime action"):
            control_runtime(
                in_memory_session_scope,
                JobRepository,
                FakeSettings(),
                MagicMock(),
                MagicMock(),
                "job-1",
                "target",
                "pause",
            )

    def test_attacker_role_uses_attacker_vm_name(self, in_memory_session_scope, monkeypatch):
        import time

        autospec_setattr(monkeypatch, time, "sleep", lambda _: None)
        job_id = _create_job(in_memory_session_scope)
        run_cmd = MagicMock()
        refresh_fn = MagicMock(return_value={"power_state": "off", "role": "attacker"})

        control_runtime(
            in_memory_session_scope,
            JobRepository,
            FakeSettings(),
            run_cmd,
            refresh_fn,
            job_id,
            "attacker",
            "shutdown",
        )

        args = run_cmd.call_args[0][0]
        assert args[-1] == f"astinuslab-{job_id}-attacker"


class TestRefreshRuntime:
    def test_returns_runtime_dict_for_running_vm(self, in_memory_session_scope):
        from astinuslab.jobs.runtime_support import sync_runtime_record as real_sync

        job_id = _create_job(in_memory_session_scope)
        result = refresh_runtime(
            in_memory_session_scope,
            JobRepository,
            lambda jid, role: f"astinuslab-{jid}",
            lambda vm_name: "on",
            lambda jid: "10.0.0.1",
            lambda *a, **k: real_sync(in_memory_session_scope, JobRepository, *a, **k),
            job_id,
            "target",
        )
        assert result is not None
        assert result["power_state"] == "on"
        assert result["ip_address"] == "10.0.0.1"

    def test_returns_none_when_runtime_record_missing(self, in_memory_session_scope):
        # Use a job_id that has no runtime record
        result = refresh_runtime(
            in_memory_session_scope,
            JobRepository,
            lambda jid, role: f"astinuslab-{jid}",
            lambda vm_name: "off",
            lambda jid: None,
            lambda *a, **k: None,
            "nonexistent-job",
            "target",
        )
        assert result is None

    def test_sets_state_stopped_when_power_off(self, in_memory_session_scope):
        from astinuslab.jobs.runtime_support import sync_runtime_record as real_sync

        job_id = _create_job(in_memory_session_scope)
        result = refresh_runtime(
            in_memory_session_scope,
            JobRepository,
            lambda jid, role: f"astinuslab-{jid}",
            lambda vm_name: "off",
            lambda jid: None,
            lambda *a, **k: real_sync(in_memory_session_scope, JobRepository, *a, **k),
            job_id,
            "target",
        )
        assert result is not None
        assert result["state"] == "stopped"
        assert result["power_state"] == "off"


@pytest.mark.parametrize("observed_power,expected_status", [("off", "waiting_for_capture"), ("unknown", "failed")])
def test_refresh_preserves_defined_stopped_target_for_offline_capture(
    in_memory_session_scope, observed_power, expected_status
):
    from astinuslab.db.models import CapacityReservation
    from astinuslab.jobs.runtime_support import sync_runtime_record as real_sync

    job_id = _create_job(in_memory_session_scope)
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        repo.update_job(job_id, status="waiting_for_capture", current_stage="waiting_for_capture")
        session.add(
            CapacityReservation(job_id=job_id, vm_count=1, memory_mb=2048, storage_bytes=1024, state="reserved")
        )
    refresh_runtime(
        in_memory_session_scope,
        JobRepository,
        lambda jid, role: f"astinuslab-{jid}",
        lambda _: observed_power,
        lambda _: None,
        lambda *a, **k: real_sync(in_memory_session_scope, JobRepository, *a, **k),
        job_id,
        "target",
    )
    with in_memory_session_scope() as session:
        job = JobRepository(session).get_job(job_id)
        assert job is not None
        assert job.status == expected_status
        assert session.get(CapacityReservation, job_id).state == "reserved"
        if observed_power == "off":
            assert job.current_stage == "target_stopped"
            assert job.finished_at is None
