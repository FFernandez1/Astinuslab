"""Tests for Job status state machine: transition_job_status()."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from astinuslab.api.schemas import JobCreate
from astinuslab.db.repositories import InvalidJobTransitionError, JobRepository


def _payload(**kwargs) -> JobCreate:
    defaults: dict[str, Any] = dict(
        target_id="ubuntu-22-04-default",
        scenario_id="immediate-capture",
        execution_mode="immediate",
    )
    defaults.update(kwargs)
    return JobCreate(**defaults)


def _make_job(repo: JobRepository, *, status: str = "queued") -> str:
    job = repo.create_job(_payload())
    if status != "queued":
        # Bypass transition guard to seed arbitrary status for testing
        job.status = status
        job.current_stage = status
        repo.session.flush()
    return job.id


class TestInvalidJobTransitionError:
    def test_is_value_error(self):
        err = InvalidJobTransitionError("queued", "completed")
        assert isinstance(err, ValueError)

    def test_message_contains_statuses(self):
        err = InvalidJobTransitionError("queued", "completed")
        assert "queued" in str(err)
        assert "completed" in str(err)

    def test_exposes_from_and_to_attributes(self):
        err = InvalidJobTransitionError("preparing_image", "failed")
        assert err.from_status == "preparing_image"
        assert err.to_status == "failed"


class TestTransitionJobStatus:
    def test_valid_transition_updates_status(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job_id = _make_job(repo, status="queued")
            repo.transition_job_status(job_id, "preparing_image")
            job = repo.get_job(job_id)
            assert job is not None
            assert job.status == "preparing_image"

    def test_valid_transition_updates_current_stage(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job_id = _make_job(repo, status="queued")
            repo.transition_job_status(job_id, "preparing_image")
            job = repo.get_job(job_id)
            assert job is not None
            assert job.current_stage == "preparing_image"

    def test_valid_transition_writes_extra_fields(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job_id = _make_job(repo, status="publishing_artifact")
            now = datetime.now(UTC)
            repo.transition_job_status(
                job_id,
                "completed",
                finished_at=now,
                error_summary="wrapped up",
            )
            job = repo.get_job(job_id)
            assert job is not None
            assert job.status == "completed"
            assert job.error_summary == "wrapped up"
            assert job.finished_at is not None

    def test_invalid_transition_raises(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job_id = _make_job(repo, status="queued")
            with pytest.raises(InvalidJobTransitionError):
                repo.transition_job_status(job_id, "completed")

    def test_terminal_to_any_raises(self, in_memory_session_scope):
        for terminal in ("completed", "failed", "cancelled"):
            with in_memory_session_scope() as session:
                repo = JobRepository(session)
                job_id = _make_job(repo, status=terminal)
                with pytest.raises(InvalidJobTransitionError):
                    repo.transition_job_status(job_id, "failed")

    def test_terminal_to_same_raises(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job_id = _make_job(repo, status="completed")
            with pytest.raises(InvalidJobTransitionError):
                repo.transition_job_status(job_id, "completed")

    @pytest.mark.parametrize(
        "status",
        [
            "queued",
            "preparing_image",
            "waiting_for_guest_access",
            "mounting_output_share",
            "running_scenario_script",
            "provisioning_workload",
            "waiting_for_capture",
            "capture_requested",
            "acquiring_memory",
            "acquiring_disk",
            "generating_isf",
            "publishing_artifact",
            "cancel_requested",
        ],
    )
    def test_self_transition_rejected(self, in_memory_session_scope, status):
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job_id = _make_job(repo, status=status)
            with pytest.raises(InvalidJobTransitionError):
                repo.transition_job_status(job_id, status, error_summary="should not persist")
            job = repo.get_job(job_id)
            assert job is not None
            assert job.status == status
            assert job.error_summary is None

    def test_job_not_found_raises(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            with pytest.raises(ValueError, match="not found"):
                repo.transition_job_status("nonexistent-id", "preparing_image")

    def test_cancel_from_preparing_image_is_valid(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job_id = _make_job(repo, status="preparing_image")
            repo.transition_job_status(job_id, "cancel_requested")
            job = repo.get_job(job_id)
            assert job is not None
            assert job.status == "cancel_requested"

    def test_failed_from_waiting_for_capture_is_valid(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job_id = _make_job(repo, status="waiting_for_capture")
            now = datetime.now(UTC)
            repo.transition_job_status(
                job_id,
                "failed",
                error_summary="boom",
                finished_at=now,
            )
            job = repo.get_job(job_id)
            assert job is not None
            assert job.status == "failed"
            assert job.error_summary == "boom"

    def test_returns_updated_job(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job_id = _make_job(repo, status="queued")
            result = repo.transition_job_status(job_id, "preparing_image")
            assert result.status == "preparing_image"

    @pytest.mark.parametrize(
        "from_status, to_status",
        [
            ("queued", "preparing_image"),
            ("queued", "cancel_requested"),
            ("queued", "cancelled"),
            ("queued", "failed"),
            ("preparing_image", "waiting_for_guest_access"),
            ("preparing_image", "cancel_requested"),
            ("preparing_image", "failed"),
            ("waiting_for_guest_access", "mounting_output_share"),
            ("waiting_for_guest_access", "cancel_requested"),
            ("waiting_for_guest_access", "failed"),
            ("mounting_output_share", "running_scenario_script"),
            ("mounting_output_share", "provisioning_workload"),
            ("mounting_output_share", "waiting_for_capture"),
            ("mounting_output_share", "cancel_requested"),
            ("mounting_output_share", "failed"),
            ("running_scenario_script", "waiting_for_capture"),
            ("running_scenario_script", "acquiring_memory"),
            ("running_scenario_script", "acquiring_disk"),
            ("running_scenario_script", "cancel_requested"),
            ("running_scenario_script", "failed"),
            ("provisioning_workload", "waiting_for_capture"),
            ("provisioning_workload", "cancel_requested"),
            ("provisioning_workload", "failed"),
            ("waiting_for_capture", "capture_requested"),
            ("waiting_for_capture", "acquiring_memory"),
            ("waiting_for_capture", "acquiring_disk"),
            ("waiting_for_capture", "cancel_requested"),
            ("waiting_for_capture", "failed"),
            ("capture_requested", "acquiring_memory"),
            ("capture_requested", "acquiring_disk"),
            ("capture_requested", "cancelled"),
            ("capture_requested", "failed"),
            ("acquiring_memory", "publishing_artifact"),
            ("acquiring_memory", "acquiring_disk"),
            ("acquiring_memory", "cancelled"),
            ("acquiring_memory", "failed"),
            ("acquiring_disk", "publishing_artifact"),
            ("acquiring_disk", "cancelled"),
            ("acquiring_disk", "failed"),
            ("publishing_artifact", "completed"),
            ("publishing_artifact", "cancelled"),
            ("publishing_artifact", "failed"),
            ("cancel_requested", "cancelled"),
            ("cancel_requested", "failed"),
        ],
    )
    def test_all_valid_transitions(self, in_memory_session_scope, from_status, to_status):
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job_id = _make_job(repo, status=from_status)
            repo.transition_job_status(job_id, to_status)
            job = repo.get_job(job_id)
            assert job is not None
            assert job.status == to_status
