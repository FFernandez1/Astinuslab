"""What the operator is told when the backend refuses.

The API raises well-designed conflicts -- a label already in use, a CIDR too
small to host anything, a VM cap reached -- and every one of them is covered by
a backend test. None was covered at the point where it matters: the screen. A
refusal that never reaches the operator is a dead button, and the Playground has
already shipped that bug once (see render-alerts.js).

Nothing here is mocked. The conflicts are raised by the real backend and read
off the real wizard.

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
# tests/python/api/test_schemas_cidr.py owns the full grammar; these are the
# three an operator actually types.
BAD_CIDRS = [
    ("10.42.9.0/31", "at least 4 addresses"),
    ("not-a-cidr", "subnet_cidr"),
]


def _api(base_url: str, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Cookie": f"astinuslab_csrf={E2E_CSRF_TOKEN}", "X-CSRF-Token": E2E_CSRF_TOKEN}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(f"{base_url}{path}", data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = response.read().decode("utf-8")
    return json.loads(payload) if payload else {}


# Destroyed VMs stay in the session payload; the cap counts only the live ones.
_CAPACITY_EXEMPT = {"destroyed", "failed", "stopped", "shutoff", "shutdown"}


def _active_vms(base_url: str) -> list[dict]:
    return [vm for vm in _session(base_url)["vms"] if vm.get("state") not in _CAPACITY_EXEMPT]


def _session(base_url: str) -> dict:
    try:
        return _api(base_url, "GET", "/api/v1/playground/sessions/current")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"vms": [], "networks": []}
        raise


def _seed_lab(base_url: str, *labels: str) -> str:
    session = _api(
        base_url,
        "POST",
        "/api/v1/playground/networks",
        {"name": "labnet", "subnet_cidr": "10.42.2.0/24", "isolated": True},
    )
    network_id = session["networks"][0]["id"]
    for label in labels:
        _api(
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
    return network_id


def test_a_duplicate_label_is_explained_without_losing_the_form(playground_base_url: str, page: Page, create_vm_wizard):
    """Four steps of wizard input must survive a name clash."""
    _seed_lab(playground_base_url, "web-1")

    wizard = create_vm_wizard(playground_base_url).open(label="web-1")
    wizard.advance_to_submit().submit()

    error = wizard.error
    expect(error).to_be_visible()
    expect(error).to_contain_text("web-1")

    # The wizard stays open on the operator's work, and the label they typed is
    # still there to correct.
    expect(wizard.modal).to_be_visible()
    expect(wizard.submit_button).to_be_enabled()
    assert [vm["label"] for vm in _session(playground_base_url)["vms"]] == ["web-1"]

    # Correcting it succeeds without reopening the wizard.
    wizard.back_to_start().fill_label("web-2").advance_to_submit().submit()
    expect(wizard.modal).to_be_hidden()
    expect(page.locator('.pg-v2-map-node[data-type="vm"]', has_text="web-2")).to_be_visible(timeout=15_000)


@pytest.mark.parametrize("cidr,expected", BAD_CIDRS)
def test_an_unusable_subnet_is_refused_with_its_reason(
    playground_base_url: str, page: Page, create_vm_wizard, cidr: str, expected: str
):
    """A draft network is local until the VM is created, so the server has the
    last word. What matters is that its word reaches the operator and that
    nothing is left behind."""
    _seed_lab(playground_base_url)

    wizard = create_vm_wizard(playground_base_url).open(label="web-draft")
    wizard.next(2).add_draft_network("draftnet", cidr).next().submit()

    error = wizard.error
    expect(error).to_be_visible()
    expect(error).to_contain_text(expected)
    expect(wizard.modal).to_be_visible()

    # No half-built lab: the seeded network is still the only one, and no VM was
    # created from the rejected submission.
    session = _session(playground_base_url)
    assert [network["subnet_cidr"] for network in session["networks"]] == ["10.42.2.0/24"]
    assert session["vms"] == []


@pytest.mark.parametrize("label", ["web 1", "<img src=x>"])
def test_an_invalid_label_is_refused_before_the_wizard_will_advance(
    playground_base_url: str, page: Page, create_vm_wizard, label: str
):
    """`label` is the one free-text field on this form.

    The schema refuses anything outside ^[a-zA-Z0-9_-]{1,128}$, so markup never
    reaches the renderer -- better than escaping it on the way out. The wizard
    enforces the same rule up front rather than letting the operator walk three
    more steps to find out, and says which rule was broken.
    """
    _seed_lab(playground_base_url)

    wizard = create_vm_wizard(playground_base_url).open(label=label)

    expect(wizard.next_button).to_be_disabled()
    warning = wizard.warning
    expect(warning).to_be_visible()
    expect(warning).to_contain_text("VM label")
    assert _session(playground_base_url)["vms"] == []


def test_an_overlong_label_is_refused_by_the_server_and_explained(
    playground_base_url: str, page: Page, create_vm_wizard
):
    """The length rule is enforced in only one of the two places the pattern is.

    The input carries `pattern` but no `maxlength`, so 129 characters of a valid
    alphabet sail past the client and are refused by the schema. That is safe --
    the boundary holds -- but the operator learns about it three steps later
    than they learn about a bad character. Pinned as-is; tightening the client
    would be a behaviour change, not a bug fix.
    """
    _seed_lab(playground_base_url)

    wizard = create_vm_wizard(playground_base_url).open(label="L" * 129)
    wizard.advance_to_submit().submit()

    error = wizard.error
    expect(error).to_be_visible()
    expect(error).to_contain_text("128 characters")
    expect(wizard.modal).to_be_visible()
    assert _session(playground_base_url)["vms"] == []


def test_a_label_at_the_length_limit_is_accepted(playground_base_url: str, page: Page, create_vm_wizard):
    """The other side of the boundary: 128 is valid, 129 is not."""
    _seed_lab(playground_base_url)
    label = "L" * 128

    wizard = create_vm_wizard(playground_base_url).open(label=label)
    wizard.advance_to_submit().submit()

    expect(wizard.modal).to_be_hidden()
    assert [vm["label"] for vm in _session(playground_base_url)["vms"]] == [label]


def test_reaching_the_vm_cap_is_explained_and_recoverable(capped_base_url: str, page: Page, create_vm_wizard):
    """The point where a lab silently stops growing.

    Recovery is the half that makes this a UI test: destroying a VM must make
    the next create work without a reload.
    """
    network_id = _seed_lab(capped_base_url, "web-1", "web-2")
    assert len(_session(capped_base_url)["vms"]) == 2

    wizard = create_vm_wizard(capped_base_url).open(label="web-3")
    wizard.advance_to_submit().submit()

    error = wizard.error
    expect(error).to_be_visible()
    expect(error).to_contain_text("capacity")
    expect(wizard.submit_button).to_be_enabled()
    assert len(_session(capped_base_url)["vms"]) == 2

    # Free a slot through the API, then retry the same form.
    victim = _session(capped_base_url)["vms"][0]["id"]
    _api(capped_base_url, "DELETE", f"/api/v1/playground/vms/{victim}")
    expect(page.locator('.pg-v2-map-node[data-type="vm"]')).to_have_count(1, timeout=15_000)

    wizard.submit()
    expect(wizard.modal).to_be_hidden()
    expect(page.locator('.pg-v2-map-node[data-type="vm"]', has_text="web-3")).to_be_visible(timeout=15_000)
    assert len(_active_vms(capped_base_url)) == 2
    assert network_id


def test_a_substituted_subnet_is_announced(playground_base_url: str, page: Page, create_vm_wizard):
    """An overlapping CIDR is not refused -- the server picks a free one instead.

    That is deliberate, and the dialog says so when it creates the network
    directly. On the wizard's draft path the substitution happens later, at VM
    creation, by which time the dialog is gone. An operator who typed one subnet
    and got another has to be told: in a lab they will point an attacker VM, a
    capture filter or a firewall rule at the subnet they think they asked for.
    """
    _seed_lab(playground_base_url)

    wizard = create_vm_wizard(playground_base_url).open(label="web-overlap")
    wizard.next(2).add_draft_network("overlapnet", "10.42.2.0/24").next().submit()

    expect(wizard.modal).to_be_hidden()
    created = [n for n in _session(playground_base_url)["networks"] if n["name"] == "overlapnet"][0]
    assert created["subnet_cidr"] != "10.42.2.0/24", created

    alerts = page.locator("#pg-alerts")
    expect(alerts).to_be_visible()
    expect(alerts).to_contain_text("10.42.2.0/24")
    expect(alerts).to_contain_text(created["subnet_cidr"])
