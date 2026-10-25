"""What a Job does after it is admitted, seen from the browser.

`test_jobs_ui.py` stops at creation, deliberately: its backend enqueues and
never executes, because a job run reaches for `sudo` and libvirt and would
provision a real VM from a test. This module runs against
`running_jobs_base_url`, whose worker executes inline under
`ASTINUSLAB_FAKE_ORCHESTRATION` -- every binding that would reach the host is
replaced by one that refuses, so the job walks the real status graph and no
machine is created. See `src/astinuslab/orchestration/fake_runtime.py`.

What that makes reachable, and what it does not:

* an interactive-lab job runs to the capture hand-off, so the live page can be
  read while a job is genuinely live, and Cancel can be clicked for real;
* a capture job fails where it would have produced evidence -- the fake does not
  forge an artifact -- which is a deterministic failed job, and therefore the
  first chance this suite has had to click Retry;
* a *successful* capture is still out of reach, so the capture trigger is
  asserted to reach the real acquisition path rather than to complete.

Because tasks run inline, the run finishes inside the POST that starts it: the
browser sees the end state, not a live transition. Progression is still visible
and asserted -- the event log is what the operator actually reads to find out
what a job did -- but ordering over wall-clock time is proved below the browser
in `tests/python/orchestration/test_fake_orchestration.py`.

Run with:
    pytest tests/python/e2e/ -q
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from astinuslab.jobs import job_status

pytestmark = pytest.mark.e2e

# The stages a target boot passes through, in the order the operator should see
# them. Imported vocabulary, so a rename in the status graph reaches this test.
BOOT_STAGES = [
    job_status.JobStatus.QUEUED,
    job_status.JobStatus.PREPARING_IMAGE,
    job_status.JobStatus.WAITING_FOR_GUEST_ACCESS,
    job_status.JobStatus.MOUNTING_OUTPUT_SHARE,
]
BOOT_STAGE_VALUES = [str(stage) for stage in BOOT_STAGES]


def _status_badge(page: Page):
    return page.locator("[data-live-status-label]").first


def _rendered_status(page: Page) -> str:
    """The raw status the live badge is rendering, read off `badge-{{ status }}`.

    The badge's text is a humanised, translated label ("waiting for capture"),
    so comparing it to the status vocabulary fails on the underscores and would
    fail again in any other locale. The class is the state attribute here.
    """
    classes = (_status_badge(page).get_attribute("class") or "").split()
    rendered = [name.removeprefix("badge-") for name in classes if name.startswith("badge-")]
    assert len(rendered) == 1, f"expected exactly one status class on the badge, got {classes}"
    return rendered[0]


def _walked_boot_stages(page: Page) -> list[str]:
    """The boot stages named in the event log, in order, consecutive repeats collapsed.

    `.log-state` is reused by the Host Actions card, where it holds an action
    type rather than a status -- filtering to the status vocabulary keeps this
    to the event log without depending on which card comes first.
    """
    walked: list[str] = []
    for text in page.locator(".log-state").all_text_contents():
        state = text.strip()
        if state in BOOT_STAGE_VALUES and (not walked or walked[-1] != state):
            walked.append(state)
    return walked


def _cancel_form(page: Page):
    return page.locator('form[action$="/cancel"]')


def _capture_form(page: Page):
    return page.locator('form[action$="/acquire-memory-dump"]')


def test_a_job_launched_from_the_wizard_runs_past_admission(
    running_jobs_base_url: str, running_jobs_ssh_key: str, job_wizard, page: Page
):
    wizard = job_wizard(running_jobs_base_url)
    job_id = wizard.launch("manual_trigger")

    assert _rendered_status(page) == str(job_status.JobStatus.WAITING_FOR_CAPTURE)
    # A label the operator can read, not just a class the test can see.
    expect(_status_badge(page)).not_to_be_empty()
    # The record of a boot that happened: without these the job could have
    # jumped to its end state without going through the phase under test.
    assert _walked_boot_stages(page) == BOOT_STAGE_VALUES
    body = page.locator("body")
    expect(body).to_contain_text("192.0.2.10")
    expect(body).to_contain_text("5.15.0-fake-orchestration")

    assert wizard.job(job_id)["status"] not in job_status.TERMINAL_STATUSES


def test_the_live_page_offers_the_controls_that_act_on_a_running_machine(
    running_jobs_base_url: str, running_jobs_ssh_key: str, job_wizard, page: Page
):
    job_wizard(running_jobs_base_url).launch("manual_trigger")

    expect(_cancel_form(page)).to_have_count(1)
    expect(_capture_form(page)).to_have_count(1)
    expect(page.locator('form[action$="/runtimes/target/reboot"]')).to_have_count(1)
    expect(page.locator('form[action$="/runtimes/target/shutdown"]')).to_have_count(1)


def test_cancelling_from_the_live_page_ends_the_job_and_withdraws_those_controls(
    running_jobs_base_url: str, running_jobs_ssh_key: str, job_wizard, page: Page
):
    wizard = job_wizard(running_jobs_base_url)
    job_id = wizard.launch("manual_trigger")

    _cancel_form(page).locator("button").first.click()
    expect(_status_badge(page)).to_have_class(f"badge badge-{job_status.JobStatus.CANCELLED}", timeout=20_000)

    cancelled = wizard.job(job_id)
    assert cancelled["status"] == str(job_status.JobStatus.CANCELLED)
    # A control that still posts at a machine that is gone is the dead-button
    # failure this review keeps finding; here the page must stop offering them.
    expect(_cancel_form(page)).to_have_count(0)
    expect(_capture_form(page)).to_have_count(0)


def test_a_job_that_fails_says_why_and_offers_a_retry(
    running_jobs_base_url: str, running_jobs_ssh_key: str, job_wizard, page: Page
):
    wizard = job_wizard(running_jobs_base_url)
    job_id = wizard.launch("immediate")

    expect(_status_badge(page)).to_have_class(f"badge badge-{job_status.JobStatus.FAILED}", timeout=20_000)
    failed = wizard.job(job_id)
    summary = failed["error_summary"]
    assert summary, "a failed job with no summary leaves the operator nothing to act on"
    # Not the wording -- the backend's own reason, reaching the screen. Asserted
    # against the API's value so this cannot pass on a hardcoded message.
    expect(page.locator("body")).to_contain_text(summary)
    expect(page.locator('form[action$="/retry"]')).to_have_count(1)


def test_retrying_a_failed_job_creates_a_second_job(
    running_jobs_base_url: str, running_jobs_ssh_key: str, job_wizard, page: Page
):
    wizard = job_wizard(running_jobs_base_url)
    first = wizard.launch("immediate")
    expect(page.locator('form[action$="/retry"]')).to_have_count(1, timeout=20_000)

    page.locator('form[action$="/retry"] button[type="submit"]').first.click()
    page.wait_for_url(lambda url: "/jobs/" in url and not url.endswith("/new"), timeout=20_000)

    jobs = wizard.jobs()
    assert len(jobs) == 2, f"retry must leave the original alone and add one, got {jobs}"
    second = page.url.rsplit("/", 1)[-1]
    assert second != first
    by_id = {job["id"]: job for job in jobs}
    assert by_id[first]["scenario_id"] == by_id[second]["scenario_id"]
    assert by_id[first]["target_id"] == by_id[second]["target_id"]


def test_the_capture_trigger_reaches_the_real_acquisition_path(
    running_jobs_base_url: str, running_jobs_ssh_key: str, job_wizard, page: Page
):
    """The button is wired to the acquisition, not merely rendered.

    The fake refuses to produce evidence, so this cannot assert a completed
    capture. What it can assert is that clicking the control takes the job out
    of the hand-off and into the acquisition -- a form pointed at nothing, or a
    route that quietly does not exist, would leave it sitting where it was.
    """
    wizard = job_wizard(running_jobs_base_url)
    job_id = wizard.launch("manual_trigger")
    assert _rendered_status(page) == str(job_status.JobStatus.WAITING_FOR_CAPTURE)

    _capture_form(page).locator('button[name="capture_type"][value="memory"]').first.click()
    page.wait_for_timeout(2_000)

    after = wizard.job(job_id)
    assert after["status"] != str(job_status.JobStatus.WAITING_FOR_CAPTURE), (
        "the capture control left the job where it was: it is not wired to the acquisition"
    )
    assert str(job_status.JobStatus.ACQUIRING_MEMORY) in [
        (text or "").strip() for text in page.locator(".log-state").all_text_contents()
    ], "the operator is never told the acquisition started"
