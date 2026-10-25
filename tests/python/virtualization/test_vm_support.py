from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from tests.python._support.helpers import autospec_setattr


class _FakeLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


class _FakeConn:
    def listAllDomains(self, flags=0):
        return []

    def close(self):
        pass


class _FakeVMLifecycle:
    def __init__(self, run_command, libvirt_module):
        self.run_command = run_command
        self.libvirt_module = libvirt_module

    def destroy(self, conn, vm_name: str):
        return None


def test_teardown_vm_removes_tmp_root_even_with_stale_virtiofs_pid(monkeypatch, tmp_path):
    import astinuslab.virtualization.vm_lifecycle as lifecycle_mod
    import astinuslab.virtualization.vm_support as vm_mod

    job_id = "job-cleanup"
    tmp_root = tmp_path / "storage" / "tmp" / job_id
    tmp_root.mkdir(parents=True)
    (tmp_root / "virtiofsd.pid").write_text("4321", encoding="utf-8")
    (tmp_root / "virtiofsd.sock").write_text("", encoding="utf-8")
    (tmp_root / "virtiofs-staging").mkdir()
    serial_log_dir = tmp_path / "storage" / "logs" / "serial"
    serial_log_dir.mkdir(parents=True)
    serial_log = serial_log_dir / f"astinuslab-{job_id}-serial.log"
    serial_log.write_text("serial", encoding="utf-8")

    commands: list[list[str]] = []
    destroyed: list[tuple[str, str]] = []

    def fake_run_command(cmd, **kwargs):
        commands.append(cmd)
        return ""

    fake_libvirt = SimpleNamespace(open=lambda uri: _FakeConn())
    autospec_setattr(monkeypatch, vm_mod, "import_module", lambda name: fake_libvirt)
    monkeypatch.setattr(lifecycle_mod, "VMLifecycle", _FakeVMLifecycle)

    import os
    import signal

    def fake_kill(pid: int, sig: int):
        assert pid == 4321
        assert sig == signal.SIGTERM
        raise ProcessLookupError("already gone")

    monkeypatch.setattr(os, "kill", fake_kill)

    settings = SimpleNamespace(
        base_dir=tmp_path,
        data_dir=tmp_path,
        storage_dir=tmp_path / "storage",
        libvirt_uri="qemu:///system",
        libvirt_image_base_dir=Path("/var/lib/libvirt/images/astinuslab"),
        serial_log_dir=serial_log_dir,
    )

    vm_mod.teardown_vm(
        fake_run_command,
        _FakeLogger(),
        settings,
        lambda canonical_job_id, role: destroyed.append((canonical_job_id, role)),
        job_id,
    )

    assert not tmp_root.exists()
    assert destroyed == [(job_id, "target")]
    assert not serial_log.exists()
    assert [
        "sudo",
        "-n",
        "/usr/local/libexec/astinuslab/libvirt_storage_admin.sh",
        "remove_job_dir",
        f"/var/lib/libvirt/images/astinuslab/{job_id}",
    ] in commands
