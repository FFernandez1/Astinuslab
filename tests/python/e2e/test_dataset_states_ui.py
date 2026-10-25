"""Both ends of the data range: a fresh install, and a lab that has been used.

Every other test in this tier arranges exactly the data it needs. Nobody had
looked at the two states an operator actually meets -- the first five minutes,
when every list is empty, and six months in, when one of them is not.

Run with:
    pytest tests/python/e2e/ -q
"""

from __future__ import annotations

import json
import urllib.request

import pytest
from playwright.sync_api import Page, expect

from astinuslab.ui.jobs_routes import JOBS_PAGE_SIZE

pytestmark = pytest.mark.e2e

E2E_CSRF_TOKEN = "playground-e2e-csrf-token"
# Enough rows that a page which renders them badly shows it. Six months of daily
# acquisitions is roughly this.
CROWDED_JOB_COUNT = 200


def _create_job(base_url: str) -> str:
    body = json.dumps(
        {
            "target_id": "ubuntu-22-04-default",
            "scenario_id": "immediate-capture",
            "execution_mode": "immediate",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/api/v1/jobs",
        data=body,
        method="POST",
        headers={
            "Cookie": f"astinuslab_csrf={E2E_CSRF_TOKEN}",
            "X-CSRF-Token": E2E_CSRF_TOKEN,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))["id"]


def _page_scrolls_sideways(page: Page) -> bool:
    return page.evaluate("() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1")


def test_an_empty_jobs_page_says_so(playground_base_url: str, page: Page):
    """A bare table head is not an empty state."""
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(f"{playground_base_url}/jobs")

    expect(page.locator(".empty-state")).to_be_visible()
    expect(page.locator("#jobs-table tbody tr")).to_have_count(0)
    # ...and the way out of the empty state is on the page.
    expect(page.get_by_role("link", name="+ New Job")).to_be_visible()
    assert errors == []


def test_an_images_page_with_nothing_on_disk_says_so(bare_install_base_url: str, page: Page):
    """The state a fresh install is in before anything has been downloaded."""
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(f"{bare_install_base_url}/images")

    empty = page.locator(".vault-empty-state")
    expect(empty).to_be_visible()
    expect(empty).to_contain_text("No images")
    assert errors == []


def test_storage_with_no_evidence_says_there_are_no_dumps(bare_install_base_url: str, page: Page):
    """Storage is three sibling pages behind a tab strip, not one list.

    The state that matters on a fresh install is the evidence view: an operator
    who has captured nothing should be told that, and told where dumps come from.
    """
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(f"{bare_install_base_url}/storage")

    tabs = page.locator(".tab-strip .tab-link")
    expect(tabs).to_have_count(3)
    expect(page.locator(".tab-link--active")).to_have_attribute("href", "/storage")
    assert not _page_scrolls_sideways(page)

    page.locator('.tab-strip a[href="/dumps"]').click()
    expect(page).to_have_url(f"{bare_install_base_url}/dumps")
    expect(page.get_by_text("No dumps in storage/dumps/")).to_be_visible()
    assert errors == []


def test_two_hundred_jobs_render_without_breaking_the_page(jobs_base_url: str, active_ssh_key: str, page: Page):
    """Six months in, this page is the one an operator opens most.

    Seeded through the API against the non-executing Jobs backend, so the rows
    are real job records and nothing is provisioned.
    """
    first = _create_job(jobs_base_url)
    for _ in range(CROWDED_JOB_COUNT - 2):
        _create_job(jobs_base_url)
    last = _create_job(jobs_base_url)

    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(f"{jobs_base_url}/jobs")

    # History is paginated: the first page holds one page of rows and says how many exist.
    expect(page.locator("#jobs-table tbody tr")).to_have_count(JOBS_PAGE_SIZE)
    expect(page.locator("#jobs-page-range")).to_have_text(f"1–{JOBS_PAGE_SIZE} / {CROWDED_JOB_COUNT}")
    expect(page.locator("#jobs-next")).to_be_visible()
    expect(page.locator(".empty-state")).to_have_count(0)
    # A wide table has to scroll inside its own container, not take the page with it.
    assert not _page_scrolls_sideways(page)
    assert errors == []

    # Both ends of the list are still reachable, not just rendered: the newest job
    # leads the first page and the oldest closes the last one.
    last_page = -(-CROWDED_JOB_COUNT // JOBS_PAGE_SIZE)
    page.goto(f"{jobs_base_url}/jobs?page={last_page}")
    expect(page.locator(f'#jobs-table tbody tr:has(a[href="/jobs/{first}"])')).to_have_count(1)
    page.goto(f"{jobs_base_url}/jobs")
    expect(page.locator(f'#jobs-table tbody tr:has(a[href="/jobs/{last}"])')).to_have_count(1)
    page.locator(f'a[href="/jobs/{last}"]').first.click()
    expect(page).to_have_url(f"{jobs_base_url}/jobs/{last}")
