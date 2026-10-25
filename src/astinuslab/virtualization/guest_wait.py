from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time
from importlib import import_module
from pathlib import Path
from typing import IO, cast


def wait_for_guest_ssh(
    is_cancel_requested,
    logger,
    subprocess_module,
    time_module,
    job_id: str,
    guest_ip: str,
    ssh_key: Path,
    guest_user: str,
    *,
    max_wait_seconds: int = 600,
    poll_interval_seconds: int = 5,
    known_hosts_path: Path | None = None,
    guest_platform: str = "linux",
) -> None:
    """Poll until the guest accepts SSH connections with the injected key."""
    if guest_platform not in {"linux", "windows"}:
        raise ValueError("unsupported guest platform for SSH readiness")
    if guest_platform == "windows" and known_hosts_path is None:
        raise ValueError("Windows SSH readiness requires verified known_hosts")
    probe = (
        'powershell.exe -NoLogo -NoProfile -NonInteractive -Command "exit 0"' if guest_platform == "windows" else "true"
    )
    deadline = time_module.monotonic() + max_wait_seconds
    ssh_cmd = [
        "ssh",
        "-i",
        str(ssh_key),
        "-o",
        f"StrictHostKeyChecking={'yes' if known_hosts_path is not None else 'no'}",
        "-o",
        f"UserKnownHostsFile={known_hosts_path if known_hosts_path is not None else '/dev/null'}",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "BatchMode=yes",
        # Linux cloud guests use the injected key. A distro's GSSAPI default
        # can stall before key authentication and exhaust every readiness probe.
        *(["-o", "PreferredAuthentications=publickey"] if guest_platform == "linux" else []),
        f"{guest_user}@{guest_ip}",
        probe,
    ]
    last_error = ""
    attempt = 0

    while time_module.monotonic() < deadline:
        if is_cancel_requested(job_id):
            raise RuntimeError("cancel_requested")

        try:
            result = subprocess_module.run(ssh_cmd, text=True, capture_output=True, timeout=15)
            if result.returncode == 0:
                logger.info("[%s] guest SSH is ready at %s", job_id, guest_ip)
                return
            last_error = (result.stderr or result.stdout or "").strip()
        except subprocess.TimeoutExpired:
            last_error = "ssh command timed out"

        logger.info("[%s] guest SSH not ready yet at %s: %s", job_id, guest_ip, last_error)
        time_module.sleep(min(1.5**attempt, poll_interval_seconds))
        attempt += 1

    hint = (
        "Hint: check QGA bootstrap completion, sshd startup, the injected public key and its Windows ACLs, "
        "and the verified SSH host key."
        if guest_platform == "windows"
        else "Hint: if this target uses a stock cloud image that was not prepared for AstinusLab, cloud-init does not "
        "apply the NoCloud seed and the SSH key is never installed -- use a prepared image "
        "(see the Images documentation)."
    )
    raise SSHTimeoutError(
        f"Guest SSH not available after {max_wait_seconds}s at {guest_ip}. Last error: {last_error or 'no details'}. "
        + hint
    )


class SSHHostKeyMismatchError(RuntimeError):
    """The guest host key did not match the QGA bootstrap result."""


def record_verified_ssh_host_key(
    guest_ip: str,
    known_hosts_path: Path,
    expected_fingerprint: str,
    *,
    subprocess_module=subprocess,
) -> None:
    """Record an ssh-keyscan result only after matching the QGA fingerprint."""
    result = subprocess_module.run(
        ["ssh-keyscan", "-4", "-T", "10", "-t", "ed25519", guest_ip],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    matching_lines: list[str] = []
    for line in (result.stdout or "").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 3 or parts[1] != "ssh-ed25519":
            continue
        try:
            key_bytes = __import__("base64").b64decode(parts[2], validate=True)
            fingerprint = "SHA256:" + __import__("base64").b64encode(
                __import__("hashlib").sha256(key_bytes).digest()
            ).decode().rstrip("=")
        except (ValueError, TypeError):
            continue
        if fingerprint == expected_fingerprint:
            matching_lines.append(line)
    if not matching_lines:
        raise SSHHostKeyMismatchError(f"SSH host key mismatch for {guest_ip}")
    known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
    known_hosts_path.write_text("".join(f"{line}\n" for line in matching_lines), encoding="utf-8")
    known_hosts_path.chmod(0o600)


def _run_subprocess(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Wrapper around subprocess.run to isolate shell execution for tests."""
    max_output_bytes = kwargs.pop("_max_output_bytes", None)
    if max_output_bytes is not None:
        return _run_bounded_subprocess(cmd, max_output_bytes=max_output_bytes, **kwargs)
    return subprocess.run(cmd, **kwargs)


def _run_bounded_subprocess(
    cmd: list[str], *, max_output_bytes: int, timeout: float, check: bool, **kwargs
) -> subprocess.CompletedProcess:
    """Run a small host query without unbounded stdout/stderr buffering."""

    if kwargs.pop("capture_output", False) is not True or kwargs.pop("text", False) is not True:
        raise ValueError("bounded subprocesses require capture_output=True and text=True")
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        **kwargs,
    )
    stdout = process.stdout
    stderr = process.stderr
    if stdout is None or stderr is None:
        process.kill()
        process.wait()
        raise RuntimeError("bounded subprocess pipes were not created")
    selector = selectors.DefaultSelector()
    streams: dict[IO[bytes], bytearray] = {stdout: bytearray(), stderr: bytearray()}
    total = 0
    deadline = time.monotonic() + timeout
    try:
        for stream in streams:
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd, timeout)
            events = selector.select(remaining)
            if not events:
                raise subprocess.TimeoutExpired(cmd, timeout)
            for key, _ in events:
                stream = cast(IO[bytes], key.fileobj)
                chunk = os.read(stream.fileno(), 8192)
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                total += len(chunk)
                if total > max_output_bytes:
                    raise RuntimeError("QEMU Guest Agent response exceeded its output limit")
                streams[stream].extend(chunk)
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Preserve the original failure even if host I/O delays SIGKILL.
            pass
        raise
    finally:
        try:
            selector.close()
        finally:
            stdout.close()
            stderr.close()
    result = subprocess.CompletedProcess(
        cmd,
        process.returncode,
        stdout=bytes(streams[stdout]).decode("utf-8", errors="replace"),
        stderr=bytes(streams[stderr]).decode("utf-8", errors="replace"),
    )
    if check and result.returncode:
        raise subprocess.CalledProcessError(result.returncode, cmd, output=result.stdout, stderr=result.stderr)
    return result


class SSHTimeoutError(RuntimeError):
    """Raised when guest SSH never becomes reachable before the deadline."""


MAX_QGA_RESPONSE_BYTES = 256 * 1024


def _run_qga_command(libvirt_uri: str, vm_name: str, payload: dict, timeout: int = 5) -> str:
    """Run one bounded QGA command via virsh.

    The QEMU Guest Agent's own timeout is not a host-process deadline: a
    wedged ``virsh`` could otherwise keep an authored session frozen forever.
    Keep the response bounded as well; normal QGA metadata/file responses are
    far below this limit and guest observations enforce their stricter 64 KiB
    payload cap separately.
    """
    cmd = ["virsh", "-c", libvirt_uri, "qemu-agent-command", vm_name, "--timeout", str(timeout), json.dumps(payload)]
    result = _run_subprocess(
        cmd,
        capture_output=True,
        text=True,
        check=True,
        timeout=max(1, timeout),
        _max_output_bytes=MAX_QGA_RESPONSE_BYTES,
    )
    output = result.stdout or ""
    if len(output.encode("utf-8", errors="replace")) > MAX_QGA_RESPONSE_BYTES:
        raise RuntimeError("QEMU Guest Agent response exceeded its output limit")
    return output


def wait_for_guest_agent(
    is_cancel_requested,
    logger,
    job_id: str,
    vm_name: str,
    libvirt_uri: str,
    *,
    max_wait_seconds: int = 300,
    poll_interval_seconds: int = 5,
    time_module=time,
) -> None:
    deadline = time_module.monotonic() + max_wait_seconds
    last_error = ""
    start_time = time_module.monotonic()

    logger.info("[%s] waiting for guest agent to become ready (max %ds)...", job_id, max_wait_seconds)

    while time_module.monotonic() < deadline:
        if is_cancel_requested(job_id):
            raise RuntimeError("cancel_requested")

        try:
            _run_qga_command(libvirt_uri, vm_name, {"execute": "guest-ping"})
            elapsed = time_module.monotonic() - start_time
            logger.info("[%s] guest agent is ready after %.1fs", job_id, elapsed)
            return
        except Exception as exc:
            if not _should_retry_guest_agent_error(exc):
                logger.error("[%s] permanent guest agent error: %s", job_id, exc)
                raise
            last_error = str(exc)
            elapsed = time_module.monotonic() - start_time
            logger.info("[%s] guest agent not ready yet (%.1fs elapsed): %s", job_id, elapsed, exc)
            time_module.sleep(poll_interval_seconds)

    raise RuntimeError(f"guest_agent_not_ready_after_{max_wait_seconds}s: {last_error}")


def verify_guest_disk_capacity(
    logger,
    session_scope,
    repo_cls,
    job_id: str,
    vm_name: str,
    libvirt_uri: str,
    vm_disk_gb: int,
) -> None:
    """Verify the guest root partition reflects the requested disk size.

    Best-effort check via QGA. Logs a warning and records a job event if the
    visible root capacity is below ~85% of the requested size; never raises.
    """
    try:
        fs_info_raw = _run_qga_command(libvirt_uri, vm_name, {"execute": "guest-get-fsinfo"})
        fs_info = json.loads(fs_info_raw).get("return", [])
        root_fs = next((fs for fs in fs_info if fs.get("mountpoint") == "/"), None)
        if root_fs:
            total_gb = root_fs.get("total-bytes", 0) / (1024**3)
            if total_gb < (vm_disk_gb * 0.85):
                logger.warning(
                    "[%s] Disk size verification warning: guest root partition is %.1fGB, expected ~%dGB. "
                    "Cloud-init 'growpart' might have failed.",
                    job_id,
                    total_gb,
                    vm_disk_gb,
                )
                with session_scope() as session:
                    repo = repo_cls(session)
                    repo.add_event(
                        job_id,
                        "provisioning_workload",
                        f"Warning: Guest root partition is only {total_gb:.1f}GB (requested {vm_disk_gb}GB). "
                        "Workload installation might fail due to lack of space.",
                    )
    except Exception as exc:
        logger.debug("[%s] Could not verify guest disk size via QGA: %s", job_id, exc)


def collect_guest_metadata_via_qga(vm_name: str, libvirt_uri: str) -> dict[str, str | None]:
    _run_qga_command(libvirt_uri, vm_name, {"execute": "guest-info"})
    output = _run_qga_command(libvirt_uri, vm_name, {"execute": "guest-get-osinfo"})
    osinfo = json.loads(output)["return"]

    distro_id = _normalize_distro_id(osinfo.get("id"))
    distro_version = _normalize_optional_text(osinfo.get("version-id"))
    kernel_release = _normalize_optional_text(osinfo.get("kernel-release"))
    arch = _normalize_optional_text(osinfo.get("machine"))
    return {
        "distro_id": distro_id,
        "distro_version": distro_version,
        "kernel_release": kernel_release,
        "arch": arch,
    }


def collect_guest_metadata_via_ssh(
    guest_ip: str,
    ssh_key: Path,
    guest_user: str,
    detect_script_path: Path,
) -> dict[str, str | None]:
    ssh_cmd = [
        "ssh",
        "-i",
        str(ssh_key),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "BatchMode=yes",
        f"{guest_user}@{guest_ip}",
        "bash -s -- json",
    ]
    with detect_script_path.open("r", encoding="utf-8") as script:
        result = _run_subprocess(
            ssh_cmd,
            stdin=script,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    raw = json.loads(result.stdout)
    return {
        "distro_id": _normalize_distro_id(raw.get("distro_id")),
        "distro_version": _normalize_optional_text(raw.get("distro_version")),
        "kernel_release": _normalize_optional_text(raw.get("kernel_release")),
        "arch": _normalize_optional_text(raw.get("arch")),
    }


def _normalize_optional_text(value, *, lower: bool = False) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.lower() in {"none", "null"}:
        return None
    return text.lower() if lower else text


def _normalize_distro_id(value) -> str | None:
    text = _normalize_optional_text(value, lower=True)
    if text is None:
        return None
    aliases = {
        "oraclelinux": "ol",
        "oracle": "ol",
        "redhat": "rhel",
        "red hat enterprise linux": "rhel",
        "opensuse leap": "opensuse-leap",
        "opensuse tumbleweed": "opensuse-tumbleweed",
        "arch linux": "arch",
    }
    return aliases.get(text.replace("_", "-"), text.replace("_", "-"))


def _should_retry_guest_agent_error(exc: Exception) -> bool:
    # If it's a subprocess error (from virsh), check if it's a transient failure
    if isinstance(exc, subprocess.CalledProcessError):
        message = (exc.stderr or "").lower()
        # "Guest agent is not responded" or similar are retryable
        return not _is_permanent_guest_agent_error_message(message)

    # Legacy libvirt error check
    libvirt_error_type = _get_libvirt_error_type()
    if libvirt_error_type is not None:
        if isinstance(exc, libvirt_error_type):
            return not _is_permanent_guest_agent_error_message(str(exc).lower())

    return False


def _get_libvirt_error_type():
    try:
        libvirt_module = import_module("libvirt")
    except Exception:
        return None
    return getattr(libvirt_module, "libvirtError", None)


def _is_permanent_guest_agent_error_message(message: str) -> bool:
    return any(
        marker in message
        for marker in (
            "invalid argument",
            "permission denied",
            "unsupported",
            "unknown command",
            "not found",
            "malformed",
            "syntax",
            "bad request",
        )
    )
