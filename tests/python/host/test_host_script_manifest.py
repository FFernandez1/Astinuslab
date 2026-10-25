from __future__ import annotations

import json
import os
import re
from pathlib import Path

from astinuslab.core import fake_mode
from astinuslab.virtualization import host_script_manifest as manifest
from tests.python._support.helpers import autospec_setattr

ROOT = Path(__file__).resolve().parents[3]


def test_required_scripts_exist_and_are_executable():
    base = ROOT / "ops" / "host"
    for name in manifest.REQUIRED_SCRIPTS:
        path = base / name
        assert path.exists(), f"missing required host script: {name}"
        assert os.access(path, os.X_OK), f"required host script is not executable: {name}"


def test_invoked_host_scripts_are_in_required_manifest():
    invoked: set[str] = set()
    pattern = re.compile(r'"([a-z0-9_/-]+\.sh)"')
    for path in (ROOT / "src" / "astinuslab").rglob("*.py"):
        if path.name == "host_script_manifest.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "scripts" not in text and ".sh" not in text:
            continue
        for match in pattern.findall(text):
            name = match.rsplit("/", 1)[-1]
            if name in manifest.REQUIRED_SCRIPTS or name in manifest.OPTIONAL_SCRIPTS:
                invoked.add(name)
    missing = invoked - set(manifest.REQUIRED_SCRIPTS) - set(manifest._ALLOWED_HELPERS)
    assert not missing, f"add to REQUIRED_SCRIPTS or justify as helper/optional: {sorted(missing)}"


def test_check_host_scripts_flags_missing_exec_bit(tmp_path, monkeypatch):
    script = tmp_path / "run_guest_script.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    autospec_setattr(monkeypatch, manifest, "host_scripts_dir", lambda: tmp_path)
    monkeypatch.setattr(manifest, "REQUIRED_SCRIPTS", ("run_guest_script.sh",))
    monkeypatch.setattr(manifest, "OPTIONAL_SCRIPTS", ())

    assert manifest.check_host_scripts() == [
        {"name": "run_guest_script.sh", "required": True, "status": "not_executable"}
    ]


def test_check_privileged_helpers_skips_sudo_when_not_installed(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(manifest, "PRIVILEGED_HELPER_DIR", tmp_path)
    autospec_setattr(monkeypatch, manifest.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    rows = manifest.check_privileged_helpers()

    assert rows
    assert all(row["installed"] is False and row["sudo_ok"] is False for row in rows)
    assert calls == []


def test_storage_root_policy_mismatch_is_actionable(tmp_path, monkeypatch):
    # This test exercises the real privileged path; the suite runs under
    # ASTINUSLAB_FAKE_LIBVIRT=1, where the ACL helper is deliberately not invoked.
    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    policy_path = tmp_path / "acl-policy.json"
    policy_path.write_text('{"allowed_roots": ["/srv/astinuslab/storage"]}', encoding="utf-8")
    monkeypatch.setattr(manifest, "ACL_POLICY_PATH", policy_path)
    configured_root = tmp_path / "checkout" / "storage"
    settings = type("Settings", (), {"storage_dir": configured_root, "base_dir": tmp_path / "checkout"})()

    result = manifest.check_acl_policy_storage_root(settings)

    assert result["code"] == "acl_policy_storage_root_mismatch"
    assert str(configured_root.resolve()) in result["message"]
    assert "/srv/astinuslab/storage" in result["message"]
    assert "sudo ops/deploy/configure-privileged-roots.sh" in result["message"]


def _playground_settings(tmp_path, disk_dir, capture_dir):
    return type(
        "Settings",
        (),
        {
            "storage_dir": tmp_path / "srv" / "storage",
            "base_dir": tmp_path / "opt",
            "playground_disk_dir": disk_dir,
            "playground_capture_dir": capture_dir,
        },
    )()


def test_playground_directories_outside_policy_are_actionable(tmp_path, monkeypatch):
    # A registered custom storage root leaves these separate settings under the
    # old root; every Playground launch then fails in the ACL helper while the
    # storage-root check alone still reports ready.
    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    policy_path = tmp_path / "acl-policy.json"
    registered = tmp_path / "srv" / "storage"
    policy_path.write_text(json.dumps({"allowed_roots": [str(registered)]}), encoding="utf-8")
    monkeypatch.setattr(manifest, "ACL_POLICY_PATH", policy_path)
    old_disks = tmp_path / "opt" / "storage" / "playground" / "disks"
    settings = _playground_settings(tmp_path, old_disks, registered / "playground" / "captures")

    assert manifest.check_acl_policy_storage_root(settings)["code"] == "ready"
    result = manifest.check_acl_policy_playground_dirs(settings)

    assert result["code"] == "acl_policy_playground_dir_mismatch"
    assert f"playground_disk_dir={old_disks.resolve()}" in result["message"]
    assert "playground_capture_dir" not in result["message"]
    assert "ASTINUSLAB_PLAYGROUND_DISK_DIR" in result["message"]


def test_playground_directories_inside_policy_are_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    policy_path = tmp_path / "acl-policy.json"
    registered = tmp_path / "srv" / "storage"
    policy_path.write_text(json.dumps({"allowed_roots": [str(registered)]}), encoding="utf-8")
    monkeypatch.setattr(manifest, "ACL_POLICY_PATH", policy_path)
    settings = _playground_settings(
        tmp_path, registered / "playground" / "disks", registered / "playground" / "captures"
    )

    assert manifest.check_acl_policy_playground_dirs(settings)["code"] == "ready"


def test_playground_directory_check_is_not_applicable_with_fake_libvirt(tmp_path, monkeypatch):
    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", True)
    settings = _playground_settings(tmp_path, tmp_path / "a", tmp_path / "b")

    assert manifest.check_acl_policy_playground_dirs(settings)["code"] == "not_applicable"


def test_fake_libvirt_skips_acl_policy_and_helper_checks(monkeypatch):
    monkeypatch.setattr(manifest.fake_mode, "FAKE_LIBVIRT", True)
    monkeypatch.setattr(manifest, "check_privileged_helpers", lambda: (_ for _ in ()).throw(AssertionError()))

    assert manifest.check_acl_policy_storage_root()["code"] == "not_applicable"
    assert not any("privileged helper" in problem for problem in manifest.preflight_problems())
