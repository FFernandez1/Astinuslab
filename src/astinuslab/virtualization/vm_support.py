from __future__ import annotations

import shutil
import subprocess
import time
from importlib import import_module
from pathlib import Path

from ..core.config import settings
from .host_helpers import libvirt_storage_admin_helper
from .process_support import redacted_command
from .uefi import assert_runtime_directory_unowned


def start_virtiofsd(logger, job_id: str, socket_path: Path, share_dir: Path, pid_file_path: Path) -> int:
    """Start virtiofsd in the background and persist its PID before returning.

    Writing the PID file inside this function (rather than in the caller) closes a
    narrow race: if the external write_text were to fail, virtiofsd would already
    be running but teardown_vm would have nothing to find via virtiofsd.pid,
    leaking the process on the host.
    """
    binary = "/usr/libexec/virtiofsd"
    if not Path(binary).exists():
        binary = "/usr/lib/qemu/virtiofsd"

    cmd = [
        binary,
        "--socket-path",
        str(socket_path),
        "--shared-dir",
        str(share_dir),
    ]
    logger.info("[%s] starting virtiofsd: %s", job_id, redacted_command(cmd))
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    # Wait for the socket to be created (up to 5 seconds)
    for _ in range(50):
        if socket_path.exists():
            socket_path.chmod(0o777)
            try:
                pid_file_path.write_text(str(process.pid))
            except Exception as exc:
                _ = exc
                process.terminate()
                raise
            return process.pid
        time.sleep(0.1)

    # If it failed to create the socket, kill it and raise
    process.terminate()
    raise RuntimeError(f"virtiofsd failed to create socket at {socket_path}")


def open_libvirt_connection(libvirt_uri: str):
    libvirt = import_module("libvirt")

    def _libvirt_dummy_error_handler(ctx, err):
        pass

    libvirt.registerErrorHandler(f=_libvirt_dummy_error_handler, ctx=None)
    conn = libvirt.open(libvirt_uri)
    if conn is None:
        raise RuntimeError(f"Could not open libvirt connection: {libvirt_uri}")
    return conn


def teardown_vm(
    run_command,
    logger,
    settings,
    mark_runtime_destroyed,
    job_id: str,
    mark_runtime_teardown_failed=None,
) -> bool:
    """Destroy and undefine the job VM + remove its storage dir. Errors are non-fatal."""
    helper = libvirt_storage_admin_helper()
    teardown_succeeded = False
    try:
        from .vm_lifecycle import VMLifecycle

        libvirt = import_module("libvirt")
        conn = libvirt.open(settings.libvirt_uri)
        if conn is None:
            raise RuntimeError(f"Could not open libvirt connection: {settings.libvirt_uri}")
        try:
            VMLifecycle(run_command=run_command, libvirt_module=libvirt).destroy(conn, f"astinuslab-{job_id}")
            assert_runtime_directory_unowned(conn, settings.libvirt_image_base_dir / job_id)
        finally:
            close_conn = getattr(conn, "close", None)
            if callable(close_conn):
                close_conn()
        run_command(["sudo", "-n", str(helper), "remove_job_dir", f"{settings.libvirt_image_base_dir}/{job_id}"])
        vm_names = run_command(["virsh", "-c", settings.libvirt_uri, "list", "--all", "--name"]).splitlines()
        if not any(name.strip().startswith("astinuslab-") for name in vm_names):
            run_command(["sudo", "-n", str(helper), "reset_default_network"])
        logger.info("[%s] VM torn down", job_id)
        teardown_succeeded = True
    except Exception as exc:
        logger.warning("[%s] VM teardown failed (non-fatal): %s", job_id, exc)
    role = "attacker" if job_id.endswith("-attacker") else "target"
    canonical_job_id = job_id[:-9] if role == "attacker" else job_id
    if teardown_succeeded:
        mark_runtime_destroyed(canonical_job_id, role)
    elif mark_runtime_teardown_failed is not None:
        mark_runtime_teardown_failed(canonical_job_id, role)

    if not teardown_succeeded:
        # A domain may still own its share and diagnostics; retain both.
        return False

    vfs_pid_path = settings.storage_dir / "tmp" / job_id / "virtiofsd.pid"
    if vfs_pid_path.exists():
        try:
            vfs_pid = int(vfs_pid_path.read_text().strip())
            import os
            import signal

            os.kill(vfs_pid, signal.SIGTERM)
            logger.info("[%s] virtiofsd (pid=%d) terminated", job_id, vfs_pid)
        except Exception as exc:
            logger.warning("[%s] failed to terminate virtiofsd: %s", job_id, exc)

    if not getattr(settings, "keep_failed_vm", False):
        serial_log = settings.serial_log_dir / f"astinuslab-{job_id}-serial.log"
        try:
            serial_log.unlink(missing_ok=True)
        except Exception:
            pass

    if teardown_succeeded and not getattr(settings, "keep_failed_vm", False):
        tmp_root = settings.storage_dir / "tmp"
        job_tmp_dir = tmp_root / job_id
        try:
            tmp_root_resolved = tmp_root.resolve()
            job_tmp_resolved = job_tmp_dir.resolve()
            if tmp_root_resolved not in job_tmp_resolved.parents:
                logger.warning("[%s] refusing to remove tmp dir outside storage/tmp: %s", job_id, job_tmp_dir)
                return teardown_succeeded
            shutil.rmtree(job_tmp_dir, ignore_errors=False)
            logger.info("[%s] removed tmp dir: %s", job_id, job_tmp_dir)
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("[%s] failed to remove tmp dir: %s", job_id, exc)
    return teardown_succeeded


def check_host_disk_space(logger, settings, job_id: str, required_gb: int = 6) -> None:
    """Raise RuntimeError if the storage directory has less than required_gb free."""
    storage_dir = settings.storage_dir
    storage_dir.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(storage_dir)
    free_gb = usage.free / (1024**3)
    logger.info("[%s] host disk free: %.1f GB (required: %d GB)", job_id, free_gb, required_gb)
    if free_gb < required_gb:
        raise RuntimeError(
            f"Insufficient disk space on host: {free_gb:.1f} GB free, {required_gb} GB required "
            f"(directory: {storage_dir})"
        )


def target_by_id(load_targets, target_id: str) -> dict:
    for target in load_targets():
        if target["id"] == target_id:
            return target
    raise ValueError(f"Target not found: {target_id}")


def scenario_by_id(load_scenarios, scenario_id: str) -> dict:
    for scenario in load_scenarios():
        if scenario["id"] == scenario_id:
            return scenario
    raise ValueError(f"Scenario not found: {scenario_id}")


def job_disk_image_path(job_id: str, target_id: str) -> Path:
    return settings.libvirt_image_base_dir / job_id / f"{target_id}-job.qcow2"
