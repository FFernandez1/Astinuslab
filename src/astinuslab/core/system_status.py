"""Read-only probes used by the Settings page.

* ``service_status`` — PID-file + ``/proc`` introspection.
* ``libvirt_network_info`` / ``libvirt_leases`` — added in Task 4.

All probes return plain dicts and never raise on missing data: settings UI
should degrade gracefully when libvirt or daemons are not running.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)


def _proc_exists(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def _proc_uptime_s(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # Field 22 is starttime in clock ticks since boot. Process name (field
        # 2) can contain spaces inside parens — split on the last ')'.
        after = stat.rsplit(")", 1)[1].split()
        # After the ')', field 3 (state) is index 0; starttime is field 22 → index 19.
        starttime_ticks = int(after[19])
        clk_tck = os.sysconf("SC_CLK_TCK")
        boot_time = time.clock_gettime(time.CLOCK_BOOTTIME)
        proc_age = boot_time - (starttime_ticks / clk_tck)
        return max(int(proc_age), 0)
    except (OSError, IndexError, ValueError) as exc:
        logger.debug("uptime probe failed for pid=%d: %s", pid, exc)
        return None


def service_status(*, label: str, unit: str, description: str, pid_file: Path) -> dict[str, Any]:
    pid: int | None = None
    try:
        if pid_file.exists():
            pid = int(pid_file.read_text().strip())
    except (OSError, ValueError) as exc:
        logger.debug("pid file %s unreadable: %s", pid_file, exc)
        pid = None

    running = pid is not None and _proc_exists(pid)
    uptime = _proc_uptime_s(pid) if running and pid is not None else None
    return {
        "label": label,
        "unit": unit,
        "description": description,
        "state": "running" if running else "stopped",
        "pid": pid,
        "uptime_s": uptime,
    }


def fake_mode_status() -> dict[str, Any]:
    """Report test-double state for the Settings page."""
    from .fake_mode import fake_mode_active, fake_mode_banner, fake_mode_flags

    return {
        "active": fake_mode_active(),
        "flags": fake_mode_flags(),
        "banner": fake_mode_banner(),
    }


def _virsh(cmd: list[str], *, uri: str, timeout: int = 5) -> subprocess.CompletedProcess[str]:
    argv = ["virsh", "-c", uri, *cmd]
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        # virsh not installed (e.g. CI/dev without libvirt) or hung: surface a
        # non-zero result so callers degrade to "unavailable" instead of raising.
        return subprocess.CompletedProcess(argv, returncode=1, stdout="", stderr=str(exc))


def libvirt_network_info(name: str, *, uri: str) -> dict[str, Any]:
    info = _virsh(["net-info", name], uri=uri)
    if info.returncode != 0:
        return {"state": "unavailable", "error": (info.stderr or info.stdout or "").strip()}
    dump = _virsh(["net-dumpxml", name], uri=uri)
    if dump.returncode != 0:
        return {"state": "unavailable", "error": (dump.stderr or dump.stdout or "").strip()}

    fields: dict[str, str] = {}
    for line in info.stdout.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip().lower()] = value.strip()

    root = ET.fromstring(dump.stdout)  # noqa: S314  # parses local virsh net-dumpxml output
    forward_el = root.find("forward")
    forward = forward_el.get("mode", "none") if forward_el is not None else "isolated"
    bridge_el = root.find("bridge")
    bridge = bridge_el.get("name", "") if bridge_el is not None else fields.get("bridge", "")
    ip_el = root.find("ip")
    if ip_el is not None:
        gateway = ip_el.get("address", "")
        netmask = ip_el.get("netmask", "")
        try:
            net = ipaddress.IPv4Network(f"{gateway}/{netmask}", strict=False)
            subnet = str(net)
        except (ValueError, ipaddress.NetmaskValueError):
            subnet = ""
    else:
        gateway = subnet = ""

    return {
        "state": "active" if fields.get("active", "no") == "yes" else "inactive",
        "autostart": fields.get("autostart", "no"),
        "forward": forward,
        "bridge": bridge,
        "subnet": subnet,
        "gateway": gateway,
    }


def libvirt_leases(name: str, *, uri: str, limit: int = 50) -> list[dict[str, Any]]:
    result = _virsh(["net-dhcp-leases", name], uri=uri)
    if result.returncode != 0:
        return []
    leases: list[dict[str, Any]] = []
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if not line or line.startswith("Expiry") or set(line) <= {"-"}:
            continue
        parts = line.split()
        # Format: <date> <time> <mac> <protocol> <ip/cidr> <host> <client_id...>
        if len(parts) < 6:
            continue
        ip_field = parts[4].split("/", 1)[0]
        leases.append(
            {
                "host": parts[5],
                "ip": ip_field,
                "mac": parts[2],
            }
        )
        if len(leases) >= limit:
            break
    return leases


def format_uptime(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, _ = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, _ = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"
