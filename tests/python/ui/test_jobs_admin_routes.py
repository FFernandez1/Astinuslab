"""Admin routes served by the Jobs UI module: workload cache, workload editor,
SSH keys and guest helper scripts."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
import yaml
from sqlalchemy import insert

from astinuslab.db.models import Job

_TEST_CSRF_TOKEN = "test-csrf-token"


@pytest.fixture()
def ui_client(client, monkeypatch):
    """The shared client with the CSRF pair every UI mutation requires."""
    client.cookies.set("astinuslab_csrf", _TEST_CSRF_TOKEN)
    original_request = client.request

    def request_with_csrf(method, url, *args, **kwargs):
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            headers = dict(kwargs.pop("headers", {}) or {})
            headers.setdefault("X-CSRF-Token", _TEST_CSRF_TOKEN)
            kwargs["headers"] = headers
        return original_request(method, url, *args, **kwargs)

    monkeypatch.setattr(client, "request", request_with_csrf)
    return client


def _banner(response) -> tuple[str, str, str]:
    location = urlparse(response.headers["location"])
    query = parse_qs(location.query)
    return location.path, query["message"][0], query["message_type"][0]


@pytest.fixture()
def workload_cache(tmp_path, monkeypatch):
    import astinuslab.ui.jobs_routes as routes

    cache_dir = (tmp_path / "workload-cache").resolve()
    cache_dir.mkdir()
    monkeypatch.setattr(routes, "_WORKLOAD_CACHE_DIR", cache_dir)
    monkeypatch.setattr(routes, "_WORKLOAD_CACHE_INDEX", cache_dir / "index.json")
    return cache_dir


def _write_cache(cache_dir: Path, entries: dict) -> None:
    (cache_dir / "index.json").write_text(json.dumps(entries))


class TestWorkloadCache:
    def test_missing_or_corrupt_index_lists_nothing(self, workload_cache):
        import astinuslab.ui.jobs_routes as routes

        assert routes._load_workload_cache_entries() == []
        assert routes._remove_workload_cache_entry("anything") is False

        (workload_cache / "index.json").write_text("{not json")
        assert routes._load_workload_cache_entries() == []
        assert routes._remove_workload_cache_entry("anything") is False

    def test_entries_use_real_file_size_and_skip_paths_outside_the_cache(self, workload_cache):
        import astinuslab.ui.jobs_routes as routes

        (workload_cache / "dvwa.qcow2").write_bytes(b"x" * 2048)
        _write_cache(
            workload_cache,
            {
                "dvwa": {
                    "cache_file": "dvwa.qcow2",
                    "target_id": "ubuntu-22-04-default",
                    "workload_id": "dvwa",
                    "base_image": "ubuntu-22.04.qcow2",
                    "size_bytes": 1,
                    "created_at": "2026-10-01T12:30:00+00:00",
                },
                "gone": {"cache_file": "gone.qcow2", "size_bytes": 512, "created_at": "yesterday"},
                "escape": {"cache_file": "../outside.qcow2", "size_bytes": 1},
            },
        )

        entries = {entry["key"]: entry for entry in routes._load_workload_cache_entries()}

        assert set(entries) == {"dvwa", "gone"}
        assert entries["dvwa"]["size_bytes"] == 2048
        assert entries["dvwa"]["size_human"] == "2.0 KB"
        assert entries["dvwa"]["created_at"] == "2026-10-01 12:30"
        # A recorded size stands in for a file that is no longer there; an
        # unparseable timestamp is shown as recorded rather than dropped.
        assert entries["gone"]["size_bytes"] == 512
        assert entries["gone"]["created_at"] == "yesterday"

    def test_page_lists_entries_and_total(self, ui_client, workload_cache):
        (workload_cache / "dvwa.qcow2").write_bytes(b"x" * 1024)
        _write_cache(workload_cache, {"dvwa": {"cache_file": "dvwa.qcow2", "workload_id": "dvwa"}})

        response = ui_client.get("/workload-cache?message=Done&message_type=success")

        assert response.status_code == 200
        assert "dvwa" in response.text
        assert "1.0 KB" in response.text

    def test_delete_removes_file_and_index_entry(self, ui_client, workload_cache):
        (workload_cache / "dvwa.qcow2").write_bytes(b"x")
        _write_cache(workload_cache, {"dvwa": {"cache_file": "dvwa.qcow2"}})

        response = ui_client.post("/workload-cache/delete", data={"key": "dvwa"}, follow_redirects=False)

        assert response.status_code == 303
        assert _banner(response) == ("/workload-cache", "Cache entry deleted.", "success")
        assert not (workload_cache / "dvwa.qcow2").exists()
        assert json.loads((workload_cache / "index.json").read_text()) == {}

    def test_delete_of_unknown_key_reports_an_error(self, ui_client, workload_cache):
        _write_cache(workload_cache, {})

        response = ui_client.post("/workload-cache/delete", data={"key": "missing"}, follow_redirects=False)

        path, message, message_type = _banner(response)
        assert path == "/workload-cache"
        assert message_type == "error"
        assert "not deleted" in message

    def test_delete_never_unlinks_outside_the_cache(self, ui_client, workload_cache, tmp_path):
        outside = tmp_path / "outside.qcow2"
        outside.write_bytes(b"keep")
        _write_cache(workload_cache, {"escape": {"cache_file": "../outside.qcow2"}})

        ui_client.post("/workload-cache/delete", data={"key": "escape"}, follow_redirects=False)

        assert outside.read_bytes() == b"keep"
        assert json.loads((workload_cache / "index.json").read_text()) == {}

    def test_bulk_and_delete_all_count_what_was_removed(self, ui_client, workload_cache):
        for key in ("a", "b", "c"):
            (workload_cache / f"{key}.qcow2").write_bytes(b"x")
        _write_cache(workload_cache, {key: {"cache_file": f"{key}.qcow2"} for key in ("a", "b", "c")})

        bulk = ui_client.post("/workload-cache/bulk-delete", data={"keys": ["a", "missing"]}, follow_redirects=False)
        assert _banner(bulk)[1] == "Deleted 1 cache entry."

        remaining = ui_client.post("/workload-cache/delete-all", follow_redirects=False)
        assert _banner(remaining)[1] == "Deleted 2 cache entries."
        assert json.loads((workload_cache / "index.json").read_text()) == {}


@pytest.fixture()
def workload_catalog(tmp_path, monkeypatch):
    """Point the workload editor at a temporary example catalog."""
    import astinuslab.ui as ui_mod
    import astinuslab.ui.jobs_routes as routes
    from astinuslab.core.config import settings

    example = tmp_path / "config" / "workloads.example.yaml"
    example.parent.mkdir()
    example.write_text(
        yaml.safe_dump({"version": 1, "workloads": [{"id": "dvwa", "display_name": "DVWA"}]}),
        encoding="utf-8",
    )
    data = settings.model_dump()
    data.update(workloads_catalog_path=example, base_dir=tmp_path)
    monkeypatch.setattr(ui_mod, "settings", SimpleNamespace(**data))
    invalidations: list[None] = []
    monkeypatch.setattr(routes, "invalidate_catalog_caches", lambda: invalidations.append(None))
    return SimpleNamespace(example=example, live=example.with_name("workloads.yaml"), invalidations=invalidations)


def _live_ids(catalog) -> list[str]:
    raw = yaml.safe_load(catalog.live.read_text(encoding="utf-8"))
    return [item["id"] for item in raw["workloads"]]


class TestWorkloadEditor:
    def test_example_catalog_is_never_the_write_target(self, workload_catalog):
        import astinuslab.ui.jobs_routes as routes

        assert routes._workloads_catalog_write_path() == workload_catalog.live

    def test_editor_yaml_prefers_the_catalog_entry(self, workload_catalog):
        import astinuslab.ui.jobs_routes as routes

        assert yaml.safe_load(routes._editor_yaml_for_workload("dvwa")) == {"id": "dvwa", "display_name": "DVWA"}

        workload_catalog.live.write_text(
            yaml.safe_dump({"workloads": [{"id": "dvwa", "display_name": "Edited"}]}), encoding="utf-8"
        )
        assert yaml.safe_load(routes._editor_yaml_for_workload("dvwa"))["display_name"] == "Edited"

    def test_editor_yaml_rebuilds_a_bundled_stack_and_defaults_unknown_ids(self, workload_catalog, monkeypatch):
        import astinuslab.ui.jobs_routes as routes

        bundled = {
            "id": "lamp",
            "display_name": "LAMP",
            "tags": ["web"],
            "services": ["http"],
            "is_stack": True,
            "components": ["apache", "mysql"],
            "install_script": "lamp/install.sh",
        }
        monkeypatch.setattr(routes, "workload_by_id", lambda workload_id: bundled if workload_id == "lamp" else None)

        rebuilt = yaml.safe_load(routes._editor_yaml_for_workload("lamp"))
        assert rebuilt["type"] == "stack"
        assert rebuilt["components"] == ["apache", "mysql"]
        assert rebuilt["install_script"] == "lamp/install.sh"
        assert rebuilt["enabled"] is True
        assert routes._editor_yaml_for_workload("unknown") == routes._default_workload_yaml()

    def test_page_shows_the_relative_write_path(self, ui_client, workload_catalog):
        response = ui_client.get("/workloads?edit=dvwa")

        assert response.status_code == 200
        assert "config/workloads.yaml" in response.text

    @pytest.mark.parametrize(
        ("editor_yaml", "detail"),
        [
            ("id: [unclosed", "Invalid YAML"),
            ("- just\n- a list\n", "must be a YAML object"),
            ("display_name: no id\n", "non-empty id"),
        ],
    )
    def test_save_rejects_invalid_templates(self, ui_client, workload_catalog, editor_yaml, detail):
        response = ui_client.post("/workloads/save", data={"editor_yaml": editor_yaml})

        assert response.status_code == 400
        assert detail in response.text
        assert not workload_catalog.live.exists()
        assert workload_catalog.invalidations == []

    def test_save_replaces_a_renamed_entry_and_appends_a_new_one(self, ui_client, workload_catalog):
        renamed = ui_client.post(
            "/workloads/save",
            data={"editor_yaml": "id: dvwa-2\ndisplay_name: DVWA 2\n", "original_id": "dvwa"},
            follow_redirects=False,
        )
        assert renamed.status_code == 303
        assert _banner(renamed) == ("/workloads", "Saved workload template: dvwa-2", "success")
        assert _live_ids(workload_catalog) == ["dvwa-2"]

        ui_client.post("/workloads/save", data={"editor_yaml": "id: juice-shop\n"}, follow_redirects=False)
        assert _live_ids(workload_catalog) == ["dvwa-2", "juice-shop"]
        assert workload_catalog.example.read_text(encoding="utf-8").count("dvwa") == 1
        assert len(workload_catalog.invalidations) == 2

    def test_delete_requires_an_id_and_removes_only_that_entry(self, ui_client, workload_catalog):
        assert ui_client.post("/workloads/delete", data={"workload_id": "  "}).status_code == 400

        ui_client.post("/workloads/save", data={"editor_yaml": "id: juice-shop\n"}, follow_redirects=False)
        response = ui_client.post("/workloads/delete", data={"workload_id": "dvwa"}, follow_redirects=False)

        assert _banner(response) == ("/workloads", "Deleted workload template: dvwa", "success")
        assert _live_ids(workload_catalog) == ["juice-shop"]

    def test_delete_starts_from_the_example_catalog_when_no_live_copy_exists(self, ui_client, workload_catalog):
        ui_client.post("/workloads/delete", data={"workload_id": "dvwa"}, follow_redirects=False)

        assert _live_ids(workload_catalog) == []
        assert "dvwa" in workload_catalog.example.read_text(encoding="utf-8")


class TestSshKeyMutations:
    @pytest.mark.parametrize(
        ("path", "field", "seam"),
        [
            ("/ssh-keys/generate", "name", "generate_key"),
            ("/ssh-keys/set-active", "name", "set_active"),
            ("/ssh-keys/delete", "name", "delete_key"),
        ],
    )
    def test_key_operations_redirect_or_report_the_refusal(self, ui_client, monkeypatch, path, field, seam):
        import astinuslab.ui.jobs_routes as routes

        calls: list[str] = []
        monkeypatch.setattr(routes, seam, calls.append)
        ok = ui_client.post(path, data={field: " lab-key "}, follow_redirects=False)
        assert ok.status_code == 303
        assert ok.headers["location"] == "/ssh-keys"
        assert calls and calls[0].strip() == "lab-key"

        def refuse(name):
            raise ValueError(f"refused {name.strip()}")

        monkeypatch.setattr(routes, seam, refuse)
        refused = ui_client.post(path, data={field: "lab-key"})
        assert refused.status_code == 400
        assert "refused lab-key" in refused.text

    def test_generate_reports_a_keygen_failure(self, ui_client, monkeypatch):
        import astinuslab.ui.jobs_routes as routes

        def fail(name):
            raise RuntimeError("ssh-keygen missing")

        monkeypatch.setattr(routes, "generate_key", fail)

        response = ui_client.post("/ssh-keys/generate", data={"name": "lab-key"})

        assert response.status_code == 400
        assert "ssh-keygen missing" in response.text

    def test_bulk_delete_counts_deleted_and_skipped_keys(self, ui_client, monkeypatch):
        import astinuslab.ui.jobs_routes as routes

        def delete(name):
            if name == "active":
                raise ValueError("cannot delete the active key")

        monkeypatch.setattr(routes, "delete_key", delete)

        response = ui_client.post(
            "/ssh-keys/bulk-delete", data={"names": ["old", "active", "spare"]}, follow_redirects=False
        )

        path, message, message_type = _banner(response)
        assert (path, message_type) == ("/ssh-keys", "success")
        assert "2" in message and "1" in message


def _seed_job(scope, job_id: str, *, guest_ip: str | None) -> None:
    with scope() as session:
        session.execute(
            insert(Job),
            [
                {
                    "id": job_id,
                    "target_id": "ubuntu-22-04-default",
                    "scenario_id": "synthetic-scenario",
                    "execution_mode": "interactive",
                    "status": "waiting_for_capture",
                    "guest_ip": guest_ip,
                }
            ],
        )


class TestRunScripts:
    def _scripts(self, *names_and_bodies: tuple[str, bytes]):
        return [("scripts", (name, body, "text/x-shellscript")) for name, body in names_and_bodies]

    def test_unknown_job_is_not_found(self, ui_client):
        response = ui_client.post("/jobs/missing/run-scripts", files=self._scripts(("a.sh", b"true")))

        assert response.status_code == 404

    def test_guest_without_an_address_is_a_conflict(self, ui_client, in_memory_session_scope):
        _seed_job(in_memory_session_scope, "booting", guest_ip=None)

        response = ui_client.post("/jobs/booting/run-scripts", files=self._scripts(("a.sh", b"true")))

        assert response.status_code == 409

    def test_oversized_script_is_refused_before_anything_runs(self, ui_client, in_memory_session_scope, monkeypatch):
        import astinuslab.ui.jobs_routes as routes

        _seed_job(in_memory_session_scope, "ready", guest_ip="192.0.2.10")
        monkeypatch.setattr(routes, "_find_target", lambda target_id: None)
        ran: list[str] = []
        monkeypatch.setattr(routes, "_exec_script_on_guest", lambda *args: ran.append(args[3]))

        response = ui_client.post("/jobs/ready/run-scripts", files=self._scripts(("big.sh", b"x" * (1024 * 1024 + 1))))

        assert response.status_code == 413
        assert ran == []

    def test_each_script_runs_as_the_target_user_and_results_render(
        self, ui_client, in_memory_session_scope, monkeypatch
    ):
        import astinuslab.ui.jobs_routes as routes

        _seed_job(in_memory_session_scope, "ready", guest_ip="192.0.2.10")
        monkeypatch.setattr(routes, "_find_target", lambda target_id: {"default_username": "analyst"})
        calls: list[tuple] = []

        def fake_exec(guest_ip, guest_user, content, filename, timeout):
            calls.append((guest_ip, guest_user, content, filename, timeout))
            return {
                "filename": filename,
                "success": filename == "ok.sh",
                "exit_code": 0 if filename == "ok.sh" else 2,
                "duration_ms": 5,
                "output": "hello" if filename == "ok.sh" else "",
                "error": None if filename == "ok.sh" else "script failed",
            }

        monkeypatch.setattr(routes, "_exec_script_on_guest", fake_exec)

        response = ui_client.post(
            "/jobs/ready/run-scripts",
            files=self._scripts(("ok.sh", b"echo hello"), ("bad.sh", b"exit 2")),
            data={"timeout": "30"},
        )

        assert response.status_code == 200
        assert calls == [
            ("192.0.2.10", "analyst", b"echo hello", "ok.sh", 30),
            ("192.0.2.10", "analyst", b"exit 2", "bad.sh", 30),
        ]
        assert "hello" in response.text
        assert "script failed" in response.text
        assert "exit 2" in response.text
