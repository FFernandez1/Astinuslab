"""Playground UI e2e suite.

Run with:
    pytest tests/python/e2e/ -q
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Iterable

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

# Generated labels are `<role>-<noun>-<4 hex>`, capped at 16 characters, with
# the role abbreviated -- see static/playground/name-generator.js and its unit
# test in tests/browser/unit/playground/name-generator.test.mjs, which is the
# authority on the format. The full-word forms these patterns used to match
# (`target-<word>-<word>-NN`) have not been generated since the labels were
# shortened.
TARGET_LABEL_RE = re.compile(r"\btgt-[a-z]+-[0-9a-f]{4}\b")
ATTACKER_LABEL_RE = re.compile(r"\batk-[a-z]+-[0-9a-f]{4}\b")
NETWORK_NAME_RE = re.compile(r"\bnet-[a-z]+-[0-9a-f]{4}\b")
E2E_CSRF_TOKEN = "playground-e2e-csrf-token"


# Three rules, hand-written, all of them "does this element have an accessible
# name". This is NOT axe-core and must not be named as if it were: contrast,
# focus order, ARIA validity, landmark structure and keyboard traps are all
# unchecked. Loading real axe-core from node_modules would cover those and is
# the open item here; it needs a new frontend dependency, so it is a decision
# rather than an edit.
ACCESSIBLE_NAME_CHECK_SOURCE = r"""
window.__accessibleNameCheck = {
  run: async () => {
    const violations = [];
    const add = (id, impact, nodes, help) => {
      if (nodes.length) violations.push({ id, impact, help, nodes: nodes.map((node) => ({ target: [node] })) });
    };
    const selector = (el) => {
      if (el.id) return `#${CSS.escape(el.id)}`;
      const cls = Array.from(el.classList || []).slice(0, 2).map((c) => `.${CSS.escape(c)}`).join("");
      return `${el.tagName.toLowerCase()}${cls}`;
    };
    const nameOf = (el) => (el.getAttribute("aria-label") || el.getAttribute("title") || el.textContent || "").trim();
    add("button-name", "critical",
      Array.from(document.querySelectorAll("button, [role='button']")).filter((el) => !nameOf(el)).map(selector),
      "Buttons must have discernible text");
    add("aria-dialog-name", "serious",
      Array.from(document.querySelectorAll("[role='dialog']")).filter((el) => {
        const id = el.getAttribute("aria-labelledby");
        return !(el.getAttribute("aria-label") || (id && document.getElementById(id)?.textContent?.trim()));
      }).map(selector),
      "Dialogs must have an accessible name");
    add("image-alt", "critical",
      Array.from(document.querySelectorAll("img")).filter((el) => !el.hasAttribute("alt")).map(selector),
      "Images must have alternate text");
    return { violations };
  }
};
"""


def _api_json(base_url: str, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Cookie": f"astinuslab_csrf={E2E_CSRF_TOKEN}",
        "X-CSRF-Token": E2E_CSRF_TOKEN,
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{base_url}/api/v1/playground{path}",
        data=data,
        method=method,
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = response.read().decode("utf-8")
    return json.loads(payload) if payload else {}


def _api_status(base_url: str, method: str, path: str, body: dict | None = None) -> int:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Cookie": f"astinuslab_csrf={E2E_CSRF_TOKEN}",
        "X-CSRF-Token": E2E_CSRF_TOKEN,
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{base_url}/api/v1/playground{path}",
        data=data,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _create_network_and_vm(base_url: str, *, label: str = "web-1") -> dict:
    session = _api_json(
        base_url,
        "POST",
        "/networks",
        {"name": "labnet", "subnet_cidr": "10.42.2.0/24", "isolated": True},
    )
    network = session["networks"][0]
    vm = _api_json(
        base_url,
        "POST",
        "/vms",
        {
            "label": label,
            "image_id": "ubuntu-22-04-default",
            "role": "target",
            "network_ids": [network["id"]],
            "disk_gb": 20,
        },
    )
    return {"session": session, "network": network, "vm": vm}


def _detail_panel(page: Page):
    return page.locator("#pg-detail-panel")


def _accept_dialogs(page: Page) -> None:
    page.on("dialog", lambda dialog: dialog.accept())


def _confirm_custom_dialog(page: Page) -> None:
    dialog = page.locator(".modal-overlay--confirm")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("button", name="Cancel")).to_be_visible()
    dialog.locator("[data-confirm-yes]").click()
    expect(dialog).to_have_count(0)


def _tab_until_focused(page: Page, selector: str, *, max_tabs: int = 30):
    for _ in range(max_tabs):
        page.keyboard.press("Tab")
        if page.evaluate(
            """(selector) => {
                const active = document.activeElement;
                return Boolean(active && active.matches(selector));
            }""",
            selector,
        ):
            return page.locator(":focus")
    raise AssertionError(f"Could not tab to {selector}")


def _inject_accessible_name_check(page: Page) -> None:
    page.evaluate("source => { Function(source)(); }", ACCESSIBLE_NAME_CHECK_SOURCE)


def _assert_no_missing_accessible_names(page: Page) -> None:
    """Every button, dialog and image on the page can be named by assistive tech.

    That is the whole promise. It is a narrow one -- see the note on
    ACCESSIBLE_NAME_CHECK_SOURCE for what it does not look at.
    """
    _inject_accessible_name_check(page)
    result = page.evaluate("() => __accessibleNameCheck.run()")
    unnamed = [v for v in result["violations"] if v.get("impact") in {"serious", "critical"}]
    assert unnamed == [], f"elements with no accessible name: {unnamed}"


def _expect_texts(locator, texts: Iterable[str]) -> None:
    for text in texts:
        expect(locator).to_contain_text(text)


def test_playground_operator_happy_path(playground_base_url: str, page: Page, create_vm_wizard):
    _accept_dialogs(page)
    wizard = create_vm_wizard(playground_base_url).open()
    expect(wizard.modal).to_be_visible()
    assert page.locator(".pg-hud-images .pg-hud-image").count() >= 2
    page.locator('#create-fields input[name="label"]').fill("web-1")
    page.get_by_role("button", name="Next").click()
    page.get_by_role("button", name="Next").click()

    page.locator(".pg-hud-netadd").click()
    expect(page.locator("#create-network-modal")).to_be_visible()
    page.locator("#create-network-name").fill("labnet")
    page.locator("#create-network-cidr").fill("10.42.2.0/24")
    page.locator("#create-network-submit").click()
    expect(page.locator("#create-network-modal")).to_be_hidden()
    expect(page.locator(".pg-hud-netbox .pg-hud-nrow")).to_have_count(1)
    assert _api_status(playground_base_url, "GET", "/sessions/current") == 404

    page.get_by_role("button", name="Next").click()
    page.locator("#create-vm-submit").click()
    expect(page.locator("#create-vm-modal")).to_be_hidden()

    vm_node = page.locator('.pg-v2-map-node[data-type="vm"]', has_text="web-1").first
    expect(vm_node).to_have_attribute("data-state", "running", timeout=10_000)
    vm_node.click()
    panel = _detail_panel(page)
    _expect_texts(panel, ["web-1", "ubuntu 22.04", "10.42.2.10", "Capture RAM", "Capture Disk"])

    network_node = page.locator('.pg-v2-map-node[data-type="network"]').first
    network_node.click()
    _expect_texts(panel, ["labnet", "10.42.2.0/24", "isolated", "libvirt", "1 VM", "1 attached VM(s)"])
    expect(panel.get_by_role("button", name="Delete Network")).to_be_disabled()

    vm_node.click()
    panel.get_by_role("button", name="Capture RAM").click()
    stream = page.locator("#stream-log")
    expect(stream).to_contain_text("queued", timeout=30_000)
    expect(stream).to_contain_text("running", timeout=30_000)
    expect(stream).to_contain_text("completed", timeout=30_000)

    panel.get_by_role("button", name="Destroy VM").click()
    _confirm_custom_dialog(page)
    expect(page.locator('.pg-v2-map-node[data-type="vm"]', has_text="web-1")).to_have_count(0, timeout=10_000)

    network_node = page.locator('.pg-v2-map-node[data-type="network"]').first
    expect(network_node).to_be_visible()
    network_node.click()
    _expect_texts(panel, ["0 VMs", "Delete Network"])
    expect(panel.get_by_role("button", name="Delete Network")).to_be_enabled()

    panel.get_by_role("button", name="Delete Network").click()
    expect(page.locator('.pg-v2-map-node[data-type="network"]')).to_have_count(0, timeout=10_000)

    page.locator("#btn-end-session").click()
    _confirm_custom_dialog(page)
    # Cleanup does not drop the operator straight back to an empty board: the
    # page holds data-state="cleanup" and reports where the evidence went until
    # the banner is dismissed. Reading the report is the last step of ending a
    # lab, so the test takes it.
    status = page.locator("#pg-top-metrics")
    expect(status).to_contain_text("Session ended", timeout=10_000)
    status.get_by_role("button", name="Dismiss").click()
    expect(page.locator("#playground-root")).to_have_attribute("data-state", "idle", timeout=10_000)
    expect(page.locator("#btn-end-session")).to_have_attribute("hidden", "")


def test_playground_draft_network_cancel_does_not_create_session(
    playground_base_url: str, page: Page, create_vm_wizard
):
    create_vm_wizard(playground_base_url).open()
    page.locator('#create-fields input[name="label"]').fill("web-cancel")
    page.get_by_role("button", name="Next").click()
    page.get_by_role("button", name="Next").click()

    page.locator(".pg-hud-netadd").click()
    page.locator("#create-network-name").fill("cancelnet")
    page.locator("#create-network-cidr").fill("10.42.3.0/24")
    page.locator("#create-network-submit").click()
    expect(page.locator("#create-network-modal")).to_be_hidden()
    expect(page.locator(".pg-hud-netbox .pg-hud-nrow")).to_have_count(1)

    page.locator("#create-vm-close").click()
    expect(page.locator("#create-vm-modal")).to_be_hidden()
    assert _api_status(playground_base_url, "GET", "/sessions/current") == 404


def test_playground_vm_label_input_keeps_focus_while_typing(playground_base_url: str, page: Page, create_vm_wizard):
    create_vm_wizard(playground_base_url).open()
    label = page.locator('#create-fields input[name="label"]')
    expect(label).to_be_visible()
    label.click()
    page.keyboard.type("web-focus-1")

    expect(label).to_be_focused()
    expect(label).to_have_value("web-focus-1")
    assert page.evaluate("() => document.activeElement?.getAttribute('name')") == "label"


def test_playground_blank_target_label_uses_generated_funny_id(playground_base_url: str, page: Page, create_vm_wizard):
    create_vm_wizard(playground_base_url).open()
    page.get_by_role("button", name="Next").click()
    page.get_by_role("button", name="Next").click()
    page.get_by_role("button", name="Next").click()
    review_text = page.locator("#create-fields").inner_text()
    match = TARGET_LABEL_RE.search(review_text)
    assert match, review_text

    page.locator("#create-vm-submit").click()
    expect(page.locator("#create-vm-modal")).to_be_hidden()
    expect(page.locator('.pg-v2-map-node[data-type="vm"]', has_text=match.group(0))).to_be_visible(timeout=10_000)


def test_playground_blank_attacker_label_uses_generated_funny_id(
    playground_base_url: str, page: Page, create_vm_wizard
):
    create_vm_wizard(playground_base_url).open("attacker")
    placeholder = page.locator('#create-fields input[name="label"]').get_attribute("placeholder")
    assert placeholder is not None
    assert ATTACKER_LABEL_RE.match(placeholder)

    page.locator("#create-vm-submit").click()
    expect(page.locator("#create-vm-modal")).to_be_hidden()
    expect(page.locator('.pg-v2-map-node[data-type="vm"]', has_text=placeholder)).to_be_visible(timeout=10_000)


def test_playground_blank_draft_network_name_uses_generated_funny_id(
    playground_base_url: str, page: Page, create_vm_wizard
):
    create_vm_wizard(playground_base_url).open()
    page.get_by_role("button", name="Next").click()
    page.get_by_role("button", name="Next").click()
    page.locator(".pg-hud-netadd").click()
    placeholder = page.locator("#create-network-name").get_attribute("placeholder")
    assert placeholder is not None
    assert NETWORK_NAME_RE.match(placeholder)

    page.locator("#create-network-cidr").fill("10.42.4.0/24")
    page.locator("#create-network-submit").click()
    expect(page.locator("#create-network-modal")).to_be_hidden()
    expect(page.locator(".pg-hud-netbox .pg-hud-nrow", has_text=placeholder)).to_be_visible()


def test_playground_typed_draft_network_name_is_preserved(playground_base_url: str, page: Page, create_vm_wizard):
    create_vm_wizard(playground_base_url).open()
    page.get_by_role("button", name="Next").click()
    page.get_by_role("button", name="Next").click()
    page.locator(".pg-hud-netadd").click()
    page.locator("#create-network-name").fill("manualnet")
    page.locator("#create-network-cidr").fill("10.42.5.0/24")
    page.locator("#create-network-submit").click()

    expect(page.locator("#create-network-modal")).to_be_hidden()
    expect(page.locator(".pg-hud-netbox .pg-hud-nrow", has_text="manualnet")).to_be_visible()


def test_playground_keyboard_paths_and_accessible_names(playground_base_url: str, page: Page, create_vm_wizard):
    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(f"{playground_base_url}/playground")
    _assert_no_missing_accessible_names(page)

    _create_network_and_vm(playground_base_url)
    page.goto(f"{playground_base_url}/playground")
    vm_node = page.locator('.pg-v2-map-node[data-type="vm"]').first
    expect(vm_node).to_have_attribute("data-state", "running", timeout=10_000)
    _assert_no_missing_accessible_names(page)

    _tab_until_focused(page, '.pg-v2-map-node[data-type="vm"]')
    page.keyboard.press("Enter")
    expect(vm_node).to_have_attribute("aria-pressed", "true")

    network_node = page.locator(".pg-v2-map-node[data-type='network']").first
    _tab_until_focused(page, ".pg-v2-map-node[data-type='network']")
    page.keyboard.press("Enter")
    expect(network_node).to_have_attribute("aria-pressed", "true")
    _expect_texts(_detail_panel(page), ["labnet", "Network Details"])

    wizard = create_vm_wizard(playground_base_url).open(goto=False)
    expect(wizard.modal).to_be_visible()
    assert page.locator(".pg-hud-images .pg-hud-image").count() >= 2
    _assert_no_missing_accessible_names(page)

    second_image = page.locator(".pg-hud-images .pg-hud-image").nth(1)
    expect(second_image).to_have_attribute("aria-checked", "false")
    second_image.focus()
    page.keyboard.press("Space")
    expect(second_image).to_have_attribute("aria-checked", "true")
