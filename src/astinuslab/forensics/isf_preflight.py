"""Host-side preflight checks for ISF extraction jobs."""

from __future__ import annotations

import shutil
import subprocess

from .vol3_tools import check_dwarf2json_tool


class PreflightError(RuntimeError):
    """Raised when a required host prerequisite is missing or unusable."""


_REQUIRED_TOOLS: list[tuple[str, str]] = [
    ("rpm2cpio", "sudo apt install rpm2cpio"),
    ("cpio", "sudo apt install cpio"),
    ("virt-customize", "sudo apt install libguestfs-tools"),
    ("qemu-img", "sudo apt install qemu-utils"),
    ("virsh", "sudo apt install libvirt-clients"),
]


def _check_tool(name: str, install_hint: str | None = None) -> str:
    path = shutil.which(name)
    if not path:
        hint = f" Install: {install_hint}" if install_hint else ""
        raise PreflightError(f"required host tool missing: {name}.{hint}")
    return path


def _check_libvirt(uri: str) -> str:
    try:
        out = subprocess.run(
            ["virsh", "-c", uri, "version", "--daemon"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise PreflightError(f"cannot connect to libvirt at {uri}. Is libvirtd running? ({exc})") from exc
    for line in out.splitlines():
        if "running" in line.lower() and ":" in line:
            return line.split(":", 1)[1].strip()
    return out.strip().splitlines()[-1] if out.strip() else "unknown"


def check_host_prerequisites(libvirt_uri: str) -> dict[str, str]:
    """Verify host has all tools required for ISF extraction.

    Returns a dict keyed by tool name.  The value is a resolved path for every
    required tool, the daemon version for ``libvirt``, and -- for ``dwarf2json``
    alone -- a readiness *code* such as ``ready`` or ``dwarf2json_hash_mismatch``,
    because that tool is reported rather than required (see below).  Raises
    PreflightError on the first missing/unusable prerequisite.
    """
    info: dict[str, str] = {}
    for tool, hint in _REQUIRED_TOOLS:
        info[tool] = _check_tool(tool, install_hint=hint)
    info["libvirt"] = _check_libvirt(libvirt_uri)
    # dwarf2json is reported, never fatal here.  An ISF-only job runs the provider
    # chain first, and its local_cache and prebuilt providers deliver a ready ISF
    # without ever invoking dwarf2json.  The remaining providers -- debuginfod and
    # host_fetch -- return a vmlinux and DO require the tool, as do the local build
    # and the live-guest transfer; every one of those paths re-verifies it
    # immediately before use and surfaces its readiness code on failure.  Blocking
    # the whole preflight would deny operators the two providers that need nothing
    # from dwarf2json.
    dwarf2json = check_dwarf2json_tool()
    info["dwarf2json"] = dwarf2json.code
    return info
