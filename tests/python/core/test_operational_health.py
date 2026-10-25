from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from astinuslab.core import operational_health
from astinuslab.core.operational_health import Probe, collect_readiness


def _passes() -> None:
    return None


def _fails() -> None:
    raise RuntimeError("probe failed")


def test_collect_readiness_reports_ready_when_all_checks_pass():
    result = collect_readiness(
        (
            Probe("database", critical=True, run=_passes),
            Probe("runner", critical=False, run=_passes),
        )
    )

    assert result.http_status == 200
    assert result.payload == {
        "status": "ready",
        "checks": {
            "database": {"status": "ok", "critical": True},
            "runner": {"status": "ok", "critical": False},
        },
    }


def test_collect_readiness_reports_degraded_for_optional_failure():
    result = collect_readiness(
        (
            Probe("database", critical=True, run=_passes),
            Probe("runner", critical=False, run=_fails),
        )
    )

    assert result.http_status == 200
    assert result.payload["status"] == "degraded"
    assert result.payload["checks"]["runner"] == {
        "status": "unavailable",
        "critical": False,
    }


def test_collect_readiness_reports_not_ready_for_critical_failure():
    result = collect_readiness(
        (
            Probe("database", critical=True, run=_fails),
            Probe("runner", critical=False, run=_passes),
        )
    )

    assert result.http_status == 503
    assert result.payload["status"] == "not_ready"
    assert result.payload["checks"]["database"] == {
        "status": "unavailable",
        "critical": True,
    }


def test_probe_database_executes_select_one():
    engine = create_engine("sqlite:///:memory:", future=True)

    operational_health.probe_database(engine)


def test_probe_storage_writes_fsyncs_and_removes_probe_file(tmp_path):
    storage_dir = tmp_path / "storage"

    operational_health.probe_storage(storage_dir)

    assert (storage_dir / "tmp").is_dir()
    assert list((storage_dir / "tmp").glob(".readiness-*")) == []


def test_probe_service_accepts_live_development_pid_without_systemctl(tmp_path, monkeypatch):
    pid_file = tmp_path / "runner.pid"
    pid_file.write_text("123")
    monkeypatch.setattr(operational_health, "_pid_is_live", lambda pid: pid == 123)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("systemctl must not run for a live PID")

    monkeypatch.setattr(operational_health.subprocess, "run", fail_if_called)

    operational_health.probe_service(pid_file, "astinuslab-runner@test.service")


def test_probe_service_uses_bounded_systemctl_fallback(tmp_path, monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, "active\n", "")

    monkeypatch.setattr(operational_health.subprocess, "run", fake_run)

    operational_health.probe_service(
        tmp_path / "missing.pid",
        "astinuslab-runner@test.service",
    )

    assert seen["argv"] == [
        "systemctl",
        "is-active",
        "--quiet",
        "astinuslab-runner@test.service",
    ]
    assert seen["kwargs"]["timeout"] == 1.0
    assert seen["kwargs"]["check"] is False
    assert seen["kwargs"]["capture_output"] is True


def test_probe_service_rejects_inactive_unit(tmp_path, monkeypatch):
    monkeypatch.setattr(
        operational_health.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 3, "inactive\n", ""),
    )

    with pytest.raises(RuntimeError, match="service unavailable"):
        operational_health.probe_service(
            tmp_path / "missing.pid",
            "astinuslab-runner@test.service",
        )


def test_failed_service_diagnostics_are_logged_but_not_returned(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(
        operational_health.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv,
            3,
            "inactive\n",
            "unit failed\nretry exhausted",
        ),
    )

    result = collect_readiness(
        (
            Probe(
                "runner",
                critical=False,
                run=lambda: operational_health.probe_service(
                    tmp_path / "missing.pid",
                    "astinuslab-runner@test.service",
                ),
            ),
        )
    )

    assert "returncode=3" in caplog.text
    assert "detail='unit failed retry exhausted'" in caplog.text
    assert "unit failed" not in repr(result.payload)


def test_probe_libvirt_uses_read_only_bounded_command(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, "qemu:///system\n", "")

    monkeypatch.setattr(operational_health.subprocess, "run", fake_run)

    operational_health.probe_libvirt("qemu:///system")

    assert seen["argv"] == ["virsh", "-c", "qemu:///system", "uri"]
    assert seen["kwargs"]["timeout"] == 1.0
    assert seen["kwargs"]["check"] is False
    assert seen["kwargs"]["capture_output"] is True


def test_probe_libvirt_failure_retains_bounded_sanitized_diagnostics(monkeypatch):
    detail = "first line\n" + ("x" * 400)
    monkeypatch.setattr(
        operational_health.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, "", detail),
    )

    with pytest.raises(RuntimeError) as exc_info:
        operational_health.probe_libvirt("qemu:///system")

    message = str(exc_info.value)
    assert message.startswith("libvirt unavailable returncode=1 detail='first line ")
    assert "\n" not in message
    assert len(message) < 340


def test_default_probes_have_stable_names_and_criticality(monkeypatch):
    monkeypatch.setattr(
        operational_health.pwd,
        "getpwuid",
        lambda uid: type("User", (), {"pw_name": "test"})(),
    )

    probes = operational_health.default_probes()

    assert [(probe.name, probe.critical) for probe in probes] == [
        ("database", True),
        ("storage", True),
        ("runner", False),
        ("analysis", False),
        ("libvirt", False),
    ]


def test_default_worker_probes_use_configured_storage_directory(tmp_path, monkeypatch):
    base_dir = tmp_path / "checkout"
    storage_dir = tmp_path / "external-storage"
    seen_pid_files = []
    monkeypatch.setattr(
        operational_health,
        "settings",
        SimpleNamespace(
            base_dir=base_dir,
            storage_dir=storage_dir,
            libvirt_uri="qemu:///system",
        ),
    )
    monkeypatch.setattr(
        operational_health.pwd,
        "getpwuid",
        lambda uid: type("User", (), {"pw_name": "test"})(),
    )
    monkeypatch.setattr(
        operational_health,
        "probe_service",
        lambda pid_file, unit: seen_pid_files.append(pid_file),
    )

    probes = operational_health.default_probes()
    probes[2].run()
    probes[3].run()

    assert seen_pid_files == [
        storage_dir / "runner.pid",
        storage_dir / "analysis.pid",
    ]
