from __future__ import annotations

import pytest

from astinuslab.db.repositories import JobRepository


@pytest.fixture(autouse=True)
def _pinned_host_capacity(available_host_capacity):
    """Job admission in this module must not depend on the host's free RAM or disk."""


def test_full_job_lifecycle(client, in_memory_session_scope):
    # 1. Create job
    resp = client.post(
        "/api/v1/jobs",
        json={
            "target_id": "ubuntu-22-04-default",
            "scenario_id": "immediate-capture",
            "execution_mode": "immediate",
        },
    )
    assert resp.status_code == 201
    job_id = resp.json()["id"]

    # 2. Verify initial state in DB and API
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job = repo.get_job(job_id)
        assert job is not None
        assert job.status == "queued"

    resp = client.get(f"/api/v1/jobs/{job_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"

    # 3. Drive the real lifecycle path through legal repository transitions.
    for status in (
        "preparing_image",
        "waiting_for_guest_access",
        "mounting_output_share",
        "acquiring_memory",
        "publishing_artifact",
        "completed",
    ):
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job = repo.transition_job_status(job_id, status)
            assert job.status == status

        resp = client.get(f"/api/v1/jobs/{job_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == status
