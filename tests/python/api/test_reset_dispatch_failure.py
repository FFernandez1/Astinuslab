from __future__ import annotations

from types import SimpleNamespace

from astinuslab.db.models import Job
from astinuslab.jobs import job_services


def test_reset_returns_503_when_broker_does_not_accept_task(client, created_job, in_memory_session_scope, monkeypatch):
    job_id = created_job["id"]
    with in_memory_session_scope() as session:
        job = session.get(Job, job_id)
        job.status = "waiting_for_capture"
        job.lab_snapshot_name = "lab-clean"

    def unavailable(*args, **kwargs):
        raise RuntimeError("test broker unavailable")

    monkeypatch.setattr(job_services, "revert_lab_snapshot_task", SimpleNamespace(delay=unavailable))
    response = client.post(f"/api/v1/jobs/{job_id}/reset-lab")
    assert response.status_code == 503
    assert response.json()["detail"] == "Task queue unavailable"
    with in_memory_session_scope() as session:
        assert session.get(Job, job_id).status == "waiting_for_capture"
