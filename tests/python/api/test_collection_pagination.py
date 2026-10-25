from __future__ import annotations

from datetime import UTC, datetime

import pytest

from astinuslab.db.models import Acquisition, Artifact, Case, HostAction, Job, JobEvent, Report

_CREATED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _job(job_id: str) -> Job:
    return Job(
        id=job_id,
        target_id="ubuntu-22-04-default",
        scenario_id="immediate-capture",
        execution_mode="immediate",
        status="completed",
        capture_type="memory",
        created_at=_CREATED,
    )


@pytest.fixture()
def paginated_collections(in_memory_session_scope):
    with in_memory_session_scope() as session:
        session.add_all([_job(job_id) for job_id in ("job-a", "job-b", "job-c")])
        session.add_all(
            [Case(id=f"case-{suffix}", name=suffix, status="open", created_at=_CREATED) for suffix in ("a", "b", "c")]
        )
        session.add_all(
            [
                Acquisition(id=f"acq-{suffix}", name=suffix, status="live", created_at=_CREATED)
                for suffix in ("a", "b", "c")
            ]
        )
        session.add_all(
            [
                Artifact(
                    id=f"art-{suffix}",
                    producer="test",
                    kind="memory",
                    path=f"/tmp/{suffix}.dump",
                    created_at=_CREATED,
                )
                for suffix in ("a", "b", "c")
            ]
        )
        session.add_all(
            [
                Report(
                    id=f"rep-{suffix}",
                    report_type="test",
                    status="completed",
                    path=f"/tmp/{suffix}.json",
                    created_at=_CREATED,
                )
                for suffix in ("a", "b", "c")
            ]
        )
        session.add_all(
            [
                JobEvent(id=event_id, job_id="job-a", state="queued", message=str(event_id), created_at=_CREATED)
                for event_id in (101, 102, 103)
            ]
        )
        session.add_all(
            [
                HostAction(
                    id=action_id,
                    job_id="job-a",
                    action_type="test",
                    status="completed",
                    created_at=_CREATED,
                )
                for action_id in (201, 202, 203)
            ]
        )


@pytest.mark.parametrize(
    ("path", "expected_first", "expected_second"),
    [
        ("/api/v1/jobs", ["job-c", "job-b"], ["job-a"]),
        ("/api/v1/jobs/job-a/logs", [101, 102], [103]),
        ("/api/v1/cases", ["case-c", "case-b"], ["case-a"]),
        ("/api/v1/artifacts", ["art-c", "art-b"], ["art-a"]),
        ("/api/v1/reports", ["rep-c", "rep-b"], ["rep-a"]),
        ("/api/v1/acquisitions", ["acq-c", "acq-b"], ["acq-a"]),
        ("/api/v1/host-actions", [201, 202], [203]),
    ],
)
def test_collection_pages_are_bounded_arrays_with_stable_tie_breakers(
    client,
    paginated_collections,
    path,
    expected_first,
    expected_second,
):
    first = client.get(path, params={"limit": 2})
    second = client.get(path, params={"limit": 2, "offset": 2})

    assert first.status_code == 200
    assert second.status_code == 200
    assert isinstance(first.json(), list)
    assert [row["id"] for row in first.json()] == expected_first
    assert [row["id"] for row in second.json()] == expected_second


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/jobs",
        "/api/v1/jobs/job-a/logs",
        "/api/v1/cases",
        "/api/v1/artifacts",
        "/api/v1/reports",
        "/api/v1/acquisitions",
        "/api/v1/host-actions",
    ],
)
@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": -1}, {"limit": 501}, {"offset": -1}])
def test_collection_pages_reject_invalid_bounds(client, paginated_collections, path, params):
    response = client.get(path, params=params)

    assert response.status_code == 422


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/jobs",
        "/api/v1/jobs/{job_id}/logs",
        "/api/v1/cases",
        "/api/v1/artifacts",
        "/api/v1/reports",
        "/api/v1/acquisitions",
        "/api/v1/host-actions",
    ],
)
def test_collection_openapi_contract_has_shared_default_and_maximum(path, client):
    parameters = client.get("/openapi.json").json()["paths"][path]["get"]["parameters"]
    schemas = {parameter["name"]: parameter["schema"] for parameter in parameters}

    assert schemas["limit"] == {"type": "integer", "maximum": 500, "minimum": 1, "default": 100, "title": "Limit"}
    assert schemas["offset"] == {"type": "integer", "minimum": 0, "default": 0, "title": "Offset"}


def test_jobs_default_page_stops_at_one_hundred(client, in_memory_session_scope):
    with in_memory_session_scope() as session:
        session.add_all([_job(f"job-{index:03d}") for index in range(101)])

    response = client.get("/api/v1/jobs")

    assert response.status_code == 200
    assert len(response.json()) == 100
