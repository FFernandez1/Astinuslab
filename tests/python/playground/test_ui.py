import html.parser
import json
import re
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from astinuslab.main import app


class _IdIndex(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._stack: list[tuple[str, str | None]] = []
        self.by_id: dict[str, dict] = {}
        self.by_attr: dict[str, list[dict]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v for k, v in attrs}
        ancestors = [sid for _, sid in self._stack if sid]
        entry = {"tag": tag, "attrs": a, "ancestors": ancestors}
        if "id" in a and a["id"]:
            self.by_id[a["id"]] = entry
        for k in a:
            if k.startswith("data-"):
                self.by_attr.setdefault(k, []).append(entry)
        self._stack.append((tag, a.get("id")))

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                return


def parse_ids(html_text: str) -> _IdIndex:
    idx = _IdIndex()
    idx.feed(html_text)
    return idx


def get(path: str):
    return TestClient(app).get(path)


def body_has_single_main_and_h1(html_text: str) -> bool:
    return html_text.count("<main") == 1 and "<h1" in html_text


def test_playground_page_renders_v2_shell():
    res = get("/playground")
    assert res.status_code == 200
    idx = parse_ids(res.text)

    for expected_id in (
        "playground-root",
        "pg-top-metrics",
        "connection-map-section",
        "map-canvas",
        "map-world",
        "connection-map-svg",
        "map-save-status",
        "stream-log",
        "spawn-error",
        "network-error",
        "end-session-error",
        "pg-detail-panel",
        "create-vm-modal",
        "create-vm-warning",
        "capture-section",
    ):
        assert expected_id in idx.by_id, f"missing id {expected_id}"

    for removed_id in ("btn-save-topology", "create-mode-row", "btn-add-network"):
        assert removed_id not in idx.by_id, f"unexpected id {removed_id}"

    assert "pg-v2-map-status" in (idx.by_id["map-save-status"]["attrs"].get("class") or "").split()
    assert idx.by_id["map-save-status"]["attrs"].get("data-state") == "idle"
    assert idx.by_id["map-save-status"]["attrs"].get("aria-live") == "polite"
    assert "pg-stream-log" in (idx.by_id["stream-log"]["attrs"].get("class") or "").split()
    assert idx.by_id["stream-log"]["attrs"].get("aria-live") == "polite"

    for err_id, live in (
        ("spawn-error", "assertive"),
        ("network-error", "assertive"),
        ("end-session-error", "assertive"),
    ):
        node = idx.by_id[err_id]
        assert "pg-form-error" in (node["attrs"].get("class") or "").split()
        assert node["attrs"].get("aria-live") == live

    open_create_values = {e["attrs"].get("data-open-create") for e in idx.by_attr.get("data-open-create", [])}
    assert {"target", "attacker"} <= open_create_values
    assert ">+ Network<" not in res.text


def test_playground_nav_link_present_on_any_page():
    res = get("/playground")
    assert res.status_code == 200
    assert 'href="/playground"' in res.text


def test_playground_static_assets_use_spawn_flow_contract():
    html = get("/playground")
    css = get("/static/css/pages/playground.css")
    assert html.status_code == 200
    assert css.status_code == 200
    assert "/static/css/pages/playground.css?v=" in html.text
    assert "/static/playground/index.js?v=" in html.text
    assert "/static/playground" + ".js" not in html.text
    for cls in [
        ".pg-v2-link-layer",
        ".pg-v2-map-world",
        ".pg-v2-map-status",
        '.pg-v2-map-node[data-type="network"]',
        ".pg-v2-creator",
    ]:
        assert cls in css.text
    assert ".pg-v2-mode-row" not in css.text
    assert ".pg-v2-choice-card" not in css.text


def test_playground_modal_css_matches_mockup_layout_contract():
    css = get("/static/css/pages/playground.css").text

    assert "backdrop-filter: blur(8px)" in css
    assert re.search(r"\.pg-v2-creator--wizard\s*\{[^}]*max-width:\s*760px", css, re.S)
    assert ".pg-v2-wizard-steps" not in css
    assert ".pg-v2-workload-grid" in css
    assert "max-height: min(46vh, 360px)" in css
    assert "grid-template-columns: 150px minmax(0, 1fr)" not in css
    assert ".pg-v2-modal-footer [hidden]" in css
    assert re.search(r"\.pg-foot-btn\[hidden\]\s*\{[^}]*display:\s*none\s*!important", css, re.S)


def test_playground_name_generator_contract():
    js = get("/static/playground/name-generator.js")
    assert js.status_code == 200
    text = js.text
    assert "export function generatedPlaygroundName" in text
    assert "crypto.getRandomValues" in text
    assert "Math.random" in text
    for prefix in ['"target"', '"attacker"', '"network"']:
        assert prefix in text
    assert "padStart(4" in text


def test_playground_wizard_uses_generated_vm_labels():
    js = get("/static/playground/wizard.js")
    assert js.status_code == 200
    text = js.text
    assert 'from "./name-generator.js"' in text
    assert "generatedPlaygroundName" in text
    assert "generatedLabel" in text
    assert "w.label || w.generatedLabel" in text
    assert "placeholder: w.generatedLabel" in text


def test_playground_network_modal_uses_generated_network_names():
    js = get("/static/playground/render-modal.js")
    assert js.status_code == 200
    text = js.text
    assert 'from "./name-generator.js"' in text
    assert "generatedPlaygroundName" in text
    assert "generatedNetworkName" in text
    assert "networkName || generatedNetworkName" in text
    assert 'placeholder", generatedNetworkName' in text or "placeholder: generatedNetworkName" in text


def test_playground_create_wizard_uses_project_primary_for_active_states():
    css = get("/static/css/pages/playground.css")
    assert css.status_code == 200
    text = css.text
    assert '.pg-v2-workload-card[data-active="true"]' in text
    assert "color-mix(in srgb, var(--primary)" in text


def test_playground_module_api_exports_fetch_wrappers():
    r = get("/static/playground/api.js")
    assert r.status_code == 200
    text = r.text
    for sym in [
        "export async function getSession",
        "export async function getCatalogImages",
        "export async function getCatalogWorkloads",
        "export async function createVm",
        "export async function createNetwork",
        "export async function startCapture",
        "export async function endSession",
        "export function openCaptureStream",
    ]:
        assert sym in text, f"missing: {sym}"
    assert '"/api/v1/playground"' in text
    assert "vcpus" in text
    assert "memory_mb" in text


def test_playground_module_state_exports_store_api():
    r = get("/static/playground/state.js")
    assert r.status_code == 200
    for sym in [
        "export function getState",
        "export function setState",
        "export function subscribe",
    ]:
        assert sym in r.text
    assert "catalog: { target: [], attacker: [], network: [] }" in r.text
    # The density preference died with the role groups; nothing persists to
    # localStorage from the store any more.
    assert "pg.density." not in r.text
    assert "densityMode" not in r.text


def test_playground_module_ui_exports_helpers():
    r = get("/static/playground/ui.js")
    assert r.status_code == 200
    assert 'import { el } from "../lib/dom.js"' in r.text
    assert "export { el };" in r.text
    for sym in [
        "export function svgEl",
        "export function kvRow",
        "export function barLine",
        "export function cpuColor",
        "export function roleAccent",
    ]:
        assert sym in r.text
    assert "innerHTML" not in r.text


def test_shared_dom_module_exports_helpers():
    r = get("/static/lib/dom.js")
    assert r.status_code == 200
    assert "export function el" in r.text
    assert "export function $" in r.text
    assert "innerHTML" not in r.text


def test_playground_module_pollers_exports_lifecycle():
    r = get("/static/playground/pollers.js")
    assert r.status_code == 200
    for sym in ["export function startSessionPoll", "export function stopSessionPoll"]:
        assert sym in r.text
    assert "visibilitychange" in r.text


def test_playground_pollers_fetch_and_merge_vm_metrics():
    api = get("/static/playground/api.js")
    pollers = get("/static/playground/pollers.js")
    assert api.status_code == 200
    assert pollers.status_code == 200
    assert "export async function getSessionMetrics" in api.text
    assert "/sessions/current/metrics" in api.text
    assert "getSessionMetrics" in pollers.text
    assert "cpu_pct" in pollers.text
    assert "mem_pct" in pollers.text


def test_playground_module_index_imports_foundations():
    r = get("/static/playground/index.js")
    assert r.status_code == 200
    text = r.text
    for sym in [
        'from "./state.js"',
        'from "./api.js"',
        'from "./pollers.js"',
        "startSessionPoll",
        "DOMContentLoaded",
    ]:
        assert sym in text
    assert '["target", "attacker", "network"]' in text
    assert "getCatalogImages(role)" in text


def test_playground_module_render_top_emits_status_line():
    js = get("/static/playground/render-top.js")
    css = get("/static/css/pages/playground.css")
    assert js.status_code == 200 and css.status_code == 200
    for sym in [
        "export function mountTop",
        "export function deriveStatus",
        "pg-status-item",
        "pg-status-dot",
        "pg-status-review",
        "pg-status-elapsed",
    ]:
        assert sym in js.text
    for cls in [".pg-statusline", ".pg-status-dot", ".pg-status-review", ".pg-status-elapsed"]:
        assert cls in css.text
    # The metric-card strip it replaced is gone from both layers.
    assert "pg-v2-metric-card" not in js.text
    assert ".pg-v2-metric-card" not in css.text
    assert "btn-end-session" in js.text


def test_playground_map_groups_by_segment_not_by_role():
    js = get("/static/playground/render-map.js")
    css = get("/static/css/pages/playground.css")
    assert js.status_code == 200
    # A network is a rail, a NIC is a stub, and a machine's band comes from the
    # segment it is attached to — role no longer places anything.
    for sym in ["renderWires", "pg-v2-rail", "pg-v2-wire", "bandOf", "linkCallout"]:
        assert sym in js.text
    for cls in [".pg-v2-rail", ".pg-v2-wire", ".pg-v2-map-link", ".pg-v2-map-context"]:
        assert cls in css.text
    # Role lanes were the column layout being replaced; they are gone from both
    # layers, along with the role-group modules they inherited from.
    for gone_sym in ["buildLanes", "laneLabels", "pg-v2-map-lanes", "Targets · ", "Attackers · "]:
        assert gone_sym not in js.text
    for gone_cls in [".pg-v2-map-lanes", ".pg-v2-map-lane-rule", ".pg-v2-map-lane-label"]:
        assert gone_cls not in css.text
    for gone in [
        "/static/playground/render-role-groups.js",
        "/static/playground/render-density-toggle.js",
        "/static/playground/render-vm-row.js",
        "/static/playground/render-vm-card.js",
    ]:
        assert get(gone).status_code == 404


def test_playground_map_draws_network_nodes_as_devices():
    js = get("/static/playground/render-map.js")
    css = get("/static/css/pages/playground.css")
    # A Network Node is its own object on the map, and says what it is for.
    for sym in ["deviceNode", "isDeviceVm", "function_tags", "firewall", "nat-gateway", "dev-ports"]:
        assert sym in js.text
    for cls in [".pg-v2-map-device", ".pg-v2-map-device .dev-fn", ".pg-v2-map-device .dev-port"]:
        assert cls in css.text
    # Cyan separates appliances from the violet that segments own.
    assert "--c-cyan-bd" in get("/static/css/tokens.css").text


def test_playground_map_nodes_size_to_labels_within_bounds():
    """A long machine name widens its box; a very long one clips instead of
    dragging the rest of the layout out of shape."""
    css = get("/static/css/pages/playground.css").text
    js = get("/static/playground/render-map.js").text

    node_rule = re.search(r"\.pg-v2-map-node\s*\{(.*?)\}", css, re.S)
    assert node_rule, "base map node rule missing"
    assert "width: max-content" in node_rule.group(1)
    assert "min-width: 150px" in node_rule.group(1)
    assert "max-width: 280px" in node_rule.group(1)

    device_rule = re.search(r"\.pg-v2-map-node\.pg-v2-map-device\s*\{(.*?)\}", css, re.S)
    assert device_rule, "device node rule missing"
    assert "min-width: 210px" in device_rule.group(1)
    assert "max-width: 340px" in device_rule.group(1)

    # Geometry follows the boxes the browser laid out — a hardcoded width would
    # put every stub off-centre the moment a label grew.
    assert "offsetWidth" in js
    assert "function measure(" in js
    assert "function flowBand(" in js


def test_playground_module_render_map_mounts():
    js = get("/static/playground/render-map.js")
    assert js.status_code == 200
    for sym in ["export function mountMap", "btn-map-zoom-in", "btn-map-zoom-out", "btn-map-fit", "map-save-status"]:
        assert sym in js.text


def test_playground_module_render_details_emits_sections():
    js = get("/static/playground/render-details.js")
    assert js.status_code == 200
    for sym in [
        "export function mountDetails",
        "Machine",
        "Workload",
        "Services",
        "Connect",
        "Capture RAM",
        "Capture Disk",
        "Storage",
        "pg-ssh-copy-btn",
    ]:
        assert sym in js.text
    # Capture and power ride in one bar above the body; the inventory collapses.
    assert "pg-v2-actionbar" in js.text
    assert "inventoryDisclosure" in js.text
    assert "Evidence · this machine" in js.text


def test_playground_module_render_details_surfaces_vm_error_summary():
    js = get("/static/playground/render-details.js")
    assert js.status_code == 200
    assert "error_summary" in js.text
    assert "VM warning" in js.text


def test_playground_network_delete_shows_block_and_error_feedback():
    js = get("/static/playground/render-details.js")
    assert js.status_code == 200
    assert "Cannot delete" in js.text
    assert "networkDelete" in js.text
    assert "pg-v2-warning" in js.text


def test_playground_static_assets_render_workload_services_in_details():
    js = get("/static/playground/render-details.js")
    css = get("/static/css/pages/playground.css")
    assert js.status_code == 200
    assert css.status_code == 200
    assert "workload_display_name" in js.text
    assert "workload_state" in js.text
    assert "services" in js.text
    assert ".pg-v2-service-row" in css.text
    assert ".pg-v2-service-url" in css.text


def test_playground_module_render_details_wires_nic_attach_detach_controls():
    js = get("/static/playground/render-details.js")
    api = get("/static/playground/api.js")
    assert js.status_code == 200
    assert api.status_code == 200
    for sym in [
        "attachNic",
        "detachNic",
        "Network interfaces",
        "Attach network",
        "Detach",
        "nicAttach",
        "nicDetach",
    ]:
        assert sym in js.text or sym in api.text


def test_playground_map_surfaces_partial_nic_reachability():
    map_js = get("/static/playground/render-map.js")
    assert map_js.status_code == 200
    assert "reachability_status" in map_js.text
    assert "partial" in map_js.text


def test_playground_module_capture_console_mounts():
    js = get("/static/playground/render-capture.js")
    assert js.status_code == 200
    for sym in ["export function mountCapture", "capture-section", "stream-log", "stream-vm", "stream-kind"]:
        assert sym in js.text


def test_playground_module_render_modal_mounts_network_creator():
    js = get("/static/playground/render-modal.js")
    assert js.status_code == 200
    for sym in [
        "export function mountCreateNetwork",
        "create-network-modal",
    ]:
        assert sym in js.text


def test_playground_network_modal_uses_create_modal_family_chrome():
    js = get("/static/playground/render-modal.js").text
    html = get("/playground").text

    assert "PLAYGROUND · NEW NETWORK" in html
    assert "create-network-modal" in js
    assert "pg-modal-narrow" in html


def test_playground_module_wizard_flow():
    js = get("/static/playground/wizard.js")
    assert js.status_code == 200
    for sym in [
        "export function mountCreateVm",
        "buildVerticalRail",
        "buildImageGrid",
        "buildNetworkRows",
        "buildHudSlider",
        "Basics",
        "Workload",
        "Resources",
        "Review",
    ]:
        assert sym in js.text
    assert "diskGb: isNetwork ? 4 : 20" in js.text
    assert "DISK_PRESETS" in js.text
    assert "vcpus: isNetwork ? 1" in js.text
    assert "memoryMb: isNetwork ? 512" in js.text
    assert "memoryMb: isNetwork ? 512 : isAttacker ? undefined : 4096" in js.text
    assert "buildHudSlider" in js.text
    assert "function memoryDisplay" in js.text
    assert "memory" in js.text
    assert "pg-hud-sl" in get("/static/playground/render-modal.js").text
    assert "vcpus: w.vcpus || undefined" in js.text
    assert "memory_mb: w.memoryMb || undefined" in js.text
    assert "Create Network Node" in js.text
    assert "singleTickLabel(role)" in js.text
    assert "barLine" not in js.text
    assert "setW({ query" not in js.text
    assert "card.hidden" in js.text or "card.dataset.search" in js.text
    assert "autoInfo" in js.text
    assert "renderFlatRole(st, w, role)" in js.text


def test_playground_target_wizard_exposes_resource_controls():
    js = get("/static/playground/wizard.js")
    assert js.status_code == 200
    text = js.text
    assert "vcpus: isNetwork ? 1 : isAttacker ? 2 : 2" in text
    assert "memoryMb: isNetwork ? 512 : isAttacker ? undefined : 4096" in text
    assert 'label: "vcpus"' in text
    assert 'label: tt("pg_wiz_field_memory", "memory")' in text
    assert "memoryDisplay" in text
    assert 'seqRow("memory", memoryDisplay(w.memoryMb))' in text
    assert "vcpus: w.vcpus || undefined" in text
    assert "memory_mb: w.memoryMb || undefined" in text
    assert "function validateStep" in text
    assert "create-vm-warning" in text


def test_playground_wizard_selects_new_vm_after_spawn_for_details_panel():
    js = get("/static/playground/wizard.js").text

    assert "const createdVm = await createVm" in js
    assert "selectedId: createdVm?.id" in js


def test_playground_wizard_sets_mockup_modal_chrome_and_role_footer():
    js = get("/static/playground/wizard.js").text

    assert "function setModalChrome" in js
    assert "modal.dataset.role = role" in js
    assert "PLAYGROUND · NEW VM" in js
    assert "Create target" in js
    assert "Create attacker" in js
    assert "Create Network Node" in js
    assert "renderFooter(role, w)" in js
    assert "cancel.hidden = false" in js
    assert "cancel.hidden = isTarget" not in js
    assert "back.hidden = !isTarget" in js
    assert "next.hidden = !isTarget || isLast" in js


def test_playground_idle_ui_contract_hides_the_detail_panel():
    html = get("/playground").text
    css = get("/static/css/pages/playground.css").text

    assert body_has_single_main_and_h1(html)
    # Nothing on the idle page describes machines that do not exist yet.
    assert "pg-role-groups" not in html
    assert '#playground-root[data-state="idle"] .pg-v2-detail-panel' in css
    assert re.search(
        r'#playground-root\[data-state="idle"\]\s+\.pg-v2-detail-panel\s*\{[^}]*display:\s*none',
        css,
        re.S,
    )


def test_playground_api_uses_optional_session_endpoint_for_idle_ui():
    api = get("/static/playground/api.js").text

    assert '"/sessions/current-or-null"' in api
    assert 'request("GET", "/sessions/current")' not in api


def test_playground_template_exposes_network_node_create_button():
    html = get("/playground").text
    assert 'data-open-create="network"' in html
    assert ">+ Network Node<" in html


def test_playground_icon_buttons_have_accessible_labels():
    html = get("/playground").text
    assert 'aria-label="Zoom map in"' in html
    assert 'aria-label="Zoom map out"' in html
    assert 'aria-label="Fit map to view"' in html
    assert 'aria-label="Close create VM dialog"' in html
    assert 'aria-label="Close create network dialog"' in html
    assert 'for="create-network-name"' in html
    assert 'for="create-network-cidr"' in html


def test_playground_no_monolithic_asset_reference_remains():
    html = get("/playground").text
    assert "playground" + ".js" not in html


def test_playground_create_modals_expose_mockup_chrome_hooks():
    html = get("/playground").text

    assert 'class="pg-v2-title-stack"' in html
    assert 'class="pg-v2-modal-eyebrow"' in html
    assert "PLAYGROUND · NEW VM" in html
    assert "PLAYGROUND · NEW NETWORK" in html
    assert 'data-modal-kind="create-vm"' in html
    assert 'data-modal-kind="create-network"' in html


def test_create_vm_modal_has_footer_and_close_only_header():
    res = get("/playground")
    assert res.status_code == 200
    idx = parse_ids(res.text)
    # Submit button must exist and live inside the modal footer (not in the header tool group).
    submit_node = idx.by_id.get("create-vm-submit")
    assert submit_node is not None, "create-vm-submit must remain on the page"
    assert "create-vm-modal-footer" in submit_node["ancestors"], (
        "create-vm-submit must live inside #create-vm-modal-footer, not in the panel header"
    )
    # Only close button remains in the HUD toolbar, outside the modal footer.
    close_node = idx.by_id["create-vm-close"]
    assert "pg-hud-x" in (close_node["attrs"].get("class") or "").split()
    assert "create-vm-modal-footer" not in close_node["ancestors"]


def test_playground_js_uses_backend_job_id_contract():
    top = get("/static/playground/render-top.js").text
    index = get("/static/playground/index.js").text
    assert "job_id" in top
    assert "session?.id" not in top
    assert "job_id" in index


def test_playground_api_exports_topology_and_normalization_helpers():
    js = get("/static/playground/api.js").text
    for sym in [
        "export function normalizeSession",
        "export async function refreshSession",
        "export async function getTopologyLayout",
        "export async function saveTopologyNode",
    ]:
        assert sym in js, f"missing {sym}"
    for endpoint in [
        '"/sessions/current/topology-layout"',
        '"/sessions/current/recipe"',
        '"/recipes/import"',
        "`/vms/${encodeURIComponent(vmId)}/actions`",
    ]:
        assert endpoint in js


def test_playground_normalize_session_hides_resources_during_teardown():
    api_path = (
        Path(__file__).resolve().parents[3] / "src" / "astinuslab" / "web" / "static" / "playground" / "api.js"
    ).as_uri()
    script = f"""
        import {{ normalizeSession }} from {json.dumps(api_path)};
        const normalized = normalizeSession({{
          job_id: "pg-destroy",
          status: "destroy_requested",
          vms: [{{ id: "vm-1", label: "victim", state: "running", nics: [{{ job_network_id: "net-1" }}] }}],
          networks: [{{ id: "net-1", name: "default", state: "active", subnet_cidr: "10.42.1.0/24" }}]
        }});
        console.log(JSON.stringify({{
          status: normalized.status,
          vms: normalized.vms,
          networks: normalized.networks
        }}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        text=True,
        capture_output=True,
        check=True,
    )

    normalized = json.loads(result.stdout)
    assert normalized == {"status": "destroy_requested", "vms": [], "networks": []}


def test_playground_evidence_helpers_fetch_every_api_page():
    api_path = (
        Path(__file__).resolve().parents[3] / "src" / "astinuslab" / "web" / "static" / "playground" / "api.js"
    ).as_uri()
    script = f"""
        import {{ getArtifactsForJob, getReportsForJob }} from {json.dumps(api_path)};
        const calls = [];
        globalThis.fetch = async (url) => {{
          calls.push(url);
          const offset = Number(new URL(url, "http://localhost").searchParams.get("offset"));
          const prefix = url.includes("/artifacts") ? "artifact" : "report";
          const count = offset === 0 ? 500 : 1;
          const rows = Array.from({{ length: count }}, (_, index) => ({{ id: `${{prefix}}-${{offset + index}}` }}));
          return {{ ok: true, status: 200, text: async () => JSON.stringify(rows) }};
        }};
        const artifacts = await getArtifactsForJob("job/a");
        const reports = await getReportsForJob("job/a");
        console.log(JSON.stringify({{ calls, artifactCount: artifacts.length, reportCount: reports.length }}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        text=True,
        capture_output=True,
        check=True,
    )

    data = json.loads(result.stdout)
    assert data["artifactCount"] == 501
    assert data["reportCount"] == 501
    assert data["calls"] == [
        "/api/v1/artifacts?job_id=job%2Fa&limit=500&offset=0",
        "/api/v1/artifacts?job_id=job%2Fa&limit=500&offset=500",
        "/api/v1/reports?job_id=job%2Fa&limit=500&offset=0",
        "/api/v1/reports?job_id=job%2Fa&limit=500&offset=500",
    ]


def test_playground_recipe_controls_match_existing_modal_design():
    html = get("/playground").text
    idx = parse_ids(html)
    for button_id in ("btn-export-recipe", "btn-import-recipe"):
        assert button_id in idx.by_id
        classes = (idx.by_id[button_id]["attrs"].get("class") or "").split()
        assert "pg-foot-btn" in classes
        assert "pg-footer" in idx.by_id[button_id]["ancestors"]
    end_node = idx.by_id["btn-end-session"]
    assert "pg-footer" in end_node["ancestors"]
    assert "pg-foot-btn--danger" in (end_node["attrs"].get("class") or "").split()
    for node_id in ("import-recipe-modal", "import-recipe-title", "import-recipe-submit", "import-recipe-cancel"):
        assert node_id in idx.by_id
    assert "import-recipe-modal" in idx.by_id["import-recipe-title"]["ancestors"]
    assert "import-recipe-modal-footer" in idx.by_id["import-recipe-submit"]["ancestors"]


def test_playground_detail_exposes_lifecycle_actions_separate_from_capture():
    js = get("/static/playground/render-details.js").text
    for sym in ["can_start", "can_stop", "can_reboot", "vmAction(vm.id, action)", "Start", "Stop", "Reboot"]:
        assert sym in js
    assert "Capture RAM" in js and "Capture Disk" in js


def test_playground_topology_layout_clamps_stale_positions_and_uses_segment_bands():
    js = get("/static/playground/render-map.js").text
    assert "function clampPos" in js
    assert "flowBand" in js
    assert "isDeviceVm" in js
    assert "bandOf" in js
    assert "saveTopologyNode(node)" in get("/static/playground/api.js").text


def test_playground_status_line_sits_above_the_map():
    html = get("/playground").text
    assert html.index('id="pg-top-metrics"') < html.index('id="map-save-status"')
    assert html.index('id="map-save-status"') < html.index('id="map-canvas"')


def test_playground_modals_have_footer_owned_actions():
    idx = parse_ids(get("/playground").text)
    for button_id, footer_id in (
        ("create-vm-submit", "create-vm-modal-footer"),
        ("create-vm-cancel", "create-vm-modal-footer"),
        ("create-network-submit", "create-network-modal-footer"),
        ("create-network-cancel", "create-network-modal-footer"),
    ):
        assert button_id in idx.by_id
        assert footer_id in idx.by_id[button_id]["ancestors"]
    assert "create-network-submit" not in idx.by_id["create-network-close"]["ancestors"]


def test_playground_detail_reads_normalized_services_and_capture_flags():
    js = get("/static/playground/render-details.js").text
    assert "vm.services" in js
    assert "workload_services" not in js
    assert "can_capture_memory" in js
    assert "can_capture_disk" in js
    assert "action_block_reason" in js


def test_playground_screenshot_script_uses_mocked_populated_session():
    script = (Path(__file__).resolve().parents[3] / "tools" / "dev" / "playground_screenshots.cjs").read_text()
    for sym in ["fixtureSession", "fixtureTopology", "page.route", "pg-populated-map.png", "pg-detail-populated.png"]:
        assert sym in script


def test_playground_footer_status_bar_present():
    html = get("/playground").text
    idx = parse_ids(html)
    footer = idx.by_id.get("pg-footer")
    assert footer is not None, "playground must include the status-bar footer"
    assert "pg-footer" in (footer["attrs"].get("class") or "").split()
    for slot in ("pg-foot-sid", "pg-foot-elapsed", "pg-foot-utc"):
        assert slot in idx.by_id, f"missing footer slot id {slot}"
        assert "pg-footer" in idx.by_id[slot]["ancestors"]


def test_playground_header_drops_recipe_and_end_session_buttons():
    html = get("/playground").text
    idx = parse_ids(html)
    for btn_id in ("btn-export-recipe", "btn-import-recipe", "btn-end-session"):
        assert btn_id in idx.by_id
        assert "pg-v2-actions" not in idx.by_id[btn_id]["ancestors"]


def test_playground_map_collapse_and_footer_css():
    css = get("/static/css/pages/playground.css").text
    assert ".pg-footer" in css
    assert ".pg-foot-btn" in css
    assert ".pg-foot-btn--danger" in css
    assert ".pg-foot-live" in css
    assert "transition: height 180ms ease-out" in css
    assert '#playground-root[data-populated="false"] .pg-v2-map-canvas' in css


def test_playground_render_top_drops_capture_status_metric():
    js = get("/static/playground/render-top.js").text
    assert "Capture status" not in js
    assert "mountFooter" in js
    assert "data-elapsed" in js
    assert "pg-foot-utc" in js
    assert "pg-foot-sid" in js
    assert 'id="pg-foot-elapsed" data-elapsed="true"' in get("/playground").text


def test_create_network_cidr_field_is_not_hardcoded():
    html = get("/playground").text
    assert 'id="create-network-cidr"' in html
    assert 'value="10.42.2.0/24"' not in html
    assert 'id="network-notice"' in html


def test_playground_first_run_onboarding_section_removed():
    html = get("/playground").text
    idx = parse_ids(html)
    assert "pg-empty-onboarding" not in idx.by_id
    assert 'id="connection-map-section"' in html
    assert "PLAYGROUND · GET STARTED" not in html
    assert "Build your first lab" not in html
    assert "Recipes (JSON) import/export from the footer" not in html


def test_playground_onboarding_css_removed():
    css = get("/static/css/pages/playground.css").text
    assert "pg-onboarding" not in css
