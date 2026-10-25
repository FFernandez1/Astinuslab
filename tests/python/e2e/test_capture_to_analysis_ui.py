"""The seam between capturing evidence and analysing it.

The Playground publishes an artifact; the Volatility workbench lists it for
analysis. Each half is covered on its own -- tests/python/playground for the
capture, tests/python/forensics for analysis context and custody -- but nothing
carried one artifact across both against a real backend through the real pages.
Misattributing a memory image to the wrong VM is the worst defect this product
can emit, and it is exactly the kind that lives in a seam.

Scope: identity, not execution. What is asserted here is that the capture
arrives in the analysis inventory carrying its own session, VM and artifact
identity, that two captures do not cross, and that the run stays blocked while
no analysis tool is configured -- the state a first-time operator meets, which
is why this tier keeps the Volatility toolchain pinned absent.

What a completed run attributes its results to is covered next door in
test_volatility_run_ui.py, which supplies a stand-in for the operator-supplied
binary this project never ships. A run against real Volatility remains
tests/python/host_real/test_volatility_real.py.

Run with:
    pytest tests/python/e2e/ -q
"""

from __future__ import annotations

import json
import urllib.request

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

E2E_CSRF_TOKEN = "playground-e2e-csrf-token"


def _api(base_url: str, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Cookie": f"astinuslab_csrf={E2E_CSRF_TOKEN}", "X-CSRF-Token": E2E_CSRF_TOKEN}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(f"{base_url}{path}", data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = response.read().decode("utf-8")
    return json.loads(payload) if payload else {}


def _spawn_target(base_url: str, label: str, network_id: str | None = None) -> dict:
    if network_id is None:
        session = _api(
            base_url,
            "POST",
            "/api/v1/playground/networks",
            {"name": "labnet", "subnet_cidr": "10.42.2.0/24", "isolated": True},
        )
        network_id = session["networks"][0]["id"]
    vm = _api(
        base_url,
        "POST",
        "/api/v1/playground/vms",
        {
            "label": label,
            "image_id": "ubuntu-22-04-default",
            "role": "target",
            "network_ids": [network_id],
            "disk_gb": 20,
        },
    )
    return {"vm": vm, "network_id": network_id}


def _capture_ram(page: Page, base_url: str, label: str) -> None:
    """Take a RAM capture through the Playground UI and wait for publication."""
    page.goto(f"{base_url}/playground")
    node = page.locator('.pg-v2-map-node[data-type="vm"]', has_text=label).first
    expect(node).to_have_attribute("data-state", "running", timeout=15_000)
    node.click()
    page.locator("#pg-detail-panel").get_by_role("button", name="Capture RAM").click()
    expect(page.locator("#stream-log")).to_contain_text("completed", timeout=60_000)


def _published_capture(base_url: str, vm_id: str) -> dict:
    captures = _api(base_url, "GET", f"/api/v1/playground/vms/{vm_id}/captures")
    completed = [row for row in captures if row["status"] in {"completed", "succeeded"}]
    assert completed, captures
    return completed[0]


def test_a_playground_capture_reaches_the_workbench_with_its_identity(playground_base_url: str, page: Page):
    page.set_viewport_size({"width": 1600, "height": 900})
    spawned = _spawn_target(playground_base_url, "target-A")
    vm = spawned["vm"]
    _capture_ram(page, playground_base_url, "target-A")

    capture = _published_capture(playground_base_url, vm["id"])

    # The analysis inventory is the contract between the two features, and the
    # row must trace back to the capture the Playground reported -- same VM, same
    # session, same file -- not merely look plausible.
    dumps = _api(playground_base_url, "GET", "/api/v1/volatility/dumps")["dumps"]
    mine = [row for row in dumps if row.get("artifact_id")]
    assert len(mine) == 1, dumps
    entry = mine[0]
    assert entry["vm_id"] == vm["id"]
    assert entry["vm_label"] == "target-A"
    assert entry["session_id"] == capture["session_id"]
    assert entry["file"] == capture["path"].rsplit("/", 1)[-1]
    assert capture["id"] in entry["file"]

    # ...and the workbench shows that identity rather than a neighbour's.
    page.goto(f"{playground_base_url}/volatility")
    option = page.locator("#dump-select option").first
    expect(option).to_contain_text("target-A")
    expect(option).to_contain_text(entry["artifact_id"])
    expect(page.locator("#m-src")).to_contain_text("target-A")
    expect(page.locator("#m-src")).to_contain_text(entry["artifact_id"])

    # A Playground capture arrives without a verified OS context, so the
    # workbench must ask for one instead of assuming.
    expect(page.locator("#analysis-context-form")).to_be_visible()


def test_two_captures_do_not_cross_in_the_workbench(playground_base_url: str, page: Page):
    """The misattribution case: two VMs in one session, one dump each."""
    page.set_viewport_size({"width": 1600, "height": 900})
    first = _spawn_target(playground_base_url, "target-A")
    _spawn_target(playground_base_url, "target-B", network_id=first["network_id"])

    _capture_ram(page, playground_base_url, "target-A")
    _capture_ram(page, playground_base_url, "target-B")

    dumps = _api(playground_base_url, "GET", "/api/v1/volatility/dumps")["dumps"]
    by_label = {row["vm_label"]: row for row in dumps if row.get("artifact_id")}
    assert set(by_label) == {"target-A", "target-B"}, dumps
    assert by_label["target-A"]["artifact_id"] != by_label["target-B"]["artifact_id"]

    page.goto(f"{playground_base_url}/volatility")
    expect(page.locator("#dump-select option")).to_have_count(2)

    # Selecting one shows that one's identity, with nothing borrowed from the other.
    for label in ("target-A", "target-B"):
        entry = by_label[label]
        other = by_label["target-B" if label == "target-A" else "target-A"]
        page.locator("#dump-select").select_option(f"artifact:{entry['artifact_id']}")
        expect(page.locator("#m-src")).to_contain_text(label)
        expect(page.locator("#m-src")).to_contain_text(entry["artifact_id"])
        expect(page.locator("#m-src")).not_to_contain_text(other["artifact_id"])


def test_the_run_control_stays_blocked_while_the_analysis_tool_is_unavailable(playground_base_url: str, page: Page):
    """No operator-supplied Volatility binary is configured here, and must not be faked.

    The workbench is enabled (`vol3_enabled` defaults true) but not ready, which
    is the state most first-time operators meet. Running must be refused rather
    than attempted.
    """
    page.set_viewport_size({"width": 1600, "height": 900})
    _spawn_target(playground_base_url, "target-A")
    _capture_ram(page, playground_base_url, "target-A")

    page.goto(f"{playground_base_url}/volatility")
    workbench = page.locator("#vol-workbench")
    expect(workbench).to_have_attribute("data-vol3-enabled", "1")
    expect(workbench).to_have_attribute("data-analysis-ready", "0")
    expect(page.locator("#run-btn")).to_be_disabled()
    expect(page.locator("#vol-readiness-banner")).to_be_visible()
