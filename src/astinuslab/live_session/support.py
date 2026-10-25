from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path


def resolve_attacker_config(attacker_by_id_fn, attacker_vm_id: str | None, workload: dict | None) -> dict | None:
    attacker_cfg: dict | None = None
    if attacker_vm_id:
        catalog_entry = attacker_by_id_fn(attacker_vm_id)
        if catalog_entry and catalog_entry.get("image_ref"):
            attacker_cfg = catalog_entry
        else:
            raise RuntimeError(f"Requested attacker configuration is unavailable: {attacker_vm_id}")
    if attacker_cfg is None and workload is not None:
        attacker_cfg = workload.get("attacker_vm") or None
    return attacker_cfg


def start_attacker_boot(
    logger,
    settings,
    boot_simple_vm,
    job_id: str,
    target: dict,
    attacker_cfg: dict | None,
    attacker_vm_vcpus: int | None,
    attacker_vm_memory_mb: int | None,
    attacker_vm_disk_gb: int | None,
) -> tuple[threading.Thread | None, dict]:
    attacker_thread_result: dict = {"ip": None, "error": None}
    attacker_thread: threading.Thread | None = None
    if not (attacker_cfg and attacker_cfg.get("image_ref")):
        raise RuntimeError("Requested attacker configuration has no image reference")

    attacker_image = settings.images_dir / attacker_cfg["image_ref"]
    if not attacker_image.is_file():
        raise RuntimeError(f"Requested attacker VM image is unavailable: {attacker_image}")

    _att_image = attacker_image
    _att_cfg = attacker_cfg

    def _run_attacker_boot() -> None:
        try:
            ip = boot_simple_vm(
                job_id,
                "attacker",
                _att_image,
                _att_cfg.get("os_variant", "generic"),
                _att_cfg.get("username", "kali"),
                target.get("network_profile"),
                vm_vcpus=attacker_vm_vcpus,
                vm_memory_mb=attacker_vm_memory_mb,
                vm_disk_gb=attacker_vm_disk_gb,
            )
            attacker_thread_result["ip"] = ip
        except Exception as exc:
            attacker_thread_result["error"] = exc

    attacker_thread = threading.Thread(target=_run_attacker_boot, daemon=True)
    attacker_thread.start()
    logger.info("[%s] attacker VM boot started in parallel with workload provisioning", job_id)
    return attacker_thread, attacker_thread_result


def finish_attacker_boot(logger, job_id: str, attacker_thread, attacker_thread_result: dict) -> tuple[bool, str | None]:
    attacker_booted = False
    attacker_ip_for_isolation: str | None = None
    if attacker_thread is None:
        return attacker_booted, attacker_ip_for_isolation

    attacker_thread.join()
    if attacker_thread_result["error"]:
        raise RuntimeError(
            f"Requested attacker VM boot failed: {attacker_thread_result['error']}"
        ) from attacker_thread_result["error"]
    if attacker_thread_result["ip"]:
        attacker_booted = True
        attacker_ip_for_isolation = attacker_thread_result["ip"]
    else:
        raise RuntimeError("Requested attacker VM boot completed without a guest IP")
    return attacker_booted, attacker_ip_for_isolation


def wait_for_capture_handoff(
    session_scope,
    repo_cls,
    complete_action,
    job_id: str,
    scenario: dict,
    action_id: int | None,
) -> bool:
    max_wait = int(scenario.get("trigger", {}).get("max_wait_seconds", 7200))

    wait_deadline = datetime.now(UTC) + timedelta(seconds=max_wait)
    with session_scope() as session:
        repo = repo_cls(session)
        repo.transition_job_status(job_id, "waiting_for_capture", capture_wait_deadline=wait_deadline)
        repo.add_event(job_id, "waiting_for_capture", f"Guest ready. Waiting for capture request (max {max_wait}s)")

    complete_action(action_id)
    return True


def resolve_live_session_workload_cache(
    logger,
    session_scope,
    repo_cls,
    load_workload_id_fn,
    workload_by_id_fn,
    check_workload_cache_fn,
    workload_cache_key_fn,
    job_id: str,
    target_id: str,
    image_path: Path,
) -> tuple[str | None, dict | None, Path | None, Path | None, str | None]:
    workload_id = load_workload_id_fn(job_id)
    if not workload_id:
        return None, None, None, None, None

    workload = workload_by_id_fn(workload_id)
    if workload is None:
        raise RuntimeError(f"Workload not found: {workload_id}")

    workload_cache_path = check_workload_cache_fn(target_id, workload_id, image_path)
    if not workload_cache_path:
        return workload_id, workload, None, None, None

    cache_key = workload_cache_key_fn(target_id, workload_id, image_path)
    boot_cache_image = workload_cache_path
    boot_cache_instance_id = f"workload-cache-{cache_key}"
    logger.info("[%s] workload cache hit — booting from %s", job_id, workload_cache_path)
    with session_scope() as session:
        repo = repo_cls(session)
        repo.add_event(job_id, "preparing_image", f"Workload cache hit: booting from snapshot ({cache_key[:8]}…)")
    return workload_id, workload, workload_cache_path, boot_cache_image, boot_cache_instance_id


def apply_post_provisioning_network_state(
    apply_network_isolation_fn,
    create_lab_snapshot_fn,
    cleanup,
    job_id: str,
    workload_id: str | None,
    workload: dict | None,
    guest_ip: str,
) -> None:
    if workload_id and workload is not None:
        if workload.get("network_isolation"):
            if apply_network_isolation_fn(job_id, guest_ip, attacker_ip=cleanup.attacker_ip_for_isolation):
                cleanup.isolated_guest_ip = guest_ip
            else:
                raise RuntimeError(
                    f"Workload {workload_id} requires network isolation but it could not be applied; "
                    "aborting lab boot (fail closed). Check the network_isolation_admin.sh helper and "
                    "its sudoers rule."
                )
    else:
        create_lab_snapshot_fn(
            job_id,
            description="Base target clean state",
            event_state="waiting_for_capture",
            event_message="Lab snapshot created after target boot: lab-clean",
        )


def finish_attacker_boot_to_cleanup(
    finish_attacker_boot_fn,
    logger,
    cleanup,
    job_id: str,
    attacker_thread,
    attacker_thread_result: dict | None,
) -> None:
    attacker_booted, attacker_ip_for_isolation = finish_attacker_boot_fn(
        logger, job_id, attacker_thread, attacker_thread_result
    )
    cleanup.attacker_booted = attacker_booted
    cleanup.attacker_ip_for_isolation = attacker_ip_for_isolation


def guard_runtime_workload_context(
    session_scope,
    repo_cls,
    runtime_workload_context_fn,
    job_id: str,
    workload_id: str | None,
    workload: dict | None,
    target: dict,
    metadata: dict[str, str | None],
) -> dict | None:
    if not (workload_id and workload is not None):
        return None
    try:
        return runtime_workload_context_fn(job_id, workload, target, metadata)
    except RuntimeError as exc:
        with session_scope() as session:
            repo = repo_cls(session)
            repo.add_event(job_id, "mounting_output_share", str(exc))
        raise


def provision_live_session_workload(
    session_scope,
    repo_cls,
    finalize_workload_provisioning_fn,
    provision_workload_fn,
    save_workload_cache_fn,
    create_lab_snapshot_fn,
    job_id: str,
    target_id: str,
    workload_id: str | None,
    workload: dict | None,
    workload_cache_path: Path | None,
    image_path: Path,
    guest_ip: str,
    guest_user: str,
    runtime_target: dict | None,
) -> None:
    if not (workload_id and workload is not None):
        return

    if workload_cache_path:
        with session_scope() as session:
            repo = repo_cls(session)
            repo.transition_job_status(job_id, "provisioning_workload")
            repo.add_event(job_id, "provisioning_workload", "Workload restored from snapshot cache — skipping install")
        finalize_workload_provisioning_fn(job_id, workload, guest_ip)
    else:
        provision_workload_fn(job_id, workload, guest_ip, guest_user, target_context=runtime_target)
        save_workload_cache_fn(job_id, target_id, workload_id, image_path, guest_ip, guest_user)

    create_lab_snapshot_fn(
        job_id,
        description="Post-workload clean state",
        event_state="provisioning_workload",
        event_message="Lab snapshot created after workload provisioning: lab-clean",
    )
