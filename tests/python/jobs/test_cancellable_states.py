"""Cancellation policy: explicit cancellable-states set (B04)."""

from __future__ import annotations

import pytest

from astinuslab.api.schemas import JobCreate
from astinuslab.db.repositories import JobRepository
from astinuslab.jobs import job_control
from astinuslab.jobs.job_status import ALLOWED_TRANSITIONS, CANCELLABLE_STATUSES, TERMINAL_STATUSES


def _make_job(scope, status):
    with scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
            )
        )
        # Walk allowed edges from "queued" to the target status (BFS) so the
        # row reaches capture phases without bypassing the state machine.
        if status != "queued":
            path = _shortest_path("queued", status)
            for step in path[1:]:
                repo.transition_job_status(job.id, step)
        return job.id


def _shortest_path(start, goal):
    frontier = [[start]]
    seen = {start}
    while frontier:
        path = frontier.pop(0)
        if path[-1] == goal:
            return path
        for nxt in ALLOWED_TRANSITIONS.get(path[-1], set()):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append([*path, nxt])
    raise AssertionError(f"no path from {start} to {goal}")


def test_cancellable_set_matches_graph():
    assert CANCELLABLE_STATUSES == {
        "queued",
        "waiting_for_capacity",
        "preparing_image",
        "waiting_for_guest_access",
        "mounting_output_share",
        "generating_isf",
        "running_scenario_script",
        "provisioning_workload",
        "waiting_for_capture",
    }
    assert CANCELLABLE_STATUSES.isdisjoint(TERMINAL_STATUSES)


@pytest.mark.parametrize("status", ["capture_requested", "acquiring_memory", "acquiring_disk", "publishing_artifact"])
def test_capture_phases_refuse_cancel_without_raising(in_memory_session_scope, monkeypatch, status):
    monkeypatch.setattr(job_control.db, "session_scope", in_memory_session_scope)
    job_id = _make_job(in_memory_session_scope, status)

    assert job_control.request_job_cancel(job_id) is False

    with in_memory_session_scope() as session:
        job = JobRepository(session).get_job(job_id)
        assert job is not None
        assert job.status == status  # untouched — refusal happened before any mutation


def test_cancellable_state_still_cancels(in_memory_session_scope, monkeypatch):
    monkeypatch.setattr(job_control.db, "session_scope", in_memory_session_scope)
    job_id = _make_job(in_memory_session_scope, "preparing_image")

    assert job_control.request_job_cancel(job_id) is True

    with in_memory_session_scope() as session:
        job = JobRepository(session).get_job(job_id)
        assert job is not None
        assert job.status == "cancel_requested"


def test_cancel_requested_is_idempotent(in_memory_session_scope, monkeypatch):
    monkeypatch.setattr(job_control.db, "session_scope", in_memory_session_scope)
    job_id = _make_job(in_memory_session_scope, "preparing_image")
    assert job_control.request_job_cancel(job_id) is True
    assert job_control.request_job_cancel(job_id) is True
