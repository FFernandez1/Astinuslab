from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from astinuslab.virtualization.guest_wait import (
    SSHTimeoutError,
    collect_guest_metadata_via_qga,
    collect_guest_metadata_via_ssh,
    wait_for_guest_agent,
    wait_for_guest_ssh,
)


class _FakeTime:
    def __init__(self, start: float = 0.0):
        self._now = start

    def monotonic(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += seconds


@patch("astinuslab.virtualization.guest_wait._run_subprocess")
def test_wait_for_guest_agent_retries_until_ping_succeeds(mock_run):
    # First call fails, second succeeds
    mock_run.side_effect = [
        subprocess.CalledProcessError(
            1, ["virsh"], stderr="error: Guest agent is not responding: QEMU guest agent is not connected"
        ),
        MagicMock(stdout='{"return": {}}'),
    ]

    wait_for_guest_agent(
        is_cancel_requested=lambda _: False,
        logger=SimpleNamespace(info=lambda *a, **k: None),
        job_id="job-123",
        vm_name="astinuslab-job-123",
        libvirt_uri="qemu:///system",
        max_wait_seconds=5,
        poll_interval_seconds=0,
    )
    assert mock_run.call_count == 2


def test_wait_for_guest_agent_raises_on_cancel():
    with pytest.raises(RuntimeError, match="cancel_requested"):
        wait_for_guest_agent(
            is_cancel_requested=lambda _: True,
            logger=SimpleNamespace(info=lambda *a, **k: None),
            job_id="job-123",
            vm_name="astinuslab-job-123",
            libvirt_uri="qemu:///system",
            max_wait_seconds=5,
            poll_interval_seconds=0,
        )


@patch("astinuslab.virtualization.guest_wait._run_subprocess")
def test_wait_for_guest_agent_times_out_cleanly(mock_run):
    mock_run.side_effect = subprocess.CalledProcessError(1, ["virsh"], stderr="error: Guest agent is not responding")
    fake_time = _FakeTime()

    with pytest.raises(RuntimeError, match="guest_agent_not_ready_after_2s"):
        wait_for_guest_agent(
            is_cancel_requested=lambda _: False,
            logger=SimpleNamespace(info=lambda *a, **k: None, error=lambda *a, **k: None),
            job_id="job-123",
            vm_name="astinuslab-job-123",
            libvirt_uri="qemu:///system",
            max_wait_seconds=2,
            poll_interval_seconds=1,
            time_module=fake_time,
        )


@patch("astinuslab.virtualization.guest_wait._run_subprocess")
def test_wait_for_guest_agent_propagates_permanent_errors(mock_run):
    # "unknown command" is considered permanent in our logic
    mock_run.side_effect = subprocess.CalledProcessError(1, ["virsh"], stderr="error: unknown command: guest-ping")

    with pytest.raises(subprocess.CalledProcessError) as exc:
        wait_for_guest_agent(
            is_cancel_requested=lambda _: False,
            logger=SimpleNamespace(info=lambda *a, **k: None, error=lambda *a, **k: None),
            job_id="job-123",
            vm_name="astinuslab-job-123",
            libvirt_uri="qemu:///system",
            max_wait_seconds=5,
            poll_interval_seconds=0,
        )
    assert exc.value.returncode == 1
    assert "unknown command" in exc.value.stderr


@pytest.mark.parametrize(
    ("stderr", "expected_detail"),
    [
        ("Permission denied", "Permission denied"),
        ("", "no details"),
    ],
)
def test_wait_for_guest_ssh_timeout_explains_stock_image_trap(stderr, expected_detail, tmp_path):
    fake_time = _FakeTime()
    fake_subprocess = SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr=stderr))

    with pytest.raises(SSHTimeoutError) as exc:
        wait_for_guest_ssh(
            is_cancel_requested=lambda _job: False,
            logger=SimpleNamespace(info=lambda *a, **k: None),
            subprocess_module=fake_subprocess,
            time_module=fake_time,
            job_id="job-123",
            guest_ip="192.0.2.10",
            ssh_key=tmp_path / "id_ed25519",
            guest_user="ubuntu",
            max_wait_seconds=1,
            poll_interval_seconds=1,
        )

    message = str(exc.value)
    assert "Guest SSH not available" in message
    assert expected_detail in message
    assert "prepared image" in message


def test_wait_for_guest_ssh_uses_private_known_hosts_when_supplied(tmp_path):
    calls = []
    fake_time = _FakeTime()

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    known_hosts = tmp_path / "known_hosts"
    wait_for_guest_ssh(
        is_cancel_requested=lambda _job: False,
        logger=SimpleNamespace(info=lambda *a, **k: None),
        subprocess_module=SimpleNamespace(run=run),
        time_module=fake_time,
        job_id="job-123",
        guest_ip="192.0.2.10",
        ssh_key=tmp_path / "id_ed25519",
        guest_user="Administrator",
        known_hosts_path=known_hosts,
        max_wait_seconds=1,
        poll_interval_seconds=1,
    )

    command = calls[0][0]
    assert "StrictHostKeyChecking=yes" in command
    assert f"UserKnownHostsFile={known_hosts}" in command
    assert "PreferredAuthentications=publickey" in command


def test_record_verified_ssh_host_key_requires_qga_fingerprint(tmp_path):
    from astinuslab.virtualization.guest_wait import record_verified_ssh_host_key

    key_blob = base64.b64encode(b"test-ed25519-key").decode("ascii")
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(b"test-ed25519-key").digest()).decode().rstrip("=")
    known_hosts = tmp_path / "known_hosts"
    fake_subprocess = SimpleNamespace(
        run=lambda *args, **kwargs: SimpleNamespace(
            stdout=f"192.0.2.10 ssh-ed25519 {key_blob} test-host\n", stderr="", returncode=0
        )
    )

    record_verified_ssh_host_key(
        "192.0.2.10",
        known_hosts,
        fingerprint,
        subprocess_module=fake_subprocess,
    )

    assert known_hosts.read_text(encoding="utf-8") == f"192.0.2.10 ssh-ed25519 {key_blob} test-host\n"
    assert known_hosts.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("platform", ["windows", "linux"])
def test_ssh_readiness_selects_platform_command_and_preserves_strict_verification(tmp_path, platform):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        expected = (
            'powershell.exe -NoLogo -NoProfile -NonInteractive -Command "exit 0"' if platform == "windows" else "true"
        )
        return SimpleNamespace(returncode=0 if command[-1] == expected else 1, stdout="", stderr="bad probe")

    wait_for_guest_ssh(
        lambda _: False,
        SimpleNamespace(info=lambda *a: None),
        SimpleNamespace(run=run),
        _FakeTime(),
        "job-test",
        "192.0.2.10",
        tmp_path / "key",
        "Administrator" if platform == "windows" else "ubuntu",
        guest_platform=platform,
        known_hosts_path=tmp_path / "known_hosts",
        max_wait_seconds=1,
    )
    assert len(commands) == 1
    assert "StrictHostKeyChecking=yes" in commands[0]
    assert f"UserKnownHostsFile={tmp_path / 'known_hosts'}" in commands[0]
    assert "BatchMode=yes" in commands[0]


def test_windows_ssh_readiness_refuses_unverified_host(tmp_path):
    with pytest.raises(ValueError, match="known.hosts"):
        wait_for_guest_ssh(
            lambda _: False,
            SimpleNamespace(info=lambda *a: None),
            SimpleNamespace(run=lambda *a, **k: pytest.fail("SSH before host verification")),
            _FakeTime(),
            "job-test",
            "192.0.2.10",
            tmp_path / "key",
            "Administrator",
            guest_platform="windows",
        )


def test_windows_ssh_readiness_timeout_reports_windows_bootstrap_checks(tmp_path):
    with pytest.raises(SSHTimeoutError) as exc:
        wait_for_guest_ssh(
            lambda _: False,
            SimpleNamespace(info=lambda *a: None),
            SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="Permission denied")),
            _FakeTime(),
            "job-test",
            "192.0.2.10",
            tmp_path / "key",
            "Administrator",
            guest_platform="windows",
            known_hosts_path=tmp_path / "known_hosts",
            max_wait_seconds=1,
        )
    assert "QGA" in str(exc.value)
    assert "sshd" in str(exc.value)
    assert "cloud-init" not in str(exc.value)


@patch("astinuslab.virtualization.guest_wait._run_subprocess")
def test_collect_guest_metadata_via_qga_normalizes_fields(mock_run):
    mock_run.side_effect = [
        MagicMock(stdout='{"return": {}}'),  # guest-info
        MagicMock(
            stdout=json.dumps(
                {
                    "return": {
                        "id": None,
                        "version-id": "",
                        "pretty-name": "Ubuntu 22.04.4 LTS",
                        "kernel-release": "null",
                        "machine": "  ",
                    }
                }
            )
        ),  # guest-get-osinfo
    ]

    metadata = collect_guest_metadata_via_qga("astinuslab-job-123", "qemu:///system")

    assert metadata["distro_id"] is None
    assert metadata["distro_version"] is None
    assert metadata["kernel_release"] is None
    assert metadata["arch"] is None


@patch("astinuslab.virtualization.guest_wait._run_subprocess")
def test_collect_guest_metadata_via_qga_normalizes_oracle_linux_id(mock_run):
    mock_run.side_effect = [
        MagicMock(stdout='{"return": {}}'),
        MagicMock(
            stdout=json.dumps(
                {
                    "return": {
                        "id": "oraclelinux",
                        "version-id": "9",
                        "kernel-release": "5.15.0-1.el9uek.x86_64",
                        "machine": "x86_64",
                    }
                }
            )
        ),
    ]

    metadata = collect_guest_metadata_via_qga("astinuslab-job-123", "qemu:///system")

    assert metadata["distro_id"] == "ol"


@patch("astinuslab.virtualization.guest_wait._run_subprocess")
def test_collect_guest_metadata_via_ssh_parses_detect_environment_json(mock_run, tmp_path):
    key = tmp_path / "id_ed25519"
    script = tmp_path / "detect_environment.sh"
    key.write_text("key", encoding="utf-8")
    script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    mock_run.return_value = MagicMock(
        stdout=json.dumps(
            {
                "distro_id": "Debian",
                "distro_version": "12",
                "kernel_release": "6.1.0-18-amd64",
                "arch": "x86_64",
            }
        ),
        stderr="",
        returncode=0,
    )

    metadata = collect_guest_metadata_via_ssh(
        "192.168.122.10",
        key,
        "debian",
        script,
    )

    assert metadata == {
        "distro_id": "debian",
        "distro_version": "12",
        "kernel_release": "6.1.0-18-amd64",
        "arch": "x86_64",
    }
    assert mock_run.call_args.kwargs["check"] is True
