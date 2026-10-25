"""Regression checks for destructive form scope and recoverable Job input."""

from __future__ import annotations

import importlib
import json
import re
from types import SimpleNamespace

import pytest


def test_preview_uses_candidate_policy_without_saving(client, monkeypatch):
    from astinuslab import app_settings

    ui = importlib.import_module("astinuslab.ui.settings")
    app_settings.save_app_settings({"retention": {"dumps_days": 90, "logs_days": 180, "auto_enabled": False}})
    before = app_settings.load_app_settings()
    calls = []

    def preview(**kw):
        calls.append(kw)
        return {"dumps_deleted": 2, "logs_deleted": 3}

    monkeypatch.setattr(ui, "run_retention", preview)
    response = client.post(
        "/settings/retention/preview", data={"dumps_days": "1", "logs_days": "2", "auto_enabled": "on"}
    )
    assert response.status_code == 200
    assert app_settings.load_app_settings() == before
    assert calls == [{"dry_run": True, "retention_days": (1, 2)}]
    assert "would delete 2 dumps" in response.text
    assert 'name="dumps_days" value="1"' in response.text
    assert 'name="logs_days" value="2"' in response.text


@pytest.mark.parametrize(
    "field", ["vm_vcpus", "vm_disk_gb", "attacker_vm_vcpus", "attacker_vm_disk_gb", "vm_memory_mb"]
)
@pytest.mark.parametrize("value", ["1.5", "-1", "0", "abc"])
def test_invalid_resources_return_recoverable_form(client, monkeypatch, field, value):
    from astinuslab.ui import job_mutations

    calls = []

    def create(*args):
        calls.append(args)
        return SimpleNamespace(id="unused")

    monkeypatch.setattr(job_mutations, "create_job_service", create)
    response = client.post(
        "/jobs/new",
        data={
            "target_id": "ubuntu-22-04-default",
            "scenario_id": "lab-session",
            "custom_script": "retained script",
            "vm_memory_mb": "4096",
            field: value,
        },
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("text/html")
    assert 'id="wiz-form"' in response.text
    assert f'"{field}":' in response.text
    assert '"custom_script": "retained script"' in response.text
    assert '"target_id": "ubuntu-22-04-default"' in response.text
    assert calls == []


@pytest.mark.parametrize("value, expected", [("", None), ("1", 1), ("128", 128)])
def test_integer_resource_boundary_preserves_payload(client, monkeypatch, value, expected):
    from astinuslab.ui import job_mutations

    payloads = []

    def create(_, payload):
        payloads.append(payload)
        return SimpleNamespace(id="accepted")

    monkeypatch.setattr(job_mutations, "create_job_service", create)
    response = client.post(
        "/jobs/new",
        data={
            "target_id": "ubuntu-22-04-default",
            "scenario_id": "lab-session",
            "vm_vcpus": value,
            "vm_disk_gb": value,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert payloads[0].vm_vcpus == expected
    assert payloads[0].vm_disk_gb == expected


def test_reset_unexpected_failure_preserves_fetch_error(client, monkeypatch):
    from astinuslab.ui import job_mutations

    def fail(_):
        raise RuntimeError("snapshot service unavailable")

    monkeypatch.setattr(job_mutations, "reset_lab_service", fail)
    response = client.post("/jobs/example/reset-lab", headers={"Accept": "application/json"})
    assert response.status_code == 503
    assert response.json()["detail"] == "Reset failed: snapshot service unavailable"
    assert not response.history
    response = client.post("/jobs/example/reset-lab", follow_redirects=False)
    assert response.status_code == 303
    assert "message=" in response.headers["location"]


@pytest.mark.parametrize("lang", ["es", "fr", "ca"])
def test_job_page_exports_live_action_translations(client, created_job, lang):
    from astinuslab.i18n import get_translations

    client.cookies.set("lang", lang)
    response = client.get(f"/jobs/{created_job['id']}")
    match = re.search(r'<script id="al-i18n"[^>]*>(.*?)</script>', response.text, re.S)
    assert match is not None
    dictionary = json.loads(match[1])
    for key in ("job_reset_failed", "job_upload_failed", "job_upload_item_dump", "job_upload_stored"):
        assert dictionary[key] == get_translations(lang)[key]


def test_cache_deletion_refusal_is_visible(client, monkeypatch):
    from astinuslab.ui import jobs_routes

    monkeypatch.setattr(jobs_routes, "_remove_workload_cache_entry", lambda key: False)
    response = client.post("/workload-cache/delete", data={"key": "missing"}, follow_redirects=False)
    assert "message_type=success" not in response.headers["location"]
    assert "not" in response.headers["location"].lower()
