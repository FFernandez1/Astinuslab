"""Reload and browser navigation against a live lab.

The Playground holds session state on the server and a 5-second poller in the
page, so the browser's own controls are a first-class hazard: a reload mid-form
must not leave resources behind, a reload with a lab running must bring the lab
back as the operator arranged it, and leaving and returning must not end up with
two pollers talking to one session.

Run with:
    pytest tests/python/e2e/ -q
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

E2E_CSRF_TOKEN = "playground-e2e-csrf-token"
POLL_INTERVAL_MS = 5100  # pollers.js INTERVAL_MS, plus a tick


def _api(base_url: str, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Cookie": f"astinuslab_csrf={E2E_CSRF_TOKEN}", "X-CSRF-Token": E2E_CSRF_TOKEN}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(f"{base_url}{path}", data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = response.read().decode("utf-8")
    return json.loads(payload) if payload else {}


def _session_status(base_url: str) -> int:
    try:
        _api(base_url, "GET", "/api/v1/playground/sessions/current")
        return 200
    except urllib.error.HTTPError as exc:
        return exc.code


def _seed_lab(base_url: str, label: str = "web-1") -> dict:
    session = _api(
        base_url,
        "POST",
        "/api/v1/playground/networks",
        {"name": "labnet", "subnet_cidr": "10.42.2.0/24", "isolated": True},
    )
    return _api(
        base_url,
        "POST",
        "/api/v1/playground/vms",
        {
            "label": label,
            "image_id": "ubuntu-22-04-default",
            "role": "target",
            "network_ids": [session["networks"][0]["id"]],
            "disk_gb": 20,
        },
    )


def _wait_for_saved_x(base_url: str, node_id: str, *, timeout: float = 10.0) -> float:
    """Return the x the server has stored for a node, waiting for the autosave."""
    deadline = time.monotonic() + timeout
    seen: float | None = None
    while time.monotonic() < deadline:
        layout = _api(base_url, "GET", "/api/v1/playground/sessions/current/topology-layout")
        rows = [row for row in layout["nodes"] if row["node_id"] == node_id]
        if rows:
            seen = rows[0]["x"]
            return seen
        time.sleep(0.1)
    raise AssertionError(f"topology layout for {node_id} was never saved (last seen {seen})")


def _node_position(page: Page, node_id: str) -> dict:
    return page.locator(f'.pg-v2-map-node[data-node-id="{node_id}"]').evaluate(
        "node => ({ left: node.style.left, top: node.style.top })"
    )


def test_reloading_mid_wizard_leaves_no_session_behind(playground_base_url: str, page: Page, create_vm_wizard):
    """Reload is the common way out of a form; cancel is the tested one.

    A draft network exists only in the page until the VM is created, so an
    abandoned wizard must leave the backend exactly as it found it.
    """
    wizard = create_vm_wizard(playground_base_url).open(label="web-abandoned")
    wizard.next(2).add_draft_network("cancelnet", "10.42.3.0/24")
    expect(page.locator(".pg-hud-netbox .pg-hud-nrow")).to_have_count(1)

    page.reload()

    assert _session_status(playground_base_url) == 404
    expect(page.locator("#playground-root")).to_have_attribute("data-state", "idle")
    expect(page.locator("#create-vm-modal")).to_be_hidden()


def test_a_reload_brings_the_lab_back_where_the_operator_left_it(playground_base_url: str, page: Page):
    """Node positions are the operator's own arrangement of the map."""
    page.set_viewport_size({"width": 1600, "height": 900})
    vm = _seed_lab(playground_base_url)
    page.goto(f"{playground_base_url}/playground")

    node = page.locator(f'.pg-v2-map-node[data-node-id="{vm["id"]}"]')
    expect(node).to_be_visible()
    origin = _node_position(page, vm["id"])
    node.focus()
    for _ in range(5):
        page.keyboard.press("ArrowRight")

    expected_left = _node_position(page, vm["id"])["left"]
    assert expected_left != origin["left"]
    # The autosave is a PATCH the page fires after the move; wait for it to land
    # rather than sleeping on it, so a slow machine waits and a fast one does not.
    expected_x = float(expected_left.removesuffix("px"))
    assert _wait_for_saved_x(playground_base_url, vm["id"]) == pytest.approx(expected_x)

    # text_content, not inner_text: the label is uppercased in CSS, and the
    # expect() assertion below compares the DOM text rather than the rendering.
    header = page.locator(".pg-header-label").text_content()
    assert header
    page.reload()

    node = page.locator(f'.pg-v2-map-node[data-node-id="{vm["id"]}"]')
    expect(node).to_be_visible()
    assert _node_position(page, vm["id"]) == {"left": expected_left, "top": origin["top"]}
    # Same lab, not a new one: the header names the session it named before.
    expect(page.locator(".pg-header-label")).to_have_text(header)
    expect(page.locator('.pg-v2-map-node[data-type="network"]')).to_have_count(1)


def test_leaving_and_returning_leaves_exactly_one_poller(playground_base_url: str, page: Page):
    """Two pollers on one session would double every request and race each other."""
    page.set_viewport_size({"width": 1600, "height": 900})
    vm = _seed_lab(playground_base_url)
    page.goto(f"{playground_base_url}/playground")
    expect(page.locator(f'.pg-v2-map-node[data-node-id="{vm["id"]}"]')).to_be_visible()

    page.goto(f"{playground_base_url}/jobs")
    page.clock.install()
    page.go_back()

    # The lab is still there, rendered from the server rather than a stale shell.
    expect(page.locator(f'.pg-v2-map-node[data-node-id="{vm["id"]}"]')).to_be_visible()

    polls: list[str] = []
    page.on("request", lambda request: polls.append(request.url) if "current-or-null" in request.url else None)
    for expected in (1, 2):
        page.clock.fast_forward(POLL_INTERVAL_MS)
        page.wait_for_timeout(300)
        assert len(polls) == expected, polls
