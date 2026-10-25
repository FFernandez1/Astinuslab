"""What a completed Volatility run attributes its results to.

`test_capture_to_analysis_ui.py` proves a capture reaches the analysis inventory
as the same artifact, and stops there: running a plugin needs the
operator-supplied Volatility binary this project never ships, so everything past
the run control was proved only below the browser. This module runs a plugin to
completion against `analysis_base_url`, whose `[vol3].volatility_bin` points at
`fixtures/fake_vol3.py`.

No product code changes for it. There is no fake-analysis branch in
`vol3_plugin_run.py`: the real pipeline runs end to end -- executable
verification, symbol staging, the subprocess, the saved-config ISF pin check,
JSON parsing, severity flagging, provenance and artifact attribution. Only the
framework that would read the memory image is absent, and the run record says
so, because it names the executable that actually ran.

Misattributed evidence is the worst defect this product can emit and the one a
single-feature test cannot see, so the assertions here are made against the run
record's own provenance -- which artifact, which session, which acquisition,
which bytes, which symbol table, which binary -- rather than against what the
page happens to say.

Everything up to the run control -- spawn, capture, state the context, supply
symbols -- is `analysis_workbench` in conftest, shared with the module that
covers what the workbench says when a run fails.

Run with:
    pytest tests/python/e2e/ -q
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

PLUGIN = "linux.pslist.PsList"


def _run_plugin(page: Page, plugin: str = PLUGIN, *, reload: bool = True) -> None:
    """Select a plugin, run it, and wait for *that* run to finish.

    The wait is on #cmd -- the command line the workbench echoes for the run it
    is showing -- and not on the word "completed" anywhere in the panel. A
    second run in the same session would find that word already there from the
    first, so a test waiting on it would sail past a run that never happened.
    """
    if reload:
        page.reload()
    page.locator(f'[data-plugin="{plugin}"]').first.click()
    run = page.locator("#run-btn")
    expect(run).to_be_enabled(timeout=15_000)
    run.click()
    expect(page.locator("#cmd")).to_contain_text(plugin, timeout=60_000)
    expect(page.locator("#task-state")).to_have_text("completed", timeout=60_000)


def _analysed(workbench, page: Page) -> dict:
    """A dump ready to analyse, with `PLUGIN` already run against it."""
    row = workbench.prepare()
    _run_plugin(page)
    return row


def _run_record(workbench, job_id: str) -> dict:
    record = workbench.api("GET", f"/api/v1/volatility/runs/latest?job_id={job_id}&plugin={PLUGIN}")
    assert record.get("status") == "completed", record
    return record


def test_a_completed_run_attributes_its_results_to_the_capture_it_analysed(
    analysis_base_url, analysis_workbench, page: Page
):
    workbench = analysis_workbench(analysis_base_url)
    row = _analysed(workbench, page)
    capture = workbench.api("GET", f"/api/v1/playground/vms/{row['vm_id']}/captures")[0]

    memory = _run_record(workbench, row["job_id"])["execution"]["inputs"]["memory"]
    assert memory["artifact_id"] == row["artifact_id"]
    assert memory["session_id"] == row["session_id"]
    assert memory["acquisition_id"] == capture["id"]
    # The bytes, not just the name: recorded at capture and recomputed at use.
    assert memory["sha256"] == memory["sha256_at_use"] == capture["sha256"]
    assert memory["integrity_verified"] is True
    # ...and the context the operator stated, carried into the run rather than
    # re-derived from whatever the VM happens to be running now.
    context = memory["analysis_context"]
    assert context["source"] == "operator_verified"
    assert context["kernel_release"] == workbench.KERNEL_RELEASE
    assert context["artifact_id"] == row["artifact_id"]


def test_a_completed_run_records_the_binary_it_actually_ran(
    analysis_base_url, analysis_workbench, page: Page, fake_vol3_bin: Path
):
    """The provenance anchor, and the reason a stand-in executable is honest here.

    A run is only as trustworthy as the tool that produced it, so the record has
    to name that tool by path and by hash. Under this tier that resolves to the
    fixture -- which is the point: a report produced through a stand-in says so
    on its face, and this test is what proves it would.
    """
    workbench = analysis_workbench(analysis_base_url)
    row = _analysed(workbench, page)
    record = _run_record(workbench, row["job_id"])

    tool = record["execution"]["tool"]
    assert Path(tool["executable"]["path"]) == fake_vol3_bin.resolve()
    assert tool["executable"]["sha256"] == hashlib.sha256(fake_vol3_bin.read_bytes()).hexdigest()
    assert record["execution"]["argv"][0] == str(fake_vol3_bin)
    # A hash of the launcher is not a hash of what it imports, and the record
    # must not overclaim: this stays false for a real Volatility too.
    assert tool["package_identity_verified"] is False


def test_a_completed_run_records_the_symbols_it_used_and_says_they_are_unverified(
    analysis_base_url, analysis_workbench, page: Page
):
    workbench = analysis_workbench(analysis_base_url)
    row = _analysed(workbench, page)
    stored = workbench.upload_isf(row["job_id"])
    record = _run_record(workbench, row["job_id"])

    isf = record["execution"]["inputs"]["isf"]
    assert isf["sha256"] == isf["sha256_at_use"] == stored["sha256"]
    # An operator-supplied ISF bypasses build-id enforcement, so the run must
    # carry that caveat rather than presenting the symbols as established.
    assert isf["source"] == "manual_upload"
    assert isf["trust"] == "unverified"
    # And vol3 must have resolved them from the staged copy, not from anywhere
    # else on the host -- the check the saved config exists to make.
    assert isf["pinned"] is True
    assert isf["staged"]["sha256"] == stored["sha256"]

    # The caveat reaches the operator, not only the record.
    expect(page.locator("#vol-workbench")).to_contain_text("unverified symbols")


def test_the_rendered_table_is_what_the_tool_emitted(analysis_base_url, analysis_workbench, page: Page):
    """Between the tool's stdout and the screen sit a parser and a renderer."""
    workbench = analysis_workbench(analysis_base_url)
    row = _analysed(workbench, page)
    record = _run_record(workbench, row["job_id"])

    assert record["columns"] == ["PID", "TID", "PPID", "COMM"]
    emitted = {values[record["columns"].index("COMM")] for values in record["rows"]}
    assert emitted == {
        "astinuslab-fake-vol3",
        "synthetic-row-not-evidence",
        "no-memory-image-was-read",
    }
    workbench = page.locator("#vol-workbench")
    for process in sorted(emitted):
        expect(workbench).to_contain_text(process)
    # The command line the operator is shown names the dump that was analysed.
    expect(workbench).to_contain_text(row["file"])


def test_a_second_plugin_on_the_same_dump_keeps_its_own_result(analysis_base_url, analysis_workbench, page: Page):
    """Running one plugin after another is the ordinary session, not an edge case."""
    workbench = analysis_workbench(analysis_base_url)
    row = _analysed(workbench, page)
    second_plugin = "linux.lsmod.Lsmod"

    _run_plugin(page, second_plugin, reload=False)

    first = _run_record(workbench, row["job_id"])
    second = workbench.api(
        "GET",
        f"/api/v1/volatility/runs/latest?job_id={row['job_id']}&plugin={second_plugin}",
    )
    assert second["status"] == "completed", second
    assert first["run_id"] != second["run_id"]
    assert second["execution"]["argv"][-1] == second_plugin
    # Two runs, one piece of evidence: both must name it, and neither may
    # quietly analyse something else.
    for record in (first, second):
        assert record["execution"]["inputs"]["memory"]["artifact_id"] == row["artifact_id"]


def test_a_run_does_not_borrow_the_other_dump_in_the_session(analysis_base_url, analysis_workbench, page: Page):
    """Two captures, one run: the misattribution case, past the inventory seam."""
    workbench = analysis_workbench(analysis_base_url)
    network_id = workbench.spawn_target("target-A")
    workbench.spawn_target("target-B", network_id=network_id)
    workbench.capture_ram("target-A")
    workbench.capture_ram("target-B")

    rows = {row["artifact_id"]: row for row in workbench.dump_rows()}
    assert {row["vm_label"] for row in rows.values()} == {"target-A", "target-B"}

    workbench.open()
    options = page.locator("#dump-select option")
    expect(options).to_have_count(2)
    # Deliberately the option that is not first. A workbench that ignored the
    # selection and analysed whatever sat at the top of the list would pass a
    # test that picked the top one -- which is the failure being ruled out.
    selected = str(options.last.get_attribute("value"))
    chosen = next(row for row in rows.values() if row["selection_id"] == selected)
    other = next(row for row in rows.values() if row["artifact_id"] != chosen["artifact_id"])

    page.locator("#dump-select").select_option(selected)
    expect(page.locator("#m-src")).to_contain_text(chosen["artifact_id"])
    expect(page.locator("#m-src")).not_to_contain_text(other["artifact_id"])
    workbench.state_capture_context(note=f"{chosen['vm_label']} only")

    refreshed = {row["artifact_id"]: row for row in workbench.dump_rows()}
    analysis_job = refreshed[chosen["artifact_id"]]["job_id"]
    assert analysis_job and analysis_job != refreshed[other["artifact_id"]]["job_id"]
    workbench.upload_isf(analysis_job)

    # Re-select after the reload: the run must use what is on screen now, and
    # the assertion below has to be about the operator's choice, not the order
    # the inventory happened to arrive in.
    page.reload()
    page.locator("#dump-select").select_option(selected)
    expect(page.locator("#m-src")).to_contain_text(chosen["artifact_id"])
    _run_plugin(page, reload=False)

    memory = _run_record(workbench, analysis_job)["execution"]["inputs"]["memory"]
    assert memory["artifact_id"] == chosen["artifact_id"]
    assert memory["artifact_id"] != other["artifact_id"]
    assert chosen["file"] in memory["path"]
    assert other["file"] not in memory["path"]
