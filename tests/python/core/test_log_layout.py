"""Installed role logs retain public IDs without trusting service-owned paths."""

import json
import os
from types import SimpleNamespace

import pytest

from astinuslab.core import logging_config

pytestmark = pytest.mark.unit


@pytest.fixture
def role_logs(tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    logs = storage / "logs"
    roles = {"api": os.getuid(), "runner": os.getuid() + 1, "analysis": os.getuid() + 2}
    owners = {}
    for role, name in logging_config.SERVICE_LOG_FILES.items():
        directory = logs / role
        directory.mkdir(parents=True)
        path = directory / name
        path.write_text(f"current {role}\n")
        owners[directory.stat().st_ino] = roles[role]
        owners[path.stat().st_ino] = roles[role]
    logs.chmod(0o1770)
    marker = logs / ".astinuslab-service-logs.json"
    marker.write_text(json.dumps({"schema": "astinuslab-service-logs/v1", "roles": roles}))
    marker.chmod(0o644)
    owners[logs.stat().st_ino] = 0
    owners[marker.stat().st_ino] = 0
    original = os.fstat

    def fixture_custody(fd):
        info = original(fd)
        values = list(info)
        values[4] = owners.get(info.st_ino, info.st_uid)
        return os.stat_result(values)

    # Metadata-only unit fixture; distinct-UID kernel/ACL proof is a separate gate.
    monkeypatch.setattr(os, "fstat", fixture_custody)
    return storage, owners


def test_installed_logging_writes_the_provisioned_role_path(role_logs, monkeypatch):
    storage, _ = role_logs
    monkeypatch.setattr(logging_config, "settings", SimpleNamespace(log_to_file=True, storage_dir=storage))
    assert logging_config.log_file_for("analysis") == storage / "logs/analysis/analysis-worker.log"


def test_role_identifiers_and_finite_legacy_aliases_are_distinct(role_logs):
    from astinuslab.core import log_layout

    storage, _ = role_logs
    (storage / "logs/api.log").write_text("retained flat history")
    assert log_layout.available_log_names(storage) == ("api.log", "runner.log", "analysis-worker.log", "legacy-api.log")
    with log_layout.open_log(storage, "api.log") as stream:
        assert stream.read() == b"current api\n"
    with log_layout.open_log(storage, "legacy-api.log") as stream:
        assert stream.read() == b"retained flat history"


@pytest.mark.parametrize("attack", ["symlink", "hardlink"])
def test_role_log_open_refuses_link_substitution(role_logs, tmp_path, attack):
    from astinuslab.core import log_layout

    storage, _ = role_logs
    outside = tmp_path / "outside"
    outside.write_bytes(b"untouched")
    path = storage / "logs/api/api.log"
    path.unlink()
    if attack == "symlink":
        path.symlink_to(outside)
    else:
        path.hardlink_to(outside)
    with pytest.raises((OSError, ValueError)):
        with log_layout.open_log(storage, "api.log", writable=True):
            pytest.fail("unsafe log was opened")
    assert outside.read_bytes() == b"untouched"


def test_open_log_descriptor_survives_role_filename_replacement(role_logs):
    from astinuslab.core import log_layout

    storage, _ = role_logs
    path = storage / "logs/api/api.log"
    with log_layout.open_log(storage, "api.log") as stream:
        path.rename(path.with_suffix(".log.1"))
        path.write_bytes(b"new generation")
        assert stream.read() == b"current api\n"


def test_invalid_installed_marker_cannot_fall_back_to_flat_log(role_logs):
    from astinuslab.core import log_layout

    storage, _ = role_logs
    (storage / "logs/.astinuslab-service-logs.json").write_text("{}")
    (storage / "logs/api.log").write_text("old")
    with pytest.raises(ValueError):
        with log_layout.open_log(storage, "api.log"):
            pytest.fail("invalid marker fell back to flat path")


def test_invalid_layout_is_visible_in_log_metadata(role_logs, monkeypatch):
    from astinuslab.ui import logs_routes

    storage, _ = role_logs
    (storage / "logs/.astinuslab-service-logs.json").write_text("{}")
    monkeypatch.setattr(logs_routes, "settings", SimpleNamespace(storage_dir=storage))
    entries = logs_routes._available_logs()
    assert all(not item["exists"] for item in entries)
    assert all("operator maintenance" in item["layout_error"] for item in entries)
    template = logs_routes.templates.get_template("partials/log_metadata.html")
    rendered = template.render(log_metadata=entries[0])
    assert 'role="alert"' in rendered
    assert "storage/logs/" not in rendered


def test_role_truncation_preserves_inodes_and_does_not_create_missing_files(role_logs):
    from types import SimpleNamespace

    from astinuslab.storage import storage_admin_support as support

    storage, _ = role_logs
    api = storage / "logs/api/api.log"
    inode = api.stat().st_ino
    missing = storage / "logs/runner/runner.log"
    missing.unlink()
    history = storage / "logs/api.log"
    history.write_bytes(b"history")
    result = support.truncate_active_logs(SimpleNamespace(storage_dir=storage))
    assert result["ok"] is True
    assert api.stat().st_ino == inode and api.read_bytes() == b""
    assert not missing.exists()
    assert history.read_bytes() == b"history"


def test_runtime_clear_preserves_role_anchors_marker_and_history(role_logs):
    from types import SimpleNamespace

    from astinuslab.storage import storage_admin_support as support

    storage, _ = role_logs
    logs = storage / "logs"
    anchor_inode = logs.stat().st_ino
    marker = logs / ".astinuslab-service-logs.json"
    before = marker.read_bytes()
    (logs / "api.log").write_bytes(b"retained history")
    result = support.clear_runtime_traces(SimpleNamespace(storage_dir=storage), lambda: False)
    assert result["ok"] is True
    assert logs.stat().st_ino == anchor_inode and marker.read_bytes() == before
    assert (logs / "api/api.log").read_bytes() == b""
    assert (logs / "api.log").read_bytes() == b"retained history"
    assert result["fixed_skipped"] >= 1


def test_download_uses_open_inode_and_keeps_range_contract(role_logs, monkeypatch):
    import asyncio

    from astinuslab.ui import logs_routes

    storage, _ = role_logs
    monkeypatch.setattr(logs_routes, "settings", SimpleNamespace(storage_dir=storage))
    response = logs_routes.admin_logs_download("api.log")
    path = storage / "logs/api/api.log"
    path.rename(path.with_suffix(".log.1"))
    path.write_bytes(b"untrusted replacement")
    messages = []

    async def send(message):
        messages.append(message)

    async def receive():
        return {"type": "http.request"}

    asyncio.run(response({"type": "http", "method": "GET", "headers": [(b"range", b"bytes=0-6")]}, receive, send))
    assert messages[0]["status"] == 206
    assert dict(messages[0]["headers"])[b"content-disposition"] == b'attachment; filename="api.log"'
    assert b"".join(item.get("body", b"") for item in messages) == b"current"


def test_available_and_tail_use_current_role_and_legacy_history(role_logs, monkeypatch):
    from astinuslab.ui import logs_routes

    storage, _ = role_logs
    monkeypatch.setattr(logs_routes, "settings", SimpleNamespace(storage_dir=storage))
    (storage / "logs/api.log").write_text("retained history")
    names = {entry["name"]: entry for entry in logs_routes._available_logs()}
    assert names["api.log"]["size_kb"] == len(b"current api\n") / 1024
    assert "legacy-api.log" in names
    assert logs_routes._tail_log("api.log")[0]["text"] == "current api"
    assert logs_routes._tail_log("legacy-api.log")[0]["text"] == "retained history"


def test_retention_preserves_installed_layout_marker(role_logs):
    from astinuslab.storage.retention import _purge_old_files

    storage, _ = role_logs
    marker = storage / "logs/.astinuslab-service-logs.json"
    os.utime(marker, (0, 0))
    _purge_old_files(storage / "logs", 1)
    assert marker.exists()
