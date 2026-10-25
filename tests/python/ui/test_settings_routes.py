from __future__ import annotations

import importlib

import pytest

from tests.python._support.helpers import autospec_setattr


@pytest.fixture(autouse=True)
def _app_settings_db(in_memory_session_scope, monkeypatch):
    from astinuslab import app_settings as app_settings_mod

    monkeypatch.setattr(app_settings_mod, "session_scope", in_memory_session_scope)


def _settings_ui():
    return importlib.import_module("astinuslab.ui.settings")


def test_settings_page_renders(client, monkeypatch):
    settings_ui = _settings_ui()

    autospec_setattr(
        monkeypatch,
        settings_ui,
        "_collect_services",
        lambda: [
            {
                "label": "astinuslab-api",
                "unit": "astinuslab-api.service",
                "description": "x",
                "state": "running",
                "pid": 1,
                "uptime_s": 60,
            }
        ],
    )
    autospec_setattr(
        monkeypatch,
        settings_ui,
        "_collect_network",
        lambda: {
            "state": "active",
            "autostart": "yes",
            "forward": "nat",
            "bridge": "virbr-al0",
            "subnet": "10.42.0.0/24",
            "gateway": "10.42.0.1",
        },
    )
    autospec_setattr(monkeypatch, settings_ui, "_collect_leases", lambda: [])
    autospec_setattr(
        monkeypatch,
        settings_ui,
        "_collect_storage",
        lambda: {
            "stats": {
                "dumps_count": 0,
                "dumps_mb": 0,
                "logs_count": 0,
                "logs_mb": 0,
                "workload_cache_count": 0,
                "workload_cache_mb": 0,
                "free_gb": 100.0,
            },
            "retention": {"dumps_days": 90, "logs_days": 180, "auto_enabled": False},
        },
    )

    resp = client.get("/settings")
    assert resp.status_code == 200
    body = resp.text
    assert 'id="settings-services"' in body
    assert 'id="settings-network"' in body
    assert 'id="settings-storage"' in body
    assert "astinuslab-api" in body
    assert "evidence storage / cleanup" in body
    assert "danger zone" in body


def test_settings_page_has_unique_fragment_ids(client, monkeypatch):
    settings_ui = _settings_ui()

    autospec_setattr(
        monkeypatch,
        settings_ui,
        "_collect_services",
        lambda: [
            {
                "label": "astinuslab-api",
                "unit": "astinuslab-api.service",
                "description": "x",
                "state": "running",
                "pid": 1,
                "uptime_s": 60,
            }
        ],
    )
    autospec_setattr(
        monkeypatch,
        settings_ui,
        "_collect_network",
        lambda: {
            "state": "active",
            "autostart": "yes",
            "forward": "nat",
            "bridge": "virbr-al0",
            "subnet": "10.42.0.0/24",
            "gateway": "10.42.0.1",
        },
    )
    autospec_setattr(monkeypatch, settings_ui, "_collect_leases", lambda: [])
    autospec_setattr(
        monkeypatch,
        settings_ui,
        "_collect_storage",
        lambda: {
            "stats": {
                "dumps_count": 0,
                "dumps_mb": 0,
                "logs_count": 0,
                "logs_mb": 0,
                "workload_cache_count": 0,
                "workload_cache_mb": 0,
                "free_gb": 100.0,
            },
            "retention": {"dumps_days": 90, "logs_days": 180, "auto_enabled": False},
        },
    )

    resp = client.get("/settings")
    assert resp.status_code == 200
    body = resp.text
    assert body.count('id="settings-services"') == 1
    assert body.count('id="settings-network"') == 1
    assert body.count('id="settings-storage"') == 1


def test_settings_page_uses_admin_layout_hooks(client, monkeypatch):
    settings_ui = _settings_ui()

    autospec_setattr(
        monkeypatch,
        settings_ui,
        "_collect_services",
        lambda: [
            {
                "label": "astinuslab-api",
                "unit": "astinuslab-api.service",
                "description": "x",
                "state": "running",
                "pid": 1,
                "uptime_s": 60,
            }
        ],
    )
    autospec_setattr(
        monkeypatch,
        settings_ui,
        "_collect_network",
        lambda: {
            "state": "active",
            "autostart": "yes",
            "forward": "nat",
            "bridge": "virbr-al0",
            "subnet": "10.42.0.0/24",
            "gateway": "10.42.0.1",
        },
    )
    autospec_setattr(monkeypatch, settings_ui, "_collect_leases", lambda: [])
    autospec_setattr(
        monkeypatch,
        settings_ui,
        "_collect_storage",
        lambda: {
            "stats": {
                "dumps_count": 0,
                "dumps_mb": 0,
                "logs_count": 0,
                "logs_mb": 0,
                "workload_cache_count": 0,
                "workload_cache_mb": 0,
                "free_gb": 100.0,
            },
            "retention": {"dumps_days": 90, "logs_days": 180, "auto_enabled": False},
        },
    )

    resp = client.get("/settings")
    assert resp.status_code == 200
    body = resp.text
    assert 'class="wiz-shell settings-host-ops"' in body
    assert '<h1 class="page-head__title">settings / host ops</h1>' in body
    assert 'class="settings-card settings-card--runtime"' in body
    assert 'class="settings-storage-row"' not in body
    assert 'class="settings-retention-form"' in body
    assert 'class="settings-card settings-card--danger settings-danger-zone"' in body
    assert 'class="settings-network-grid"' in body


def test_fragment_services_returns_only_section(client, monkeypatch):
    settings_ui = _settings_ui()

    autospec_setattr(
        monkeypatch,
        settings_ui,
        "_collect_services",
        lambda: [
            {
                "id": "api",
                "label": "astinuslab-api",
                "unit": "astinuslab-api.service",
                "description": "",
                "state": "running",
                "pid": 2,
                "uptime_s": 30,
            }
        ],
    )
    resp = client.get("/settings/fragment/services")
    assert resp.status_code == 200
    assert 'id="settings-services"' in resp.text
    assert 'id="settings-network"' not in resp.text


def test_fragment_network_handles_unavailable(client, monkeypatch):
    settings_ui = _settings_ui()

    autospec_setattr(monkeypatch, settings_ui, "_collect_network", lambda: {"state": "unavailable", "error": "boom"})
    autospec_setattr(monkeypatch, settings_ui, "_collect_leases", lambda: [])
    resp = client.get("/settings/fragment/network")
    assert resp.status_code == 200
    assert "libvirt unavailable" in resp.text


def test_fragment_storage_returns_only_section(client, monkeypatch):
    settings_ui = _settings_ui()

    autospec_setattr(
        monkeypatch,
        settings_ui,
        "_collect_storage",
        lambda: {
            "stats": {
                "dumps_count": 1,
                "dumps_mb": 10,
                "logs_count": 0,
                "logs_mb": 0,
                "workload_cache_count": 0,
                "workload_cache_mb": 0,
                "free_gb": 50.0,
            },
            "retention": {"dumps_days": 10, "logs_days": 20, "auto_enabled": False},
        },
    )
    resp = client.get("/settings/fragment/storage")
    assert resp.status_code == 200
    assert 'id="settings-storage"' in resp.text
    assert "evidence storage / cleanup" in resp.text
    assert "danger zone" in resp.text


def test_retention_rejects_non_integer(client):
    # Invalid input is surfaced as an in-page error toast (htmx swaps on 2xx),
    # not a 422 the operator never sees.
    resp = client.post("/settings/retention", data={"dumps_days": "abc", "logs_days": "5"})
    assert resp.status_code == 200
    assert "must be an integer" in resp.text
    assert 'class="settings-toast settings-toast--err"' in resp.text


def test_retention_persists_valid_values(client):
    from astinuslab import app_settings as app_settings_mod

    resp = client.post("/settings/retention", data={"dumps_days": "12", "logs_days": "34"})
    assert resp.status_code == 200
    saved = app_settings_mod.load_app_settings()["retention"]
    assert saved == {"dumps_days": 12, "logs_days": 34, "auto_enabled": False}


def test_retention_persists_auto_enabled_true(client):
    from astinuslab import app_settings as app_settings_mod

    resp = client.post(
        "/settings/retention",
        data={"dumps_days": "12", "logs_days": "34", "auto_enabled": "1"},
    )

    assert resp.status_code == 200
    assert app_settings_mod.load_app_settings()["retention"]["auto_enabled"] is True


def test_retention_persists_auto_enabled_false_when_unchecked(client):
    from astinuslab import app_settings as app_settings_mod

    app_settings_mod.save_app_settings({"retention": {"auto_enabled": True}})

    resp = client.post("/settings/retention", data={"dumps_days": "12", "logs_days": "34"})

    assert resp.status_code == 200
    assert app_settings_mod.load_app_settings()["retention"]["auto_enabled"] is False


def test_retention_run_persists_and_runs(client, monkeypatch):
    from astinuslab import app_settings as app_settings_mod

    settings_ui = _settings_ui()

    called: list[bool] = []

    def fake_run():
        called.append(True)
        return {"dumps_deleted": 2, "logs_deleted": 1}

    monkeypatch.setattr(settings_ui, "run_retention", fake_run)
    resp = client.post("/settings/retention/run", data={"dumps_days": "7", "logs_days": "14"})
    assert resp.status_code == 200
    assert called == [True]
    assert "dumps_deleted: 2" in resp.text
    assert app_settings_mod.load_app_settings()["retention"] == {
        "dumps_days": 7,
        "logs_days": 14,
        "auto_enabled": False,
    }


def test_retention_preview_is_non_destructive(client, monkeypatch):
    settings_ui = _settings_ui()

    called: list[bool] = []

    def fake_run(*, dry_run=False, retention_days=None):
        called.append(dry_run)
        assert retention_days == (7, 14)
        return {"dumps_deleted": 5, "logs_deleted": 3}

    monkeypatch.setattr(settings_ui, "run_retention", fake_run)
    resp = client.post("/settings/retention/preview", data={"dumps_days": "7", "logs_days": "14"})
    assert resp.status_code == 200
    assert called == [True]  # invoked as a dry run
    assert "would delete 5 dumps" in resp.text
    assert 'class="settings-toast settings-toast--info"' in resp.text


def test_symbols_clear_invokes_helper(client, monkeypatch):
    settings_ui = _settings_ui()

    called: list[bool] = []

    def _clear(_settings):
        called.append(True)
        return {"action": "clear_symbols", "ok": True, "message": "Symbol cache cleared: 3 entries.", "removed": 3}

    monkeypatch.setattr(settings_ui, "clear_vol3_symbol_cache", _clear)
    resp = client.post("/settings/symbols/clear")
    assert resp.status_code == 200
    assert called == [True]
    assert "Symbol cache cleared: 3 entries." in resp.text


def test_guardrail_save_persists(client):
    from astinuslab import app_settings as app_settings_mod

    resp = client.post("/settings/guardrail", data={"min_free_gib": "25"})
    assert resp.status_code == 200
    assert app_settings_mod.load_app_settings()["disk_guardrail"]["min_free_gib"] == 25
    assert "25 GiB" in resp.text


def test_guardrail_rejects_non_integer(client):
    resp = client.post("/settings/guardrail", data={"min_free_gib": "lots"})
    assert resp.status_code == 200
    assert "must be an integer" in resp.text
    assert "settings-toast--err" in resp.text


def test_service_stop_all_calls_schedule(client, monkeypatch):
    settings_ui = _settings_ui()

    called: list[bool] = []

    def _stop_services(*_):
        called.append(True)
        return {"action": "stop_services", "ok": True, "message": "ok"}

    monkeypatch.setattr(settings_ui, "schedule_stop_services", _stop_services)
    autospec_setattr(
        monkeypatch,
        settings_ui,
        "_collect_services",
        lambda: [
            {"id": "api", "label": "x", "unit": "x", "description": "", "state": "running", "pid": 1, "uptime_s": 1}
        ],
    )
    resp = client.post("/settings/services/stop-all")
    assert resp.status_code == 200
    assert called == [True]


def test_service_stop_single_targets_pid_file(client, tmp_path, monkeypatch):
    import types

    settings_ui = _settings_ui()

    pid_file = tmp_path / "api.pid"
    pid_file.write_text("4242")
    monkeypatch.setattr(settings_ui, "settings", types.SimpleNamespace(storage_dir=tmp_path))

    killed: list = []

    def _stop_pid(p):
        killed.append(p)
        return True

    monkeypatch.setattr(settings_ui, "_stop_pid_file", _stop_pid)
    autospec_setattr(
        monkeypatch,
        settings_ui,
        "_collect_services",
        lambda: [
            {
                "id": "api",
                "label": "x",
                "unit": "x",
                "description": "",
                "state": "stopped",
                "pid": None,
                "uptime_s": None,
            }
        ],
    )
    resp = client.post("/settings/services/api/stop")
    assert resp.status_code == 200
    assert killed == [pid_file]


def test_service_restart_surfaces_guidance(client, monkeypatch):
    # Restart is owned by systemd; the route returns 200 with guidance so htmx
    # actually shows it (a 501 would silently no-op the swap).
    settings_ui = _settings_ui()
    autospec_setattr(
        monkeypatch,
        settings_ui,
        "_collect_services",
        lambda: [
            {"id": "api", "label": "x", "unit": "x", "description": "", "state": "running", "pid": 1, "uptime_s": 1}
        ],
    )
    resp = client.post("/settings/services/api/restart")
    assert resp.status_code == 200
    assert "managed by systemd" in resp.text
    assert 'class="settings-toast settings-toast--info"' in resp.text


def test_service_unknown_id_returns_404(client):
    resp = client.post("/settings/services/ghost/stop")
    assert resp.status_code == 404


def test_network_restart_calls_helper(client, monkeypatch):
    settings_ui = _settings_ui()

    autospec_setattr(
        monkeypatch,
        settings_ui,
        "reset_libvirt_network",
        lambda _: {"action": "reset_network", "ok": True, "message": "restarted"},
    )
    autospec_setattr(
        monkeypatch,
        settings_ui,
        "_collect_network",
        lambda: {
            "state": "active",
            "autostart": "yes",
            "forward": "nat",
            "bridge": "virbr-al0",
            "subnet": "10.0.0.0/24",
            "gateway": "10.0.0.1",
        },
    )
    autospec_setattr(monkeypatch, settings_ui, "_collect_leases", lambda: [])
    resp = client.post("/settings/network/restart")
    assert resp.status_code == 200
    assert "restarted" in resp.text


class TestActivityPanel:
    """The Settings page is where an operator looks when something is wrong."""

    def test_fragment_returns_only_the_activity_section(self, client):
        resp = client.get("/settings/fragment/activity")

        assert resp.status_code == 200
        assert 'id="settings-activity"' in resp.text
        assert 'id="settings-storage"' not in resp.text

    def test_the_panel_is_on_the_page(self, client):
        resp = client.get("/settings")

        assert resp.status_code == 200
        assert 'id="settings-activity"' in resp.text
        assert "recent activity" in resp.text

    def test_an_empty_window_says_so_rather_than_rendering_a_blank_table(self, client):
        resp = client.get("/settings/fragment/activity")

        assert "No actions ran in the last 24 hours." in resp.text

    def test_action_rows_render_with_their_durations(self, client, monkeypatch):
        settings_ui = _settings_ui()

        autospec_setattr(
            monkeypatch,
            settings_ui,
            "_activity_ctx",
            lambda request: {
                "request": request,
                "metrics": {
                    "actions": [
                        {
                            "action_type": "acquire_memory_dump",
                            "completed": 4,
                            "failed": 1,
                            "p50_seconds": 42.0,
                            "p95_seconds": 91.0,
                            "max_seconds": 91.0,
                        }
                    ],
                    "jobs": {"completed": 4},
                    "queue": {"queued": 2, "running": 1, "oldest_queued_age_seconds": 180.0},
                },
                **settings_ui._base_ctx(request),
            },
        )

        resp = client.get("/settings/fragment/activity")

        assert "acquire_memory_dump" in resp.text
        assert "42s" in resp.text
        assert "91s" in resp.text
        assert "3m" in resp.text

    def test_a_metrics_failure_does_not_take_the_settings_page_down(self, client, monkeypatch):
        from astinuslab.core import operational_metrics

        def _boom(*args, **kwargs):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(operational_metrics, "collect_metrics", _boom)

        resp = client.get("/settings")

        assert resp.status_code == 200
        assert 'id="settings-storage"' in resp.text
