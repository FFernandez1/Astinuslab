"""Deep links to a Job that is no longer there.

Job ids end up in bookmarks, runbooks and chat messages, and jobs get deleted --
that is what the delete control on the Jobs table is for. Following such a link
later is ordinary, and the operator following it is usually looking for evidence
they think still exists.

Run with:
    pytest tests/python/e2e/ -q
"""

from __future__ import annotations

import json
import urllib.request

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

E2E_CSRF_TOKEN = "playground-e2e-csrf-token"


def _post(base_url: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Cookie": f"astinuslab_csrf={E2E_CSRF_TOKEN}", "X-CSRF-Token": E2E_CSRF_TOKEN}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(f"{base_url}{path}", data=data, method="POST", headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read().decode("utf-8")
    return json.loads(payload) if payload else {}


def _create_job(base_url: str) -> str:
    return _post(
        base_url,
        "/api/v1/jobs",
        {
            "target_id": "ubuntu-22-04-default",
            "scenario_id": "immediate-capture",
            "execution_mode": "immediate",
        },
    )["id"]


def _expect_readable_not_found(page: Page, base_url: str) -> None:
    """A dead end an operator can read and leave, not an API error body."""
    body = page.locator("body").inner_text()
    assert not body.strip().startswith("{"), body[:200]
    # The operator is inside the product, not on a bare error body: the nav is
    # there, and the page itself offers the way back.
    expect(page.locator("nav.sidebar")).to_be_visible()
    expect(page.locator("#main-content").get_by_role("link", name="Jobs")).to_be_visible()
    assert "not found" in body.lower(), body[:200]


def test_a_live_job_link_works_and_a_stale_one_is_readable(jobs_base_url: str, active_ssh_key: str, page: Page):
    """The same page, before and after the id stops resolving.

    Deliberately not written by deleting the job: deletion is refused while a
    job is live (409) and getting one to a terminal state exercises the job
    state machine, not this link. The path under test is the same either way --
    `repo.get_job(job_id)` returning None -- so the pair here is a real job's
    page and an id from the same family that no longer resolves.
    """
    job_id = _create_job(jobs_base_url)
    page.set_viewport_size({"width": 1600, "height": 900})

    page.goto(f"{jobs_base_url}/jobs/{job_id}")
    expect(page.locator("#live-section")).to_have_attribute("data-job-id", job_id)

    response = page.goto(f"{jobs_base_url}/jobs/{job_id}-gone")
    assert response is not None and response.status == 404
    _expect_readable_not_found(page, jobs_base_url)


@pytest.mark.parametrize("job_id", ["does-not-exist", "pg-abc123def456"])
def test_an_unknown_job_link_lands_on_something_readable(playground_base_url: str, page: Page, job_id: str):
    """A mistyped id, and a Playground session id -- which /jobs refuses by design."""
    page.set_viewport_size({"width": 1600, "height": 900})
    response = page.goto(f"{playground_base_url}/jobs/{job_id}")

    assert response is not None and response.status == 404
    _expect_readable_not_found(page, playground_base_url)


def test_api_clients_still_get_json(playground_base_url: str, page: Page):
    """The HTML page is for browsers; a machine asking for JSON must still get it."""
    # From inside the app's own origin: a bare page.evaluate runs on about:blank,
    # where this fetch is cross-origin and never reaches the server.
    page.goto(f"{playground_base_url}/jobs")
    result = page.evaluate(
        """async () => {
            const response = await fetch("/api/v1/jobs/does-not-exist", {
                headers: { Accept: "application/json" },
            });
            return { status: response.status, body: await response.text() };
        }"""
    )
    assert result["status"] == 404
    assert json.loads(result["body"])["detail"]
