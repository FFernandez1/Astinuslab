from __future__ import annotations

import subprocess
import time
from pathlib import Path


def runtime_vm_name(job_id: str, role: str) -> str:
    return f"astinuslab-{job_id}" if role == "target" else f"astinuslab-{job_id}-attacker"


def sync_runtime_record(
    session_scope,
    repo_cls,
    job_id: str,
    role: str,
    *,
    vm_name: str | None = None,
    ip_address: str | None = None,
    state: str | None = None,
    power_state: str | None = None,
    snapshot_name: str | None = None,
    capture_memory_enabled: bool | None = None,
    capture_disk_enabled: bool | None = None,
) -> None:
    runtime_name = vm_name or runtime_vm_name(job_id, role)
    fields: dict[str, object] = {}
    if ip_address is not None:
        fields["ip_address"] = ip_address
    if state is not None:
        fields["state"] = state
    if power_state is not None:
        fields["power_state"] = power_state
    if snapshot_name is not None:
        fields["snapshot_name"] = snapshot_name
    if capture_memory_enabled is not None:
        fields["capture_memory_enabled"] = capture_memory_enabled
    if capture_disk_enabled is not None:
        fields["capture_disk_enabled"] = capture_disk_enabled
    with session_scope() as session:
        repo = repo_cls(session)
        repo.upsert_runtime(job_id, role, runtime_name, **fields)


def mark_runtime_destroyed(session_scope, repo_cls, job_id: str, role: str) -> None:
    with session_scope() as session:
        repo = repo_cls(session)
        job = repo.get_job(job_id)
        repo.upsert_runtime(
            job_id,
            role,
            runtime_vm_name(job_id, role),
            state="destroyed",
            power_state="off",
            ip_address=None,
            snapshot_name=None if role == "attacker" else (job.lab_snapshot_name if job else None),
            capture_memory_enabled=(role == "target"),
            capture_disk_enabled=(role == "target"),
        )


def mark_runtime_teardown_failed(session_scope, repo_cls, job_id: str, role: str) -> None:
    with session_scope() as session:
        repo = repo_cls(session)
        repo.upsert_runtime(
            job_id,
            role,
            runtime_vm_name(job_id, role),
            state="teardown_failed",
            power_state="unknown",
        )


def read_vm_power_state(run_command, settings, vm_name: str) -> str:
    try:
        output = run_command(["virsh", "-c", settings.libvirt_uri, "domstate", vm_name])
    except Exception:
        return "unknown"
    normalized = output.strip().lower()
    if normalized in {"running", "idle", "blocked", "in shutdown"}:
        return "on"
    if normalized in {"shut off", "crashed", "pmsuspended"}:
        return "off"
    return normalized or "unknown"


def _parse_dhcp_lease_candidates(leases: str, mac: str) -> list[str]:
    candidates: list[str] = []
    normalized_mac = mac.lower()
    for line in leases.splitlines():
        if normalized_mac in line.lower():
            for token in line.split():
                if "/" in token and not token.startswith("-"):
                    candidates.append(token)
    return candidates


def _parse_virsh_domifaddr_candidates(domifaddr: str, mac: str) -> list[str]:
    candidates: list[str] = []
    normalized_mac = mac.lower()
    for line in domifaddr.splitlines():
        lowered = line.lower()
        if normalized_mac in lowered or "ipv4" in lowered:
            for token in line.split():
                if "/" in token and "." in token:
                    candidates.append(token)
    return candidates


def _parse_qga_ip_candidates(agent_out: str) -> list[str]:
    candidates: list[str] = []
    for line in agent_out.splitlines():
        lowered = line.lower()
        if "ipv4" in lowered:
            for token in line.split():
                if "/" in token and "." in token:
                    ip = token.split("/")[0]
                    if ip.startswith("127.") or ip.startswith("169.254."):
                        continue
                    candidates.append(ip)
    return candidates


def _build_guest_ip_error(
    *,
    vm_state: str,
    libvirt_uri: str,
    libvirt_network: str,
    mac: str,
    domiflist_detail: str,
    last_leases: str,
    last_domifaddr: str,
    last_domifaddr_err: str,
    serial_tail: str,
) -> str:
    detail_lines = [
        f"VM state: {vm_state}",
        f"Libvirt URI: {libvirt_uri}",
        f"Libvirt network: {libvirt_network}",
        f"Detected MAC: {mac}",
        "",
        "Domain interfaces:",
        domiflist_detail or "(empty)",
        "",
        "DHCP leases (final poll):",
        last_leases or "(empty)",
        "",
        "domifaddr (final poll):",
        last_domifaddr or "(empty)",
    ]
    if last_domifaddr_err:
        detail_lines.extend(["", f"domifaddr stderr: {last_domifaddr_err}"])
    if serial_tail:
        detail_lines.extend(["", "Serial console (last 60 lines):", serial_tail])
    return "Could not resolve guest IP from libvirt DHCP/domifaddr\n" + "\n".join(detail_lines)


def parse_guest_ip(
    job_id: str,
    settings,
    run_command,
    *,
    libvirt_uri: str | None = None,
    libvirt_network: str | None = None,
    subprocess_module=subprocess,
    time_module=time,
    path_cls=Path,
    logger=None,
) -> str:
    """Resolve the guest IP by matching the VM's MAC address in DHCP leases."""
    vm_name = f"astinuslab-{job_id}"
    libvirt_uri = libvirt_uri or settings.libvirt_uri
    libvirt_network = libvirt_network or settings.libvirt_network

    def _capture_command(cmd: list[str], quiet_fail: bool = False) -> tuple[int, str, str]:
        try:
            return 0, run_command(cmd, job_id=job_id, quiet_fail=quiet_fail), ""
        except subprocess_module.CalledProcessError as exc:
            stdout = (exc.output or "").strip()
            stderr = (exc.stderr or "").strip()
            return exc.returncode, stdout, stderr
        except AssertionError:
            result = subprocess_module.run(
                cmd,
                text=True,
                capture_output=True,
            )
            return result.returncode, result.stdout.strip(), result.stderr.strip()

    mac_out = run_command(["virsh", "-c", libvirt_uri, "domiflist", vm_name], job_id=job_id)
    mac: str | None = None
    for line in mac_out.splitlines():
        parts = line.split()
        if len(parts) == 5 and ":" in parts[4]:
            mac = parts[4].lower()
            break
    if mac is None:
        raise RuntimeError(f"Could not obtain MAC address for VM {vm_name}")

    if logger is not None:
        logger.info("[%s] guest MAC detected: %s", job_id, mac)

    last_leases = ""
    last_domifaddr = ""
    last_domifaddr_err = ""
    for attempt in range(36):
        _, leases, _ = _capture_command(
            ["virsh", "-c", libvirt_uri, "net-dhcp-leases", libvirt_network],
            quiet_fail=True,
        )
        last_leases = leases
        dhcp_candidates = _parse_dhcp_lease_candidates(leases, mac)
        if dhcp_candidates:
            if logger is not None:
                logger.info(
                    "[%s] guest IP resolved from DHCP lease on attempt %d: %s",
                    job_id,
                    attempt + 1,
                    dhcp_candidates[0],
                )
            return dhcp_candidates[0].split("/")[0]

        domifaddr_rc, domifaddr, domifaddr_err = _capture_command(
            ["virsh", "-c", libvirt_uri, "domifaddr", vm_name],
            quiet_fail=True,
        )
        last_domifaddr = domifaddr
        last_domifaddr_err = domifaddr_err
        if domifaddr_rc != 0:
            domifaddr = ""
        domifaddr_candidates = _parse_virsh_domifaddr_candidates(domifaddr, mac)
        if domifaddr_candidates:
            if logger is not None:
                logger.info(
                    "[%s] guest IP resolved from domifaddr on attempt %d: %s",
                    job_id,
                    attempt + 1,
                    domifaddr_candidates[0],
                )
            return domifaddr_candidates[0].split("/")[0]

        agent_rc, agent_out, _agent_err = _capture_command(
            ["virsh", "-c", libvirt_uri, "domifaddr", vm_name, "--source", "agent"],
            quiet_fail=True,
        )
        if agent_rc == 0:
            agent_candidates = _parse_qga_ip_candidates(agent_out)
            if agent_candidates:
                if logger is not None:
                    logger.info(
                        "[%s] guest IP resolved from guest-agent on attempt %d: %s",
                        job_id,
                        attempt + 1,
                        agent_candidates[0],
                    )
                return agent_candidates[0]

        if attempt < 35:
            time_module.sleep(min(1.5**attempt, 10))

    vm_state = "unknown"
    try:
        vm_state = subprocess_module.check_output(
            ["virsh", "-c", libvirt_uri, "domstate", vm_name],
            text=True,
            stderr=subprocess_module.DEVNULL,
        ).strip()
    except Exception:
        pass

    domiflist_detail = mac_out.strip()
    iface_rc, iface_detail, iface_err = _capture_command(["virsh", "-c", libvirt_uri, "domiflist", vm_name])
    if iface_rc == 0 and iface_detail:
        domiflist_detail = iface_detail
    elif iface_err:
        domiflist_detail = f"(domiflist failed: {iface_err})"

    serial_log_path = path_cls(settings.serial_log_dir / f"astinuslab-{job_id}-serial.log")
    serial_tail = ""
    if serial_log_path.exists():
        try:
            lines = serial_log_path.read_text(errors="replace").splitlines()
            serial_tail = "\n".join(lines[-60:])
        except Exception:
            pass

    if logger is not None:
        logger.warning(
            "[%s] unable to resolve guest IP after polling DHCP/domifaddr on network %s;"
            " vm_state=%s; mac=%s; last leases output: %s; last domifaddr: %s; domifaddr_err=%s",
            job_id,
            libvirt_network,
            vm_state,
            mac,
            last_leases,
            last_domifaddr,
            last_domifaddr_err,
        )
        if serial_tail:
            logger.warning("[%s] serial console tail:\n%s", job_id, serial_tail)

    error = _build_guest_ip_error(
        vm_state=vm_state,
        libvirt_uri=libvirt_uri,
        libvirt_network=libvirt_network,
        mac=mac,
        domiflist_detail=domiflist_detail,
        last_leases=last_leases,
        last_domifaddr=last_domifaddr,
        last_domifaddr_err=last_domifaddr_err,
        serial_tail=serial_tail,
    )
    raise RuntimeError(error)
