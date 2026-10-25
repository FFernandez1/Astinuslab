from __future__ import annotations

import json
import subprocess
import time
from datetime import UTC, datetime


def mark_action_running(
    session_scope, repo_cls, action_by_job: dict[str, int], job_id: str, action_id: int | None
) -> None:
    if action_id is None:
        return
    action_by_job[job_id] = action_id
    with session_scope() as session:
        repo = repo_cls(session)
        repo.update_action(action_id, status="running", started_at=datetime.now(UTC))


def complete_action(session_scope, repo_cls, action_id: int | None) -> None:
    if action_id is None:
        return
    with session_scope() as session:
        repo = repo_cls(session)
        repo.update_action(
            action_id,
            status="completed",
            finished_at=datetime.now(UTC),
            active_pid=None,
        )


def mark_job_preparing_image(session_scope, repo_cls, job_id: str) -> None:
    with session_scope() as session:
        repo = repo_cls(session)
        repo.transition_job_status(
            job_id,
            "preparing_image",
            started_at=datetime.now(UTC),
        )
        repo.add_event(job_id, "preparing_image", "Preparing image and VM")


def handle_exception(session_scope, repo_cls, logger, exc: Exception, job_id: str, action_id: int | None) -> None:
    logger.error("[%s] exception during orchestration: %s", job_id, exc)
    if str(exc) == "cancel_requested":
        with session_scope() as session:
            repo = repo_cls(session)
            repo.transition_job_status(
                job_id,
                "cancelled",
                finished_at=datetime.now(UTC),
            )
            repo.add_event(job_id, "cancelled", "Job cancelled")
            if action_id is not None:
                repo.update_action(action_id, status="cancelled", finished_at=datetime.now(UTC), active_pid=None)
        return
    if isinstance(exc, subprocess.CalledProcessError) and exc.returncode == -15:
        with session_scope() as session:
            repo = repo_cls(session)
            repo.transition_job_status(
                job_id,
                "cancelled",
                finished_at=datetime.now(UTC),
            )
            repo.add_event(job_id, "cancelled", "Job interrupted by cancellation")
            if action_id is not None:
                repo.update_action(action_id, status="cancelled", finished_at=datetime.now(UTC), active_pid=None)
        return
    with session_scope() as session:
        repo = repo_cls(session)
        message = str(exc)
        if isinstance(exc, subprocess.CalledProcessError):
            stderr = (exc.stderr or "").strip()
            stdout = (exc.output or "").strip()
            message = f"{message}\nstderr:\n{stderr}\nstdout:\n{stdout}".strip()

        lower_msg = message.lower()
        friendly_hint = ""
        if "no space left on device" in lower_msg or "enospc" in lower_msg:
            friendly_hint = (
                "HINT: The guest VM ran out of disk space. This often happens when installing "
                "large workloads on a base image with a small root partition. "
                "Try increasing 'disk_gb' in the Target configuration (catalog/targets.yaml).\n\n"
            )
        elif "curly braces is no longer supported" in lower_msg:
            friendly_hint = (
                "HINT: PHP Fatal Error: Your workload script uses deprecated curly-brace syntax "
                "which is not supported by the PHP version on the guest VM. "
                "Update the script to use square brackets [] for string offsets.\n\n"
            )
        elif "can't connect to local mysql server through socket" in lower_msg:
            friendly_hint = (
                "HINT: MySQL Connection Error: The database service might not have started correctly. "
                "Check the 'install.sh' script to ensure 'systemctl start mysql' was called and wait for it to be ready.\n\n"
            )
        elif "could not get lock /var/lib/dpkg/lock-frontend" in lower_msg:
            friendly_hint = (
                "HINT: APT Lock Error: Another process is currently using the package manager (possibly unattended-upgrades). "
                "Consider adding a retry loop or 'wait' for the lock in your installation script.\n\n"
            )
        elif "bad permissions" in lower_msg and "ssh_keys" in lower_msg:
            friendly_hint = (
                "HINT: SSH Key Permission Error: The private key has unsafe permissions. "
                "AstinusLab requires strict 0600 permissions for private keys. Run './astinuslab.sh setup' to fix.\n\n"
            )
        elif "apt-get: command not found" in lower_msg or "dnf: command not found" in lower_msg:
            friendly_hint = (
                "HINT: Workload/package-manager mismatch: the workload script tried to use a package manager "
                "that is not available on the selected guest distro. Check the workload compatibility matrix "
                "and the target distro metadata before retrying.\n\n"
            )
        elif "is incompatible with detected guest" in lower_msg:
            friendly_hint = (
                "HINT: Workload compatibility rejected this target after guest metadata detection. "
                "The target image may be mislabeled, or the workload compatibility matrix needs a more precise rule.\n\n"
            )

        if friendly_hint:
            message = friendly_hint + message

        repo.transition_job_status(
            job_id,
            "failed",
            error_summary=message,
            finished_at=datetime.now(UTC),
        )
        repo.add_event(job_id, "failed", f"Orchestration failed: {message}")
        if action_id is not None:
            repo.update_action(
                action_id, status="failed", finished_at=datetime.now(UTC), error_summary=message, active_pid=None
            )


def create_lab_snapshot(
    session_scope,
    repo_cls,
    run_command,
    sync_runtime_record,
    settings,
    logger,
    job_id: str,
    *,
    description: str = "Clean lab state",
    event_state: str = "waiting_for_capture",
    event_message: str = "Lab snapshot created: lab-clean",
) -> str:
    snapshot_name = "lab-clean"
    vm_name = f"astinuslab-{job_id}"
    with session_scope() as session:
        repo = repo_cls(session)
        job = repo.get_job(job_id)
        if job and job.lab_snapshot_name:
            return job.lab_snapshot_name
    try:
        run_command(
            [
                "virsh",
                "-c",
                settings.libvirt_uri,
                "snapshot-create-as",
                vm_name,
                snapshot_name,
                "--disk-only",
                "--atomic",
                "--no-metadata",
            ],
            job_id=job_id,
        )
    except subprocess.CalledProcessError as exc:
        logger.warning(
            "[%s] external snapshot creation failed (non-fatal); reset will not be available. Error: %s", job_id, exc
        )
        return ""

    with session_scope() as session:
        repo = repo_cls(session)
        repo.update_job(job_id, lab_snapshot_name=snapshot_name)
        repo.add_event(job_id, event_state, event_message)
    sync_runtime_record(job_id, "target", snapshot_name=snapshot_name)

    logger.info("[%s] lab snapshot created: %s", job_id, snapshot_name)
    return snapshot_name


def revert_lab_snapshot(
    session_scope,
    repo_cls,
    run_command,
    sync_runtime_record,
    settings,
    logger,
    job_id: str,
) -> bool:
    with session_scope() as session:
        repo = repo_cls(session)
        job = repo.get_job(job_id)
        if job is None:
            return False
        snapshot_name = job.lab_snapshot_name
        if not snapshot_name:
            return False

    vm_name = f"astinuslab-{job_id}"
    try:
        # Destroy VM to release lock on overlay
        run_command(["virsh", "-c", settings.libvirt_uri, "destroy", vm_name], job_id=job_id)

        # Identify active disk and its backing file
        blklist = run_command(["virsh", "-c", settings.libvirt_uri, "domblklist", vm_name, "--details"], job_id=job_id)
        vda_path = None
        for line in blklist.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[2] == "vda":
                vda_path = parts[3]
                break

        if not vda_path or vda_path == "-":
            raise RuntimeError("Could not find vda disk path")

        info = run_command(["qemu-img", "info", "--output=json", vda_path], job_id=job_id)
        backing_file = json.loads(info).get("backing-filename")
        if not backing_file:
            raise RuntimeError("vda does not have a backing file")

        # Recreate overlay pointing to the same backing file, resetting state
        last_exc = None
        for attempt in range(3):
            try:
                run_command(
                    ["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", backing_file, vda_path], job_id=job_id
                )
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                if 'Failed to get "write" lock' in str(exc) or "Is another process using the image" in str(exc):
                    if attempt < 2:
                        time.sleep(1.0)
                        continue
                raise
        if last_exc:
            raise last_exc

        # Start VM
        run_command(["virsh", "-c", settings.libvirt_uri, "start", vm_name], job_id=job_id)
    except subprocess.CalledProcessError as exc:
        logger.error("[%s] external snapshot revert failed: %s", job_id, exc)
        return False
    except Exception as exc:
        logger.error("[%s] external snapshot revert failed: %s", job_id, exc)
        return False

    with session_scope() as session:
        repo = repo_cls(session)
        repo.add_event(job_id, "waiting_for_capture", "Lab reset: VM reverted to clean external snapshot")
    sync_runtime_record(job_id, "target", state="running", power_state="on")

    logger.info("[%s] lab snapshot reverted to: %s", job_id, snapshot_name)
    return True
