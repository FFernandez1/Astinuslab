"""Tests for ISF-only action payload semantics."""

from __future__ import annotations

import json

from astinuslab.api.schemas import JobCreate
from astinuslab.db.repositories import JobRepository


def _make_job_with_action(scope, *, mode="immediate", scenario_id="immediate-capture", build_vol3_isf=False):
    """Create a job with its initial action payload for inspection."""
    with scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id=scenario_id,
                execution_mode=mode,
                build_vol3_isf=build_vol3_isf,
            )
        )
        payload = {"build_vol3_isf": True, "isf_only": True} if build_vol3_isf else {}
        action_type = {
            "immediate": "run_immediate_job",
            "post_script": "run_post_script_job",
        }.get(mode, "run_manual_trigger_job")
        action = repo.enqueue_action(job.id, action_type, payload)
        return job.id, action.id


class TestISFOnlyInitialPayload:
    def test_immediate_payload_marks_isf_only_when_flag_true(self, in_memory_session_scope):
        job_id, action_id = _make_job_with_action(in_memory_session_scope, build_vol3_isf=True)

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            action = repo.get_action(action_id)
            _payload = json.loads(action.payload or "{}") if action else {}

        assert _payload.get("build_vol3_isf") is True
        assert _payload.get("isf_only") is True

    def test_post_script_payload_marks_isf_only_when_flag_true(self, in_memory_session_scope):
        _job_id, action_id = _make_job_with_action(
            in_memory_session_scope,
            mode="post_script",
            scenario_id="workload-sample-post-script",
            build_vol3_isf=True,
        )

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            action = repo.get_action(action_id)
            _payload = json.loads(action.payload or "{}") if action else {}

        assert _payload.get("build_vol3_isf") is True
        assert _payload.get("isf_only") is True

    def test_immediate_payload_omits_isf_only_when_flag_false(self, in_memory_session_scope):
        _job_id, action_id = _make_job_with_action(in_memory_session_scope, build_vol3_isf=False)

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            action = repo.get_action(action_id)
            _payload = json.loads(action.payload or "{}") if action else {}

        assert "isf_only" not in _payload


class TestAcquireMemoryDumpNoISF:
    def test_acquire_memory_dump_action_payload_has_no_isf_only_flag(self, in_memory_session_scope):
        """Verify that acquire_memory_dump actions never carry the ISF-only flag."""
        from astinuslab.forensics.services import enqueue_acquire_memory_dump_service

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job = repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="lab-session",
                    execution_mode="manual_trigger",
                )
            )
            repo.force_transition(
                job.id,
                "waiting_for_capture",
                started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            )
            job_id = job.id

        with in_memory_session_scope() as session:
            enqueue_acquire_memory_dump_service(session, job_id)

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            actions = repo.list_actions(job_id)
            acquire_actions = [a for a in actions if a.action_type == "acquire_memory_dump"]
            assert len(acquire_actions) == 1
            payload = json.loads(acquire_actions[0].payload or "{}")
            assert "isf_only" not in payload
