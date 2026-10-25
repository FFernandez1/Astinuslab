from __future__ import annotations

import io
import json
import os
import select
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from .host_helpers import libvirt_storage_admin_helper

# --- Bounds -----------------------------------------------------------------
#
# A workload is untrusted and a disk image copy is slow; neither may be allowed
# to run forever or to fill the worker's memory with its own output.

# Kept per stream, in memory, for diagnosis. The tail is what explains a failure;
# the head of a runaway log almost never does.
MAX_STREAM_TAIL_BYTES = 256 * 1024
# Hard ceiling across both streams. Past this the process group is killed: a
# command producing this much output is malfunctioning, not working.
MAX_TOTAL_OUTPUT_BYTES = 64 * 1024 * 1024
_REAP_TIMEOUT_SECONDS = 5.0
_DRAIN_TIMEOUT_SECONDS = 10.0

# Timeout classes. Deliberately not one universal value - an ACL helper that
# takes a minute is broken, while a disk image copy that takes an hour is
# normal, and a single number that suits both suits neither.
DEFAULT_TIMEOUT_SECONDS = 900.0
_TIMEOUT_CLASSES: tuple[tuple[tuple[str, ...], float], ...] = (
    # Privileged helpers: local, small, and fast.
    (("fix_libvirt_path_acl", "libvirt_storage_admin", "network_isolation_admin"), 120.0),
    # Quick host queries.
    (("sha256sum", "getfacl", "setfacl", "id", "systemctl", "virsh", "pgrep", "stat"), 300.0),
    # Guest interaction over SSH.
    (("run_guest_script", "mount_output_share_remote", "build_vol3_symbol_remote", "ssh", "scp"), 3600.0),
    # Image preparation and conversion.
    (("qemu-img", "rebuild_cloud_image", "bake_workload", "prepare_target_image", "prebake_attacker_image"), 7200.0),
    # Disk acquisition: bounded only by how large the guest's disk is.
    (("acquire_disk_host", "acquire_disk_remote", "publish_disk_to_host"), 21600.0),
)


# --- Redaction --------------------------------------------------------------
#
# A command line reaches three places an operator can read: the log, the
# exception message, and HostAction.error_summary, which the UI renders. None
# of them should have to be trusted with a credential. Nothing in the product
# currently passes key material on a command line — SSH is given a key *path* —
# so this is a standing guard rather than a repair, and it is kept narrow on
# purpose: an over-redacted line no longer explains the failure it was written
# to explain.

_SECRET_FLAGS = frozenset(
    {
        "--password",
        "--passphrase",
        "--token",
        "--secret",
        "--api-key",
        "--apikey",
        "--auth-token",
        "--admin-token",
    }
)
# `-i` names a private key for these, and something else everywhere else
# (`qemu-img convert -i` is an index).
_SSH_FAMILY = frozenset({"ssh", "scp", "sftp", "ssh-keyscan", "rsync"})
_REDACTED = "<redacted>"


def _redact_key_path(value: str) -> str:
    """Keep which key was used; drop where the operator keeps it."""
    path = PurePosixPath(value)
    label = "<ssh_keys>" if "ssh_keys" in path.parts else "<key>"
    return f"{label}/{path.name}"


def redact_argv(cmd: list[str]) -> list[str]:
    """A copy of *cmd* safe to log, with credential-shaped values replaced."""
    if not cmd:
        return []
    program = PurePosixPath(cmd[0]).name
    ssh_like = program in _SSH_FAMILY
    out: list[str] = []
    index = 0
    while index < len(cmd):
        arg = cmd[index]
        lowered = arg.lower()
        head, sep, _ = arg.partition("=")
        if sep and head.lower() in _SECRET_FLAGS:
            out.append(f"{head}={_REDACTED}")
        elif lowered in _SECRET_FLAGS and index + 1 < len(cmd):
            out.append(arg)
            out.append(_REDACTED)
            index += 1
        elif ssh_like and arg == "-i" and index + 1 < len(cmd):
            out.append(arg)
            out.append(_redact_key_path(cmd[index + 1]))
            index += 1
        else:
            out.append(arg)
        index += 1
    return out


def redacted_command(cmd: list[str]) -> str:
    """*cmd* rendered as one line, safe to log or put in an exception message."""
    return " ".join(redact_argv(cmd))


def _timeout_executable(cmd: list[str]) -> str:
    """Find the executable or shell-script operand, never its data arguments."""
    index = 0
    while index < len(cmd):
        program = PurePosixPath(cmd[index]).name
        index += 1
        if program == "sudo":
            while index < len(cmd):
                option = cmd[index]
                if option == "--":
                    index += 1
                    break
                if option in {"-n", "-E", "-H", "--non-interactive", "--preserve-env", "--set-home"}:
                    index += 1
                elif option in {"-u", "-g", "--user", "--group"}:
                    index += 2
                elif option.startswith(("--user=", "--group=")) or (
                    option.startswith(("-u", "-g")) and len(option) > 2
                ):
                    index += 1
                elif option.startswith("-"):
                    # Unknown wrapper syntax: retain the default deadline.
                    return ""
                else:
                    break
            continue
        if program in {"bash", "sh"}:
            while index < len(cmd):
                option = cmd[index]
                if option == "--":
                    index += 1
                    break
                if option in {"-o", "-O"}:
                    index += 2
                elif option.startswith("-"):
                    # In particular, -c/-lc execute inline code and -s reads
                    # stdin. Neither makes a later argument a script operand.
                    if not option[1:] or any(flag not in "euxvfn" for flag in option[1:]):
                        return ""
                    index += 1
                else:
                    break
            return PurePosixPath(cmd[index]).name if index < len(cmd) else ""
        return program
    return ""


def timeout_for(cmd: list[str]) -> float:
    """Choose a deadline from the command identity, including known wrappers."""
    program = _timeout_executable(cmd)
    script = program.removesuffix(".sh")
    for names, seconds in _TIMEOUT_CLASSES:
        if any(
            program == name or ("_" in name and (script == name or script.startswith(name + "_"))) for name in names
        ):
            return seconds
    return DEFAULT_TIMEOUT_SECONDS


class _BoundedStream:
    """Keeps the last N bytes of a stream and counts everything that went past."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._buffer = bytearray()
        self.total_bytes = 0
        self.truncated = False

    def feed(self, chunk: bytes) -> None:
        self.total_bytes += len(chunk)
        self._buffer += chunk
        if len(self._buffer) > self._limit:
            del self._buffer[: len(self._buffer) - self._limit]
            self.truncated = True

    def text(self) -> str:
        # A byte tail can start mid-character; replacement is correct here and
        # raising on a diagnostic string would hide the actual failure.
        return self._buffer.decode("utf-8", errors="replace")


def job_pid_dir(storage_dir: Path, job_id: str) -> Path:
    """Return the per-job subprocess-group registry directory.

    The registry contains one empty file per live subprocess group leader. Files
    are normally removed when run_command exits; stale files can remain after a
    worker crash and are cleaned up by kill_job_subprocesses on the next kill.
    """
    return storage_dir / "tmp" / "job-pids" / job_id


PID_REGISTRY_SCHEMA = "job-process/v1"


def read_process_identity(pid: int) -> dict[str, object] | None:
    """Identity of a live process, or None when it is gone or unreadable.

    A PID on its own identifies nothing durable: the kernel recycles numbers, so
    a record written before a crash may name a process that has since been
    replaced by something unrelated. Start time is what makes the pair unique -
    two processes can share a PID, but not a PID and the tick they started on.
    """
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None
    # The comm field is parenthesised and may itself contain spaces or brackets,
    # so fields are counted from after the last ')'.
    tail = stat_text.rpartition(")")[2].split()
    if len(tail) < 20:
        return None
    try:
        start_time_ticks = int(tail[19])
    except ValueError:
        return None
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        pgid = None
    executable = ""
    try:
        executable = os.readlink(f"/proc/{pid}/exe")
    except (OSError, ValueError):
        # Unreadable for a process owned by another user; the start time still
        # distinguishes it, so this is recorded empty rather than treated as fatal.
        executable = ""
    return {"pid": pid, "pgid": pgid, "start_time_ticks": start_time_ticks, "expected_executable": executable}


def verify_process_identity(record: dict[str, object]) -> bool:
    """True only when the live process still matches everything the record claims."""
    pid = record.get("pid")
    if not isinstance(pid, int):
        return False
    current = read_process_identity(pid)
    if current is None:
        return False
    if current["start_time_ticks"] != record.get("start_time_ticks"):
        return False
    recorded_pgid = record.get("pgid")
    if recorded_pgid is not None and current["pgid"] != recorded_pgid:
        return False
    recorded_exe = record.get("expected_executable") or ""
    current_exe = current["expected_executable"] or ""
    if recorded_exe and current_exe and recorded_exe != current_exe:
        return False
    return True


def register_job_pid(storage_dir: Path, job_id: str, pid: int, *, job_id_action: int | None = None) -> None:
    """Record a subprocess group leader with enough identity to verify it later."""
    pid_dir = job_pid_dir(storage_dir, job_id)
    pid_dir.mkdir(parents=True, exist_ok=True)
    identity = read_process_identity(pid) or {
        "pid": pid,
        "pgid": None,
        "start_time_ticks": None,
        "expected_executable": "",
    }
    record = {
        "schema_version": PID_REGISTRY_SCHEMA,
        **identity,
        "job_id": job_id,
        "action_id": job_id_action,
    }
    path = pid_dir / str(pid)
    tmp = pid_dir / f".{pid}.tmp"
    tmp.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def unregister_job_pid(storage_dir: Path, job_id: str, pid: int) -> None:
    pid_dir = job_pid_dir(storage_dir, job_id)
    try:
        (pid_dir / str(pid)).unlink()
    except OSError:
        pass
    try:
        pid_dir.rmdir()
    except OSError:
        pass


def set_action_pid(session_scope, repo_cls, action_by_job: dict[str, int], job_id: str | None, pid: int | None) -> None:
    if not job_id:
        return
    with session_scope() as session:
        repo = repo_cls(session)
        action_id = action_by_job.get(job_id)
        if action_id is not None:
            repo.update_action(action_id, active_pid=pid)


def run_command(
    logger,
    session_scope,
    repo_cls,
    action_by_job: dict[str, int],
    job_processes: dict[str, set[subprocess.Popen]],
    process_lock,
    cmd: list[str],
    *,
    cwd: Path | None = None,
    job_id: str | None = None,
    extra_env: dict[str, str] | None = None,
    timeout: float | None = None,
    is_cancel_requested_fn: Callable[[str], bool] | None = None,
    quiet_fail: bool = False,
    storage_dir: Path | None = None,
) -> str:
    env = {**os.environ, **(extra_env or {})}
    if timeout is None:
        timeout = timeout_for(cmd)
    logger.debug("[%s] run: %s (timeout=%s)", job_id or "-", redacted_command(cmd), timeout)
    out_sink = _BoundedStream(MAX_STREAM_TAIL_BYTES)
    err_sink = _BoundedStream(MAX_STREAM_TAIL_BYTES)
    stop_reading = threading.Event()
    readers = []
    read_errors: list[Exception] = []
    primary_error: BaseException | None = None
    cleanup_error: BaseException | None = None
    exit_reason = "normal"

    def cleanup_failed(operation: str, exc: BaseException) -> None:
        nonlocal cleanup_error
        if cleanup_error is None:
            cleanup_error = exc
        logger.warning("[%s] subprocess pid=%d %s failed: %s", job_id or "-", process.pid, operation, exc)

    def drain(stream, sink: _BoundedStream, fd: int | None) -> None:
        try:
            while not stop_reading.is_set():
                if fd is None:
                    chunk = stream.read(65536)  # In-memory test adapters only.
                else:
                    if not select.select([fd], [], [], 0.05)[0]:
                        continue
                    try:
                        chunk = os.read(fd, 65536)
                    except BlockingIOError:
                        continue
                if not chunk:
                    break
                sink.feed(chunk)
            else:
                read_errors.append(RuntimeError("subprocess pipe drain deadline expired before EOF"))
        except (ValueError, OSError) as exc:
            read_errors.append(exc)

    # Binary pipes: the tail is bounded in bytes, and decoding is deferred so a
    # cut in the middle of a multibyte character cannot raise mid-capture.
    process = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        if job_id:
            with process_lock:
                job_processes.setdefault(job_id, set()).add(process)
            set_action_pid(session_scope, repo_cls, action_by_job, job_id, process.pid)
            if storage_dir is not None:
                try:
                    register_job_pid(storage_dir, job_id, process.pid)
                except OSError as exc:
                    logger.warning("[%s] Failed to register subprocess PID %d: %s", job_id, process.pid, exc)
        for stream, sink in ((process.stdout, out_sink), (process.stderr, err_sink)):
            if stream is None:
                continue
            fd = None if isinstance(stream, io.BytesIO) else stream.fileno()
            if fd is not None:
                os.set_blocking(fd, False)
            reader = threading.Thread(target=drain, args=(stream, sink, fd), daemon=True)
            readers.append((reader, stream))
            reader.start()
        deadline = (time.monotonic() + timeout) if timeout else None
        while process.poll() is None:
            if out_sink.total_bytes + err_sink.total_bytes > MAX_TOTAL_OUTPUT_BYTES:
                exit_reason = "output_limit"
                break
            if deadline and time.monotonic() > deadline:
                exit_reason = "timeout"
                break
            if is_cancel_requested_fn and job_id and is_cancel_requested_fn(job_id):
                exit_reason = "cancelled"
                logger.info("[%s] cancel detected — terminating subprocess pid=%d", job_id, process.pid)
                break
            time.sleep(0.5)
    except BaseException as exc:
        primary_error = exc
    finally:
        hard_kill = exit_reason in {"timeout", "output_limit"}
        try:
            if primary_error is not None or exit_reason != "normal" or process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL if hard_kill else signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except OSError as exc:
                    cleanup_failed("signal", exc)
            try:
                process.wait(timeout=_REAP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                if hard_kill:
                    raise
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError as exc:
                    cleanup_failed("SIGKILL", exc)
                process.wait(timeout=_REAP_TIMEOUT_SECONDS)
        except BaseException as exc:
            cleanup_failed("bounded reap (cleanup incomplete)", exc)

        drain_deadline = time.monotonic() + _DRAIN_TIMEOUT_SECONDS
        # Reserve a small part of the shared budget for cooperative shutdown.
        stop_margin = min(0.1, _DRAIN_TIMEOUT_SECONDS / 2)
        for reader, _stream in readers:
            if reader.ident is not None:
                try:
                    reader.join(timeout=max(0, drain_deadline - time.monotonic() - stop_margin))
                except Exception as exc:
                    cleanup_failed("reader join", exc)
        stop_reading.set()
        for reader, _stream in readers:
            if reader.ident is not None:
                try:
                    reader.join(timeout=max(0, drain_deadline - time.monotonic()))
                except Exception as exc:
                    cleanup_failed("reader shutdown", exc)
        for read_error in read_errors:
            cleanup_failed("pipe read", read_error)
        for stream in (process.stdout, process.stderr):
            if any(reader.is_alive() and owned is stream for reader, owned in readers):
                cleanup_failed("pipe drain", RuntimeError("subprocess pipe cleanup incomplete"))
                continue
            if stream is not None:
                try:
                    stream.close()
                except Exception as exc:
                    cleanup_failed("pipe close", exc)
        if job_id:
            try:
                with process_lock:
                    processes = job_processes.get(job_id)
                    if processes is not None:
                        processes.discard(process)
                        if not processes:
                            job_processes.pop(job_id, None)
            except Exception as exc:
                cleanup_failed("local registry clear", exc)
            if storage_dir is not None:
                try:
                    unregister_job_pid(storage_dir, job_id, process.pid)
                except Exception as exc:
                    cleanup_failed("PID registry clear", exc)
            try:
                set_action_pid(session_scope, repo_cls, action_by_job, job_id, None)
            except Exception as exc:
                cleanup_failed("action PID clear", exc)

    if primary_error is not None:
        raise primary_error

    stdout = out_sink.text()
    stderr = err_sink.text()
    if out_sink.truncated or err_sink.truncated:
        # Say what was dropped rather than logging an untrusted stream in full.
        logger.warning(
            "[%s] output truncated to the last %d bytes per stream (stdout=%d bytes, stderr=%d bytes): %s",
            job_id or "-",
            MAX_STREAM_TAIL_BYTES,
            out_sink.total_bytes,
            err_sink.total_bytes,
            cmd[0] if cmd else "-",
        )

    if exit_reason == "output_limit":
        total = out_sink.total_bytes + err_sink.total_bytes
        logger.error(
            "[%s] command killed after producing %d bytes of output: %s",
            job_id or "-",
            total,
            redacted_command(cmd),
        )
        raise RuntimeError(
            f"Command exceeded the output limit of {MAX_TOTAL_OUTPUT_BYTES} bytes "
            f"({total} bytes produced): {redacted_command(cmd)}"
        )
    if exit_reason == "timeout":
        logger.error("[%s] command timed out after %ss: %s", job_id or "-", timeout, redacted_command(cmd))
        raise RuntimeError(f"Command timed out after {timeout}s: {redacted_command(cmd)}\nstderr:\n{stderr}")
    if exit_reason == "cancelled":
        raise RuntimeError("cancel_requested")
    if process.returncode != 0:
        if process.returncode is None and cleanup_error is not None:
            raise cleanup_error
        if not quiet_fail:
            logger.error(
                "[%s] command failed (rc=%d): %s\nstderr: %s",
                job_id or "-",
                process.returncode,
                redacted_command(cmd),
                stderr,
            )
        raise subprocess.CalledProcessError(process.returncode, cmd, output=stdout, stderr=stderr)
    if cleanup_error is not None:
        raise cleanup_error
    logger.debug("[%s] command ok: %s", job_id or "-", redacted_command(cmd))
    return stdout.strip()


def require_noninteractive_sudo(run_command_fn, settings) -> None:
    """Verify the storage helper can be invoked via passwordless sudo."""
    helper = libvirt_storage_admin_helper()
    try:
        run_command_fn(["sudo", "-n", str(helper), "noop"])
    except subprocess.CalledProcessError as exc:
        detail_raw = exc.stderr or exc.output or ""
        if isinstance(detail_raw, bytes):
            detail = detail_raw.decode(errors="replace").strip()
        else:
            detail = str(detail_raw).strip()
        raise RuntimeError(
            "HOST NOT PROVISIONED: the host runner requires passwordless sudo for the libvirt storage helper at:\n"
            f"  {helper}\n"
            "\n"
            'If the detail says "sudo: a password is required", the AstinusLab host has not been provisioned.\n'
            "\n"
            "To install the required sudoers rule, run ONE of:\n"
            "  sudo bash ops/deploy/install.sh\n"
            "  sudo bash ops/host/install_sudoers_astinuslab.sh\n"
            "\n"
            f"Detail: {detail}"
        ) from exc
