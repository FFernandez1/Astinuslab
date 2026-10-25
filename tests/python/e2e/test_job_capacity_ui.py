"""What a Job waiting for a VM slot tells the operator (E2E-J03).

The capacity subsystem's whole output is a Job that has been admitted to the
queue and is not allowed to start yet. Until now nothing in a browser had ever
seen it: it needs a worker that really runs, so the first Job holds a
reservation, *and* a cap low enough for the second to be refused a slot. The
first half only arrived with ASTINUSLAB_FAKE_ORCHESTRATION; `capped_jobs_*` is
the second.

The specification this suite carried for it described a partly different
product -- it expected the wait on both surfaces, which is right, and a
`capacity_reason` the page would have to be told to render, when the page
already renders it along with the promise that the Job resumes by itself. What
was actually untested is everything below.

Not covered here: that the promise is kept. Resumption is the
`admit-waiting-jobs` beat task on a fifteen-second schedule, and this tier runs
tasks inline with no beat -- cancelling the holder leaves the waiter waiting
for as long as a test cares to look. It is proved below the browser in
tests/python/jobs/test_capacity_scheduler.py and test_admission_atomicity.py.

Run with:
    pytest tests/python/e2e/ -q
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from astinuslab.jobs import job_status

pytestmark = pytest.mark.e2e

# Interactive Lab: it boots and then stays live waiting for the operator, so it
# holds its reservation after the request that started it has returned. An
# immediate-capture job would finish and give the slot straight back.
HOLDS_THE_SLOT = "manual_trigger"


def _status_badge(page: Page):
    return page.locator("[data-live-status-label]").first


def _rendered_status(page: Page) -> str:
    """The raw status the badge is rendering, read off `badge-{{ status }}`.

    The label is humanised and translated ("Waiting for capacity"), so
    comparing it against the status vocabulary fails on the underscores and
    would fail again in any other locale.
    """
    classes = (_status_badge(page).get_attribute("class") or "").split()
    rendered = [name.removeprefix("badge-") for name in classes if name.startswith("badge-")]
    assert len(rendered) == 1, f"expected exactly one status class on the badge, got {classes}"
    return rendered[0]


def _two_jobs(wizard) -> tuple[str, str]:
    """One Job holding the only slot, and one that could not have it.

    Returned in that order. The second is asserted to be waiting here rather
    than in each test, because a test that silently got two running Jobs would
    otherwise pass by asserting nothing.
    """
    holder = wizard.launch(HOLDS_THE_SLOT)
    waiter = wizard.launch(HOLDS_THE_SLOT)
    assert wizard.job(waiter)["status"] == str(job_status.JobStatus.WAITING_FOR_CAPACITY)
    assert wizard.job(holder)["status"] == str(job_status.JobStatus.WAITING_FOR_CAPTURE)
    return holder, waiter


def test_a_second_job_waits_for_capacity_and_says_why_on_its_live_page(
    capped_jobs_base_url: str, capped_jobs_ssh_key: str, job_wizard, page: Page
):
    """The live page names the state, the reason, and what happens next."""
    wizard = job_wizard(capped_jobs_base_url)
    _holder, waiter = _two_jobs(wizard)

    assert _rendered_status(page) == str(job_status.JobStatus.WAITING_FOR_CAPACITY)
    # A label the operator can read, not only a class the test can see.
    expect(_status_badge(page)).not_to_be_empty()

    # The scheduler's own words, asserted against the API's value: a page that
    # rendered its own sentence about capacity could not pass this.
    reason = wizard.job(waiter)["capacity_reason"]
    assert reason, "a job held back with no reason leaves the operator nothing to act on"
    wait_block = page.locator(".job-capacity-wait")
    expect(wait_block).to_have_count(1)
    expect(wait_block).to_contain_text(reason)
    # ...and the answer to "so what do I do about it", which is nothing.
    expect(wait_block).to_contain_text("automatically")

    # A phase stepper would say the job is somewhere in a run it has not begun.
    expect(page.locator(".phase-stepper")).to_have_count(0)
    expect(page.locator('[aria-current="step"]')).to_have_count(0)

    # The wait is in the record the operator reads to reconstruct what happened.
    log = page.locator(".log-state")
    assert str(job_status.JobStatus.WAITING_FOR_CAPACITY) in [text.strip() for text in log.all_text_contents()]


def test_the_jobs_table_shows_which_job_is_waiting_and_why(
    capped_jobs_base_url: str, capped_jobs_ssh_key: str, job_wizard, page: Page
):
    """The table is where an operator with several Jobs finds the held-back one.

    Asserted per row. A table that printed the reason once somewhere on the
    page would tell them a Job is waiting without telling them which.
    """
    wizard = job_wizard(capped_jobs_base_url)
    holder, waiter = _two_jobs(wizard)
    reason = wizard.job(waiter)["capacity_reason"]

    page.goto(f"{capped_jobs_base_url}/jobs")
    waiting_row = page.locator("tr", has_text=waiter[: len("job-") + 4]).first
    holding_row = page.locator("tr", has_text=holder[: len("job-") + 4]).first

    expect(waiting_row.locator(".job-capacity-wait")).to_have_count(1)
    expect(waiting_row).to_contain_text(reason)
    # The Job that has the slot is not waiting for one, and must not be dressed
    # as though it were.
    expect(holding_row.locator(".job-capacity-wait")).to_have_count(0)
    expect(page.locator(".job-capacity-wait")).to_have_count(1)


def test_a_waiting_job_can_be_cancelled_without_disturbing_the_one_holding_the_slot(
    capped_jobs_base_url: str, capped_jobs_ssh_key: str, job_wizard, page: Page
):
    """Waiting is not a state an operator should have to sit out.

    A Job held back before it booted has nothing to tear down, so cancelling it
    is the one cheap escape from a full queue -- and it must not take the
    running Job with it, which is the way a capacity bug would show up here.
    """
    wizard = job_wizard(capped_jobs_base_url)
    holder, waiter = _two_jobs(wizard)

    cancel = page.locator('form[action$="/cancel"]')
    expect(cancel).to_have_count(1)
    cancel.locator("button").first.click()

    expect(_status_badge(page)).to_have_class(f"badge badge-{job_status.JobStatus.CANCELLED}", timeout=20_000)
    assert wizard.job(waiter)["status"] == str(job_status.JobStatus.CANCELLED)
    # Nothing left to act on, and nothing left to explain.
    expect(page.locator('form[action$="/cancel"]')).to_have_count(0)
    expect(page.locator(".job-capacity-wait")).to_have_count(0)

    assert wizard.job(holder)["status"] == str(job_status.JobStatus.WAITING_FOR_CAPTURE)
    page.goto(f"{capped_jobs_base_url}/jobs/{holder}")
    assert _rendered_status(page) == str(job_status.JobStatus.WAITING_FOR_CAPTURE)
