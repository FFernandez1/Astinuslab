"""Jobs wizard e2e: the form creates exactly one real Job and opens its live page.

The browser suite renders this wizard in several places but had never submitted
it, so nothing proved a Job could be created through the UI at all.

These run against `jobs_server`, which enqueues but never executes: job
orchestration is not faked by ASTINUSLAB_FAKE_LIBVIRT and would reach for
privileged host commands. The boundary under test is admission -- the form
becomes one Job, bound to its live page -- not what a worker does afterwards.

Run with:
    pytest tests/python/e2e/ -q
"""

from __future__ import annotations

import json
import re
import urllib.request

import pytest
from playwright.sync_api import Page, expect

from astinuslab.jobs import job_status

pytestmark = pytest.mark.e2e

JOB_URL_RE = re.compile(r"/jobs/[A-Za-z0-9._-]+$")
# Imported rather than restated so a vocabulary change reaches this test.
TERMINAL_STATUSES = job_status.TERMINAL_STATUSES


def _jobs(base_url: str) -> list[dict]:
    with urllib.request.urlopen(f"{base_url}/api/v1/jobs", timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    assert isinstance(payload, list), payload  # GET /api/v1/jobs is declared list[JobOut]
    return payload


def _open_wizard_at_review(page: Page, base_url: str) -> None:
    """Pick the first target and the immediate-capture scenario, then walk to Review."""
    page.goto(f"{base_url}/jobs/new")
    page.locator('[name="target_id"]').first.check(force=True)
    page.locator('[name="scenario_id"][data-mode="immediate"]').first.check(force=True)

    launch = page.locator("#wiz-launch-btn")
    next_button = page.locator("#wiz-next-btn")
    # The wizard skips steps that do not apply to the chosen scenario, so walk
    # forward until the submit control appears rather than pinning a step count.
    for _ in range(4):
        if launch.is_visible():
            break
        expect(next_button).to_be_enabled()
        next_button.click()
    expect(launch).to_be_visible()
    # Landing on Review is the precondition every test here depends on; a
    # wizard that grew or lost a step must fail loudly rather than submit from
    # wherever the walk happened to stop.
    expect(page.locator('#wiz-stepper .wiz-stepper__step[data-step="review"]')).to_have_attribute("data-active", "")


def test_job_wizard_creates_one_job_and_opens_its_live_page(jobs_base_url: str, active_ssh_key: str, page: Page):
    assert _jobs(jobs_base_url) == []

    _open_wizard_at_review(page, jobs_base_url)
    page.locator("#wiz-launch-btn").click()

    expect(page).to_have_url(JOB_URL_RE)
    job_id = page.url.rsplit("/", 1)[-1]

    created = _jobs(jobs_base_url)
    assert [job["id"] for job in created] == [job_id]

    # The live page is bound to the job that was just created and reports a real
    # status, not an empty placeholder.
    expect(page.locator("#live-section")).to_have_attribute("data-job-id", job_id)
    status = page.locator("[data-live-job-status]")
    expect(status).to_be_visible()
    assert status.inner_text().strip()

    # No worker consumes the queue here, so the job must be sitting in a
    # pre-execution state. A terminal status would mean the form did something
    # other than admit the job.
    assert created[0]["status"] not in TERMINAL_STATUSES, created[0]["status"]


def test_launch_control_locks_on_submit(jobs_base_url: str, active_ssh_key: str, page: Page):
    """The submit guard must latch before the response arrives, not after navigation."""
    _open_wizard_at_review(page, jobs_base_url)

    # Answer the POST with 204 so the browser keeps the current document: with
    # the form still mounted, the guard is observable instead of being masked by
    # the redirect. (Aborting instead would replace the page with a network
    # error page and take the form with it.)
    page.route(
        "**/jobs/new",
        lambda route: route.fulfill(status=204) if route.request.method == "POST" else route.continue_(),
    )
    launch = page.locator("#wiz-launch-btn")
    launch.click()

    expect(launch).to_be_disabled()


def test_a_second_click_cannot_submit_again(jobs_base_url: str, active_ssh_key: str, page: Page):
    """A double-click must produce one submission.

    Deliberately not written as `dblclick()` on the live form: there the
    redirect tears the page down before the second click lands, so the test
    passes with the guard removed. Answering 204 keeps the document mounted,
    which makes the second click land on a form that is still there -- and the
    guard the only thing stopping it.
    """
    _open_wizard_at_review(page, jobs_base_url)

    submissions: list[str] = []

    def handle(route):
        if route.request.method == "POST":
            submissions.append(route.request.url)
            route.fulfill(status=204)
        else:
            route.continue_()

    page.route("**/jobs/new", handle)

    launch = page.locator("#wiz-launch-btn")
    launch.click()
    expect(launch).to_be_disabled()
    # force=True skips Playwright's own enabled check, so the click really is
    # delivered; a disabled button is then the browser refusing to activate it.
    launch.click(force=True)

    assert submissions == [f"{jobs_base_url}/jobs/new"]
