"""One click on a capture control, one acquisition.

``POST /vms/{id}/captures`` mints a fresh ``acquisition_id`` on every call and
carries no idempotency key, so nothing below the browser can stop a second click
from taking a second dump of the same machine. In fake mode the duplicate is a
28-byte file; on a real host it is another full-size memory image, racing the
first one for the same disk and published beside it as separate evidence.

The clicks here are dispatched through the element reference captured *before*
the first click, so the second one lands on the button node the handler was
bound to even after the panel has re-rendered. Disabling the control on re-render
is therefore not enough to pass: the guard has to be in the handler.

Nothing is mocked. The captures are taken by the real backend in fake-libvirt
mode and counted through the real API.

Run with:
    pytest tests/python/e2e/ -q
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

E2E_CSRF_TOKEN = "playground-e2e-csrf-token"

# Both capture controls post to the same endpoint and neither is reversible.
CAPTURE_CONTROLS = [("Capture RAM", "memory"), ("Capture Disk", "disk")]

# Reads the live buttons out of the action bar by label, from inside the same
# task that clicked -- the only way to observe the guard's own DOM state without
# racing a capture that finishes in well under a second.
_CLICK_TWICE_AND_REPORT = """
(el) => {
  const read = (name) => {
    const b = [...document.querySelectorAll('.pg-v2-actionbar-row button')].find(
      (n) => n.textContent.trim() === name,
    );
    return {
      locked: b?.getAttribute('aria-disabled') === 'true',
      focusable: Boolean(b) && !b.disabled,
      title: b?.title || '',
    };
  };
  el.click();
  const after = { ram: read('Capture RAM'), disk: read('Capture Disk') };
  el.click();
  return after;
}
"""


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


def _seed_running_vm(base_url: str, label: str = "web-1") -> str:
    """One network, one running target. Returns the VM id."""
    session = _api(
        base_url,
        "POST",
        "/networks",
        {"name": "labnet", "subnet_cidr": "10.42.2.0/24", "isolated": True},
    )
    network_id = session["networks"][0]["id"]
    vm = _api(
        base_url,
        "POST",
        "/vms",
        {
            "label": label,
            "image_id": "ubuntu-22-04-default",
            "role": "target",
            "network_ids": [network_id],
            "disk_gb": 20,
        },
    )
    return str(vm["id"])


def _open_vm_panel(page: Page, base_url: str, label: str = "web-1"):
    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(f"{base_url}/playground")
    node = page.locator('.pg-v2-map-node[data-type="vm"]', has_text=label).first
    expect(node).to_have_attribute("data-state", "running", timeout=15_000)
    node.click()
    panel = page.locator("#pg-detail-panel")
    expect(panel).to_contain_text("Capture RAM")
    return panel


def _acquisitions(base_url: str, vm_id: str) -> list[dict]:
    return list(_api(base_url, "GET", f"/vms/{vm_id}/captures"))


@pytest.mark.parametrize("label,kind", CAPTURE_CONTROLS)
def test_clicking_a_capture_control_twice_takes_one_dump(playground_base_url: str, page: Page, label: str, kind: str):
    vm_id = _seed_running_vm(playground_base_url)
    panel = _open_vm_panel(page, playground_base_url)

    panel.get_by_role("button", name=label).evaluate(_CLICK_TWICE_AND_REPORT)
    expect(page.locator("#stream-log")).to_contain_text("completed", timeout=30_000)
    page.wait_for_timeout(1_000)

    taken = _acquisitions(playground_base_url, vm_id)
    assert [a["kind"] for a in taken] == [kind], f"expected one {kind} acquisition, got {taken}"


def test_the_capture_controls_lock_while_an_acquisition_is_running(playground_base_url: str, page: Page):
    _seed_running_vm(playground_base_url)
    panel = _open_vm_panel(page, playground_base_url)

    after = panel.get_by_role("button", name="Capture RAM").evaluate(_CLICK_TWICE_AND_REPORT)

    # Both controls, not just the one that was clicked: the page has one capture
    # console, so a second acquisition started alongside the first would write
    # its phases into the same log with nothing to tell the two apart.
    assert after["ram"]["locked"], "Capture RAM read as available while its own acquisition was running"
    assert after["disk"]["locked"], "Capture Disk read as available while a RAM acquisition was running"
    for control in ("ram", "disk"):
        assert after[control]["title"], f"the locked {control} control gives the operator no reason"
        # `disabled` would drop the control out of the tab order mid-capture and
        # take the operator's focus with it; the refusal belongs in the handler.
        assert after[control]["focusable"], f"the locked {control} control left the tab order"


def test_the_capture_controls_unlock_once_the_acquisition_finishes(playground_base_url: str, page: Page):
    vm_id = _seed_running_vm(playground_base_url)
    panel = _open_vm_panel(page, playground_base_url)

    panel.get_by_role("button", name="Capture RAM").click()
    expect(page.locator("#stream-log")).to_contain_text("completed", timeout=30_000)

    ram = panel.get_by_role("button", name="Capture RAM")
    expect(ram).not_to_have_attribute("aria-disabled", "true", timeout=10_000)
    ram.click()
    expect(page.locator("#stream-log")).to_contain_text("completed", timeout=30_000)
    page.wait_for_timeout(1_000)

    taken = _acquisitions(playground_base_url, vm_id)
    assert len(taken) == 2, f"a deliberate second capture must still be possible, got {taken}"
