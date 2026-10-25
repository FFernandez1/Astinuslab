import subprocess
from types import SimpleNamespace

from astinuslab.jobs.runtime_support import parse_guest_ip

_DOMIFLIST = (
    " Interface  Type     Source   Model    MAC\n"
    "------------------------------------------------------\n"
    " vnet0      network  default  virtio   52:54:00:aa:bb:cc\n"
)


def _make_run_command(*, agent_returns_ip: bool, lease_empty: bool = True, default_domifaddr_empty: bool = True):
    """Build a fake run_command and a call log."""
    calls: list[list[str]] = []

    def fake(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[3] == "domiflist":
            return _DOMIFLIST
        if cmd[3] == "net-dhcp-leases":
            return "" if lease_empty else "fake lease 52:54:00:aa:bb:cc 10.0.0.99/24"
        if cmd[3] == "domifaddr":
            if "--source" in cmd and cmd[cmd.index("--source") + 1] == "agent":
                if agent_returns_ip:
                    return (
                        " Name       MAC address        Protocol     Address\n"
                        " enp1s0     52:54:00:aa:bb:cc  ipv4         10.0.0.42/24\n"
                    )
                return ""
            return "" if default_domifaddr_empty else " enp1s0 ipv4 10.0.0.50/24"
        if cmd[3] == "domstate":
            return "running"
        return ""

    return fake, calls


def _settings():
    return SimpleNamespace(libvirt_uri="qemu:///system", libvirt_network="default")


def _time_stub():
    return SimpleNamespace(sleep=lambda _s: None)


def test_resolves_via_agent_when_lease_and_domifaddr_empty():
    run_command, calls = _make_run_command(agent_returns_ip=True)
    ip = parse_guest_ip(
        "job-1",
        _settings(),
        run_command,
        subprocess_module=subprocess,
        time_module=_time_stub(),
    )
    assert ip == "10.0.0.42"
    # Confirm the agent source was actually queried
    assert any("--source" in c and "agent" in c for c in calls)


def test_does_not_consult_agent_when_lease_already_resolves():
    run_command, calls = _make_run_command(agent_returns_ip=True, lease_empty=False)
    ip = parse_guest_ip(
        "job-2",
        _settings(),
        run_command,
        subprocess_module=subprocess,
        time_module=_time_stub(),
    )
    assert ip == "10.0.0.99"
    # First attempt resolved via lease; agent not consulted
    assert not any("--source" in c and "agent" in c for c in calls)
