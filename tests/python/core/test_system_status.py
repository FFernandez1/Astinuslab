from __future__ import annotations

from astinuslab.core import system_status
from tests.python._support.helpers import autospec_setattr


def test_service_status_stopped_when_pid_file_missing(tmp_path):
    pid_file = tmp_path / "absent.pid"
    result = system_status.service_status(label="api", unit="x.service", description="d", pid_file=pid_file)
    assert result["state"] == "stopped"
    assert result["pid"] is None
    assert result["uptime_s"] is None
    assert result["label"] == "api"
    assert result["unit"] == "x.service"


def test_service_status_stopped_when_pid_not_running(tmp_path, monkeypatch):
    pid_file = tmp_path / "stale.pid"
    pid_file.write_text("999999")
    autospec_setattr(monkeypatch, system_status, "_proc_exists", lambda pid: False)
    result = system_status.service_status(label="api", unit="x.service", description="d", pid_file=pid_file)
    assert result["state"] == "stopped"
    assert result["pid"] == 999999
    assert result["uptime_s"] is None


def test_service_status_running_with_uptime(tmp_path, monkeypatch):
    pid_file = tmp_path / "live.pid"
    pid_file.write_text("4321")
    autospec_setattr(monkeypatch, system_status, "_proc_exists", lambda pid: True)
    autospec_setattr(monkeypatch, system_status, "_proc_uptime_s", lambda pid: 7384)
    result = system_status.service_status(label="api", unit="x.service", description="d", pid_file=pid_file)
    assert result == {
        "label": "api",
        "unit": "x.service",
        "description": "d",
        "state": "running",
        "pid": 4321,
        "uptime_s": 7384,
    }


def test_format_uptime_human():
    assert system_status.format_uptime(0) == "0s"
    assert system_status.format_uptime(59) == "59s"
    assert system_status.format_uptime(60) == "1m"
    assert system_status.format_uptime(3601) == "1h"
    assert system_status.format_uptime(86400 + 3600) == "1d 1h"
    assert system_status.format_uptime(86400 * 6 + 3600 * 11) == "6d 11h"


def test_libvirt_network_info_parses_virsh_output(monkeypatch):
    info_text = (
        "Name:           astinuslab0\n"
        "UUID:           4b9c-...\n"
        "Active:         yes\n"
        "Persistent:     yes\n"
        "Autostart:      yes\n"
        "Bridge:         virbr-al0\n"
    )
    dump_xml = (
        "<network>\n"
        "  <name>astinuslab0</name>\n"
        "  <forward mode='nat'/>\n"
        "  <bridge name='virbr-al0'/>\n"
        "  <ip address='10.42.0.1' netmask='255.255.255.0'>\n"
        "    <dhcp><range start='10.42.0.2' end='10.42.0.254'/></dhcp>\n"
        "  </ip>\n"
        "</network>\n"
    )

    def fake_run(cmd, **_):
        import subprocess

        if "net-info" in cmd:
            return subprocess.CompletedProcess(cmd, 0, info_text, "")
        if "net-dumpxml" in cmd:
            return subprocess.CompletedProcess(cmd, 0, dump_xml, "")
        raise AssertionError(cmd)

    monkeypatch.setattr(system_status.subprocess, "run", fake_run)
    info = system_status.libvirt_network_info("astinuslab0", uri="qemu:///system")
    assert info == {
        "state": "active",
        "autostart": "yes",
        "forward": "nat",
        "bridge": "virbr-al0",
        "subnet": "10.42.0.0/24",
        "gateway": "10.42.0.1",
    }


def test_libvirt_network_info_returns_unavailable_on_error(monkeypatch):
    def fake_run(cmd, **_):
        import subprocess

        return subprocess.CompletedProcess(cmd, 1, "", "error: net not found")

    monkeypatch.setattr(system_status.subprocess, "run", fake_run)
    info = system_status.libvirt_network_info("ghost", uri="qemu:///system")
    assert info["state"] == "unavailable"
    assert "net not found" in info["error"]


def test_libvirt_leases_parses_table(monkeypatch):
    leases_text = (
        " Expiry Time           MAC address          Protocol  IP address          Hostname    Client ID\n"
        "-----------------------------------------------------------------------------------------------\n"
        " 2026-05-19 09:00:00   52:54:00:8c:4a:01    ipv4      10.42.0.21/24       atk-kali-01 *\n"
        " 2026-05-19 11:30:00   52:54:00:8c:4a:11    ipv4      10.42.0.34/24       tgt-web-01  *\n"
    )

    def fake_run(cmd, **_):
        import subprocess

        return subprocess.CompletedProcess(cmd, 0, leases_text, "")

    monkeypatch.setattr(system_status.subprocess, "run", fake_run)
    leases = system_status.libvirt_leases("astinuslab0", uri="qemu:///system")
    assert len(leases) == 2
    assert leases[0]["host"] == "atk-kali-01"
    assert leases[0]["ip"] == "10.42.0.21"
    assert leases[0]["mac"] == "52:54:00:8c:4a:01"
    assert leases[1]["ip"] == "10.42.0.34"


def test_libvirt_leases_returns_empty_on_error(monkeypatch):
    def fake_run(cmd, **_):
        import subprocess

        return subprocess.CompletedProcess(cmd, 1, "", "error")

    monkeypatch.setattr(system_status.subprocess, "run", fake_run)
    assert system_status.libvirt_leases("ghost", uri="qemu:///system") == []
