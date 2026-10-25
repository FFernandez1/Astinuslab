"""Two tabs, one lab.

The product allows exactly one playground session, and two tabs open on it is
the ordinary operator mistake. E2E-P01 was written expecting the second tab's
create to be refused with ``a playground session is already active``. It is not:
that 409 belongs to ``POST /sessions``, which the wizard never calls. The wizard
posts to ``/vms``, and ``_ensure_active_session_id`` deliberately joins whatever
session is already open. Two tabs therefore build *the same lab together*, and
what is actually untested is the reconciliation: whether a tab that did not make
a change finds out about it without being reloaded.

Every assertion below is made on a tab that has not navigated since it was
opened. A reload would prove nothing -- the page reads the session on load, so a
reloaded tab is correct by construction.

The session id is read from ``#pg-foot-sid`` with ``text_content()``: the header
renders it through ``text-transform``, so ``inner_text()`` would return it
uppercased and compare unequal to the API's own value.

Nothing is mocked. Both tabs drive the real UI against one real backend.

Run with:
    pytest tests/python/e2e/ -q
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest
from playwright.sync_api import Browser, Page, expect

pytestmark = pytest.mark.e2e

E2E_CSRF_TOKEN = "playground-e2e-csrf-token"


@pytest.fixture()
def second_tab(browser: Browser):
    """A second browser context: another tab, sharing nothing but the backend."""
    context = browser.new_context()
    tab = context.new_page()
    tab.set_viewport_size({"width": 1400, "height": 900})
    yield tab
    context.close()


def _api(base_url: str, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Cookie": f"astinuslab_csrf={E2E_CSRF_TOKEN}", "X-CSRF-Token": E2E_CSRF_TOKEN}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{base_url}/api/v1/playground{path}",
        data=data,
        method=method,
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = response.read().decode("utf-8")
    return json.loads(payload) if payload else {}


def _open_idle(tab: Page, base_url: str) -> None:
    tab.set_viewport_size({"width": 1400, "height": 900})
    tab.goto(f"{base_url}/playground")
    expect(tab.locator("#playground-root")).to_have_attribute("data-state", "idle")


def _vm_nodes(tab: Page):
    return tab.locator('.pg-v2-map-node[data-type="vm"]')


def _session_id(tab: Page) -> str:
    return (tab.locator("#pg-foot-sid").text_content() or "").strip()


def _create_target(wizard_factory, base_url: str, tab: Page, label: str):
    """Walk the real wizard in `tab`, which is already on the page and must stay there."""
    wizard = wizard_factory(base_url, on=tab).open(goto=False, label=label)
    wizard.advance_to_submit()
    wizard.submit()
    expect(wizard.modal).to_be_hidden()
    return wizard


def test_a_second_tab_picks_up_a_lab_it_did_not_create(
    playground_base_url: str, page: Page, second_tab: Page, create_vm_wizard
):
    _open_idle(page, playground_base_url)
    _open_idle(second_tab, playground_base_url)

    _create_target(create_vm_wizard, playground_base_url, page, "web-1")
    expect(_vm_nodes(page)).to_have_count(1, timeout=15_000)

    # The second tab has not navigated. Its own poller is the only thing that
    # can tell it a lab now exists.
    expect(second_tab.locator("#playground-root")).to_have_attribute("data-state", "active", timeout=20_000)
    expect(_vm_nodes(second_tab)).to_have_count(1, timeout=20_000)
    expect(_vm_nodes(second_tab).first).to_contain_text("web-1")
    assert _session_id(second_tab) == _session_id(page) != "no session"


def test_a_vm_created_in_the_second_tab_joins_the_same_lab(
    playground_base_url: str, page: Page, second_tab: Page, create_vm_wizard
):
    _open_idle(page, playground_base_url)
    _open_idle(second_tab, playground_base_url)

    _create_target(create_vm_wizard, playground_base_url, page, "web-1")
    expect(second_tab.locator("#playground-root")).to_have_attribute("data-state", "active", timeout=20_000)
    opened_lab = _session_id(page)

    wizard = _create_target(create_vm_wizard, playground_base_url, second_tab, "web-2")
    # The wizard closed, so it was not refused -- but say so, because the
    # refusal this test was written to expect would have landed right here.
    assert (wizard.error.text_content() or "").strip() == ""

    for tab in (page, second_tab):
        expect(_vm_nodes(tab)).to_have_count(2, timeout=20_000)
    assert _session_id(second_tab) == opened_lab, "the second tab started a lab of its own"
    assert _session_id(page) == opened_lab
    assert sorted(vm["label"] for vm in _api(playground_base_url, "GET", "/sessions/current")["vms"]) == [
        "web-1",
        "web-2",
    ]


def test_ending_the_lab_in_one_tab_returns_the_other_to_idle(
    playground_base_url: str, page: Page, second_tab: Page, create_vm_wizard
):
    _open_idle(page, playground_base_url)
    _open_idle(second_tab, playground_base_url)
    _create_target(create_vm_wizard, playground_base_url, page, "web-1")
    expect(_vm_nodes(second_tab)).to_have_count(1, timeout=20_000)

    # The second tab is looking at the machine while it goes away.
    _vm_nodes(second_tab).first.click()
    expect(second_tab.locator("#pg-detail-panel")).to_contain_text("web-1")

    page.locator("#btn-end-session").click()
    confirm = page.locator(".modal-overlay--confirm")
    expect(confirm).to_be_visible()
    confirm.locator("[data-confirm-yes]").click()
    # The tab that asked for cleanup keeps the "Session ended" report until it
    # is dismissed; only the tab that did not ask returns to idle on its own.
    status = page.locator("#pg-top-metrics")
    expect(status).to_contain_text("Session ended", timeout=15_000)
    status.get_by_role("button", name="Dismiss").click()
    expect(page.locator("#playground-root")).to_have_attribute("data-state", "idle", timeout=15_000)

    # Still no reload. The second tab must stop offering a lab that is gone --
    # a stale panel here is a Destroy VM button pointed at nothing.
    expect(second_tab.locator("#playground-root")).to_have_attribute("data-state", "idle", timeout=20_000)
    expect(_vm_nodes(second_tab)).to_have_count(0, timeout=20_000)
    expect(second_tab.locator("#pg-detail-panel")).to_be_empty()
    expect(second_tab.locator("#btn-end-session")).to_have_attribute("hidden", "")
    assert _session_id(second_tab) == "no session"


def test_the_tab_that_loses_the_last_slot_is_told_and_keeps_its_form(
    capped_base_url: str, page: Page, second_tab: Page, create_vm_wizard
):
    """Two wizards opened while there was room; only one of them can still win.

    The cap refusal is already covered for a tab that knew the lab was full
    before it opened its wizard. This is the other way an operator meets it:
    both tabs were right when they started, and one of them stopped being right
    while its form was on screen. The loser must be told which rule it broke and
    must not lose four steps of input to it -- and the winner's machine must be
    the one that exists.

    What this proves is the losing screen. That the database admits exactly one
    of two simultaneous creates is a property of the write path and is proved
    below the browser; sequencing the two submits here is what makes the
    loser's half of it observable at all.
    """
    _open_idle(page, capped_base_url)
    _open_idle(second_tab, capped_base_url)
    _create_target(create_vm_wizard, capped_base_url, page, "web-1")
    expect(_vm_nodes(second_tab)).to_have_count(1, timeout=20_000)

    # One slot left, and two wizards opened against it. Neither is wrong yet.
    winner = create_vm_wizard(capped_base_url).open(goto=False, label="web-winner")
    loser = create_vm_wizard(capped_base_url, on=second_tab).open(goto=False, label="web-loser")
    winner.advance_to_submit()
    loser.advance_to_submit()

    winner.submit()
    expect(winner.modal).to_be_hidden()
    expect(_vm_nodes(page)).to_have_count(2, timeout=20_000)

    loser.submit()
    expect(loser.error).to_be_visible(timeout=15_000)
    expect(loser.error).to_contain_text("capacity")
    # The wizard stays open on the operator's own input, and stays submittable:
    # freeing a slot has to be enough to retry, without retyping the form. The
    # label field belongs to the first step and is not mounted on the last, so
    # the check walks back to it rather than asserting on a control the refused
    # screen does not have.
    expect(loser.modal).to_be_visible()
    expect(loser.submit_button).to_be_enabled()
    # ...and behind the modal the losing tab has already reconciled to the lab
    # as it now is. This is the claim the single-tab cap test cannot make: a
    # tab whose own wizard is open must still be told what the other tab did,
    # or the operator retries into a refusal they have no way to understand.
    expect(_vm_nodes(second_tab)).to_have_count(2, timeout=20_000)
    loser.back_to_start()
    expect(second_tab.locator('#create-fields input[name="label"]')).to_have_value("web-loser")

    labels = sorted(vm["label"] for vm in _api(capped_base_url, "GET", "/sessions/current")["vms"])
    assert labels == ["web-1", "web-winner"], labels
