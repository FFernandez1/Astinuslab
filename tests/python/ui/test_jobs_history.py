"""Jobs history stays bounded without hiding older or active Jobs."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, insert

from astinuslab.db.models import Job
from astinuslab.db.repositories import JobRepository


def seed_history(scope, count):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with scope() as session:
        session.execute(
            insert(Job),
            [
                {
                    "id": f"history-{number:05d}",
                    "target_id": "synthetic-target",
                    "scenario_id": "synthetic-scenario",
                    "execution_mode": "immediate",
                    "status": "completed",
                    "created_at": now,
                }
                for number in range(count)
            ]
            + [
                {
                    "id": "older-active",
                    "target_id": "synthetic-target",
                    "scenario_id": "synthetic-scenario",
                    "execution_mode": "immediate",
                    "status": "waiting_for_capture",
                    "created_at": now - timedelta(days=1),
                },
                {
                    "id": "analysis-hidden",
                    "target_id": "synthetic-target",
                    "scenario_id": "synthetic-scenario",
                    "execution_mode": "artifact_analysis",
                    "status": "queued",
                    "created_at": now + timedelta(days=1),
                },
            ],
        )


def row_ids(response):
    assert response.status_code == 200
    return re.findall(r'<tr id="job-row-([^"]+)"', response.text)


@pytest.mark.parametrize("count", [100, 1000, 5000])
def test_history_page_bounds_rows_bytes_and_queries(client, in_memory_session_scope, count):
    seed_history(in_memory_session_scope, count)
    statements = []
    with in_memory_session_scope() as session:
        engine = session.get_bind()

    def record(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        response = client.get("/jobs-table-fragment")
    finally:
        event.remove(engine, "before_cursor_execute", record)
    ids = row_ids(response)
    assert len(ids) == 50
    assert ids[0] == "older-active"
    assert ids[1:] == [f"history-{number:05d}" for number in range(count - 1, count - 50, -1)]
    assert len(response.content) < 125_000
    # Count, bounded Jobs SELECT, batched artifact SELECT; unrelated auth/settings
    # queries are intentionally outside the history query contract.
    history_queries = [query for query in statements if "FROM jobs" in query or "FROM artifacts" in query]
    assert len(history_queries) == 3
    assert "LIMIT" in history_queries[1]


def test_history_navigation_filters_and_stable_order(client, in_memory_session_scope):
    seed_history(in_memory_session_scope, 100)
    pages = [client.get(f"/jobs?page={page}") for page in (1, 2, 3)]
    assert [len(row_ids(page)) for page in pages] == [50, 50, 1]
    combined = [job_id for page in pages for job_id in row_ids(page)]
    assert len(set(combined)) == 101
    assert "analysis-hidden" not in combined
    assert 'id="jobs-next" href="/jobs?page=2&amp;state=all"' in pages[0].text
    assert 'id="jobs-previous" href="/jobs?page=1&amp;state=all"' in pages[1].text
    active = client.get("/jobs?state=active")
    assert row_ids(active) == ["older-active"]
    finished = client.get("/jobs-table-fragment?page=2&state=finished")
    assert row_ids(finished) == [f"history-{number:05d}" for number in range(49, -1, -1)]
    assert 'hx-get="/jobs-table-fragment?page=2&amp;state=finished"' in finished.text
    assert 'id="jobs-filter-finished"' in finished.text
    # Existing API repository users retain descending creation order, not the
    # UI's active-first policy.
    with in_memory_session_scope() as session:
        assert JobRepository(session).list_jobs(limit=1)[0].id == "history-00099"


def test_out_of_range_page_converges_after_history_removal(client, in_memory_session_scope):
    seed_history(in_memory_session_scope, 1)
    response = client.get("/jobs-table-fragment?page=999&state=finished")
    assert row_ids(response) == ["history-00000"]
    assert 'hx-get="/jobs-table-fragment?page=1&amp;state=finished"' in response.text


def test_active_job_terminal_transition_converges_on_poll(client, in_memory_session_scope):
    seed_history(in_memory_session_scope, 1)
    assert row_ids(client.get("/jobs-table-fragment?state=active")) == ["older-active"]
    with in_memory_session_scope() as session:
        session.get(Job, "older-active").status = "completed"
    active = client.get("/jobs-table-fragment?state=active")
    assert row_ids(active) == []
    finished = client.get("/jobs-table-fragment?state=finished")
    assert row_ids(finished) == ["history-00000", "older-active"]
    assert 'class="spinner"' not in finished.text
    assert 'id="job-delete-older-active"' in finished.text


@pytest.mark.parametrize("query", ["page=0", "page=-1", "page=bad", "state=unknown"])
@pytest.mark.parametrize("path", ["/jobs", "/jobs-table-fragment"])
def test_invalid_history_navigation_is_rejected(client, path, query):
    assert client.get(f"{path}?{query}").status_code == 422
