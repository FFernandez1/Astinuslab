"""What the workbench tells the operator when a Volatility run fails.

`test_volatility_run_ui.py` proves what a *completed* run attributes its
results to. This module covers the other half of the same seam, which had been
proved only below the browser: a run that starts and then goes wrong.

Three ways it can, all reachable now that `fixtures/fake_vol3.py` takes
`ASTINUSLAB_FAKE_VOL3_MODE`:

* the tool exits non-zero and says why on stderr -- wrong symbols for the
  image, the commonest real failure;
* the tool exits zero having written output the JSON renderer did not finish,
  which is what a killed or out-of-memory vol3 leaves behind;
* the tool succeeds, but reports having resolved its symbol table from
  somewhere the product did not stage it -- rows that would parse, produced
  against symbols the product cannot account for.

The backend distinguishes all three and writes a precise reason for each. The
question here is only whether that reason reaches the person who has to act on
it, so every assertion about the reason is made against the value the API
returned rather than against copy written in this file: a page that rendered
its own generic sentence could not pass.

The timeout branch is not here. `_VOL3_TIMEOUT_SECONDS` is a module constant of
900, not a setting, so reaching it from a browser would mean a fifteen-minute
test; it stays proved below the browser.

Run with:
    pytest tests/python/e2e/ -q
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

PLUGIN = "linux.pslist.PsList"
# How many rows the stand-in emits when it is behaving, so a test can establish
# that a completed run really did put results on the screen before asserting
# that the failure after it took them away.
STAND_IN_ROW_COUNT = 3


def _run_and_fail(page: Page, plugin: str = PLUGIN, *, reload: bool = True) -> None:
    """Select a plugin, run it, and wait for *that* run to end in failure.

    The wait is on the pill's class, not on the word "failed" anywhere in the
    panel: the label is humanised and translated, and a second run in a session
    would find the word already there from the first.
    """
    if reload:
        page.reload()
    page.locator(f'[data-plugin="{plugin}"]').first.click()
    run = page.locator("#run-btn")
    expect(run).to_be_enabled(timeout=15_000)
    run.click()
    expect(page.locator("#task-pill")).to_have_class(re.compile(r"vol-pill--failed"), timeout=60_000)


def _run_to_completion(page: Page, plugin: str = PLUGIN) -> None:
    """The successful run some of these tests need to have something to clear.

    Waits on #cmd -- the command line echoed for the run being shown -- rather
    than on a word that a later run would find already present.
    """
    page.reload()
    page.locator(f'[data-plugin="{plugin}"]').first.click()
    run = page.locator("#run-btn")
    expect(run).to_be_enabled(timeout=15_000)
    run.click()
    expect(page.locator("#cmd")).to_contain_text(plugin, timeout=60_000)
    expect(page.locator("#task-pill")).to_have_class(re.compile(r"vol-pill--done"), timeout=60_000)


def _failure_record(workbench, job_id: str, plugin: str = PLUGIN) -> dict:
    record = workbench.api("GET", f"/api/v1/volatility/runs/latest?job_id={job_id}&plugin={plugin}")
    assert record.get("status") == "failed", record
    assert record.get("error"), record
    return record


def _visible_text(page: Page) -> str:
    """What the workbench actually shows.

    `inner_text()` is CSS-rendered text, so it excludes anything hidden. That
    distinction is the whole point of this module: the reason existing in the
    DOM is not the reason reaching the operator.
    """
    return page.locator("#vol-workbench").inner_text()


@pytest.mark.parametrize(
    "failing_analysis_base_url",
    ["exit_nonzero", "unparseable_json", "foreign_isf"],
    indirect=True,
)
def test_a_failed_run_shows_the_operator_the_reason_it_failed(
    failing_analysis_base_url, analysis_workbench, page: Page
):
    """The reason is written into the results panel, not only into the record.

    Asserted against the API's own `error` string. A workbench that showed a
    generic "Run failed" -- which it does show, as a summary -- could not pass
    this, and neither could one that put the reason somewhere hidden.
    """
    workbench = analysis_workbench(failing_analysis_base_url)
    row = workbench.prepare()
    _run_and_fail(page)

    reason = _failure_record(workbench, row["job_id"])["error"]
    assert reason in _visible_text(page), reason


@pytest.mark.parametrize("failing_analysis_base_url", ["after_first:exit_nonzero"], indirect=True)
def test_a_failed_run_clears_the_results_of_the_run_before_it(
    failing_analysis_base_url, analysis_workbench, page: Page
):
    """The stale-evidence case: results on screen under a badge that says failed.

    This is why the stand-in can succeed once and then fail. Asserted against a
    failure with nothing before it, every claim here is about the page's own
    starting state -- an empty table, a disabled export button -- and cannot
    fail. Asserted against a failure that follows a completed run, each one is
    about the product clearing what it must clear.
    """
    workbench = analysis_workbench(failing_analysis_base_url)
    workbench.prepare()

    _run_to_completion(page)
    expect(page.locator("#res-body tr")).to_have_count(STAND_IN_ROW_COUNT)
    expect(page.locator("#export-csv")).to_be_enabled()

    _run_and_fail(page, reload=False)

    expect(page.locator("#res-body tr")).to_have_count(0)
    expect(page.locator("#findings")).to_be_empty()
    # Nothing to take away: no evidence was produced by this run, and there are
    # no rows to export. Offering either would be handing the operator a file
    # belonging to an analysis the page is simultaneously calling failed.
    expect(page.locator("#evidence-link")).to_be_hidden()
    expect(page.locator("#export-csv")).to_be_disabled()


@pytest.mark.parametrize("failing_analysis_base_url", ["after_first:foreign_isf"], indirect=True)
def test_a_run_refused_for_foreign_symbols_says_where_the_symbols_came_from(
    failing_analysis_base_url, analysis_workbench, page: Page
):
    """The integrity case, and the one an operator is most likely to misread.

    Here the tool exits zero and emits rows that parse. The product refuses
    them because the saved config says they were resolved from a symbol table
    outside the directory it staged -- so the operator has to be told that
    rather than left to re-run into the same silent refusal, and no rows may be
    shown. Following a completed run, so that last claim is about the refusal
    and not about an empty table nobody filled.
    """
    workbench = analysis_workbench(failing_analysis_base_url)
    row = workbench.prepare()
    _run_to_completion(page)

    _run_and_fail(page, reload=False)

    reason = _failure_record(workbench, row["job_id"])["error"]
    assert reason in _visible_text(page), reason
    # Both halves of the reason: the path vol3 used, and the one it should have.
    assert "outside the staged" in reason, reason
    # Counted in the DOM rather than read off the screen: the rows the tool
    # emitted must be gone, not merely behind the view the failure switched to.
    expect(page.locator("#res-body tr")).to_have_count(0)


@pytest.mark.parametrize("failing_analysis_base_url", ["exit_nonzero"], indirect=True)
def test_the_reason_is_still_there_when_the_operator_comes_back(
    failing_analysis_base_url, analysis_workbench, page: Page
):
    """The other entry point into the failure state.

    A run's outcome is restored from the record when a plugin is re-selected,
    which an operator does by reloading or by stepping away and back. That path
    reaches the same failure state through different code, so a fix applied to
    the live poll alone would leave a reloaded page saying only "failed".
    """
    workbench = analysis_workbench(failing_analysis_base_url)
    row = workbench.prepare()
    _run_and_fail(page)
    reason = _failure_record(workbench, row["job_id"])["error"]

    page.reload()
    page.locator(f'[data-plugin="{PLUGIN}"]').first.click()
    expect(page.locator("#task-pill")).to_have_class(re.compile(r"vol-pill--failed"), timeout=30_000)

    assert reason in _visible_text(page), reason


@pytest.mark.parametrize("failing_analysis_base_url", ["exit_nonzero"], indirect=True)
def test_the_activity_log_names_the_dump_the_failed_run_was_against(
    failing_analysis_base_url, analysis_workbench, page: Page
):
    """A failed run is still a run, and its history row must identify the evidence.

    Activity is where an operator reconstructs what was attempted -- which
    plugin, against which capture, and how it ended. A row that names the
    plugin and the outcome but shows an em dash for the dump makes a session's
    failures indistinguishable from each other, which in a tool whose subject
    is evidence identity is the wrong thing to be vague about.
    """
    workbench = analysis_workbench(failing_analysis_base_url)
    row = workbench.prepare()
    _run_and_fail(page)
    run_id = _failure_record(workbench, row["job_id"])["run_id"]

    page.locator("#ptab-activity-btn").click()
    entry = page.locator("#queue-body tr", has_text=run_id)
    expect(entry).to_have_count(1)
    expect(entry).to_contain_text(PLUGIN)
    expect(entry).to_contain_text(str(row["file"]))
    # The outcome as the operator reads it, off the row rather than the API.
    expect(entry.locator(".vol-status-dot")).to_have_class(re.compile(r"\bfail\b"))
