from __future__ import annotations

from pathlib import Path

from ..images.image_inspection import require_reusable_windows_image

WINDOWS_DISK_GB = 40


def resolve_target_execution_context(
    target_by_id_fn,
    scenario_by_id_fn,
    job_target_id: str,
    job_scenario_id: str | None,
) -> tuple[dict, dict | None, str, bool]:
    target = target_by_id_fn(job_target_id)
    scenario = scenario_by_id_fn(job_scenario_id) if job_scenario_id else None
    guest_user = target.get("default_username", "ubuntu")
    keep_failed_vm = bool(target.get("keep_failed_vm", False))
    return target, scenario, guest_user, keep_failed_vm


def prepare_boot_context(
    target: dict,
    settings,
    job_image_path: str | None,
    vm_vcpus: int | None,
    vm_memory_mb: int | None,
    vm_disk_gb: int | None,
) -> tuple[Path, int | None, int | None, int | None]:
    images_dir = Path(settings.images_dir).resolve()
    if job_image_path is not None:
        candidate = Path(job_image_path).resolve()
        try:
            candidate.relative_to(images_dir)
        except ValueError as exc:
            raise ValueError(
                f"image_path must be inside the configured images_dir ({images_dir}); got {candidate}"
            ) from exc
        image_path = candidate
    else:
        image_path = images_dir / str(target["image_ref"])

    effective_vcpus = vm_vcpus or target.get("vcpu")
    effective_memory_mb = vm_memory_mb or target.get("memory_mb")
    effective_disk_gb: int | None
    if target.get("platform") == "windows":
        if target.get("family") != "windows" or target.get("reusable") is not True:
            raise ValueError("Windows target is not a validated reusable image")
        effective_disk_gb = vm_disk_gb or WINDOWS_DISK_GB
        if effective_disk_gb > WINDOWS_DISK_GB:
            raise ValueError("Windows target disk cannot exceed 40 GiB")
        image_path = require_reusable_windows_image(image_path, allowed_dir=images_dir)
    else:
        effective_disk_gb = vm_disk_gb or target.get("disk_gb")
    return image_path, effective_vcpus, effective_memory_mb, effective_disk_gb


def prepare_live_session_boot_context(
    target: dict,
    settings,
    job_image_path: str | None,
    vm_vcpus: int | None,
    vm_memory_mb: int | None,
    vm_disk_gb: int | None,
) -> tuple[Path, int | None, int | None, int | None]:
    image_path = Path(job_image_path) if job_image_path else settings.images_dir / str(target["image_ref"])
    effective_vcpus = vm_vcpus or target.get("vcpu")
    effective_memory_mb = vm_memory_mb or target.get("memory_mb")
    effective_disk_gb = vm_disk_gb or target.get("disk_gb")
    return image_path, effective_vcpus, effective_memory_mb, effective_disk_gb


def run_post_boot_script(
    session_scope,
    repo_cls,
    get_active_key_paths,
    run_command,
    is_cancel_requested,
    settings,
    job_id: str,
    guest_ip: str,
    guest_user: str,
    scenario: dict,
    custom_script: str | None,
) -> Path | None:
    script_config = scenario.get("script", {})
    run_script = False
    script_label = ""
    script_path: Path | None = None
    timeout_s = 300
    tmp_script_path: Path | None = None

    if custom_script and custom_script.strip():
        tmp_script_path = settings.storage_dir / "tmp" / f"{job_id}-custom.sh"
        tmp_script_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_script_path.write_text(custom_script, encoding="utf-8")
        tmp_script_path.chmod(0o755)
        script_path = tmp_script_path
        timeout_s = int(script_config.get("timeout_seconds", 300))
        script_label = f"custom script ({len(custom_script.splitlines())} lines)"
        run_script = True
    elif script_config.get("enabled") and script_config.get("path"):
        script_path = settings.base_dir / script_config["path"]
        timeout_s = int(script_config.get("timeout_seconds", 300))
        script_label = script_config["path"]
        run_script = True

    if not (run_script and script_path):
        return tmp_script_path

    try:
        with session_scope() as session:
            repo = repo_cls(session)
            repo.transition_job_status(job_id, "running_scenario_script")
            repo.add_event(job_id, "running_scenario_script", f"Running script: {script_label}")

        if is_cancel_requested(job_id):
            raise RuntimeError("cancel_requested")

        ssh_priv, _ = get_active_key_paths()
        run_command(
            [
                "bash",
                str(settings.base_dir / "ops" / "host" / "run_guest_script.sh"),
                guest_ip,
                str(ssh_priv),
                str(script_path),
                str(timeout_s),
            ],
            cwd=settings.base_dir,
            job_id=job_id,
            extra_env={"GUEST_USER": guest_user},
        )

        with session_scope() as session:
            repo = repo_cls(session)
            repo.add_event(job_id, "running_scenario_script", "Script completed")
            repo.transition_job_status(job_id, "waiting_for_capture")
            repo.add_event(job_id, "waiting_for_capture", "Scenario script complete; ready for capture")

        return tmp_script_path
    except Exception:
        if tmp_script_path and tmp_script_path.exists():
            tmp_script_path.unlink(missing_ok=True)
        raise
