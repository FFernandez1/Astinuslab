from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
from typing import cast

import pytest
from starlette.testclient import TestClient

from astinuslab.core import fake_mode
from astinuslab.core.config import Settings
from astinuslab.forensics.vol3_tools import ToolStatus, Vol3Readiness
from astinuslab.readiness import (
    ReadinessCheck,
    ReadinessReport,
    ReadinessStatus,
    check_api_privileged_helper_sudo,
    check_celery_broker,
    check_database_transaction,
    check_fake_mode,
    check_libvirt_connection,
    check_privileged_helper_integrity,
    check_source_non_writability,
    check_storage_free_space,
    check_storage_write,
    collect_worker_readiness_checks,
    mandatory_runtime_ready,
    map_root_policy_check,
    map_volatility_checks,
    remote_control_available,
)


def _check(name: str, status: ReadinessStatus = "passed", code: str = "ready") -> ReadinessCheck:
    return ReadinessCheck(
        name=name,
        status=status,
        code=code,
        applies_to=("api",),
        detail=f"{name}: {status}",
    )


def _helper_sudo(payload: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], payload["helper_sudo"])


def _wait_for_runner_probe(signals, *, timeout: float = 1.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = signals.worker_readiness_payload("runner")
        if _helper_sudo(payload)["code"] != "runner_readiness_probe_in_progress":
            return payload
        time.sleep(0.005)
    raise AssertionError("runner readiness probe did not finish")


def test_readiness_report_has_stable_shape_and_ignores_not_applicable_checks():
    report = ReadinessReport(
        checks=(
            _check("database_transaction"),
            _check("privileged_helper_sudo_api", "not_applicable", "not_applicable"),
        )
    )

    assert report.public_dict() == {
        "ready": True,
        "status": "ready",
        "checks": [
            {
                "name": "database_transaction",
                "status": "passed",
                "code": "ready",
                "applies_to": ["api"],
                "detail": "database_transaction: passed",
            },
            {
                "name": "privileged_helper_sudo_api",
                "status": "not_applicable",
                "code": "not_applicable",
                "applies_to": ["api"],
                "detail": "privileged_helper_sudo_api: not_applicable",
            },
        ],
    }


def test_database_check_opens_write_transaction_and_rolls_it_back():
    calls: list[str] = []

    class Connection:
        def exec_driver_sql(self, statement: str):
            calls.append(statement)

        def rollback(self):
            calls.append("ROLLBACK")

    class Engine:
        @contextmanager
        def connect(self):
            yield Connection()

    result = check_database_transaction(Engine())

    assert result.status == "passed"
    assert calls == ["BEGIN IMMEDIATE", "SELECT 1", "ROLLBACK"]


def test_database_check_returns_stable_failure_code():
    class Engine:
        @contextmanager
        def connect(self):
            raise OSError("database details must not leak")
            yield

    result = check_database_transaction(Engine())

    assert (result.status, result.code) == ("failed", "database_transaction_failed")
    assert "details" not in result.detail


def test_celery_broker_check_opens_transport_connection():
    calls: list[object] = []

    class Connection:
        def __enter__(self):
            calls.append("enter")
            return self

        def __exit__(self, *args):
            calls.append("exit")

        def ensure_connection(self, *, max_retries: int, timeout: float):
            calls.append((max_retries, timeout))

    app = SimpleNamespace(connection_for_read=lambda: Connection())

    result = check_celery_broker(app)

    assert (result.status, result.code) == ("passed", "ready")
    assert calls == ["enter", (0, 3.0), "exit"]


def test_celery_broker_check_returns_stable_failure_code():
    app = SimpleNamespace(connection_for_read=lambda: (_ for _ in ()).throw(OSError("private broker URL")))

    result = check_celery_broker(app)

    assert (result.status, result.code) == ("failed", "celery_broker_unavailable")
    assert "private broker URL" not in result.detail


def test_storage_write_check_creates_and_removes_probe(tmp_path):
    before = set(tmp_path.iterdir())

    result = check_storage_write(tmp_path)

    assert (result.status, result.code) == ("passed", "ready")
    assert set(tmp_path.iterdir()) == before


def test_storage_write_check_returns_stable_failure_code(tmp_path, monkeypatch):
    import astinuslab.readiness as readiness

    def deny(*args, **kwargs):
        raise PermissionError("private path")

    monkeypatch.setattr(readiness.tempfile, "NamedTemporaryFile", deny)

    result = check_storage_write(tmp_path)

    assert (result.status, result.code) == ("failed", "storage_not_writable")
    assert "private path" not in result.detail


def test_storage_free_space_enforces_configured_guardrail(tmp_path):
    usage = SimpleNamespace(free=4 * 1024**3)

    result = check_storage_free_space(tmp_path, minimum_free_bytes=5 * 1024**3, disk_usage_fn=lambda _p: usage)

    assert (result.status, result.code) == ("failed", "storage_free_space_below_guardrail")


def test_storage_guardrail_database_failure_is_converted_to_unavailable(monkeypatch):
    import astinuslab.readiness as readiness

    monkeypatch.setattr(
        readiness,
        "load_app_settings",
        lambda: (_ for _ in ()).throw(RuntimeError("database exception")),
    )

    assert readiness._storage_guardrail_bytes() is None


def test_root_policy_mapping_preserves_stable_policy_code():
    result = map_root_policy_check({"code": "acl_policy_storage_root_mismatch", "message": "unsafe internal paths"})

    assert result.status == "failed"
    assert result.code == "acl_policy_storage_root_mismatch"
    assert "unsafe internal paths" not in result.detail
    assert result.applies_to == ("runner",)


def test_root_policy_fails_when_playground_directories_are_outside_policy(monkeypatch):
    import astinuslab.readiness as readiness

    # Storage alone being covered is not enough: an uncovered Playground disk
    # directory made every native Playground launch fail while readiness passed.
    monkeypatch.setattr(
        readiness.host_script_manifest, "check_acl_policy_storage_root", lambda _settings: {"code": "ready"}
    )
    monkeypatch.setattr(
        readiness.host_script_manifest,
        "check_acl_policy_playground_dirs",
        lambda _settings: {"code": "acl_policy_playground_dir_mismatch", "message": "outside"},
    )

    result = map_root_policy_check(readiness._root_policy_status(cast(Settings, SimpleNamespace())))

    assert result.status == "failed"
    assert result.code == "acl_policy_playground_dir_mismatch"


def test_root_policy_reports_storage_mismatch_before_playground_directories(monkeypatch):
    import astinuslab.readiness as readiness

    monkeypatch.setattr(
        readiness.host_script_manifest,
        "check_acl_policy_storage_root",
        lambda _settings: {"code": "acl_policy_storage_root_mismatch", "message": "storage"},
    )
    monkeypatch.setattr(
        readiness.host_script_manifest,
        "check_acl_policy_playground_dirs",
        lambda _settings: (_ for _ in ()).throw(AssertionError("storage mismatch is reported first")),
    )

    assert (
        readiness._root_policy_status(cast(Settings, SimpleNamespace()))["code"] == "acl_policy_storage_root_mismatch"
    )


def test_helper_integrity_requires_regular_root_owned_mode_0755(tmp_path):
    helper = tmp_path / "helper.sh"
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o755)
    info = helper.stat()

    ready = check_privileged_helper_integrity(
        tmp_path,
        (helper.name,),
        expected_uid=info.st_uid,
        expected_gid=info.st_gid,
    )
    wrong_owner = check_privileged_helper_integrity(
        tmp_path,
        (helper.name,),
        expected_uid=info.st_uid + 1,
        expected_gid=info.st_gid,
    )
    helper.chmod(0o775)
    wrong_mode = check_privileged_helper_integrity(
        tmp_path,
        (helper.name,),
        expected_uid=info.st_uid,
        expected_gid=info.st_gid,
    )

    assert (ready.status, ready.code) == ("passed", "ready")
    assert wrong_owner.code == "privileged_helper_owner_mismatch"
    assert wrong_mode.code == "privileged_helper_mode_mismatch"


def test_helper_integrity_rejects_symlinks(tmp_path):
    target = tmp_path / "target.sh"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "helper.sh").symlink_to(target)

    result = check_privileged_helper_integrity(
        tmp_path,
        ("helper.sh",),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
    )

    assert result.code == "privileged_helper_invalid_type"


def test_api_helper_sudo_is_not_applicable_and_never_spawns_sudo(monkeypatch):
    import astinuslab.readiness as readiness

    monkeypatch.setattr(
        readiness.host_script_manifest,
        "check_privileged_helpers",
        lambda: (_ for _ in ()).throw(AssertionError("API must not probe sudo")),
    )

    result = check_api_privileged_helper_sudo()

    assert result.status == "not_applicable"
    assert result.code == "not_applicable"
    assert result.applies_to == ("api",)


class _FanoutConnection:
    """Minimal stand-in for a Celery broker connection context manager."""

    def __init__(self, channel: object) -> None:
        self.default_channel = channel

    def __enter__(self) -> _FanoutConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def test_worker_heartbeats_are_not_applicable_when_the_broker_cannot_carry_control_replies():
    # The shipped SQLite broker has no fanout, so Celery remote control can
    # never deliver a reply. A permanently unanswerable probe is reported as
    # not_applicable, never as a failure that blocks an install switch.
    runner_heartbeat, analysis_heartbeat, helper_sudo = collect_worker_readiness_checks([], remote_control=False)

    assert (runner_heartbeat.status, runner_heartbeat.code) == ("not_applicable", "remote_control_unsupported")
    assert (analysis_heartbeat.status, analysis_heartbeat.code) == ("not_applicable", "remote_control_unsupported")
    assert (helper_sudo.status, helper_sudo.code) == ("not_applicable", "remote_control_unsupported")
    assert runner_heartbeat.applies_to == ("runner",)
    assert analysis_heartbeat.applies_to == ("analysis",)


def test_missing_heartbeats_still_fail_when_the_broker_can_carry_control_replies():
    runner_heartbeat, analysis_heartbeat, helper_sudo = collect_worker_readiness_checks([], remote_control=True)

    assert (runner_heartbeat.status, runner_heartbeat.code) == ("failed", "runner_heartbeat_missing")
    assert (analysis_heartbeat.status, analysis_heartbeat.code) == ("failed", "analysis_heartbeat_missing")
    assert (helper_sudo.status, helper_sudo.code) == ("not_applicable", "runner_report_unavailable")


def test_a_broker_without_remote_control_does_not_block_the_install_switch():
    report = ReadinessReport(checks=tuple(collect_worker_readiness_checks([], remote_control=False)))

    assert mandatory_runtime_ready(report) is True


def test_remote_control_availability_tracks_transport_fanout_support():
    def app_for(channel: object) -> object:
        return SimpleNamespace(connection_for_read=lambda: _FanoutConnection(channel))

    unreachable = SimpleNamespace(
        connection_for_read=lambda: (_ for _ in ()).throw(RuntimeError("private broker state"))
    )

    assert remote_control_available(app_for(SimpleNamespace(supports_fanout=False))) is False
    assert remote_control_available(app_for(SimpleNamespace(supports_fanout=True))) is True
    assert remote_control_available(app_for(SimpleNamespace())) is True
    assert remote_control_available(unreachable) is True


def test_worker_checks_require_runner_reply_and_do_not_fail_sudo_without_one():
    runner_heartbeat, analysis_heartbeat, helper_sudo = collect_worker_readiness_checks(
        [{"analysis@host": {"service_identity": "analysis", "helper_sudo": {"status": "not_applicable"}}}]
    )

    assert (runner_heartbeat.status, runner_heartbeat.code) == ("failed", "runner_heartbeat_missing")
    assert (analysis_heartbeat.status, analysis_heartbeat.code) == ("passed", "ready")
    assert (helper_sudo.status, helper_sudo.code) == ("not_applicable", "runner_report_unavailable")


def test_worker_checks_require_analysis_reply_independently_from_runner():
    runner_heartbeat, analysis_heartbeat, helper_sudo = collect_worker_readiness_checks(
        [
            {
                "runner@host": {
                    "service_identity": "runner",
                    "helper_sudo": {"status": "passed", "code": "ready"},
                }
            }
        ]
    )

    assert (runner_heartbeat.status, runner_heartbeat.code) == ("passed", "ready")
    assert (analysis_heartbeat.status, analysis_heartbeat.code) == (
        "failed",
        "analysis_heartbeat_missing",
    )
    assert analysis_heartbeat.applies_to == ("analysis",)
    assert (helper_sudo.status, helper_sudo.code) == ("passed", "ready")


def test_worker_checks_surface_runner_owned_sudo_failure():
    runner_heartbeat, analysis_heartbeat, helper_sudo = collect_worker_readiness_checks(
        [
            {
                "celery@host": {
                    "service_identity": "runner",
                    "helper_sudo": {
                        "status": "failed",
                        "code": "privileged_helper_sudo_unavailable",
                        "detail": "Runner cannot invoke every privileged helper.",
                    },
                }
            },
            {"analysis@host": {"service_identity": "analysis"}},
        ]
    )

    assert (runner_heartbeat.status, runner_heartbeat.code) == ("passed", "ready")
    assert (analysis_heartbeat.status, analysis_heartbeat.code) == ("passed", "ready")
    assert (helper_sudo.status, helper_sudo.code) == ("failed", "privileged_helper_sudo_unavailable")
    assert helper_sudo.applies_to == ("runner",)


def test_libvirt_check_closes_connection():
    closed: list[bool] = []
    connection = SimpleNamespace(close=lambda: closed.append(True))
    libvirt_module = SimpleNamespace(open=lambda _uri: connection)

    result = check_libvirt_connection("qemu:///system", libvirt_module=libvirt_module, fake_libvirt=False)

    assert (result.status, result.code) == ("passed", "ready")
    assert closed == [True]


def test_libvirt_check_is_not_applicable_in_fake_mode():
    libvirt_module = SimpleNamespace(open=lambda _uri: (_ for _ in ()).throw(AssertionError("must not connect")))

    result = check_libvirt_connection("qemu:///system", libvirt_module=libvirt_module, fake_libvirt=True)

    assert (result.status, result.code) == ("not_applicable", "fake_libvirt_active")


def test_source_check_fails_when_runtime_can_write_application_code(tmp_path):
    source = tmp_path / "backend"
    source.mkdir()
    source.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP)

    result = check_source_non_writability((source,))

    assert (result.status, result.code) == ("failed", "application_source_writable")


@pytest.mark.parametrize("layout", ["current", "missing_ops", "writable_ops", "writable_legacy", "dangling_legacy"])
def test_source_check_accepts_current_release_layout_without_legacy_trees(tmp_path, layout):
    from astinuslab.core.config import settings
    from astinuslab.readiness import _source_paths

    # The installed archive ships ops/deploy and ops/tools; the former root
    # tools/deploy directories no longer exist. The venv is installed afterward.
    root = tmp_path / "release"
    root.mkdir()
    for name in ("src", "ops", "guest", ".venv"):
        (root / name).mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='acceptance'\n")
    if layout == "missing_ops":
        (root / "ops").rmdir()
    elif layout == "writable_ops":
        (root / "ops" / "writable.py").write_text("# runtime-writable source\n")
    elif layout == "writable_legacy":
        (root / "tools").mkdir()
        (root / "tools" / "writable.py").write_text("# legacy source still requires protection\n")
    elif layout == "dangling_legacy":
        (root / "deploy").symlink_to(root / "missing")
    for path in root.rglob("*"):
        if not path.is_symlink():
            path.chmod(0o555 if path.is_dir() else 0o644 if path.name == "writable.py" else 0o444)
    root.chmod(0o555)
    try:
        result = check_source_non_writability(
            _source_paths(settings.model_copy(update={"base_dir": root})), install_root=root
        )
        expected = {
            "current": ("passed", "ready"),
            "missing_ops": ("failed", "application_source_missing"),
            "writable_ops": ("failed", "application_source_writable"),
            "writable_legacy": ("failed", "application_source_writable"),
            "dangling_legacy": ("failed", "application_source_missing"),
        }
        assert (result.status, result.code) == expected[layout]
    finally:
        root.chmod(0o755)
        for path in root.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o755)


def test_source_check_returns_stable_code_when_tree_cannot_be_traversed(tmp_path, monkeypatch):
    source = tmp_path / "backend"
    source.mkdir()
    source.chmod(0o555)
    monkeypatch.setattr(
        type(source),
        "rglob",
        lambda self, pattern: (_ for _ in ()).throw(PermissionError("private source path")),
    )

    result = check_source_non_writability((source,))

    assert (result.status, result.code) == ("failed", "application_source_unreadable")
    assert "private source path" not in result.detail


def test_source_check_rejects_writable_install_root_without_scanning_mutable_siblings(tmp_path):
    install_root = tmp_path / "install"
    source = install_root / "backend"
    mutable_storage = install_root / "storage"
    source.mkdir(parents=True)
    mutable_storage.mkdir()
    source.chmod(0o555)
    install_root.chmod(0o755)

    result = check_source_non_writability((source,), install_root=install_root)

    assert (result.status, result.code) == ("failed", "application_source_writable")


def test_volatility_checks_keep_capabilities_and_services_separate():
    snapshot = Vol3Readiness(
        enabled=True,
        analysis=ToolStatus(ready=False, code="volatility_not_configured", detail="private"),
        isf_build=ToolStatus(ready=True, code="ready", detail="ready"),
    )

    analysis, isf_build = map_volatility_checks(snapshot)

    assert (analysis.status, analysis.code) == ("failed", "volatility_not_configured")
    assert analysis.applies_to == ("runner", "analysis")
    assert (isf_build.status, isf_build.code) == ("passed", "ready")
    assert isf_build.applies_to == ("runner",)


def test_mandatory_runtime_verdict_ignores_only_optional_volatility_failures():
    from astinuslab import readiness

    optional_only = ReadinessReport(
        checks=(
            _check("database_transaction"),
            _check("volatility_analysis", "failed", "volatility_not_configured"),
            _check("volatility_isf_build", "failed", "dwarf2json_not_configured"),
        )
    )
    core_failure = ReadinessReport(
        checks=(
            _check("database_transaction", "failed", "database_transaction_failed"),
            _check("volatility_analysis", "failed", "volatility_not_configured"),
        )
    )

    assert readiness.mandatory_runtime_ready(optional_only) is True
    assert readiness.mandatory_runtime_ready(core_failure) is False


def test_deep_readiness_uses_short_volatility_probe_timeout(monkeypatch):
    import astinuslab.readiness as readiness

    observed: list[float] = []
    snapshot = Vol3Readiness(
        enabled=False,
        analysis=ToolStatus(ready=False, code="feature_disabled", detail="disabled"),
        isf_build=ToolStatus(ready=False, code="feature_disabled", detail="disabled"),
    )

    def get_readiness(settings_obj, *, volatility_timeout_seconds: float):
        observed.append(volatility_timeout_seconds)
        return snapshot

    monkeypatch.setattr(readiness, "get_vol3_readiness", get_readiness)

    checks = readiness.check_deep_volatility_readiness(readiness.settings)

    assert len(checks) == 2
    assert observed == [3.0]


def test_fake_mode_active_makes_deep_readiness_fail(monkeypatch):
    monkeypatch.setattr(fake_mode, "fake_mode_active", lambda: True)

    result = check_fake_mode()

    assert (result.status, result.code) == ("failed", "fake_mode_active")
    assert result.applies_to == ("api", "runner", "analysis")


def test_dependency_probe_exceptions_map_to_stable_non_leaking_codes(tmp_path, monkeypatch):
    import astinuslab.readiness as readiness

    broker_error = SimpleNamespace(
        control=SimpleNamespace(
            broadcast=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("private broker state"))
        )
    )
    libvirt_error = SimpleNamespace(open=lambda _uri: (_ for _ in ()).throw(RuntimeError("private libvirt state")))

    replies = readiness.probe_worker_readiness(broker_error)
    libvirt_check = check_libvirt_connection(
        "qemu:///system",
        libvirt_module=libvirt_error,
        fake_libvirt=False,
    )
    free_space_check = check_storage_free_space(
        tmp_path,
        minimum_free_bytes=1,
        disk_usage_fn=lambda _path: (_ for _ in ()).throw(OSError("private storage path")),
    )
    root_policy_check = map_root_policy_check({"code": "not_applicable"})
    helper_check = check_privileged_helper_integrity(tmp_path, ("missing-helper.sh",))
    source_check = check_source_non_writability((tmp_path / "missing-source",))
    install_root_check = check_source_non_writability((), install_root=tmp_path / "missing-install")
    libvirt_none_check = check_libvirt_connection(
        "qemu:///system",
        libvirt_module=SimpleNamespace(open=lambda _uri: None),
        fake_libvirt=False,
    )
    free_space_ready = check_storage_free_space(
        tmp_path,
        minimum_free_bytes=1,
        disk_usage_fn=lambda _path: SimpleNamespace(free=1),
    )
    read_only_source = tmp_path / "read-only-source"
    read_only_source.mkdir()
    monkeypatch.setattr(readiness.os, "access", lambda _path, _mode: False)
    source_ready = check_source_non_writability((read_only_source,))
    _, _, in_progress_check = collect_worker_readiness_checks(
        [
            {
                "runner@host": {
                    "service_identity": "runner",
                    "helper_sudo": {
                        "status": "failed",
                        "code": "runner_readiness_probe_in_progress",
                    },
                },
                "analysis@host": {"service_identity": "analysis"},
            }
        ]
    )

    assert replies == []
    assert (libvirt_check.status, libvirt_check.code) == ("failed", "libvirt_connection_unavailable")
    assert (free_space_check.status, free_space_check.code) == ("failed", "storage_free_space_unavailable")
    assert (root_policy_check.status, root_policy_check.code) == ("not_applicable", "fake_libvirt_active")
    assert (helper_check.status, helper_check.code) == ("failed", "privileged_helper_missing")
    assert (source_check.status, source_check.code) == ("failed", "application_source_missing")
    assert (install_root_check.status, install_root_check.code) == ("failed", "application_source_missing")
    assert (libvirt_none_check.status, libvirt_none_check.code) == ("failed", "libvirt_connection_unavailable")
    assert (free_space_ready.status, free_space_ready.code) == ("passed", "ready")
    assert (source_ready.status, source_ready.code) == ("passed", "ready")
    assert (in_progress_check.status, in_progress_check.code) == (
        "failed",
        "runner_readiness_probe_in_progress",
    )
    assert "private" not in " ".join(
        check.detail for check in (libvirt_check, free_space_check, root_policy_check, helper_check, source_check)
    )


def test_collect_readiness_assembles_every_check_in_stable_order(monkeypatch, tmp_path):
    import astinuslab.readiness as readiness

    settings_obj = cast(
        Settings,
        SimpleNamespace(
            base_dir=tmp_path / "install",
            data_dir=tmp_path / "install",
            libvirt_uri="qemu:///system",
            storage_dir=tmp_path / "storage",
        ),
    )
    observed_source: list[tuple[tuple[object, ...], object]] = []

    monkeypatch.setattr(readiness, "check_database_transaction", lambda: _check("database_transaction"))
    monkeypatch.setattr(readiness, "check_celery_broker", lambda: _check("celery_broker"))
    monkeypatch.setattr(
        readiness,
        "probe_worker_readiness",
        lambda: [
            {
                "runner@host": {
                    "service_identity": "runner",
                    "helper_sudo": {"status": "passed", "code": "ready"},
                    "libvirt_connection": {"status": "passed", "code": "ready"},
                },
                "analysis@host": {"service_identity": "analysis"},
            }
        ],
    )
    monkeypatch.setattr(
        readiness,
        "check_libvirt_connection",
        lambda _uri: (_ for _ in ()).throw(AssertionError("API identity must not open libvirt")),
    )
    monkeypatch.setattr(readiness, "check_storage_write", lambda _path: _check("storage_write"))
    monkeypatch.setattr(readiness, "_storage_guardrail_bytes", lambda: 1)
    monkeypatch.setattr(
        readiness,
        "check_storage_free_space",
        lambda _path, *, minimum_free_bytes: _check("storage_free_space"),
    )
    monkeypatch.setattr(
        readiness.host_script_manifest,
        "check_acl_policy_storage_root",
        lambda _settings: {"code": "ready"},
    )
    monkeypatch.setattr(
        readiness.host_script_manifest,
        "check_acl_policy_playground_dirs",
        lambda _settings: {"code": "ready"},
    )
    monkeypatch.setattr(readiness.fake_mode, "FAKE_LIBVIRT", False)
    monkeypatch.setattr(
        readiness,
        "check_privileged_helper_integrity",
        lambda: _check("privileged_helper_integrity"),
    )
    monkeypatch.setattr(
        readiness,
        "check_api_privileged_helper_sudo",
        lambda: _check("privileged_helper_sudo_api", "not_applicable", "not_applicable"),
    )

    def source_check(paths, *, install_root):
        observed_source.append((tuple(paths), install_root))
        return _check("source_non_writability")

    monkeypatch.setattr(readiness, "check_source_non_writability", source_check)
    monkeypatch.setattr(
        readiness,
        "check_deep_volatility_readiness",
        lambda _settings: (_check("volatility_analysis"), _check("volatility_isf_build")),
    )
    monkeypatch.setattr(readiness, "check_fake_mode", lambda: _check("fake_mode"))

    report = readiness.collect_readiness(settings_obj)

    assert [check.name for check in report.checks] == [
        "database_transaction",
        "celery_broker",
        "runner_heartbeat",
        "analysis_heartbeat",
        "privileged_helper_sudo_runner",
        "libvirt_connection",
        "storage_write",
        "storage_free_space",
        "root_policy",
        "privileged_helper_integrity",
        "privileged_helper_sudo_api",
        "source_non_writability",
        "volatility_analysis",
        "volatility_isf_build",
        "fake_mode",
    ]
    assert observed_source == [(readiness._source_paths(settings_obj), settings_obj.base_dir)]


def test_deep_readiness_endpoint_uses_503_for_failed_check(client, monkeypatch):
    import astinuslab.readiness as readiness

    monkeypatch.setattr(
        readiness,
        "collect_readiness",
        lambda: ReadinessReport(checks=(_check("database_transaction", "failed", "database_transaction_failed"),)),
    )

    response = client.get("/api/v1/readiness")

    assert response.status_code == 503
    assert response.json()["ready"] is False
    assert response.json()["checks"][0]["code"] == "database_transaction_failed"


async def test_deep_readiness_deadline_returns_stable_failure_instead_of_hanging():
    from astinuslab.readiness import collect_readiness_with_deadline

    completed = threading.Event()

    def blocked_probe():
        try:
            time.sleep(0.1)
            return ReadinessReport(checks=(_check("late"),))
        finally:
            completed.set()

    started = time.monotonic()
    report = await collect_readiness_with_deadline(blocked_probe, timeout_seconds=0.01)

    assert time.monotonic() - started < 0.08
    assert report.ready is False
    assert report.checks == (
        ReadinessCheck(
            name="deep_readiness_deadline",
            status="failed",
            code="deep_readiness_timeout",
            applies_to=("api", "runner", "analysis"),
            detail="Deep readiness exceeded its response deadline.",
        ),
    )
    while not completed.is_set():
        await asyncio.sleep(0.01)


async def test_deep_readiness_deadline_uses_one_worker_and_recovers_after_timeout(monkeypatch):
    import astinuslab.readiness as readiness

    class CountingExecutor(ThreadPoolExecutor):
        def __init__(self):
            super().__init__(max_workers=1)
            self.submissions = 0

        def submit(self, fn, /, *args, **kwargs):
            self.submissions += 1
            return super().submit(fn, *args, **kwargs)

    executor = CountingExecutor()
    monkeypatch.setattr(readiness, "_READINESS_EXECUTOR", executor)

    gate = threading.Event()
    completed = threading.Event()
    started: list[str] = []

    def first_probe():
        try:
            started.append("first")
            gate.wait(timeout=0.5)
            return ReadinessReport(checks=(_check("first"),))
        finally:
            completed.set()

    def queued_probe():
        started.append("queued")
        return ReadinessReport(checks=(_check("queued"),))

    try:
        first = await readiness.collect_readiness_with_deadline(first_probe, timeout_seconds=0.01)
        queued = await readiness.collect_readiness_with_deadline(queued_probe, timeout_seconds=0.01)

        assert first.checks[0].code == "deep_readiness_timeout"
        assert queued.checks[0].code == "deep_readiness_timeout"
        assert started == ["first"]
        assert executor.submissions == 1
    finally:
        gate.set()

    assert await asyncio.to_thread(completed.wait, 1.0)
    recovered = await readiness.collect_readiness_with_deadline(queued_probe, timeout_seconds=0.1)

    assert recovered.ready is True
    assert started == ["first", "queued"]
    assert executor.submissions == 2
    executor.shutdown(wait=True)


def test_deep_readiness_endpoint_requires_admin_auth(monkeypatch):
    import astinuslab.core.security as security
    import astinuslab.readiness as readiness
    from astinuslab.main import app

    token = "deep-readiness-admin-token"
    monkeypatch.setattr(
        security,
        "settings",
        SimpleNamespace(
            auth_enabled=True,
            admin_api_token_hash=f"sha256:{hashlib.sha256(token.encode()).hexdigest()}",
        ),
    )
    monkeypatch.setattr(readiness, "collect_readiness", lambda: ReadinessReport(checks=(_check("database"),)))

    with TestClient(app) as auth_client:
        denied = auth_client.get("/api/v1/readiness")
        allowed = auth_client.get(
            "/api/v1/readiness",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert denied.status_code == 401
    assert denied.headers["www-authenticate"] == "Bearer"
    assert allowed.status_code == 200


def test_health_remains_cheap_process_liveness(client, monkeypatch):
    import astinuslab.readiness as readiness

    monkeypatch.setattr(
        readiness,
        "collect_readiness",
        lambda: (_ for _ in ()).throw(AssertionError("/health must not run dependency probes")),
    )

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_analysis_preflight_skips_runner_helper_sudo(monkeypatch):
    from astinuslab.virtualization import host_script_manifest

    monkeypatch.setattr(host_script_manifest.fake_mode, "FAKE_LIBVIRT", False)
    monkeypatch.setattr(
        host_script_manifest,
        "check_host_scripts",
        lambda: (_ for _ in ()).throw(AssertionError("analysis must not inspect runner host scripts")),
    )
    monkeypatch.setattr(
        host_script_manifest,
        "check_privileged_helpers",
        lambda: (_ for _ in ()).throw(AssertionError("analysis must not probe runner sudo")),
    )
    monkeypatch.setattr(
        host_script_manifest,
        "check_acl_policy_storage_root",
        lambda: (_ for _ in ()).throw(AssertionError("analysis must not inspect runner root policy")),
    )

    assert host_script_manifest.preflight_problems(service_identity="analysis") == []


def test_worker_readiness_payload_only_probes_sudo_for_runner(monkeypatch):
    import astinuslab.jobs.celery_worker_signals as signals

    monkeypatch.setattr(signals.fake_mode, "FAKE_LIBVIRT", False)
    monkeypatch.setattr(
        signals,
        "check_privileged_helpers",
        lambda: [{"name": "helper.sh", "installed": True, "sudo_ok": False}],
    )
    signals._reset_worker_readiness_cache_for_tests()

    scheduled = signals.worker_readiness_payload("runner")
    runner = _wait_for_runner_probe(signals)
    analysis = signals.worker_readiness_payload("analysis")

    assert _helper_sudo(scheduled)["code"] == "runner_readiness_probe_in_progress"
    assert _helper_sudo(runner)["code"] == "privileged_helper_sudo_unavailable"
    assert _helper_sudo(analysis)["status"] == "not_applicable"


def test_runner_readiness_payload_owns_libvirt_probe(monkeypatch):
    import astinuslab.jobs.celery_worker_signals as signals

    calls: list[str] = []
    monkeypatch.setattr(signals.fake_mode, "FAKE_LIBVIRT", False)
    monkeypatch.setattr(
        signals,
        "check_privileged_helpers",
        lambda: [{"name": "helper.sh", "installed": True, "sudo_ok": True}],
    )

    def check_libvirt(uri: str):
        calls.append(uri)
        return _check("libvirt_connection")

    monkeypatch.setattr(
        signals,
        "check_libvirt_connection",
        check_libvirt,
        raising=False,
    )
    signals._reset_worker_readiness_cache_for_tests()

    signals.worker_readiness_payload("runner")
    runner = _wait_for_runner_probe(signals)

    assert calls == [signals.settings.libvirt_uri]
    assert runner["libvirt_connection"] == {"status": "passed", "code": "ready"}


def test_runner_readiness_payload_caches_privileged_probe(monkeypatch):
    import astinuslab.jobs.celery_worker_signals as signals

    calls = 0

    def check_helpers():
        nonlocal calls
        calls += 1
        return [{"name": "helper.sh", "installed": True, "sudo_ok": True}]

    monkeypatch.setattr(signals.fake_mode, "FAKE_LIBVIRT", False)
    monkeypatch.setattr(signals, "check_privileged_helpers", check_helpers)
    signals._reset_worker_readiness_cache_for_tests()

    scheduled = signals.worker_readiness_payload("runner")
    first = _wait_for_runner_probe(signals)
    second = signals.worker_readiness_payload("runner")

    assert _helper_sudo(scheduled)["code"] == "runner_readiness_probe_in_progress"
    assert first == second
    assert _helper_sudo(first)["code"] == "ready"
    assert calls == 1


def test_runner_readiness_payload_single_flight_does_not_repeat_privileged_probe(monkeypatch):
    import astinuslab.jobs.celery_worker_signals as signals

    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def check_helpers():
        nonlocal calls
        calls += 1
        entered.set()
        release.wait(timeout=1.0)
        return [{"name": "helper.sh", "installed": True, "sudo_ok": True}]

    monkeypatch.setattr(signals.fake_mode, "FAKE_LIBVIRT", False)
    monkeypatch.setattr(signals, "check_privileged_helpers", check_helpers)
    signals._reset_worker_readiness_cache_for_tests()

    started = time.monotonic()
    first = signals.worker_readiness_payload("runner")
    elapsed = time.monotonic() - started
    assert entered.wait(timeout=1.0)
    concurrent = signals.worker_readiness_payload("runner")
    release.set()
    completed = _wait_for_runner_probe(signals)

    assert calls == 1
    assert elapsed < 0.1
    assert _helper_sudo(first)["code"] == "runner_readiness_probe_in_progress"
    assert _helper_sudo(concurrent)["code"] == "runner_readiness_probe_in_progress"
    assert _helper_sudo(completed)["code"] == "ready"
