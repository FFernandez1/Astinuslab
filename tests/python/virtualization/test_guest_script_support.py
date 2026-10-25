from __future__ import annotations

import subprocess
import types
from pathlib import Path

from tests.python._support.helpers import autospec_setattr


def test_ssh_opts_uses_active_private_key(monkeypatch, tmp_path):
    import astinuslab.virtualization.guest_script_support as support

    private_key = tmp_path / "id_ed25519"
    public_key = tmp_path / "id_ed25519.pub"
    autospec_setattr(monkeypatch, support, "get_active_key_paths", lambda: (private_key, public_key))

    assert support.ssh_opts() == [
        "-i",
        str(private_key),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "BatchMode=yes",
    ]


def test_exec_script_rejects_filename_with_path_separator_before_writing(tmp_path, monkeypatch):
    import astinuslab.virtualization.guest_script_support as support

    monkeypatch.setattr(
        support,
        "settings",
        types.SimpleNamespace(base_dir=tmp_path, data_dir=tmp_path, storage_dir=tmp_path / "storage"),
    )
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("subprocess must not run for unsafe script filename")

    monkeypatch.setattr(support.subprocess, "run", fake_run)

    result = support.exec_script_on_guest(
        guest_ip="192.0.2.10",
        guest_user="ubuntu",
        script_bytes=b"echo unsafe\n",
        filename="../escape.sh",
        timeout_s=5,
    )

    assert result["success"] is False
    assert result["exit_code"] == 1
    assert "invalid script filename" in result["error"]
    assert calls == []
    assert not any(Path(tmp_path).rglob("escape.sh"))


def test_exec_script_copies_runs_combines_stderr_and_cleans_local_file(tmp_path, monkeypatch):
    import astinuslab.virtualization.guest_script_support as support

    monkeypatch.setattr(
        support,
        "settings",
        types.SimpleNamespace(base_dir=tmp_path, data_dir=tmp_path, storage_dir=tmp_path / "storage"),
    )
    autospec_setattr(monkeypatch, support, "ssh_opts", lambda: ["-i", "/tmp/test-key"])
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[0] == "scp":
            local_path = Path(cmd[-2])
            assert local_path.parent == tmp_path / "storage" / "tmp" / "adhoc_scripts"
            assert local_path.name.endswith("_safe.sh")
            assert local_path.read_bytes() == b"echo safe\n"
            assert cmd[-1].startswith("ubuntu@192.0.2.10:/tmp/astinuslab_adhoc_")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "ssh":
            assert cmd[1:3] == ["-i", "/tmp/test-key"]
            assert cmd[3] == "ubuntu@192.0.2.10"
            assert "chmod +x /tmp/astinuslab_adhoc_" in cmd[4]
            assert "rm -f /tmp/astinuslab_adhoc_" in cmd[4]
            return subprocess.CompletedProcess(cmd, 0, stdout="hello\n", stderr="warn\n")
        raise AssertionError(cmd)

    monkeypatch.setattr(support.subprocess, "run", fake_run)

    result = support.exec_script_on_guest(
        guest_ip="192.0.2.10",
        guest_user="ubuntu",
        script_bytes=b"echo safe\n",
        filename="safe.sh",
        timeout_s=5,
    )

    assert result["filename"] == "safe.sh"
    assert result["success"] is True
    assert result["exit_code"] == 0
    assert result["output"] == "hello\n--- stderr ---\nwarn"
    assert result["error"] is None
    assert [call[0][0] for call in calls] == ["scp", "ssh"]
    assert list((tmp_path / "storage" / "tmp" / "adhoc_scripts").iterdir()) == []


def test_exec_script_reports_scp_failure_without_ssh(tmp_path, monkeypatch):
    import astinuslab.virtualization.guest_script_support as support

    monkeypatch.setattr(
        support,
        "settings",
        types.SimpleNamespace(base_dir=tmp_path, data_dir=tmp_path, storage_dir=tmp_path / "storage"),
    )
    autospec_setattr(monkeypatch, support, "ssh_opts", lambda: [])
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd[0])
        return subprocess.CompletedProcess(cmd, 1, stdout="copy stdout", stderr="copy denied")

    monkeypatch.setattr(support.subprocess, "run", fake_run)

    result = support.exec_script_on_guest(
        guest_ip="192.0.2.10",
        guest_user="ubuntu",
        script_bytes=b"echo nope\n",
        filename="fail.sh",
        timeout_s=5,
    )

    assert result["success"] is False
    assert result["exit_code"] == 1
    assert result["output"] == ""
    assert result["error"] == "SCP failed: copy denied"
    assert calls == ["scp"]
    assert list((tmp_path / "storage" / "tmp" / "adhoc_scripts").iterdir()) == []


def test_exec_script_reports_timeout_and_cleans_local_file(tmp_path, monkeypatch):
    import astinuslab.virtualization.guest_script_support as support

    monkeypatch.setattr(
        support,
        "settings",
        types.SimpleNamespace(base_dir=tmp_path, data_dir=tmp_path, storage_dir=tmp_path / "storage"),
    )
    autospec_setattr(monkeypatch, support, "ssh_opts", lambda: [])

    def fake_run(cmd, **kwargs):
        if cmd[0] == "scp":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])

    monkeypatch.setattr(support.subprocess, "run", fake_run)

    result = support.exec_script_on_guest(
        guest_ip="192.0.2.10",
        guest_user="ubuntu",
        script_bytes=b"sleep 10\n",
        filename="slow.sh",
        timeout_s=7,
    )

    assert result["success"] is False
    assert result["exit_code"] == 124
    assert result["output"] == ""
    assert result["error"] == "Timeout: script exceeded 7s"
    assert list((tmp_path / "storage" / "tmp" / "adhoc_scripts").iterdir()) == []
