"""Integration tests for the server-rendered UI routes."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from tests.python._support.helpers import autospec_setattr

WORDPRESS_NAME = "WordPress 6.4"
ROOT = Path(__file__).parents[3]
_TEST_CSRF_TOKEN = "test-csrf-token"


@pytest.fixture(autouse=True)
def _add_csrf_to_existing_ui_client_requests(request, monkeypatch):
    if "client" not in request.fixturenames:
        return
    client = request.getfixturevalue("client")
    client.cookies.set("astinuslab_csrf", _TEST_CSRF_TOKEN)
    original_request = client.request

    def request_with_csrf(method, url, *args, **kwargs):
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            headers = dict(kwargs.pop("headers", {}) or {})
            headers.setdefault("X-CSRF-Token", _TEST_CSRF_TOKEN)
            kwargs["headers"] = headers
        return original_request(method, url, *args, **kwargs)

    monkeypatch.setattr(client, "request", request_with_csrf)


# Modular stylesheet set served by the app (foundation + per-page sheets).
# Replaces the former single style.css monolith after CSS modularization.
_APP_CSS_FILES = [
    "css/tokens.css",
    "css/layout.css",
    "css/components.css",
    "css/pages/jobs.css",
    "css/pages/logs.css",
    "css/pages/settings.css",
    "css/pages/images.css",
    "css/pages/volatility.css",
    "css/pages/workloads.css",
]


def _app_css_text():
    """Concatenation of all app stylesheets read from disk."""
    base = ROOT / "src/astinuslab/web/static"
    return "\n".join((base / rel).read_text(encoding="utf-8") for rel in _APP_CSS_FILES)


def _served_app_css(client):
    """Concatenation of all app stylesheets fetched over HTTP."""
    return "\n".join(client.get("/static/" + rel).text for rel in _APP_CSS_FILES)


def test_wizard_button_css_contract():
    css = _app_css_text()
    assert ".wiz-btn--sm" in css
    assert ".wiz-btn--xs" in css
    assert ".wiz-btn--danger" in css
    assert '.wiz-btn--compact-choice[data-active="true"]' in css
    assert re.search(r"\.wiz-btn--primary\s*\{[^}]*background:\s*transparent", css, re.S)
    assert re.search(r"\.wiz-btn--primary\s*\{[^}]*border-color:\s*var\(--primary\)", css, re.S)


def test_selected_templates_do_not_inline_wiz_button_sizing():
    template_paths = [
        "src/astinuslab/web/templates/admin_dumps.html",
        "src/astinuslab/web/templates/admin_workloads.html",
        "src/astinuslab/web/templates/admin_ssh_keys.html",
        "src/astinuslab/web/templates/admin_images.html",
        "src/astinuslab/web/templates/partials/jobs_table.html",
        "src/astinuslab/web/templates/partials/delete_job_modal.html",
        "src/astinuslab/web/templates/job_new.html",
    ]
    pattern = re.compile(
        r'class="[^"]*wiz-btn[^"]*"[^>]*style="[^"]*(padding|font-size)|style="[^"]*(padding|font-size)[^"]*"[^>]*class="[^"]*wiz-btn'
    )
    for rel in template_paths:
        assert not pattern.search((ROOT / rel).read_text(encoding="utf-8")), rel


def test_every_server_rendered_post_form_has_csrf_token():
    template_paths = list((ROOT / "src/astinuslab/web/templates").rglob("*.html"))
    form_pattern = re.compile(r"<form\b[^>]*\bmethod=[\"']post[\"'][^>]*>", re.I)
    for path in template_paths:
        text = path.read_text(encoding="utf-8")
        for form in form_pattern.finditer(text):
            closing_tag = text.find("</form>", form.end())
            assert closing_tag != -1, path.relative_to(ROOT)
            form_body = text[form.end() : closing_tag]
            assert 'include "partials/csrf_input.html"' in form_body, path.relative_to(ROOT)

    auth_routes = (ROOT / "src/astinuslab/ui/auth_routes.py").read_text(encoding="utf-8")
    assert 'name="csrf_token"' in auth_routes


_UNSAFE_FETCH_METHOD = re.compile(r"""method\s*:\s*['"`](POST|PUT|PATCH|DELETE)['"`]""", re.I)
_INLINE_SCRIPT = re.compile(r"<script\b(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)


def _fetch_call_arguments(text: str):
    """Yield the argument text of every ``fetch(...)`` call, paren/quote aware."""
    for match in re.finditer(r"\bfetch\s*\(", text):
        index, depth, collected, quote = match.end(), 1, [], None
        while index < len(text) and depth:
            char = text[index]
            if quote:
                if char == "\\":
                    index += 2
                    continue
                if char == quote:
                    quote = None
            elif char in "\"'`":
                quote = char
            elif char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
            if depth:
                collected.append(char)
            index += 1
        yield "".join(collected)


def _client_script_sources():
    """Every line of client JS: static modules plus templates' inline scripts."""
    for path in sorted((ROOT / "src/astinuslab/web/static").rglob("*.js")):
        if path.name == "htmx.min.js":
            continue
        yield path, path.read_text(encoding="utf-8")
    for path in sorted((ROOT / "src/astinuslab/web/templates").rglob("*.html")):
        for script in _INLINE_SCRIPT.finditer(path.read_text(encoding="utf-8")):
            yield path, script.group(1)


def test_browser_mutation_clients_send_csrf_header():
    """Every unsafe fetch carries the token the app-wide CSRF dependency demands.

    An allowlist of known-good modules used to stand here, so five call sites
    that never sent the header -- reset-lab, the manual dump upload, and three
    image-vault actions -- shipped as 403s. This walks every fetch instead.
    """
    csrf_js = (ROOT / "src/astinuslab/web/static/csrf.js").read_text(encoding="utf-8")
    assert "htmx:configRequest" in csrf_js
    assert "X-CSRF-Token" in csrf_js
    # Classic scripts and inline page scripts cannot import, so they read this.
    assert "window.csrfHeaders" in csrf_js

    checked = 0
    unguarded = []
    for path, text in _client_script_sources():
        for call in _fetch_call_arguments(text):
            if not _UNSAFE_FETCH_METHOD.search(call):
                continue
            checked += 1
            if not any(token in call for token in ("csrfHeaders", "X-CSRF-Token", "csrf(")):
                unguarded.append(f"{path.relative_to(ROOT)}: {' '.join(call.split())[:90]}")
    assert checked, "fetch scanner matched nothing -- the pattern has drifted"
    assert not unguarded, "unsafe fetch without a CSRF header:\n" + "\n".join(unguarded)

    # A form built in JS posts as a form, so it needs the hidden field instead.
    modal_js = (ROOT / "src/astinuslab/web/static/delete-job-modal.js").read_text(encoding="utf-8")
    assert 'name = "csrf_token"' in modal_js


def test_htmx_swapped_partials_carry_no_inline_script():
    """A fragment's inline script carries its own CSP nonce, so it never runs.

    ``script-src`` admits only the host page's nonce. An inline script inside a
    partial is re-served with a fresh nonce on every swap, the browser refuses
    it, and the swapped-in controls stay unbound. Page-level files only.
    """
    offenders = []
    for path in sorted((ROOT / "src/astinuslab/web/templates/partials").rglob("*.html")):
        body = _JINJA_COMMENT.sub("", path.read_text(encoding="utf-8"))
        if _INLINE_SCRIPT.search(body) or "<script" in body:
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, "inline <script> in an htmx-swapped partial:\n" + "\n".join(offenders)


def test_templates_have_no_inline_event_handlers():
    """``script-src`` carries no 'unsafe-inline', so on*= attributes never fire."""
    handler = re.compile(r"\son[a-z]+\s*=\s*[\"']")
    offenders = []
    for path in sorted((ROOT / "src/astinuslab/web/templates").rglob("*.html")):
        for match in handler.finditer(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(ROOT)}: {match.group(0).strip()}")
    assert not offenders, "inline event handler blocked by CSP:\n" + "\n".join(offenders)


class TestIndexPage:
    def test_root_opens_playground(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert b"Playground" in r.content

    def test_jobs_page_content_type_is_html(self, client):
        r = client.get("/jobs")
        assert "text/html" in r.headers["content-type"]

    def test_contains_launch_form(self, client):
        r = client.get("/jobs/new")
        assert b'action="/jobs/new"' in r.content

    def test_contains_target_options(self, client):
        r = client.get("/jobs/new")
        assert b"ubuntu" in r.content.lower()

    def test_empty_jobs_state_message(self, client):
        r = client.get("/jobs")
        assert b"No jobs" in r.content

    def test_shows_existing_jobs(self, client, created_job):
        r = client.get("/jobs")
        job_id_prefix = created_job["id"][:8].encode()
        assert job_id_prefix in r.content

    def test_shows_manual_trigger_jobs(self, client, monkeypatch):
        import astinuslab.jobs.job_services as svc_mod

        autospec_setattr(monkeypatch, svc_mod, "get_active_key_paths", lambda: ("/tmp/k", "/tmp/k.pub"))
        client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "lab-session",
                "execution_mode": "manual_trigger",
            },
        )
        r = client.get("/jobs")
        assert b"lab-session" in r.content

    def test_manual_trigger_scenario_available_in_capture_modal(self, client):
        r = client.get("/jobs/new")
        assert b'value="lab-session"' in r.content
        assert b'data-mode="manual_trigger"' in r.content

    def test_acquisitions_label_absent(self, client):
        r = client.get("/jobs")
        assert b"Acquisitions" not in r.content

    def test_jobs_target_picker_hides_attacker_and_appliance_images(self, client, monkeypatch):
        import astinuslab.jobs.job_services as svc_mod
        import astinuslab.ui.jobs_routes as jobs_routes

        autospec_setattr(
            monkeypatch,
            svc_mod,
            "load_targets",
            lambda: [
                {
                    "id": "ubuntu-target",
                    "enabled": True,
                    "distro": "ubuntu",
                    "family": "debian",
                    "display_name": "Ubuntu Target",
                    "image_ref": "ubuntu.qcow2",
                },
                {
                    "id": "kali-attacker",
                    "enabled": True,
                    "distro": "kali",
                    "family": "debian",
                    "display_name": "Kali Attacker",
                    "image_ref": "kali.qcow2",
                    "cloud_init_profile": "attacker",
                },
                {
                    "id": "router-appliance",
                    "enabled": True,
                    "distro": "openwrt",
                    "family": "other",
                    "display_name": "Router Appliance",
                    "image_ref": "router.qcow2",
                    "role": "appliance",
                },
            ],
        )
        autospec_setattr(monkeypatch, jobs_routes, "load_attackers", lambda: [{"image_ref": "kali.qcow2"}])
        autospec_setattr(monkeypatch, jobs_routes, "load_network_nodes", lambda: [{"image_ref": "router.qcow2"}])

        body = client.get("/jobs/new").text

        assert 'value="ubuntu-target"' in body
        assert 'value="kali-attacker"' not in body
        assert 'value="router-appliance"' not in body


class TestJobsTableFragment:
    def test_returns_200(self, client):
        r = client.get("/jobs-table-fragment")
        assert r.status_code == 200

    def test_content_is_html(self, client):
        r = client.get("/jobs-table-fragment")
        assert "text/html" in r.headers["content-type"]

    def test_includes_auto_refresh_trigger(self, client):
        r = client.get("/jobs-table-fragment")
        assert b"hx-trigger" in r.content
        assert b"every 5s" in r.content

    def test_shows_empty_state_when_no_jobs(self, client):
        r = client.get("/jobs-table-fragment")
        assert b"No jobs" in r.content

    def test_shows_jobs_when_present(self, client, created_job):
        r = client.get("/jobs-table-fragment")
        assert created_job["id"][:8].encode() in r.content
        assert f'id="job-open-{created_job["id"]}"' in r.text
        assert f'id="job-select-{created_job["id"]}"' in r.text
        assert 'id="jobs-select-all"' in r.text
        assert 'id="jobs-bulk-delete"' in r.text

    def test_delete_modal_has_dialog_semantics(self, client, created_job):
        body = client.get("/jobs-table-fragment").text

        assert 'role="dialog"' in body
        assert 'aria-modal="true"' in body
        assert 'aria-labelledby="delete-job-modal-title"' in body
        assert 'id="delete-job-modal-title"' in body

    def test_delete_icon_button_has_accessible_name(self, client, created_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(created_job["id"], status="completed")

        body = client.get("/jobs-table-fragment").text

        assert 'aria-label="Delete job ' + created_job["id"][:8] in body
        assert f'id="job-delete-{created_job["id"]}"' in body


class TestCreateJobForm:
    def test_redirects_to_job_detail(self, client):
        r = client.post(
            "/jobs/new",
            data={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "immediate-capture",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"].startswith("/jobs/job-")

    def test_follows_redirect_to_valid_page(self, client):
        r = client.post(
            "/jobs/new",
            data={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "immediate-capture",
            },
        )
        assert r.status_code == 200
        assert b"immediate-capture" in r.content

    def test_invalid_scenario_raises_error(self, client):
        r = client.post(
            "/jobs/new",
            data={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "nonexistent-scenario",
            },
        )
        assert r.status_code == 400

    def test_post_script_scenario_creates_job(self, client):
        r = client.post(
            "/jobs/new",
            data={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "workload-sample-post-script",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303

    def test_manual_trigger_scenario_creates_job(self, client):
        r = client.post(
            "/jobs/new",
            data={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "lab-session",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303

    def test_index_hides_exact_target_image_name_in_target_cards(self, client):
        r = client.get("/jobs")
        assert b"ubuntu-22.04-server-cloudimg-amd64-disk-kvm.img" not in r.content


class TestWorkloadPanel:
    def test_manual_trigger_fragment_shows_service_labels_instead_of_cve_noise(self, client):
        r = client.get("/fragments/workload-panel?scenario_id=lab-session&target_id=ubuntu-22-04-default")
        assert r.status_code == 200
        assert WORDPRESS_NAME.encode() in r.content
        assert b"CVE-2023-2745" not in r.content
        assert b"intentionally vulnerable" not in r.content

    def test_manual_trigger_fragment_requires_target_for_compatibility_view(self, client):
        r = client.get("/fragments/workload-panel?scenario_id=lab-session")
        assert r.status_code == 200
        assert b"Select a target to show only compatible workloads." in r.content
        assert WORDPRESS_NAME.encode() not in r.content

    def test_manual_trigger_fragment_hides_incompatible_workloads(self, client):
        r = client.get("/fragments/workload-panel?scenario_id=lab-session&target_id=fedora-40")
        assert r.status_code == 200
        assert WORDPRESS_NAME.encode() not in r.content
        assert b"No workloads are compatible with the selected target." in r.content

    def test_fragment_has_no_inline_script(self, client):
        # htmx-loaded fragments cannot carry a valid CSP nonce (each response
        # gets a fresh one), so any inline <script> here is silently blocked
        # by the browser. Behaviors live in job_new.html's page script.
        r = client.get("/fragments/workload-panel?scenario_id=lab-session&target_id=ubuntu-22-04-default")
        assert r.status_code == 200
        assert "<script" not in r.text

    def test_manual_trigger_fragment_uses_compact_attacker_selector(self, client):
        r = client.get("/fragments/workload-panel?scenario_id=lab-session&target_id=ubuntu-22-04-default")
        assert r.status_code == 200
        body = r.text
        assert "data-attacker-picker" in body
        assert "data-attacker-summary" in body
        assert "data-attacker-change" in body
        assert "data-attacker-clear" in body
        assert "data-attacker-action" in body
        assert "data-attacker-action-label>select<" in body
        assert ">▾</span>" in body
        assert "attacker-row__inline-select" in body
        assert 'class="attacker-row__select"' not in body
        assert body.index("data-attacker-action") < body.index("data-attacker-clear")


class TestAdminWorkloadsPage:
    def test_search_matches_service_name(self, client):
        r = client.get("/workloads?q=mysql")
        assert r.status_code == 200
        assert WORDPRESS_NAME.encode() in r.content
        assert b"DVWA" in r.content

    def test_search_matches_service_version(self, client):
        r = client.get("/workloads?q=6.4")
        assert r.status_code == 200
        assert WORDPRESS_NAME.encode() in r.content

    def test_can_save_workload_template_to_real_catalog_path(self, client, monkeypatch, tmp_path):
        import astinuslab.catalog as catalog_mod
        import astinuslab.ui as ui_mod

        catalog_path = tmp_path / "workloads.yaml"
        catalog_path.write_text("version: 1\nworkloads: []\n", encoding="utf-8")
        data = catalog_mod.settings.model_dump()
        data["workloads_catalog_path"] = catalog_path
        data["base_dir"] = tmp_path
        data["data_dir"] = tmp_path
        patched_settings = SimpleNamespace(**data)
        monkeypatch.setattr(catalog_mod, "settings", patched_settings)
        monkeypatch.setattr(ui_mod, "settings", patched_settings)

        r = client.post(
            "/workloads/save",
            data={
                "editor_yaml": "\n".join(
                    [
                        "id: nginx-lab",
                        "enabled: true",
                        'display_name: "Nginx Lab"',
                        "services:",
                        "  - name: Nginx",
                        '    version: "1.26"',
                        "    port: 80",
                    ]
                ),
                "original_id": "",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303, f"Expected 303, got {r.status_code}. Response: {r.text}"
        saved = catalog_path.read_text(encoding="utf-8")
        assert "nginx-lab" in saved
        assert "1.26" in saved


class TestDumpVault:
    def test_nested_job_disk_dump_is_listed_downloaded_and_deleted(
        self, client, in_memory_session_scope, monkeypatch, tmp_path
    ):
        import astinuslab.ui as ui_mod
        from astinuslab.core.config import settings
        from astinuslab.db.repositories import JobRepository

        data = settings.model_dump()
        data["base_dir"] = tmp_path
        data["data_dir"] = tmp_path
        data["storage_dir"] = tmp_path / "storage"
        patched_settings = SimpleNamespace(**data)
        monkeypatch.setattr(ui_mod, "settings", patched_settings)

        response = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "lab-session",
                "execution_mode": "manual_trigger",
                "capture_type": "disk",
            },
        )
        assert response.status_code == 201
        job = response.json()

        disk_dir = tmp_path / "storage" / "dumps" / job["id"]
        disk_dir.mkdir(parents=True)
        disk_file = disk_dir / f"{job['id']}-disk.img.gz"
        disk_file.write_bytes(b"disk")

        with in_memory_session_scope() as session:
            from astinuslab.db.repositories import ArtifactRepository

            repo = JobRepository(session)
            repo.force_transition(job["id"], "completed")
            ArtifactRepository(session).create_artifact(
                kind="disk",
                producer="job_capture",
                path=str(disk_file),
                filename=disk_file.name,
                job_id=job["id"],
            )

        body = client.get("/dumps").text
        assert disk_file.name in body
        assert "dump_file_missing" not in body
        from astinuslab.ui.artifact_routes import _list_dump_groups

        vault = _list_dump_groups()
        assert vault["total_files"] == 1
        assert vault["total_gb"] == 4 / 1024**3

        download = client.get(f"/dumps/{job['id']}/disk/download")
        assert download.status_code == 200
        assert download.content == b"disk"

        delete = client.post(
            "/dumps/delete",
            data={"item": f"{job['id']}:disk"},
            follow_redirects=False,
        )
        assert delete.status_code == 303
        assert not disk_file.exists()

        with in_memory_session_scope() as session:
            stored = JobRepository(session).get_job(job["id"])
            assert stored is not None
            assert stored.disk_dump_filename is None
            assert stored.disk_dump_path is None

    def test_dump_vault_shows_live_copy_badge(self, client, in_memory_session_scope, monkeypatch, tmp_path):
        import astinuslab.ui as ui_mod
        from astinuslab.core.config import settings
        from astinuslab.db.repositories import ArtifactRepository, JobRepository

        data = settings.model_dump()
        data["base_dir"] = tmp_path
        data["data_dir"] = tmp_path
        data["storage_dir"] = tmp_path / "storage"
        monkeypatch.setattr(ui_mod, "settings", SimpleNamespace(**data))

        jobs = []
        for suffix in ("live", "offline"):
            response = client.post(
                "/api/v1/jobs",
                json={
                    "target_id": "ubuntu-22-04-default",
                    "scenario_id": "lab-session",
                    "execution_mode": "manual_trigger",
                    "capture_type": "disk",
                    "custom_script": f"echo {suffix}",
                },
            )
            assert response.status_code == 201
            jobs.append(response.json())

        live_job, offline_job = jobs
        live_file = tmp_path / "storage" / "dumps" / live_job["id"] / f"{live_job['id']}-disk.img.gz"
        offline_file = tmp_path / "storage" / "dumps" / offline_job["id"] / f"{offline_job['id']}-disk.img.gz"
        live_file.parent.mkdir(parents=True)
        offline_file.parent.mkdir(parents=True)
        live_file.write_bytes(b"live-disk")
        offline_file.write_bytes(b"offline-disk")

        with in_memory_session_scope() as session:
            job_repo = JobRepository(session)
            artifact_repo = ArtifactRepository(session)
            for job, file_path, is_live in (
                (live_job, live_file, True),
                (offline_job, offline_file, False),
            ):
                job_repo.force_transition(job["id"], "completed")
                artifact_repo.create_artifact(
                    kind="disk",
                    producer="job_capture",
                    path=str(file_path),
                    filename=file_path.name,
                    job_id=job["id"],
                    metadata_json=(
                        '{"capture_method": "remote_disk", "live_acquisition": true}'
                        if is_live
                        else '{"capture_method": "offline_disk", "live_acquisition": false}'
                    ),
                )

        body = client.get("/dumps").text
        assert "Live copy" in body
        assert body.count("Live copy — non-forensic") == 1

    def test_dump_delete_and_download_are_job_scoped(self, client, in_memory_session_scope, monkeypatch, tmp_path):
        import astinuslab.ui as ui_mod
        from astinuslab.core.config import settings
        from astinuslab.db.models import Artifact
        from astinuslab.db.repositories import ArtifactRepository, JobRepository

        data = settings.model_dump()
        data["base_dir"] = tmp_path
        data["data_dir"] = tmp_path
        data["storage_dir"] = tmp_path / "storage"
        monkeypatch.setattr(ui_mod, "settings", SimpleNamespace(**data))

        jobs = []
        for suffix in ("a", "b"):
            response = client.post(
                "/api/v1/jobs",
                json={
                    "target_id": "ubuntu-22-04-default",
                    "scenario_id": "lab-session",
                    "execution_mode": "manual_trigger",
                    "capture_type": "memory",
                    "custom_script": f"echo {suffix}",
                },
            )
            assert response.status_code == 201
            jobs.append(response.json())

        job_a, job_b = jobs
        file_a = tmp_path / "storage" / "dumps" / job_a["id"] / "memory.dump"
        file_b = tmp_path / "storage" / "dumps" / job_b["id"] / "memory.dump"
        file_a.parent.mkdir(parents=True)
        file_b.parent.mkdir(parents=True)
        file_a.write_bytes(b"dump-A")
        file_b.write_bytes(b"dump-B")

        with in_memory_session_scope() as session:
            job_repo = JobRepository(session)
            artifact_repo = ArtifactRepository(session)
            for job, file_path in ((job_a, file_a), (job_b, file_b)):
                job_repo.force_transition(job["id"], "completed")
                artifact_repo.create_artifact(
                    kind="memory",
                    producer="job_capture",
                    path=str(file_path),
                    filename="memory.dump",
                    job_id=job["id"],
                )

        download_a = client.get(f"/dumps/{job_a['id']}/memory/download")
        download_b = client.get(f"/dumps/{job_b['id']}/memory/download")
        assert download_a.status_code == 200
        assert download_b.status_code == 200
        assert download_a.content == b"dump-A"
        assert download_b.content == b"dump-B"

        delete_a = client.post(
            "/dumps/delete",
            data={"item": f"{job_a['id']}:memory"},
            follow_redirects=False,
        )
        assert delete_a.status_code == 303
        assert not file_a.exists()
        assert file_b.exists()
        with in_memory_session_scope() as session:
            assert (
                session.query(Artifact).filter(Artifact.job_id == job_a["id"], Artifact.kind == "memory").count() == 0
            )
            assert (
                session.query(Artifact).filter(Artifact.job_id == job_b["id"], Artifact.kind == "memory").count() == 1
            )

        file_a.write_bytes(b"dump-A2")
        with in_memory_session_scope() as session:
            ArtifactRepository(session).create_artifact(
                kind="memory",
                producer="job_capture",
                path=str(file_a),
                filename="memory.dump",
                job_id=job_a["id"],
            )

        bulk_delete_a = client.post(
            "/dumps/bulk-delete",
            data={"items": f"{job_a['id']}:memory"},
            follow_redirects=False,
        )
        assert bulk_delete_a.status_code == 303
        assert not file_a.exists()
        assert file_b.exists()
        with in_memory_session_scope() as session:
            assert (
                session.query(Artifact).filter(Artifact.job_id == job_a["id"], Artifact.kind == "memory").count() == 0
            )
            assert (
                session.query(Artifact).filter(Artifact.job_id == job_b["id"], Artifact.kind == "memory").count() == 1
            )

    def test_dump_delete_rejects_malformed_item(self, client, in_memory_session_scope, monkeypatch, tmp_path):
        import astinuslab.ui as ui_mod
        from astinuslab.core.config import settings
        from astinuslab.db.repositories import ArtifactRepository, JobRepository

        data = settings.model_dump()
        data["base_dir"] = tmp_path
        data["data_dir"] = tmp_path
        data["storage_dir"] = tmp_path / "storage"
        monkeypatch.setattr(ui_mod, "settings", SimpleNamespace(**data))

        response = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "lab-session",
                "execution_mode": "manual_trigger",
                "capture_type": "memory",
            },
        )
        assert response.status_code == 201
        job = response.json()
        dump_file = tmp_path / "storage" / "dumps" / job["id"] / "memory.dump"
        dump_file.parent.mkdir(parents=True)
        dump_file.write_bytes(b"dump")

        with in_memory_session_scope() as session:
            JobRepository(session).force_transition(job["id"], "completed")
            ArtifactRepository(session).create_artifact(
                kind="memory",
                producer="job_capture",
                path=str(dump_file),
                filename="memory.dump",
                job_id=job["id"],
            )

        delete = client.post(
            "/dumps/delete",
            data={"item": "no-colon-here"},
            follow_redirects=False,
        )
        assert delete.status_code == 303
        assert dump_file.exists()

    def test_dump_download_rejects_symlink_outside_vault(self, client, in_memory_session_scope, monkeypatch, tmp_path):
        import astinuslab.ui as ui_mod
        from astinuslab.core.config import settings
        from astinuslab.db.repositories import ArtifactRepository, JobRepository

        data = settings.model_dump()
        data["base_dir"] = tmp_path
        data["data_dir"] = tmp_path
        data["storage_dir"] = tmp_path / "storage"
        monkeypatch.setattr(ui_mod, "settings", SimpleNamespace(**data))

        response = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "lab-session",
                "execution_mode": "manual_trigger",
                "capture_type": "memory",
            },
        )
        assert response.status_code == 201
        job = response.json()

        outside = tmp_path / "outside.dump"
        outside.write_bytes(b"outside-vault")
        dump_link = tmp_path / "storage" / "dumps" / job["id"] / "memory.dump"
        dump_link.parent.mkdir(parents=True)
        dump_link.symlink_to(outside)

        with in_memory_session_scope() as session:
            JobRepository(session).force_transition(job["id"], "completed")
            ArtifactRepository(session).create_artifact(
                kind="memory",
                producer="job_capture",
                path=str(dump_link),
                filename="memory.dump",
                job_id=job["id"],
            )

        download = client.get(f"/dumps/{job['id']}/memory/download", follow_redirects=False)

        assert download.status_code == 303
        assert download.content != b"outside-vault"

    def test_orphan_dump_dir_is_listed_and_deleted(self, client, monkeypatch, tmp_path):
        import astinuslab.ui as ui_mod
        from astinuslab.core.config import settings

        data = settings.model_dump()
        data["base_dir"] = tmp_path
        data["data_dir"] = tmp_path
        data["storage_dir"] = tmp_path / "storage"
        monkeypatch.setattr(ui_mod, "settings", SimpleNamespace(**data))

        orphan = tmp_path / "storage" / "dumps" / "job-orphan001"
        orphan.mkdir(parents=True)
        (orphan / "memory.dump").write_bytes(b"x" * 16)
        (orphan / "memory.dump.sha256").write_text("abc")

        body = client.get("/dumps").text
        assert "job-orphan001" in body
        from astinuslab.ui.artifact_routes import _list_dump_groups

        vault = _list_dump_groups()
        assert vault["total_files"] == 1
        assert vault["total_gb"] == 16 / 1024**3

        deleted = client.post(
            "/dumps/orphans/delete",
            data={"dirnames": "job-orphan001"},
            follow_redirects=False,
        )
        assert deleted.status_code == 303
        assert not orphan.exists()

    def test_vol3_isf_only_dir_is_not_listed_as_orphan(self, client, monkeypatch, tmp_path):
        import astinuslab.ui as ui_mod
        from astinuslab.core.config import settings

        data = settings.model_dump()
        data["base_dir"] = tmp_path
        data["data_dir"] = tmp_path
        data["storage_dir"] = tmp_path / "storage"
        monkeypatch.setattr(ui_mod, "settings", SimpleNamespace(**data))

        dumps = tmp_path / "storage" / "dumps"
        isf_only = dumps / "job-isf01" / "vol3"
        isf_only.mkdir(parents=True)
        (isf_only / "symbol-build-host.log").write_text("log")
        with_dump = dumps / "job-dump01"
        with_dump.mkdir(parents=True)
        (with_dump / "memory.dump").write_bytes(b"x" * 8)

        body = client.get("/dumps").text
        assert "job-dump01" in body
        assert "job-isf01" not in body
        from astinuslab.ui.artifact_routes import _list_dump_groups

        vault = _list_dump_groups()
        assert vault["total_files"] == 1
        assert vault["total_gb"] == 8 / 1024**3

        (with_dump / "memory.dump").unlink()
        body = client.get("/dumps").text
        assert 'class="vault-summary-card"' not in body
        assert 'class="vault-empty-state"' in body

    def test_orphan_delete_refuses_traversal(self, client, monkeypatch, tmp_path):
        import astinuslab.ui as ui_mod
        from astinuslab.core.config import settings

        data = settings.model_dump()
        data["base_dir"] = tmp_path
        data["data_dir"] = tmp_path
        data["storage_dir"] = tmp_path / "storage"
        monkeypatch.setattr(ui_mod, "settings", SimpleNamespace(**data))

        (tmp_path / "storage" / "dumps").mkdir(parents=True)
        secret = tmp_path / "storage" / "secret"
        secret.mkdir()
        (secret / "keep").write_text("keep")

        client.post(
            "/dumps/orphans/delete",
            data={"dirnames": "../secret"},
            follow_redirects=False,
        )
        assert secret.exists()


class TestJobDetailPage:
    def test_returns_200_for_existing_job(self, client, created_job):
        r = client.get(f"/jobs/{created_job['id']}")
        assert r.status_code == 200

    def test_content_type_is_html(self, client, created_job):
        r = client.get(f"/jobs/{created_job['id']}")
        assert "text/html" in r.headers["content-type"]

    def test_shows_job_id(self, client, created_job):
        r = client.get(f"/jobs/{created_job['id']}")
        assert created_job["id"].encode() in r.content

    def test_shows_target_info(self, client, created_job):
        r = client.get(f"/jobs/{created_job['id']}")
        assert b"22.04" in r.content

    def test_shows_status_badge(self, client, created_job):
        r = client.get(f"/jobs/{created_job['id']}")
        assert b"queued" in r.content

    def test_shows_event_log(self, client, created_job):
        r = client.get(f"/jobs/{created_job['id']}")
        assert b"Event Log" in r.content

    def test_completed_job_with_dump_links_to_volatility(self, client, created_job, in_memory_session_scope):
        from astinuslab.db.repositories import ArtifactRepository, JobRepository

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.force_transition(
                created_job["id"],
                "completed",
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
            )
            ArtifactRepository(session).create_artifact(
                kind="memory",
                producer="job_capture",
                path=f"/tmp/dumps/{created_job['id']}/memory.dump",
                filename="memory.dump",
                job_id=created_job["id"],
            )

        r = client.get(f"/jobs/{created_job['id']}")

        assert f"/volatility?job={created_job['id']}".encode() in r.content
        assert b"Analyze in Volatility" in r.content

    def test_job_without_dump_does_not_link_to_volatility(self, client, created_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            JobRepository(session).force_transition(
                created_job["id"],
                "completed",
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
            )

        r = client.get(f"/jobs/{created_job['id']}")

        assert b"/volatility?job=" not in r.content

    def test_returns_404_for_unknown_job(self, client):
        r = client.get("/jobs/does-not-exist")
        assert r.status_code == 404

    def test_active_job_has_htmx_polling(self, client, created_job):
        r = client.get(f"/jobs/{created_job['id']}")
        assert b"hx-trigger" in r.content
        assert b"every 3s" in r.content

    def test_back_link_present(self, client, created_job):
        r = client.get(f"/jobs/{created_job['id']}")
        assert b'href="/jobs"' in r.content


class TestLiveFragment:
    @pytest.mark.parametrize(
        "status,visible",
        [
            ("queued", True),
            ("waiting_for_capacity", True),
            ("generating_isf", True),
            ("capture_requested", False),
            ("acquiring_memory", False),
            ("acquiring_disk", False),
            ("publishing_artifact", False),
        ],
    )
    def test_cancel_control_matches_supported_status(
        self, client, created_job, in_memory_session_scope, status, visible
    ):
        from astinuslab.db.repositories import JobRepository

        job_id = created_job["id"]
        with in_memory_session_scope() as session:
            JobRepository(session).update_job(job_id, status=status)
        for fragment in ("live-fragment", "live-status-fragment"):
            response = client.get(f"/jobs/{job_id}/{fragment}")
            assert response.status_code == 200
            assert (f'action="/jobs/{job_id}/cancel"' in response.text) is visible

    def test_waiting_capacity_remains_active_and_cancellable(self, client, created_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        job_id = created_job["id"]
        with in_memory_session_scope() as session:
            JobRepository(session).update_job(job_id, status="waiting_for_capacity")
        response = client.get(f"/jobs/{job_id}/live-fragment")
        assert 'hx-trigger="every 3s"' in response.text
        assert f'action="/jobs/{job_id}/cancel"' in response.text
        assert "data-upload-dump-input" not in response.text

    def test_status_poll_refreshes_controls_on_transition(self, client, created_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        job_id = created_job["id"]
        initial = client.get(f"/jobs/{job_id}")
        match = re.search(r'hx-get="([^\"]+live-status-fragment[^\"]*)"', initial.text)
        assert match is not None
        poll_url = match.group(1)
        unchanged = client.get(poll_url)
        assert "hx-retarget" not in unchanged.headers
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(job_id, status="waiting_for_capture", guest_ip="192.0.2.7")
            repo.update_runtime(job_id, "target", ip_address="192.0.2.7", state="running", power_state="on")
        changed = client.get(poll_url)
        assert changed.headers.get("hx-retarget") == "#live-section"
        assert changed.headers.get("hx-reswap") == "outerHTML"
        assert f'action="/jobs/{job_id}/acquire-memory-dump"' in changed.text
        assert "192.0.2.7" in changed.text
        assert "<script" not in changed.text
        match = re.search(r'hx-get="([^\"]+live-status-fragment[^\"]*)"', changed.text)
        assert match is not None
        next_poll = match.group(1)
        assert "hx-retarget" not in client.get(next_poll).headers

    def test_status_poll_refreshes_terminal_artifacts(self, client, created_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        job_id = created_job["id"]
        initial = client.get(f"/jobs/{job_id}")
        match = re.search(r'hx-get="([^\"]+live-status-fragment[^\"]*)"', initial.text)
        assert match is not None
        poll_url = match.group(1)
        with in_memory_session_scope() as session:
            JobRepository(session).update_job(job_id, status="completed")
            from astinuslab.db.repositories import ArtifactRepository

            ArtifactRepository(session).create_artifact(
                kind="memory",
                producer="job_capture",
                path="/storage/dumps/memory.dump",
                filename="memory.dump",
                job_id=job_id,
            )
        changed = client.get(poll_url)
        assert changed.headers.get("hx-retarget") == "#live-section"
        assert "memory.dump" in changed.text
        assert 'hx-trigger="every 3s"' not in changed.text

    def test_returns_200(self, client, created_job):
        r = client.get(f"/jobs/{created_job['id']}/live-fragment")
        assert r.status_code == 200

    def test_content_type_is_html(self, client, created_job):
        r = client.get(f"/jobs/{created_job['id']}/live-fragment")
        assert "text/html" in r.headers["content-type"]

    def test_contains_status_badge(self, client, created_job):
        r = client.get(f"/jobs/{created_job['id']}/live-fragment")
        assert b"queued" in r.content

    def test_contains_event_log(self, client, created_job):
        r = client.get(f"/jobs/{created_job['id']}/live-fragment")
        assert b"Event Log" in r.content

    def test_active_fragment_includes_poll_trigger(self, client, created_job):
        r = client.get(f"/jobs/{created_job['id']}/live-fragment")
        assert b"hx-trigger" in r.content

    def test_404_for_unknown_job(self, client):
        r = client.get("/jobs/ghost/live-fragment")
        assert r.status_code == 404

    def test_terminal_job_has_no_poll_trigger(self, client, created_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(created_job["id"], status="completed")

        r = client.get(f"/jobs/{created_job['id']}/live-fragment")
        assert b"hx-trigger" not in r.content

    def test_completed_job_without_vol3_opt_in_hides_vol3_card(self, client, created_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            from astinuslab.db.repositories import ArtifactRepository

            repo = JobRepository(session)
            repo.update_job(
                created_job["id"],
                status="completed",
                current_stage="completed",
            )
            ArtifactRepository(session).create_artifact(
                kind="memory",
                producer="job_capture",
                path="/storage/dumps/x/memory.dump",
                filename="memory.dump",
                job_id=created_job["id"],
            )

        r = client.get(f"/jobs/{created_job['id']}/live-fragment")

        assert b"Volatility 3 Analysis" not in r.content
        assert b"Volatility 3 ISF Analysis" not in r.content

    def test_job_detail_context_matches_live_fragment_context(self, client, created_job):
        from typing import cast

        from starlette.requests import Request

        from astinuslab.ui.job_context import _job_detail_context

        # `query_params` mirrors the real Request: the context reads ?message= from it
        # so a redirected mutation failure is rendered rather than silently dropped.
        request = SimpleNamespace(cookies={}, query_params={}, state=SimpleNamespace())

        detail_ctx = _job_detail_context(cast(Request, request), created_job["id"])
        live_ctx = _job_detail_context(cast(Request, request), created_job["id"])

        for key in (
            "job",
            "events",
            "actions",
            "runtimes",
            "target",
            "workload",
            "runtime_action_status",
            "is_active",
            "active_ssh_key",
            "vol3_summary",
            "vol3_enabled",
        ):
            assert key in detail_ctx
            assert key in live_ctx
        assert detail_ctx["job"].id == live_ctx["job"].id == created_job["id"]
        assert detail_ctx["is_active"] is live_ctx["is_active"]

    def test_manual_trigger_job_without_vol3_analysis_hides_vol3_card(
        self, client, in_memory_session_scope, monkeypatch
    ):
        import astinuslab.jobs.job_services as svc_mod

        autospec_setattr(monkeypatch, svc_mod, "get_active_key_paths", lambda: ("/tmp/k", "/tmp/k.pub"))
        response = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "lab-session",
                "execution_mode": "manual_trigger",
            },
        )
        assert response.status_code == 201
        job = response.json()

        with in_memory_session_scope() as session:
            from astinuslab.db.repositories import JobRepository

            repo = JobRepository(session)
            repo.update_job(
                job["id"],
                status="waiting_for_capture",
                current_stage="waiting_for_capture",
            )

        r = client.get(f"/jobs/{job['id']}/live-fragment")

        assert b"Volatility 3 Analysis" not in r.content
        assert b"Volatility 3 ISF Analysis" not in r.content

    def test_terminal_job_has_no_cancel_button(self, client, created_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(created_job["id"], status="completed")

        r = client.get(f"/jobs/{created_job['id']}/live-fragment")
        # Check for the cancel form action URL, not the word "Cancel" which
        # also appears in the always-rendered JS confirmation string.
        assert b"/cancel" not in r.content

    def test_completed_job_shows_no_spinner(self, client, created_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(created_job["id"], status="completed")

        r = client.get(f"/jobs/{created_job['id']}/live-fragment")
        assert b'class="spinner"' not in r.content

    def test_waiting_for_capture_shows_trigger_button(self, client, created_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(created_job["id"], status="waiting_for_capture", current_stage="waiting_for_capture")

        r = client.get(f"/jobs/{created_job['id']}/live-fragment")
        assert b"live-section" in r.content

    def test_active_job_progress_rail_is_embedded_in_status_card(self, client, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        response = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "workload-sample-post-script",
                "execution_mode": "post_script",
                "custom_script": "echo ready",
            },
        )
        assert response.status_code == 201
        job = response.json()

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(job["id"], status="running_scenario_script", current_stage="running_scenario_script")

        body = client.get(f"/jobs/{job['id']}/live-fragment").text

        assert 'class="card job-progress-card"' in body
        assert re.search(r'<div class="card job-progress-card"[^>]*>\s*<div class="phase-stepper"', body, re.S)
        assert 'class="flex job-progress-status"' in body
        assert 'aria-current="step"' in body
        assert body.index('class="card job-progress-card"') < body.index('class="phase-stepper"')
        assert body.index('class="phase-stepper"') < body.index('class="flex job-progress-status"')
        assert "running scenario script" in body

    def test_jobs_progress_card_css_keeps_progress_rail_attached_to_status(self):
        css = (ROOT / "src/astinuslab/web/static/css/pages/jobs.css").read_text()

        assert ".job-progress-card" in css
        assert re.search(r"\.job-progress-card\s*\{[^}]*margin-top:\s*var\(--sp-4\)", css, re.S)
        assert re.search(r"\.job-progress-card \.phase-stepper\s*\{[^}]*margin:\s*0 0 var\(--sp-3\)", css, re.S)
        assert ".job-progress-status" in css

    def test_waiting_for_capture_shows_cancel_button(self, client, created_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(created_job["id"], status="waiting_for_capture")

        r = client.get(f"/jobs/{created_job['id']}/live-fragment")
        assert b"Cancel" in r.content

    def test_failed_job_shows_retry_button(self, client, created_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(created_job["id"], status="failed", error_summary="oops")

        r = client.get(f"/jobs/{created_job['id']}/live-fragment")
        assert b"Retry Job" in r.content

    def test_failed_job_shows_error_summary(self, client, created_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(created_job["id"], status="failed", error_summary="something went wrong")

        r = client.get(f"/jobs/{created_job['id']}/live-fragment")
        assert b"something went wrong" in r.content

    def test_completed_job_shows_artifact_section_when_dump_present(self, client, created_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            from astinuslab.db.repositories import ArtifactRepository

            repo = JobRepository(session)
            repo.update_job(created_job["id"], status="completed")
            ArtifactRepository(session).create_artifact(
                kind="memory",
                producer="job_capture",
                path="/var/lib/astinuslab/dumps/dump_20260315.raw",
                filename="dump_20260315.raw",
                sha256="abc123",
                job_id=created_job["id"],
            )

        r = client.get(f"/jobs/{created_job['id']}/live-fragment")
        assert b"dump_20260315.raw" in r.content
        assert b"abc123" in r.content
        assert b"ISF status" not in r.content
        assert b"Not requested" not in r.content

    def test_isf_only_completed_job_shows_isf_result_without_dump_artifact(self, client, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        response = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "immediate-capture",
                "execution_mode": "immediate",
                "build_vol3_isf": True,
            },
        )
        assert response.status_code == 201
        job = response.json()

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(
                job["id"],
                status="completed",
                current_stage="completed",
                detected_kernel_release="5.15.0-test",
            )
            repo.add_event(
                job["id"],
                "vol3_completed",
                "Volatility 3 analysis completed. Symbol generated: /tmp/5.15.0-test.json",
            )

        r = client.get(f"/jobs/{job['id']}/live-fragment")

        assert b"ISF status" in r.content
        assert b"Pending" in r.content or b"Generated" in r.content
        assert b"Notes" in r.content
        assert b"Symbol generated" in r.content

    def test_isf_only_active_job_shows_generate_isf_phase(self, client, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        response = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "immediate-capture",
                "execution_mode": "immediate",
                "build_vol3_isf": True,
            },
        )
        assert response.status_code == 201
        job = response.json()

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(
                job["id"],
                status="generating_isf",
                current_stage="generating_isf",
            )
            repo.add_event(job["id"], "vol3_started", "Volatility 3 ISF generation started")

        r = client.get(f"/jobs/{job['id']}/live-fragment")

        assert b"generating isf" in r.content

    def test_runtime_controls_show_only_needed_actions(self, client, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        response = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "lab-session",
                "execution_mode": "manual_trigger",
                "workload_id": "wordpress-6.4",
                "attacker_vm_id": "kali",
            },
        )
        assert response.status_code == 201, response.text
        job = response.json()

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(job["id"], status="waiting_for_capture", current_stage="waiting_for_capture")
            repo.update_runtime(job["id"], "target", power_state="on", state="running")
            repo.update_runtime(job["id"], "attacker", power_state="paused", state="paused")

        r = client.get(f"/jobs/{job['id']}/live-fragment")

        assert r.status_code == 200
        assert f"/jobs/{job['id']}/runtimes/target/reboot".encode() in r.content
        assert f"/jobs/{job['id']}/runtimes/target/shutdown".encode() in r.content
        assert f"/jobs/{job['id']}/runtimes/attacker/reboot".encode() in r.content
        assert b"/start" not in r.content
        assert b"/stop" not in r.content

    def test_capture_requested_disables_target_runtime_controls(self, client, in_memory_session_scope):
        """The controls stay visible for context but cannot be submitted mid-capture."""
        import re

        from astinuslab.db.repositories import JobRepository

        response = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "lab-session",
                "execution_mode": "manual_trigger",
                "workload_id": "wordpress-6.4",
            },
        )
        assert response.status_code == 201, response.text
        job = response.json()

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(job["id"], status="capture_requested", current_stage="capture_requested")
            repo.update_runtime(job["id"], "target", power_state="on", state="running")

        r = client.get(f"/jobs/{job['id']}/live-fragment")

        assert r.status_code == 200
        assert f"/jobs/{job['id']}/runtimes/target/reboot".encode() in r.content
        assert f"/jobs/{job['id']}/runtimes/target/shutdown".encode() in r.content
        for control in ("reboot", "shutdown"):
            form = re.search(rf'<form action="/jobs/{job["id"]}/runtimes/target/{control}".*?</form>', r.text, re.S)
            assert form is not None
            assert re.search(r"<button[^>]*\bdisabled\b", form.group())


class TestCaptureUI:
    def test_redirects_after_capture_trigger(self, client, created_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(created_job["id"], status="waiting_for_capture")

        r = client.post(f"/jobs/{created_job['id']}/acquire-memory-dump", follow_redirects=False)
        assert r.status_code == 303
        assert f"/jobs/{created_job['id']}" in r.headers["location"]

    def test_409_when_not_waiting_for_capture(self, client, created_job):
        r = client.post(f"/jobs/{created_job['id']}/acquire-memory-dump")
        assert r.status_code == 409


class TestRetryUI:
    def test_redirects_to_new_job_after_retry(self, client, created_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(created_job["id"], status="failed", error_summary="test")

        r = client.post(f"/jobs/{created_job['id']}/retry", follow_redirects=False)
        assert r.status_code == 303
        # Redirects to new job (different ID from original)
        location = r.headers["location"]
        assert location.startswith("/jobs/job-")
        assert created_job["id"] not in location

    def test_409_when_job_not_failed(self, client, created_job):
        r = client.post(f"/jobs/{created_job['id']}/retry")
        assert r.status_code == 409


class TestCancelJobUI:
    def test_redirects_after_cancel(self, client, created_job, monkeypatch):
        import astinuslab.ui.job_mutations as job_mutations

        autospec_setattr(monkeypatch, job_mutations, "request_job_cancel", lambda job_id: True)
        r = client.post(f"/jobs/{created_job['id']}/cancel", follow_redirects=False)
        assert r.status_code == 303
        assert f"/jobs/{created_job['id']}" in r.headers["location"]

    def test_cancel_refusal_redirects_with_message(self, client, created_job, monkeypatch):
        import astinuslab.ui.job_mutations as job_mutations

        autospec_setattr(monkeypatch, job_mutations, "request_job_cancel", lambda job_id: False)
        r = client.post(f"/jobs/{created_job['id']}/cancel", follow_redirects=False)
        assert r.status_code == 303
        assert "message=" in r.headers["location"]
        assert "cannot%20be%20cancelled" in r.headers["location"].replace("+", "%20")


class TestForceKillUI:
    def test_force_kill_goes_through_service_layer(self, client, created_job, monkeypatch):
        """UI force-kill must call force_kill_job_service rather than touching libvirt directly."""
        import astinuslab.ui.job_mutations as job_mutations

        calls: list[str] = []

        def fake_service(session, job_id):
            calls.append(job_id)
            return None

        monkeypatch.setattr(job_mutations, "force_kill_job_service", fake_service)
        r = client.post(f"/jobs/{created_job['id']}/force-kill", follow_redirects=False)
        assert r.status_code == 303
        assert calls == [created_job["id"]]


class TestStorageAdminUI:
    def test_storage_page_renders_overview(self, client, monkeypatch):
        import astinuslab.ui.jobs_routes as jobs_routes

        autospec_setattr(
            monkeypatch,
            jobs_routes,
            "storage_stats",
            lambda settings: {
                "dumps_count": 2,
                "dumps_mb": 12.0,
                "logs_count": 1,
                "logs_mb": 3.0,
                "workload_cache_count": 0,
                "workload_cache_mb": 0.0,
                "free_gb": 42.0,
            },
        )

        r = client.get("/storage", follow_redirects=False)

        assert r.status_code == 200
        assert "Storage Admin" in r.text
        assert "0.01 GiB" in r.text
        assert "42.00 GiB" in r.text


class TestWorkloadCacheUI:
    def test_workload_cache_page_renders(self, client):
        r = client.get("/workload-cache")
        assert r.status_code == 200
        assert b"Workload Cache" in r.content
        assert b"Snapshot cache" in r.content or b"No workload snapshots cached yet" in r.content


class TestLogsAdminUI:
    def test_logs_page_renders(self, client):
        r = client.get("/logs")
        assert r.status_code == 200
        assert b"Logs" in r.content
        assert b"api.log" in r.content

    def test_copy_log_button_reports_success_and_failure(self, client):
        body = client.get("/logs").text
        assert "data-copy-log-view" in body
        assert "✓ Copied" in body
        assert "Copy failed" in body

    # ── log selection ─────────────────────────────────────────────────────────

    def test_selects_runner_log_when_requested(self, client, monkeypatch, tmp_path):
        from types import SimpleNamespace

        from astinuslab.ui import logs_routes as ui_mod

        logs_dir = tmp_path / "storage" / "logs"
        logs_dir.mkdir(parents=True)
        (logs_dir / "runner.log").write_text("runner line\n", encoding="utf-8")

        patched = SimpleNamespace(**ui_mod.settings.model_dump())
        patched.base_dir = tmp_path
        patched.data_dir = tmp_path
        patched.storage_dir = tmp_path / "storage"
        monkeypatch.setattr(ui_mod, "settings", patched)

        r = client.get("/logs?log=runner.log")
        assert r.status_code == 200
        assert b"runner.log" in r.content

    def test_selects_the_analysis_worker_log_when_requested(self, client, monkeypatch, tmp_path):
        """The analysis worker owns interactive Volatility runs; its log must be readable."""
        from types import SimpleNamespace

        from astinuslab.ui import logs_routes as ui_mod

        logs_dir = tmp_path / "storage" / "logs"
        logs_dir.mkdir(parents=True)
        (logs_dir / "analysis-worker.log").write_text("analysis line\n", encoding="utf-8")

        patched = SimpleNamespace(**ui_mod.settings.model_dump())
        patched.base_dir = tmp_path
        patched.data_dir = tmp_path
        patched.storage_dir = tmp_path / "storage"
        monkeypatch.setattr(ui_mod, "settings", patched)

        r = client.get("/logs?log=analysis-worker.log")
        assert r.status_code == 200
        assert b"analysis-worker.log" in r.content
        assert b"analysis line" in r.content

    def test_every_service_log_is_offered_on_the_page(self, client):
        from astinuslab.core.logging_config import LOG_FILE_NAMES

        body = client.get("/logs").content
        for name in LOG_FILE_NAMES:
            assert name.encode() in body

    def test_analysis_worker_log_is_downloadable(self, client, monkeypatch, tmp_path):
        from types import SimpleNamespace

        from astinuslab.ui import logs_routes as ui_mod

        logs_dir = tmp_path / "storage" / "logs"
        logs_dir.mkdir(parents=True)
        (logs_dir / "analysis-worker.log").write_text("analysis line\n", encoding="utf-8")

        patched = SimpleNamespace(**ui_mod.settings.model_dump())
        patched.base_dir = tmp_path
        patched.data_dir = tmp_path
        patched.storage_dir = tmp_path / "storage"
        monkeypatch.setattr(ui_mod, "settings", patched)

        r = client.get("/logs/analysis-worker.log/download")
        assert r.status_code == 200
        assert "analysis line" in r.text

    def test_invalid_log_name_falls_back_to_api_log(self, client):
        r = client.get("/logs?log=../../etc/passwd")
        assert r.status_code == 200
        # page renders, selected log must be api.log (the fallback)
        assert b"api.log" in r.content

    def test_download_rejects_a_log_outside_the_declared_set(self, client):
        assert client.get("/logs/docs.log/download").status_code == 404

    def test_unknown_log_name_falls_back_to_api_log(self, client):
        r = client.get("/logs?log=evil.log")
        assert r.status_code == 200
        assert b"api.log" in r.content

    # ── line count clamping ───────────────────────────────────────────────────

    def test_lines_below_minimum_clamped_to_50(self, client):
        r = client.get("/logs?lines=5")
        assert r.status_code == 200
        # The rendered form should reflect the clamped value
        assert b'value="50"' in r.content

    def test_lines_above_maximum_clamped_to_1000(self, client):
        r = client.get("/logs?lines=9999")
        assert r.status_code == 200
        assert b'value="1000"' in r.content

    def test_valid_lines_in_range_are_accepted(self, client):
        r = client.get("/logs?lines=200")
        assert r.status_code == 200
        assert b'value="200"' in r.content

    # ── HTML bounds match server clamp ────────────────────────────────────────

    def test_html_input_min_matches_server_clamp(self, client):
        body = client.get("/logs").text
        assert 'min="50"' in body

    def test_html_input_max_matches_server_clamp(self, client):
        body = client.get("/logs").text
        assert 'max="1000"' in body

    # ── missing log file ─────────────────────────────────────────────────────

    def test_missing_log_shows_no_content_message(self, client, monkeypatch, tmp_path):
        from types import SimpleNamespace

        from astinuslab.ui import logs_routes as ui_mod

        logs_dir = tmp_path / "storage" / "logs"
        logs_dir.mkdir(parents=True)
        # do NOT create api.log — it should be absent

        patched = SimpleNamespace(**ui_mod.settings.model_dump())
        patched.base_dir = tmp_path
        patched.data_dir = tmp_path
        patched.storage_dir = tmp_path / "storage"
        monkeypatch.setattr(ui_mod, "settings", patched)

        r = client.get("/logs")
        assert r.status_code == 200
        assert b"No content available" in r.content

    def test_existing_log_shows_its_content(self, client, monkeypatch, tmp_path):
        from types import SimpleNamespace

        from astinuslab.ui import logs_routes as ui_mod

        logs_dir = tmp_path / "storage" / "logs"
        logs_dir.mkdir(parents=True)
        (logs_dir / "api.log").write_text("hello from api log\n", encoding="utf-8")

        patched = SimpleNamespace(**ui_mod.settings.model_dump())
        patched.base_dir = tmp_path
        patched.data_dir = tmp_path
        patched.storage_dir = tmp_path / "storage"
        monkeypatch.setattr(ui_mod, "settings", patched)

        r = client.get("/logs")
        assert r.status_code == 200
        assert b"hello from api log" in r.content

    # ── download endpoint ─────────────────────────────────────────────────────

    def test_download_api_log_returns_file(self, client, monkeypatch, tmp_path):
        from types import SimpleNamespace

        from astinuslab.ui import logs_routes as ui_mod

        logs_dir = tmp_path / "storage" / "logs"
        logs_dir.mkdir(parents=True)
        (logs_dir / "api.log").write_text("download me\n", encoding="utf-8")

        patched = SimpleNamespace(**ui_mod.settings.model_dump())
        patched.base_dir = tmp_path
        patched.data_dir = tmp_path
        patched.storage_dir = tmp_path / "storage"
        monkeypatch.setattr(ui_mod, "settings", patched)

        r = client.get("/logs/api.log/download")
        assert r.status_code == 200
        assert b"download me" in r.content

    def test_download_runner_log_returns_file(self, client, monkeypatch, tmp_path):
        from types import SimpleNamespace

        from astinuslab.ui import logs_routes as ui_mod

        logs_dir = tmp_path / "storage" / "logs"
        logs_dir.mkdir(parents=True)
        (logs_dir / "runner.log").write_text("runner output\n", encoding="utf-8")

        patched = SimpleNamespace(**ui_mod.settings.model_dump())
        patched.base_dir = tmp_path
        patched.data_dir = tmp_path
        patched.storage_dir = tmp_path / "storage"
        monkeypatch.setattr(ui_mod, "settings", patched)

        r = client.get("/logs/runner.log/download")
        assert r.status_code == 200
        assert b"runner output" in r.content

    def test_download_absent_file_returns_404(self, client, monkeypatch, tmp_path):
        from types import SimpleNamespace

        from astinuslab.ui import logs_routes as ui_mod

        logs_dir = tmp_path / "storage" / "logs"
        logs_dir.mkdir(parents=True)
        # api.log is not created

        patched = SimpleNamespace(**ui_mod.settings.model_dump())
        patched.base_dir = tmp_path
        patched.data_dir = tmp_path
        patched.storage_dir = tmp_path / "storage"
        monkeypatch.setattr(ui_mod, "settings", patched)

        r = client.get("/logs/api.log/download")
        assert r.status_code == 404

    def test_download_rejects_path_traversal(self, client):
        r = client.get("/logs/../../etc/passwd/download")
        assert r.status_code == 404

    def test_download_rejects_arbitrary_filename(self, client):
        r = client.get("/logs/evil.log/download")
        assert r.status_code == 404

    def test_download_whitelist_only_allows_two_names(self, client, monkeypatch, tmp_path):
        """Verifies the exact whitelist: only api.log and runner.log are served."""
        from types import SimpleNamespace

        from astinuslab.ui import logs_routes as ui_mod

        logs_dir = tmp_path / "storage" / "logs"
        logs_dir.mkdir(parents=True)
        (logs_dir / "secrets.log").write_text("secret\n", encoding="utf-8")

        patched = SimpleNamespace(**ui_mod.settings.model_dump())
        patched.base_dir = tmp_path
        patched.data_dir = tmp_path
        patched.storage_dir = tmp_path / "storage"
        monkeypatch.setattr(ui_mod, "settings", patched)

        r = client.get("/logs/secrets.log/download")
        assert r.status_code == 404

    # ── docs.log ─────────────────────────────────────────────────────────────

    def test_docs_log_does_not_appear_in_file_list(self, client):
        body = client.get("/logs").text
        assert "docs.log" not in body

    def test_docs_log_download_rejected_when_present(self, client, monkeypatch, tmp_path):
        from types import SimpleNamespace

        from astinuslab.ui import logs_routes as ui_mod

        logs_dir = tmp_path / "storage" / "logs"
        logs_dir.mkdir(parents=True)
        (logs_dir / "docs.log").write_text("mkdocs output\n", encoding="utf-8")

        patched = SimpleNamespace(**ui_mod.settings.model_dump())
        patched.base_dir = tmp_path
        patched.data_dir = tmp_path
        patched.storage_dir = tmp_path / "storage"
        monkeypatch.setattr(ui_mod, "settings", patched)

        r = client.get("/logs/docs.log/download")
        assert r.status_code == 404

    # ── level filter ──────────────────────────────────────────────────────────

    def test_level_filter_param_is_reflected_in_rendered_select(self, client, monkeypatch, tmp_path):
        from types import SimpleNamespace

        from astinuslab.ui import logs_routes as ui_mod

        logs_dir = tmp_path / "storage" / "logs"
        logs_dir.mkdir(parents=True)
        (logs_dir / "api.log").write_text(
            "2026-01-01 00:00:01 [WARNING] test: warn line\n2026-01-01 00:00:02 [DEBUG] test: debug line\n",
            encoding="utf-8",
        )

        patched = SimpleNamespace(**ui_mod.settings.model_dump())
        patched.base_dir = tmp_path
        patched.data_dir = tmp_path
        patched.storage_dir = tmp_path / "storage"
        monkeypatch.setattr(ui_mod, "settings", patched)

        r = client.get("/logs?level=warning")
        assert r.status_code == 200
        assert b'value="warning" selected' in r.content or b'value="warning"  selected' in r.content

    def test_level_filter_warning_excludes_debug_lines(self, client, monkeypatch, tmp_path):
        from types import SimpleNamespace

        from astinuslab.ui import logs_routes as ui_mod

        logs_dir = tmp_path / "storage" / "logs"
        logs_dir.mkdir(parents=True)
        (logs_dir / "api.log").write_text(
            "2026-01-01 00:00:01 [WARNING] test: visible warn\n2026-01-01 00:00:02 [DEBUG] test: hidden debug\n",
            encoding="utf-8",
        )

        patched = SimpleNamespace(**ui_mod.settings.model_dump())
        patched.base_dir = tmp_path
        patched.data_dir = tmp_path
        patched.storage_dir = tmp_path / "storage"
        monkeypatch.setattr(ui_mod, "settings", patched)

        r = client.get("/logs?level=warning")
        assert b"visible warn" in r.content
        assert b"hidden debug" not in r.content

    def test_level_filter_activates_max_2000_lines(self, client):
        # With level=warning the rendered max attribute should be 2000
        r = client.get("/logs?level=warning")
        assert r.status_code == 200
        assert b'max="2000"' in r.content

    def test_no_level_filter_uses_max_1000_lines(self, client):
        r = client.get("/logs")
        assert r.status_code == 200
        assert b'max="1000"' in r.content

    # ── fragment endpoint ─────────────────────────────────────────────────────

    def test_fragment_endpoint_returns_200(self, client):
        r = client.get("/logs/fragment?log=api.log&lines=200&level=all")
        assert r.status_code == 200

    def test_fragment_endpoint_does_not_render_sidebar(self, client):
        r = client.get("/logs/fragment")
        assert r.status_code == 200
        assert b'class="sidebar"' not in r.content
        assert b"log-viewport" in r.content

    def test_invalid_level_param_falls_back_to_all(self, client):
        r = client.get("/logs?level=nonsense")
        assert r.status_code == 200
        # all levels shown - no restriction applied, page still renders
        assert b"log-viewport" in r.content

    # ── modified time is UTC-aware ────────────────────────────────────────────

    def test_available_logs_modified_is_utc_aware(self, monkeypatch, tmp_path):
        from types import SimpleNamespace

        from astinuslab.ui import logs_routes as ui_mod

        logs_dir = tmp_path / "storage" / "logs"
        logs_dir.mkdir(parents=True)
        (logs_dir / "api.log").write_text("x\n", encoding="utf-8")

        patched = SimpleNamespace(**ui_mod.settings.model_dump())
        patched.base_dir = tmp_path
        patched.data_dir = tmp_path
        patched.storage_dir = tmp_path / "storage"
        monkeypatch.setattr(ui_mod, "settings", patched)

        entries = ui_mod._available_logs()
        api_entry = next(e for e in entries if e["name"] == "api.log")
        assert api_entry["exists"] is True
        assert api_entry["modified"] is not None
        assert api_entry["modified"].tzinfo is UTC


class TestTailFiltered:
    """Unit tests for _tail_filtered() seek-from-end implementation."""

    def test_empty_file_returns_empty_list(self, tmp_path):
        from astinuslab.ui.logs_routes import _tail_filtered

        p = tmp_path / "empty.log"
        p.write_bytes(b"")
        assert _tail_filtered(p) == []

    def test_nonexistent_file_returns_empty_list(self, tmp_path):
        from astinuslab.ui.logs_routes import _tail_filtered

        assert _tail_filtered(tmp_path / "no-such.log") == []

    def test_file_with_fewer_lines_than_requested_returns_all(self, tmp_path):
        from astinuslab.ui.logs_routes import _tail_filtered

        p = tmp_path / "short.log"
        p.write_text("line1\nline2\nline3\n", encoding="utf-8")
        result = _tail_filtered(p, line_count=100)
        result_lines = [d["text"] for d in result]
        assert "line1" in result_lines
        assert "line2" in result_lines
        assert "line3" in result_lines

    def test_file_with_more_lines_than_requested_returns_only_tail(self, tmp_path):
        from astinuslab.ui.logs_routes import _tail_filtered

        p = tmp_path / "big.log"
        lines = [f"line{i:04d}" for i in range(500)]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = _tail_filtered(p, line_count=10)
        result_lines = [d["text"] for d in result]
        assert len(result_lines) == 10
        assert result_lines[-1] == "line0499"
        assert result_lines[0] == "line0490"
        assert "line0000" not in result_lines

    def test_large_file_does_not_read_from_start(self, tmp_path):
        """Verify we don't read more than needed: use a chunk smaller than file."""
        from astinuslab.ui.logs_routes import _tail_filtered

        p = tmp_path / "large.log"
        # 1000 lines of 80 chars each = ~80KB; use chunk_size=1024 to force multi-chunk
        lines = [f"{'x' * 70}{i:06d}" for i in range(1000)]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = _tail_filtered(p, line_count=5, chunk_size=1024)
        result_lines = [d["text"] for d in result]
        assert len(result_lines) == 5
        assert "000999" in result_lines[-1]

    def test_partial_chunk_prefix_is_not_returned_as_line(self, tmp_path):
        from astinuslab.ui.logs_routes import _tail_filtered

        p = tmp_path / "long.log"
        lines = [f"line{i:04d}-{'x' * 120}" for i in range(100)]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = _tail_filtered(p, line_count=5, chunk_size=256)
        result_lines = [d["text"] for d in result]
        assert len(result_lines) == 5
        assert result_lines[0].startswith("line0095-")
        assert all(line.startswith("line") for line in result_lines)

    def test_file_with_non_utf8_bytes_does_not_raise(self, tmp_path):
        from astinuslab.ui.logs_routes import _tail_filtered

        p = tmp_path / "binary.log"
        p.write_bytes(b"good line\n\xff\xfe bad bytes\ngood tail\n")
        result = _tail_filtered(p, line_count=50)
        assert any("good tail" in d["text"] for d in result)

    def test_level_filter_returns_only_matching_lines(self, tmp_path):
        from astinuslab.ui.logs_routes import _tail_filtered

        p = tmp_path / "mixed.log"
        p.write_text(
            "2026-01-01 [DEBUG] test: debug line\n"
            "2026-01-01 [WARNING] test: warn line\n"
            "2026-01-01 [DEBUG] test: another debug\n",
            encoding="utf-8",
        )
        result = _tail_filtered(p, line_count=50, level="warning")
        assert len(result) == 1
        assert result[0]["level"] == "warning"
        assert "warn line" in result[0]["text"]

    def test_level_filter_scans_past_initial_chunk_to_find_matches(self, tmp_path):
        from astinuslab.ui.logs_routes import _tail_filtered

        # 200 DEBUG lines, then 5 WARNING lines at the top
        p = tmp_path / "big.log"
        lines = [f"2026-01-01 [DEBUG] test: debug {i}\n" for i in range(200)]
        lines = [f"2026-01-01 [WARNING] test: warning {i}\n" for i in range(5)] + lines
        p.write_text("".join(lines), encoding="utf-8")
        result = _tail_filtered(p, line_count=5, level="warning", chunk_size=512)
        assert len(result) == 5
        assert all(d["level"] == "warning" for d in result)

    def test_q_filter_returns_only_matching_text(self, tmp_path):
        from astinuslab.ui.logs_routes import _tail_filtered

        p = tmp_path / "q.log"
        p.write_text(
            "2026-01-01 [INFO] test: job-aabbcc started\n2026-01-01 [INFO] test: job-xxyyzz started\n",
            encoding="utf-8",
        )
        result = _tail_filtered(p, line_count=50, q="aabbcc")
        assert len(result) == 1
        assert "aabbcc" in result[0]["text"]

    def test_max_scan_bytes_limits_search_depth(self, tmp_path):
        from astinuslab.ui.logs_routes import _tail_filtered

        # Put a WARNING at the very start of a large file - outside max_scan_bytes
        far_warning = "2026-01-01 [WARNING] test: unreachable warning\n"
        filler = "2026-01-01 [DEBUG] test: filler\n" * 10000
        p = tmp_path / "huge.log"
        p.write_text(far_warning + filler, encoding="utf-8")
        # Scan only last 1KB - the WARNING is far beyond that
        result = _tail_filtered(p, line_count=5, level="warning", max_scan_bytes=1024)
        assert not any("unreachable" in d["text"] for d in result)


class TestConfigureLogging:
    """Unit tests for configure_logging() idempotency and handler management."""

    def test_handler_is_added_to_root_logger(self):
        import logging

        from astinuslab.core.logging_config import _HANDLER_MARKER, configure_logging

        root = logging.getLogger()
        # Remove any previously installed AstinusLab handler
        root.handlers = [h for h in root.handlers if getattr(h, "name", None) != _HANDLER_MARKER]

        configure_logging("INFO")

        al_handlers = [h for h in root.handlers if getattr(h, "name", None) == _HANDLER_MARKER]
        assert len(al_handlers) == 1

    def test_calling_twice_does_not_duplicate_handler(self):
        import logging

        from astinuslab.core.logging_config import _HANDLER_MARKER, configure_logging

        configure_logging("INFO")
        configure_logging("INFO")

        root = logging.getLogger()
        al_handlers = [h for h in root.handlers if getattr(h, "name", None) == _HANDLER_MARKER]
        assert len(al_handlers) == 1

    def test_level_is_applied_to_root(self):
        import logging

        from astinuslab.core.logging_config import configure_logging

        configure_logging("WARNING")
        assert logging.getLogger().level == logging.WARNING

        configure_logging("DEBUG")
        assert logging.getLogger().level == logging.DEBUG

    def test_existing_non_astinuslab_handler_is_preserved(self):
        import logging

        from astinuslab.core.logging_config import _HANDLER_MARKER, configure_logging

        external = logging.StreamHandler()
        external.name = "external-system"
        root = logging.getLogger()
        root.addHandler(external)

        configure_logging("INFO")

        names = {getattr(h, "name", None) for h in root.handlers}
        assert "external-system" in names
        assert _HANDLER_MARKER in names

        # cleanup
        root.handlers = [h for h in root.handlers if getattr(h, "name", None) != "external-system"]

    def test_images_page_renders_download_settings(self, client):
        r = client.get("/images")
        assert r.status_code == 200
        assert b"Download Settings" in r.content
        assert b"Download image from URL" in r.content

    def test_images_settings_are_persisted(self, client):
        r = client.post(
            "/images/settings",
            data={
                "source_policy": "allowlist",
                "network_policy": "safe",
                "allowed_domains": "example.com\nimages.example.org",
                "connect_timeout_seconds": "12",
                "read_timeout_seconds": "120",
                "max_size_gb": "8",
            },
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert b"Image download settings updated." in r.content
        assert b'value="allowlist"' in r.content
        assert b"selected" in r.content
        assert b"example.com" in r.content
        assert b"images.example.org" in r.content

    def test_fetch_url_blocked_by_allowlist(self, client):
        client.post(
            "/images/settings",
            data={
                "source_policy": "allowlist",
                "network_policy": "safe",
                "allowed_domains": "trusted.example.com",
                "connect_timeout_seconds": "10",
                "read_timeout_seconds": "60",
                "max_size_gb": "20",
            },
        )
        r = client.post("/images/fetch-url", data={"url": "https://untrusted.example.org/image.qcow2"})
        assert r.status_code == 403

    def test_download_progress_has_progressbar_semantics(self, client, monkeypatch):
        from astinuslab.images.image_admin_support import URL_DOWNLOADS

        monkeypatch.setitem(
            URL_DOWNLOADS,
            "testdl",
            {
                "status": "running",
                "url": "https://trusted.example.com/image.qcow2",
                "filename": "image.qcow2",
                "bytes_done": 25,
                "total": 100,
                "error": None,
                "started_at": None,
            },
        )

        body = client.get("/images/fetch-status/testdl").text

        assert 'role="progressbar"' in body
        assert 'aria-valuemin="0"' in body
        assert 'aria-valuemax="100"' in body
        assert 'aria-valuenow="25"' in body

    def test_admin_image_rows_use_delegated_handlers(self):
        template = (ROOT / "src/astinuslab/web/templates/admin_images.html").read_text(encoding="utf-8")

        assert "data-image-meta-filename" in template
        assert "data-reinspect-image-filename" in template
        assert 'onclick="openImageMetaModal' not in template
        assert 'onclick="reinspectImage' not in template

    def test_image_vault_actions_report_status(self):
        template = (ROOT / "src/astinuslab/web/templates/admin_images.html").read_text(encoding="utf-8")

        assert "data-image-vault-status" in template
        assert "showImageVaultStatus('Opening images folder on host…')" in template
        assert "Images folder open request sent." in template
        assert "Inspection queued for" in template
        assert "Metadata saved. Reloading image vault…" in template

    @pytest.mark.parametrize(
        "template_name",
        [
            "admin_images.html",
            "admin_logs.html",
            "job_detail.html",
            "job_new.html",
            "partials/delete_job_modal.html",
            "partials/jobs_table.html",
            "partials/live_section.html",
            "partials/workload_panel.html",
        ],
    )
    def test_high_traffic_templates_do_not_use_inline_event_handlers(self, template_name):
        template = (ROOT / "src/astinuslab/web/templates" / template_name).read_text(encoding="utf-8")

        assert not re.search(
            r"\son(?:click|change|input|submit|keydown|keyup|load|error|mouseover|mouseout|focus|blur)=",
            template,
        )

    def test_templates_do_not_use_htmx_inline_script_handlers(self):
        templates_dir = ROOT / "src/astinuslab/web/templates"
        offenders = [
            str(path.relative_to(templates_dir))
            for path in templates_dir.rglob("*.html")
            if "hx-on" in path.read_text(encoding="utf-8")
        ]

        assert offenders == []

    def test_templates_do_not_use_htmx_eval_filters(self):
        templates_dir = ROOT / "src/astinuslab/web/templates"
        offenders = [
            str(path.relative_to(templates_dir))
            for path in templates_dir.rglob("*.html")
            if re.search(r"hx-trigger=\"[^\"]*\[", path.read_text(encoding="utf-8"))
        ]

        assert offenders == []


class TestSecurityHeaders:
    @pytest.mark.parametrize("path", ["/", "/jobs", "/images", "/logs"])
    def test_ui_routes_enforce_csp(self, client, path):
        response = client.get(path)

        csp = response.headers.get("content-security-policy", "")
        assert response.headers.get("content-security-policy-report-only") is None
        assert "default-src 'self'" in csp
        assert "script-src 'self' 'nonce-" in csp
        assert "'unsafe-inline'" not in csp.split(";", 2)[1]
        assert "frame-ancestors 'none'" in csp
        assert "object-src 'none'" in csp

    def test_inline_scripts_use_response_nonce(self, client):
        response = client.get("/jobs/new")
        csp = response.headers["content-security-policy"]
        match = re.search(r"script-src 'self' 'nonce-([^']+)'", csp)
        assert match is not None
        nonce = match.group(1)

        assert f'nonce="{nonce}"'.encode() in response.content

    def test_ui_assets_do_not_load_google_fonts(self):
        paths = [
            ROOT / "src/astinuslab/web/templates/base.html",
            ROOT / "src/astinuslab/web/static/css/tokens.css",
            ROOT / "src/astinuslab/web/static/css/components.css",
            ROOT / "docs/stylesheets/extra.css",
        ]
        offenders = [
            str(path.relative_to(ROOT)) for path in paths if "fonts.googleapis" in path.read_text(encoding="utf-8")
        ]
        assert offenders == []


class TestSidebar:
    def test_acquisitions_link_absent(self, client):
        r = client.get("/jobs")
        assert b'href="/acquisitions"' not in r.content

    def test_captures_label_present(self, client):
        r = client.get("/jobs")
        assert b"Jobs" in r.content

    def test_ssh_keys_page_is_labeled_security(self, client):
        r = client.get("/ssh-keys")
        assert r.status_code == 200
        assert b"Security" in r.content
        assert b"Manage SSH keypairs" in r.content

    def test_security_page_includes_api_key_generator_when_auth_enabled(self, client, monkeypatch):
        import astinuslab.ui as ui_mod
        from astinuslab.core.config import settings

        data = settings.model_dump()
        data["auth_enabled"] = True
        monkeypatch.setattr(ui_mod, "settings", SimpleNamespace(**data))

        r = client.get("/ssh-keys")

        assert r.status_code == 200
        assert b"Generate API key" in r.content
        assert b'action="/api-keys/generate"' in r.content
        assert b"shown once" in r.content.lower()

    def test_security_page_hides_api_key_generator_when_auth_disabled(self, client):
        r = client.get("/ssh-keys")

        assert r.status_code == 200
        assert b"Generate API key" not in r.content
        assert b'action="/api-keys/generate"' not in r.content

    def test_admin_api_token_generator_shows_token_once_without_caching(self, client, monkeypatch):
        import astinuslab.ui as ui_mod
        from astinuslab.core.config import settings

        data = settings.model_dump()
        data["auth_enabled"] = True
        monkeypatch.setattr(ui_mod, "settings", SimpleNamespace(**data))

        r = client.post("/api-keys/generate")

        assert r.status_code == 200
        assert r.headers["cache-control"] == "no-store"
        body = r.text
        token_match = re.search(r"ASTINUSLAB_ADMIN_API_TOKEN=([A-Za-z0-9_-]{43,})", body)
        hash_match = re.search(r"ASTINUSLAB_ADMIN_API_TOKEN_HASH=(sha256:[0-9a-f]{64})", body)
        assert token_match is not None
        assert hash_match is not None
        assert f"Authorization: Bearer {token_match.group(1)}" in body
        assert token_match.group(1) not in r.url.path


class TestSharedUILayout:
    def test_stylesheet_defines_shared_app_width_and_layout_utilities(self):
        css = _app_css_text()

        assert "--app-content-max: 1280px;" in css
        assert "max-width: var(--app-content-max);" in css
        assert ".layout-grid {" in css
        assert ".layout-grid--2 {" in css
        assert ".layout-grid--3 {" in css
        assert ".stat-grid {" in css
        assert ".auto-card-grid {" in css

    @pytest.mark.parametrize(
        ("template_name", "expected_classes"),
        [
            (
                "admin_images.html",
                ['class="wiz-shell"'],
            ),
            (
                "admin_ssh_keys.html",
                ['class="wiz-shell"'],
            ),
            (
                "admin_workload_cache.html",
                ['class="wiz-shell"'],
            ),
            (
                "partials/live_section.html",
                ['class="auto-card-grid"'],
            ),
        ],
    )
    def test_admin_pages_use_shared_layout_classes(self, template_name, expected_classes):
        template = (ROOT / "src/astinuslab/web/templates" / template_name).read_text(encoding="utf-8")

        for expected in expected_classes:
            assert expected in template
        assert "display:grid;grid-template-columns" not in template


class TestVol3IsActivePolling:
    def test_live_fragment_keeps_polling_when_vol3_queued(self, client, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        response = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "immediate-capture",
                "execution_mode": "immediate",
            },
        )
        assert response.status_code == 201
        job = response.json()

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            from astinuslab.db.repositories import ArtifactRepository

            repo.force_transition(
                job["id"],
                "completed",
                started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
                finished_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            )
            ArtifactRepository(session).create_artifact(
                kind="memory",
                producer="job_capture",
                path="/storage/dumps/x/memory.dump",
                filename="memory.dump",
                job_id=job["id"],
            )
            repo.enqueue_action(job["id"], "run_vol3_analysis")

        r = client.get(f"/jobs/{job['id']}/live-fragment")

        assert r.status_code == 200
        # HTMX polling is enabled by hx-trigger="every 3s" which is only rendered when is_active
        assert b'hx-trigger="every 3s"' in r.content

    def test_live_fragment_stops_polling_when_vol3_completed(self, client, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        response = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "immediate-capture",
                "execution_mode": "immediate",
            },
        )
        assert response.status_code == 201
        job = response.json()

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            from astinuslab.db.repositories import ArtifactRepository

            repo.force_transition(
                job["id"],
                "completed",
                started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
                finished_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            )
            ArtifactRepository(session).create_artifact(
                kind="memory",
                producer="job_capture",
                path="/storage/dumps/x/memory.dump",
                filename="memory.dump",
                job_id=job["id"],
            )
            repo.enqueue_action(job["id"], "run_vol3_analysis")
            action = next(a for a in repo.list_actions(job["id"]) if a.action_type == "run_vol3_analysis")
            repo.update_action(action.id, status="completed")

        r = client.get(f"/jobs/{job['id']}/live-fragment")

        assert r.status_code == 200
        assert b'hx-trigger="every 3s"' not in r.content

    def test_live_fragment_shows_vol3_failure_summary(self, client, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        response = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "immediate-capture",
                "execution_mode": "immediate",
                "build_vol3_isf": True,
            },
        )
        assert response.status_code == 201
        job = response.json()

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            from astinuslab.db.repositories import ArtifactRepository

            repo.force_transition(
                job["id"],
                "completed",
                started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
                finished_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            )
            ArtifactRepository(session).create_artifact(
                kind="memory",
                producer="job_capture",
                path="/storage/dumps/x/memory.dump",
                filename="memory.dump",
                job_id=job["id"],
            )
            action = repo.enqueue_action(job["id"], "run_vol3_analysis")
            repo.update_action(action.id, status="failed", error_summary="ERROR: vmlinux not found")
            repo.add_event(job["id"], "vol3_failed", "Volatility 3 analysis failed: ERROR: vmlinux not found")

        r = client.get(f"/jobs/{job['id']}/live-fragment")

        assert r.status_code == 200
        assert b"ISF status" in r.content
        assert b"Failed" in r.content
        assert b"ERROR: vmlinux not found" in r.content
        assert b"Volatility 3 ISF Analysis" not in r.content

    def test_live_fragment_shows_skipped_symbols_after_completed_without_isf(self, client, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        response = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "immediate-capture",
                "execution_mode": "immediate",
                "build_vol3_isf": True,
            },
        )
        assert response.status_code == 201
        job = response.json()

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            from astinuslab.db.repositories import ArtifactRepository

            repo.force_transition(
                job["id"],
                "completed",
                started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
                finished_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            )
            ArtifactRepository(session).create_artifact(
                kind="memory",
                producer="job_capture",
                path="/storage/dumps/x/memory.dump",
                filename="memory.dump",
                job_id=job["id"],
            )
            action = repo.enqueue_action(job["id"], "run_vol3_analysis")
            repo.update_action(action.id, status="completed")
            repo.add_event(
                job["id"],
                "vol3_completed",
                "Volatility 3 analysis completed. Phase 1 complete (no symbols: unsupported distro or download failure)",
            )

        r = client.get(f"/jobs/{job['id']}/live-fragment")

        assert r.status_code == 200
        assert b"ISF status" in r.content
        assert b"Skipped" in r.content
        assert b"unsupported distro or download failure" in r.content
        assert b"Volatility 3 ISF Analysis" not in r.content


def test_get_jobs_new_renders_wizard(client):
    resp = client.get("/jobs/new")
    assert resp.status_code == 200
    body = resp.text
    assert 'id="new-job-modal"' in body
    assert "pg-v2-creator--wizard" in body
    assert 'name="target_id"' in body
    assert 'name="scenario_id"' in body
    # Footer lives outside the scrolling form so it stays pinned; the submit
    # button is tied back to the form via the form attribute.
    assert 'form="wiz-form"' in body
    assert body.index("</form>") < body.index("pg-v2-modal-footer")


def test_jobs_new_has_four_step_stepper(client):
    body = client.get("/jobs/new").text
    assert 'class="wiz-stepper"' in body
    assert body.count('class="wiz-stepper__step"') == 4
    assert 'data-step="scenario"' in body
    assert 'data-step="workload"' in body
    assert 'data-step="configure"' in body
    assert 'data-step="review"' in body
    assert "TARGET" in body.upper()
    assert "launch job" in body.lower()


def test_jobs_new_renders_targets_and_scenarios(client):
    body = client.get("/jobs/new").text
    assert "data-target-card" in body
    assert "data-scenario-card" in body
    assert "wiz-badge--immediate" in body or "wiz-badge--post" in body or "wiz-badge--manual" in body


def test_jobs_new_renders_attack_technique_badges(client):
    body = client.get("/jobs/new").text
    assert "T1190" in body
    assert "T1059" in body
    assert "ATT&amp;CK" in body or "ATT&CK" in body


def test_jobs_new_has_capture_chip_group(client):
    body = client.get("/jobs/new").text
    assert 'role="radiogroup"' in body
    assert 'value="memory"' in body
    assert 'type="radio"' in body
    assert 'name="capture_type"' in body
    assert 'type="hidden" name="capture_type"' not in body


def test_jobs_new_does_not_expose_isf_only_build_control(client):
    body = client.get("/jobs/new").text
    assert 'name="build_vol3_isf"' not in body
    assert "ISF only" not in body


def test_jobs_new_keeps_windows_targets_without_isf_build_controls(client, monkeypatch):
    import astinuslab.ui.jobs_routes as jobs_routes_mod

    autospec_setattr(
        monkeypatch,
        jobs_routes_mod,
        "list_targets_service",
        lambda: [
            {
                "id": "ubuntu-22-04-default",
                "enabled": True,
                "distro": "ubuntu",
                "version": "22.04",
                "family": "debian",
            },
            {
                "id": "windows-server-2022",
                "enabled": True,
                "distro": "windows-server",
                "version": "2022",
                "family": "windows",
            },
        ],
    )

    body = client.get("/jobs/new").text

    assert 'value="windows-server-2022"' in body
    assert 'data-family="windows"' in body
    assert 'name="build_vol3_isf"' not in body
    assert "ISF only" not in body


def test_sidebar_links_to_volatility_workbench(client):
    body = client.get("/jobs").text
    assert 'href="/volatility"' in body
    assert "Volatility" in body


def test_volatility_page_lists_linux_symbol_targets_not_windows(client, monkeypatch):
    import astinuslab.jobs.job_services as svc_mod
    import astinuslab.ui.volatility_routes as volatility_routes

    autospec_setattr(
        monkeypatch,
        svc_mod,
        "load_targets",
        lambda: [
            {
                "id": "ubuntu-22-04-default",
                "enabled": True,
                "distro": "ubuntu",
                "version": "22.04",
                "family": "debian",
                "display_name": "Ubuntu 22.04",
                "image_ref": "ubuntu.qcow2",
            },
            {
                "id": "kali-attacker",
                "enabled": True,
                "distro": "kali",
                "version": "2024.1",
                "family": "debian",
                "display_name": "Kali Attacker",
                "image_ref": "kali.qcow2",
                "cloud_init_profile": "attacker",
            },
            {
                "id": "router-appliance",
                "enabled": True,
                "distro": "openwrt",
                "version": "23",
                "family": "other",
                "display_name": "Router Appliance",
                "image_ref": "router.qcow2",
                "role": "appliance",
            },
            {
                "id": "windows-server-2022",
                "enabled": True,
                "distro": "windows-server",
                "version": "2022",
                "family": "windows",
                "display_name": "Windows Server 2022",
            },
        ],
    )
    autospec_setattr(monkeypatch, volatility_routes, "load_attackers", lambda: [{"image_ref": "kali.qcow2"}])
    autospec_setattr(monkeypatch, volatility_routes, "load_network_nodes", lambda: [{"image_ref": "router.qcow2"}])

    body = client.get("/volatility").text

    assert 'action="/volatility/build"' in body
    assert 'name="target_id"' in body
    assert 'value="ubuntu-22-04-default"' in body
    assert 'value="kali-attacker"' not in body
    assert 'value="router-appliance"' not in body
    assert 'value="windows-server-2022"' not in body
    assert "Windows targets use PDB symbols" in body


def test_volatility_page_uses_workbench_contract(client):
    body = client.get("/volatility").text

    # base.html provides the single page <main>; the workbench nests a <section>.
    assert body.count("<main") == 1
    assert "<h1" in body
    assert 'class="page-head__title">Volatility' in body
    # Workbench shell anchors driven by the front-end JS.
    assert 'id="dump-select"' in body
    assert 'id="catalog"' in body
    assert 'id="results-panel"' in body
    assert 'id="queue-body"' in body
    assert 'id="isf-list"' in body
    assert 'id="yara-input"' in body
    assert 'id="yara-rules"' in body
    assert "/static/css/pages/volatility.css?v=" in body
    assert "/static/volatility/index.js?v=" in body
    # ISF build capability folded into the workbench, history preserved.
    assert "ISF build history" in body

    css = client.get("/static/css/pages/volatility.css").text
    assert ".vol-grid" in css
    assert ".vol-catalog" in css


def test_volatility_build_redirects_when_integration_is_disabled(client, in_memory_session_scope, monkeypatch):
    """Only a disabled integration refuses the build, and it never leaks tool detail.

    An unready dwarf2json is deliberately not a refusal: the local-cache and prebuilt
    providers still return a ready ISF without invoking the tool.  Readiness is
    pinned rather than inferred from this host's configuration.
    """
    import astinuslab.ui.volatility_routes as routes
    from astinuslab.db.repositories import JobRepository
    from astinuslab.forensics.vol3_tools import ToolStatus, Vol3Readiness

    monkeypatch.setattr(
        routes,
        "get_vol3_readiness",
        lambda: Vol3Readiness(
            enabled=False,
            analysis=ToolStatus(False, "feature_disabled", "internal detail /secret/vol"),
            isf_build=ToolStatus(False, "feature_disabled", "internal detail /secret/dwarf2json"),
        ),
    )

    with in_memory_session_scope() as session:
        before_ids = {job.id for job in JobRepository(session).list_jobs()}

    response = client.post(
        "/volatility/build",
        data={"target_id": "ubuntu-22-04-default", "scenario_id": "immediate-capture"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    parsed = urlparse(response.headers["location"])
    params = parse_qs(parsed.query)
    assert parsed.path == "/volatility"
    assert params["message_type"] == ["error"]
    assert params["message"][0].startswith("Volatility integration is disabled.")
    # The unsafe status detail must never reach a user-visible redirect.
    assert "/secret/" not in params["message"][0]
    with in_memory_session_scope() as session:
        after_ids = {job.id for job in JobRepository(session).list_jobs()}
    assert after_ids == before_ids


def test_jobs_new_has_two_vm_columns_and_six_resource_inputs(client):
    body = client.get("/jobs/new").text
    assert "wiz-vm-col--target" in body
    assert "wiz-vm-col--attacker" in body
    for fld in [
        "vm_vcpus",
        "vm_memory_mb",
        "vm_disk_gb",
        "attacker_vm_vcpus",
        "attacker_vm_memory_mb",
        "attacker_vm_disk_gb",
    ]:
        assert 'name="' + fld + '"' in body


def test_jobs_new_vm_resources_use_sliders(client):
    body = client.get("/jobs/new").text
    # v4 design: resources are preset-snapping sliders with an exact-value input.
    assert 'data-field="vm_vcpus"' in body
    assert 'data-field="vm_memory_mb"' in body
    assert 'data-field="vm_disk_gb"' in body
    assert "data-slider-range" in body
    assert 'type="range"' in body


def test_jobs_new_vm_resource_sliders_have_exact_inputs(client):
    body = client.get("/jobs/new").text
    assert "wiz-slider__input" in body
    assert "data-slider-input" in body
    assert "data-presets=" in body


def test_jobs_new_vm_resource_slider_css_present(client):
    css = _served_app_css(client)
    assert ".wiz-slider__range" in css
    assert ".wiz-slider__exact" in css
    assert "--wiz-slider-fill" in css


def test_jobs_new_css_hides_stepper_steps_with_hidden_attribute(client):
    # .wiz-stepper__step sets display:flex, which beats the UA [hidden] rule;
    # without this guard skipped steps stay visible in 3-step flows.
    css = _served_app_css(client)
    assert ".wiz-stepper__step[hidden]" in css
    assert ".attacker-row__clear[hidden]" in css


def test_script_runner_file_input_is_visually_hidden_not_display_none(client, created_job, in_memory_session_scope):
    from astinuslab.db.repositories import JobRepository

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        repo.update_job(created_job["id"], guest_ip="192.168.122.10")

    body = client.get(f"/jobs/{created_job['id']}/script-runner-fragment").text

    assert 'id="scripts-upload"' in body
    assert 'class="sr-only"' in body
    assert 'style="display:none"' not in body


def test_jobs_new_has_workload_mount_and_attacker_column(client):
    body = client.get("/jobs/new").text
    assert 'id="workload-panel-container"' in body
    assert 'id="wiz-vm-col-attacker"' in body


def test_jobs_new_places_manual_panel_before_vm_resources(client):
    body = client.get("/jobs/new").text
    assert body.index('id="workload-panel-container"') < body.index('id="wiz-vm-grid"')


def test_capture_form_has_no_inline_symbol_build_control(client, in_memory_session_scope, monkeypatch):
    import astinuslab.jobs.job_services as svc_mod
    from astinuslab.db.repositories import JobRepository

    autospec_setattr(
        monkeypatch,
        svc_mod,
        "load_targets",
        lambda: [
            {
                "id": "windows-server-2022",
                "enabled": True,
                "distro": "windows-server",
                "version": "2022",
                "family": "windows",
                "image_available": True,
                "source_type": "qcow2",
                "supports_cloud_image": True,
                "supports_iso_autoinstall": False,
                "image_ref": "windows-server-2022-x64.qcow2",
                "os_variant": "win2k22",
                "display_name": "Windows Server 2022",
            }
        ],
    )

    response = client.post(
        "/api/v1/jobs",
        json={
            "target_id": "windows-server-2022",
            "scenario_id": "lab-session",
            "execution_mode": "manual_trigger",
        },
    )
    assert response.status_code == 201
    job = response.json()

    with in_memory_session_scope() as session:
        JobRepository(session).update_job(job["id"], status="waiting_for_capture")

    body = client.get(f"/jobs/{job['id']}/live-fragment").text

    assert "Capture memory" in body
    assert "Build Vol3 symbols" not in body


def test_attacker_selector_action_slot_has_static_layout(client):
    css = _served_app_css(client)
    assert ".attacker-row__action" in css
    assert "position: relative;" in css
    assert "height: 1.35rem;" in css
    assert ".attacker-row__inline-select" in css
    assert "position: absolute;" in css
    assert "inset: 0;" in css


def test_index_links_to_jobs_new(client):
    body = client.get("/jobs").text
    assert "/jobs/new" in body
    assert 'id="new-job-overlay"' not in body


class TestJobDetailVol3Section:
    """Tests for the Volatility 3 ISF card using persisted ISFArtifact metadata."""

    def test_isf_only_completed_job_shows_isf_info(self, client, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        response = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "immediate-capture",
                "execution_mode": "immediate",
                "build_vol3_isf": True,
            },
        )
        assert response.status_code == 201
        job = response.json()

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(
                job["id"],
                status="completed",
                current_stage="completed",
                detected_kernel_release="5.15.0-test",
            )
            repo.add_event(
                job["id"],
                "vol3_completed",
                "Symbol built: /tmp/5.15.0-test.json, sha256: abc123def456789",
            )
            repo.create_isf_artifact(
                job_id=job["id"],
                kernel_release="5.15.0-test",
                path="/tmp/5.15.0-test.json",
                source="host_fetch",
                sha256="abc123def456789",
                validation_status="valid",
            )

        r = client.get(f"/jobs/{job['id']}/live-fragment")
        assert r.status_code == 200
        assert b"host_fetch" in r.content
        assert b"abc123" in r.content

    def test_isf_only_failed_job_shows_failure(self, client, in_memory_session_scope, monkeypatch, tmp_path):
        from astinuslab.db.repositories import JobRepository
        from astinuslab.ui import job_context as ui_mod

        monkeypatch.setattr(
            ui_mod,
            "settings",
            type(
                "Settings",
                (),
                {
                    **ui_mod.settings.model_dump(),
                    "storage_dir": tmp_path / "storage",
                },
            )(),
        )

        response = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "immediate-capture",
                "execution_mode": "immediate",
                "build_vol3_isf": True,
            },
        )
        assert response.status_code == 201
        job = response.json()

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(
                job["id"],
                status="failed",
                current_stage="failed",
            )
            repo.add_event(job["id"], "vol3_failed", "Volatility 3 ISF generation failed: kernel not found")

        log_path = tmp_path / "storage" / "dumps" / job["id"] / "vol3" / "symbol-build-host.log"
        log_path.parent.mkdir(parents=True)
        log_path.write_text("fetch_result phase=fetch_vmlinux exit_code=3\n", encoding="utf-8")

        r = client.get(f"/jobs/{job['id']}/live-fragment")
        assert r.status_code == 200
        # Verify failure content is rendered
        assert b"Failed" in r.content or job["id"][:8].encode() in r.content
        assert b"symbol-build-host.log" in r.content

    def test_job_without_dump_but_with_isf_does_not_show_dump_error(self, client, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        response = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "immediate-capture",
                "execution_mode": "immediate",
                "build_vol3_isf": True,
                "capture_type": "none",
            },
        )
        assert response.status_code == 201
        job = response.json()

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(
                job["id"],
                status="completed",
                current_stage="completed",
                detected_kernel_release="5.15.0-test",
            )
            repo.add_event(job["id"], "completed", "ISF-only job completed")
            repo.create_isf_artifact(
                job_id=job["id"],
                kernel_release="5.15.0-test",
                path="/tmp/5.15.0-test.json",
                source="host_fetch",
                sha256="abc123def456789",
                validation_status="valid",
            )

        r = client.get(f"/jobs/{job['id']}/live-fragment")
        assert r.status_code == 200
        # Verify ISF artifact info is shown despite no memory dump being captured
        assert b"host_fetch" in r.content
        # Verify no dump-error copy is displayed. The job detail may still expose
        # the manual memory-dump upload override for operators.
        body = r.content.lower()
        assert b"no memory dump" not in body
        assert b"dump required" not in body


# ── Image metadata / inspection endpoint tests ─────────────────────────────────

import time  # noqa: E402

from astinuslab.images import image_inspection as M  # noqa: E402
from astinuslab.images.image_inspection import (  # noqa: E402
    SIDECAR_SCHEMA_VERSION,
    ImageMetadata,
    load_sidecar,
    save_sidecar,
)


def _make_meta(**overrides) -> ImageMetadata:
    base: dict[str, Any] = dict(
        schema_version=SIDECAR_SCHEMA_VERSION,
        distro="ubuntu",
        version="24.04",
        family="debian",
        default_username="ubuntu",
        os_variant="ubuntu24.04",
        machine_type=None,
        kernel_release=None,
        arch="x86_64",
        cloud_init_profile=None,
        inject_network_config=None,
        source="inspected",
        detected_at="2026-05-02T00:00:00+00:00",
        user_edited_fields=[],
        inspect_error=None,
    )
    base.update(overrides)
    return ImageMetadata(**base)


def test_get_image_metadata_returns_sidecar(client, tmp_images_dir):
    img = tmp_images_dir / "noble.qcow2"
    img.write_bytes(b"")
    save_sidecar(img, _make_meta())

    r = client.get("/admin/images/noble.qcow2/metadata")
    assert r.status_code == 200
    body = r.json()
    assert body["distro"] == "ubuntu"
    assert body["source"] == "inspected"


def test_put_image_metadata_marks_user_edited(client, tmp_images_dir):
    img = tmp_images_dir / "noble.qcow2"
    img.write_bytes(b"")
    save_sidecar(img, _make_meta())

    r = client.put(
        "/admin/images/noble.qcow2/metadata",
        json={
            "distro": "ubuntu",
            "version": "24.04",
            "family": "debian",
            "default_username": "myuser",
            "os_variant": "ubuntu24.04",
            "machine_type": None,
            "cloud_init_profile": None,
            "inject_network_config": None,
        },
    )
    assert r.status_code == 200
    after = load_sidecar(img)
    assert after is not None
    assert after.default_username == "myuser"
    assert after.source == "user"
    assert "default_username" in after.user_edited_fields


def test_get_image_metadata_404_on_missing(client, tmp_images_dir):
    r = client.get("/admin/images/does-not-exist.qcow2/metadata")
    assert r.status_code == 404


def test_post_image_inspect_starts_job(client, tmp_images_dir, monkeypatch):
    img = tmp_images_dir / "noble.qcow2"
    img.write_bytes(b"")
    autospec_setattr(monkeypatch, M, "inspect_image", lambda *a, **kw: _make_meta())

    r = client.post("/admin/images/noble.qcow2/inspect")
    assert r.status_code == 202

    s = None
    for _ in range(50):
        s = client.get("/admin/images/noble.qcow2/inspection")
        if s.json().get("status") == "done":
            break
        time.sleep(0.05)
    assert s is not None and s.status_code == 200
    assert s.json()["status"] == "done"
