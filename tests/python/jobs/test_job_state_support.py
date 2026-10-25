"""Unit tests for job_state_support — field loading and VM preservation logic."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

from astinuslab.api.schemas import JobCreate
from astinuslab.db.repositories import JobRepository
from astinuslab.jobs.job_state_support import (
    finalize_action_tracking,
    load_job_field,
    should_preserve_failed_vm,
)


def _make_job(session_scope):
    with session_scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
            )
        )
        return job.id


class TestLoadJobField:
    def test_returns_field_value(self, in_memory_session_scope):
        job_id = _make_job(in_memory_session_scope)
        result = load_job_field(in_memory_session_scope, JobRepository, job_id, "target_id")
        assert result == "ubuntu-22-04-default"

    def test_returns_none_for_missing_job(self, in_memory_session_scope):
        result = load_job_field(in_memory_session_scope, JobRepository, "nonexistent-id", "status")
        assert result is None

    def test_returns_none_field_value(self, in_memory_session_scope):
        job_id = _make_job(in_memory_session_scope)
        result = load_job_field(in_memory_session_scope, JobRepository, job_id, "guest_ip")
        assert result is None


class TestShouldPreserveFailedVm:
    def test_preserves_when_keep_failed_vm_true_and_not_cancel(self):
        logger = MagicMock()
        exc = RuntimeError("some real failure")
        assert should_preserve_failed_vm(logger, exc, "job-1", keep_failed_vm=True) is True

    def test_does_not_preserve_when_keep_failed_vm_false(self):
        logger = MagicMock()
        exc = RuntimeError("some real failure")
        assert should_preserve_failed_vm(logger, exc, "job-1", keep_failed_vm=False) is False

    def test_does_not_preserve_on_cancel_requested(self):
        logger = MagicMock()
        exc = RuntimeError("cancel_requested")
        assert should_preserve_failed_vm(logger, exc, "job-1", keep_failed_vm=True) is False

    def test_does_not_preserve_on_sigterm_returncode(self):
        logger = MagicMock()
        exc = subprocess.CalledProcessError(-15, "virsh")
        assert should_preserve_failed_vm(logger, exc, "job-1", keep_failed_vm=True) is False

    def test_logs_info_when_preserving(self):
        logger = MagicMock()
        exc = RuntimeError("disk full")
        should_preserve_failed_vm(logger, exc, "job-1", keep_failed_vm=True)
        logger.info.assert_called_once()


class TestFinalizeActionTracking:
    def test_removes_job_from_dict(self):
        d = {"job-1": 42, "job-2": 99}
        finalize_action_tracking(d, "job-1")
        assert "job-1" not in d
        assert "job-2" in d

    def test_noop_when_job_not_in_dict(self):
        d = {"job-2": 99}
        finalize_action_tracking(d, "job-1")  # must not raise
        assert d == {"job-2": 99}
