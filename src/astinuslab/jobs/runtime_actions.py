from __future__ import annotations

import time
from datetime import UTC


def refresh_runtime(
    session_scope,
    repo_cls,
    runtime_vm_name,
    read_vm_power_state,
    parse_guest_ip,
    sync_runtime_record,
    job_id: str,
    role: str,
) -> dict | None:
    vm_name = runtime_vm_name(job_id, role)
    power_state = read_vm_power_state(vm_name)
    ip_address: str | None = None
    state = (
        "stopped"
        if power_state == "off"
        else "running"
        if power_state == "on"
        else "paused"
        if power_state == "paused"
        else "destroyed"
        if power_state == "unknown"
        else "unknown"
    )
    if power_state == "on":
        virtual_id = job_id if role == "target" else f"{job_id}-attacker"
        try:
            ip_address = parse_guest_ip(virtual_id)
        except Exception:
            ip_address = None
    sync_runtime_record(
        job_id,
        role,
        vm_name=vm_name,
        ip_address=ip_address,
        state=state,
        power_state=power_state if power_state != "unknown" else "off",
        capture_memory_enabled=(role == "target"),
        capture_disk_enabled=(role == "target"),
    )
    with session_scope() as session:
        repo = repo_cls(session)
        runtime = repo.get_runtime(job_id, role)
        if runtime is None:
            return None
        if role == "target":
            job = repo.get_job(job_id)
            # Update guest_ip unconditionally (non-status field)
            repo.update_job(job_id, guest_ip=ip_address)
            if job and job.status == "waiting_for_capture":
                if power_state == "on":
                    repo.update_job(job_id, current_stage="waiting_for_capture")
                elif power_state == "unknown":
                    from datetime import datetime

                    repo.transition_job_status(
                        job_id,
                        "failed",
                        error_summary="Target VM destroyed externally",
                        finished_at=datetime.now(UTC),
                    )
                    repo.add_event(job_id, "failed", "Target VM destroyed externally while waiting for capture")
                else:
                    repo.update_job(job_id, current_stage="target_stopped")
        else:
            repo.update_job(job_id, attacker_ip=ip_address, attacker_vm_name=vm_name)

        # A defined, powered-off target still owns its disk and reservation.
        # Keep it available for offline capture or explicit cancellation; power
        # state alone is not a request to end the Job or discard its evidence.

        return {
            "role": runtime.role,
            "vm_name": runtime.vm_name,
            "ip_address": runtime.ip_address,
            "state": runtime.state,
            "power_state": runtime.power_state,
            "snapshot_name": runtime.snapshot_name,
            "capture_memory_enabled": runtime.capture_memory_enabled,
            "capture_disk_enabled": runtime.capture_disk_enabled,
        }


def control_runtime(
    session_scope,
    repo_cls,
    settings,
    run_command,
    refresh_runtime_fn,
    job_id: str,
    role: str,
    action: str,
    *,
    vm_name: str | None = None,
) -> bool:
    # Callers that already resolved the Job's runtime record pass its name, so
    # the executed command targets the recorded identity rather than a rebuilt one.
    vm_name = vm_name or (f"astinuslab-{job_id}" if role == "target" else f"astinuslab-{job_id}-attacker")
    cmd_map = {
        "shutdown": ["virsh", "-c", settings.libvirt_uri, "shutdown", vm_name],
        # WARNING: 'pause' and 'resume' disabled due to Time Drift and forense inconsistency risks. Use external snapshots instead.
        "pause": None,
        "resume": None,
        "reboot": ["virsh", "-c", settings.libvirt_uri, "reboot", vm_name],
    }
    cmd = cmd_map.get(action)
    if cmd is None:
        raise ValueError(f"Unsupported runtime action: {action}")
    run_command(cmd, job_id=job_id)
    time.sleep(2)
    runtime = refresh_runtime_fn(job_id, role)
    with session_scope() as session:
        repo = repo_cls(session)
        repo.add_event(job_id, "runtime_control", f"{role} VM action executed: {action}")
        if role == "target" and runtime is not None:
            if runtime["power_state"] == "off":
                repo.add_event(
                    job_id,
                    "runtime_control",
                    "Target VM is now powered off; memory capture is unavailable until it is resumed by the orchestration flow",
                )
            elif runtime["power_state"] == "on":
                repo.add_event(job_id, "runtime_control", "Target VM is running again")
    return True
