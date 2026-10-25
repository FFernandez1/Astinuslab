from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def run_workload_install(
    *,
    session_scope,
    repo_cls,
    get_active_key_paths,
    run_command,
    settings,
    check_workload_health,
    is_cancel_requested,
    job_id: str,
    workload: dict,
    guest_ip: str,
    guest_user: str,
    target_context: dict | None = None,
) -> None:
    """Run a single workload install script, then verify health."""
    install_script_rel = workload.get("install_script")
    if not install_script_rel:
        raise RuntimeError(f"Workload {workload['id']} has no install_script defined")

    script_path = settings.base_dir / install_script_rel
    if not script_path.exists():
        raise RuntimeError(f"Workload install script not found: {script_path}")

    ssh_priv, _ = get_active_key_paths()
    health_cfg = workload.get("health_check", {})
    timeout_s = int(health_cfg.get("timeout_seconds", 300)) + 300

    with session_scope() as session:
        repo = repo_cls(session)
        repo.transition_job_status(job_id, "provisioning_workload")
        repo.add_event(job_id, "provisioning_workload", f"Installing: {workload['display_name']}")

    if is_cancel_requested(job_id):
        raise RuntimeError("cancel_requested")

    # Shellcheck validation: catch syntax/style errors early
    try:
        import subprocess as _sp

        _result = _sp.run(["shellcheck", "-s", "bash", str(script_path)], capture_output=True, text=True, timeout=10)
        if _result.returncode != 0:
            # We treat shellcheck failures as warnings for now, but report them if they are fatal errors.
            # Only raise if it's a known catastrophic failure (like a syntax error that would kill bash).
            # For simplicity, we just log it and attach it to the event log.
            with session_scope() as session:
                repo = repo_cls(session)
                repo.add_event(
                    job_id,
                    "provisioning_workload",
                    f"Warning: shellcheck identified issues in {workload['display_name']} install script:\n{_result.stdout}",
                )
    except Exception:
        # Don't fail the whole install if shellcheck is missing or fails itself
        pass

    extra_env: dict[str, str] = {"GUEST_USER": guest_user}
    if target_context:
        if target_context.get("distro"):
            extra_env["TARGET_DISTRO"] = str(target_context["distro"])
        if target_context.get("version"):
            extra_env["TARGET_VERSION"] = str(target_context["version"])
        if target_context.get("family"):
            extra_env["TARGET_FAMILY"] = str(target_context["family"])
        if target_context.get("arch"):
            extra_env["TARGET_ARCH"] = str(target_context["arch"])

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
        extra_env=extra_env,
    )

    check_workload_health(job_id, workload, guest_ip)


def finalize_workload_provisioning(
    session_scope,
    repo_cls,
    job_id: str,
    workload: dict,
    guest_ip: str,
) -> None:
    """Persist the lab URL once workload provisioning is complete."""
    lab_url: str | None = None
    services = workload.get("services", [])
    if services:
        svc = services[0]
        lab_url = f"http://{guest_ip}:{svc['port']}{svc.get('url_path', '/')}"

    now = datetime.now(UTC)
    with session_scope() as session:
        repo = repo_cls(session)
        repo.update_job(
            job_id,
            workload_provisioned_at=now,
            lab_url=lab_url,
        )
        repo.add_event(
            job_id,
            "provisioning_workload",
            f"Workload ready: {workload['display_name']}",
        )


def provision_workload(
    run_workload_install,
    finalize_workload_provisioning,
    provision_stack,
    job_id: str,
    workload: dict,
    guest_ip: str,
    guest_user: str,
    target_context: dict | None = None,
) -> None:
    """Provision either a single workload or a stack."""
    if workload.get("is_stack"):
        provision_stack(job_id, workload, guest_ip, guest_user, target_context=target_context)
    else:
        run_workload_install(job_id, workload, guest_ip, guest_user, target_context=target_context)
        finalize_workload_provisioning(job_id, workload, guest_ip)


def provision_stack(
    *,
    session_scope,
    repo_cls,
    get_active_key_paths,
    run_command,
    settings,
    workload_by_id_fn,
    run_workload_install,
    finalize_workload_provisioning,
    job_id: str,
    stack: dict,
    guest_ip: str,
    guest_user: str,
    target_context: dict | None = None,
) -> None:
    """Install stack components in order, then finalize as a unit."""
    ssh_priv, _ = get_active_key_paths()
    component_ids = stack.get("components", [])
    if not component_ids:
        raise RuntimeError(f"Stack {stack['id']} has no components defined")

    with session_scope() as session:
        repo = repo_cls(session)
        repo.add_event(
            job_id,
            "provisioning_workload",
            f"Provisioning stack: {stack['display_name']} ({len(component_ids)} components)",
        )

    run_command(
        [
            "ssh",
            "-i",
            str(ssh_priv),
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "ConnectTimeout=30",
            f"{guest_user}@{guest_ip}",
            "command -v apt-get >/dev/null 2>&1 && sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq || true",
        ],
        cwd=settings.base_dir,
        job_id=job_id,
    )

    for i, comp_id in enumerate(component_ids, 1):
        comp = workload_by_id_fn(comp_id)
        if comp is None:
            raise RuntimeError(f"Stack component not found: {comp_id}")
        with session_scope() as session:
            repo = repo_cls(session)
            repo.add_event(
                job_id,
                "provisioning_workload",
                f"[{i}/{len(component_ids)}] Installing component: {comp['display_name']}",
            )
        run_workload_install(job_id, comp, guest_ip, guest_user, target_context=target_context)

    finalize_workload_provisioning(job_id, stack, guest_ip)


def boot_simple_vm(
    session_scope,
    repo_cls,
    get_active_key_paths,
    run_command,
    parse_guest_ip,
    wait_for_guest_ssh,
    sync_runtime_record,
    settings,
    logger,
    job_id: str,
    vm_suffix: str,
    image_path,
    os_variant: str,
    guest_user: str,
    network_profile: str | None = None,
    vm_vcpus: int | None = None,
    vm_memory_mb: int | None = None,
    vm_disk_gb: int | None = None,
) -> str:
    """Boot a lightweight VM and return its IP once it is reachable enough."""
    virtual_id = f"{job_id}-{vm_suffix}"
    ssh_priv, ssh_pub = get_active_key_paths()
    guest_env = {
        "GUEST_USER": guest_user,
        "LIBVIRT_URI": settings.libvirt_uri,
        "LIBVIRT_NETWORK": network_profile or settings.libvirt_network,
        "CLOUD_INIT_PROFILE": "attacker",
        "ASTINUSLAB_IMAGES_DIR": str(settings.images_dir),
    }
    if vm_vcpus:
        guest_env["VM_VCPUS"] = str(vm_vcpus)
    if vm_memory_mb:
        guest_env["VM_MEMORY_MB"] = str(vm_memory_mb)
    if vm_disk_gb:
        guest_env["VM_DISK_GB"] = str(vm_disk_gb)

    with session_scope() as session:
        repo = repo_cls(session)
        repo.add_event(job_id, "provisioning_workload", f"Booting attacker VM ({vm_suffix})")

    run_command(
        [
            "bash",
            str(settings.base_dir / "ops" / "host" / "rebuild_cloud_image_libvirt_storage.sh"),
            virtual_id,
            str(image_path),
            str(ssh_pub),
            virtual_id,
            os_variant,
            str(vm_disk_gb or 40),
        ],
        cwd=settings.base_dir,
        job_id=job_id,
        extra_env=guest_env,
    )

    attacker_ip = parse_guest_ip(
        virtual_id,
        libvirt_uri=guest_env["LIBVIRT_URI"],
        libvirt_network=guest_env["LIBVIRT_NETWORK"],
    )
    ssh_ready = True
    try:
        wait_for_guest_ssh(job_id, attacker_ip, ssh_priv, guest_user)
    except RuntimeError:
        ssh_ready = False
        logger.warning("[%s] attacker VM SSH not available (non-fatal); VM is up at %s", job_id, attacker_ip)
        with session_scope() as session:
            repo = repo_cls(session)
            repo.add_event(
                job_id,
                "provisioning_workload",
                f"Attacker VM booted at {attacker_ip}, but SSH is not accepting connections yet",
            )

    with session_scope() as session:
        repo = repo_cls(session)
        repo.update_job(job_id, attacker_vm_name=f"astinuslab-{virtual_id}", attacker_ip=attacker_ip)
        repo.add_event(job_id, "provisioning_workload", f"Attacker VM ready at {attacker_ip}")
    sync_runtime_record(
        job_id,
        "attacker",
        vm_name=f"astinuslab-{virtual_id}",
        ip_address=attacker_ip,
        state="running" if ssh_ready else "ssh_unavailable",
        power_state="on",
        capture_memory_enabled=False,
        capture_disk_enabled=False,
    )

    logger.info("[%s] attacker VM up: %s @ %s", job_id, virtual_id, attacker_ip)
    return attacker_ip


def check_workload_health(
    session_scope,
    repo_cls,
    is_cancel_requested,
    logger,
    time_module,
    job_id: str,
    workload: dict,
    guest_ip: str,
) -> None:
    """Poll the workload URL until it responds or times out."""
    health_cfg = workload.get("health_check", {})
    if not health_cfg:
        return

    port = health_cfg.get("port", 80)
    url_path = health_cfg.get("url_path", "/")
    expected_status = int(health_cfg.get("expected_status", 200))
    timeout_seconds = int(health_cfg.get("timeout_seconds", 120))
    url = f"http://{guest_ip}:{port}{url_path}"

    with session_scope() as session:
        repo = repo_cls(session)
        repo.add_event(job_id, "provisioning_workload", f"Waiting for service at {url}")

    deadline = time_module.monotonic() + timeout_seconds
    last_error = ""
    health_attempt = 0
    while time_module.monotonic() < deadline:
        if is_cancel_requested(job_id):
            raise RuntimeError("cancel_requested")
        try:
            import urllib.request as _req

            with _req.urlopen(url, timeout=5) as resp:  # noqa: S310  # guest workload health probe; url is the lab VM's own service endpoint
                if resp.status == expected_status:
                    logger.info("[%s] workload health check passed: %s → %d", job_id, url, resp.status)
                    with session_scope() as session:
                        repo = repo_cls(session)
                        repo.add_event(job_id, "provisioning_workload", f"Health check passed: {url} → {resp.status}")
                    return
                last_error = f"HTTP {resp.status}"
        except Exception as exc:
            last_error = str(exc)
        time_module.sleep(min(1.5**health_attempt, 5))
        health_attempt += 1

    raise RuntimeError(
        f"Workload health check timed out after {timeout_seconds}s "
        f"({workload['display_name']} at {url}). Last error: {last_error}"
    )


def apply_network_isolation(
    session_scope,
    repo_cls,
    run_command,
    logger,
    job_id: str,
    guest_ip: str,
    attacker_ip: str | None = None,
) -> bool:
    """Apply guest network isolation via the privileged helper.

    Calls /usr/local/libexec/astinuslab/network_isolation_admin.sh under the
    NOPASSWD sudoers rule. Failures return False and record an
    ``isolation_failed`` job event; callers that REQUIRE isolation (workload
    network_isolation: true) must treat False as fatal and abort the lab boot
    (fail closed).
    """
    import subprocess

    from ..virtualization.host_helpers import network_isolation_admin_helper

    helper = network_isolation_admin_helper()
    cmd = ["sudo", "-n", str(helper), "apply", job_id, guest_ip]
    if attacker_ip:
        cmd.append(attacker_ip)

    # The helper can install the DROP before an attacker exception fails.
    # Persist custody before crossing that boundary so partial work is cleaned.
    with session_scope() as session:
        repo_cls(session).update_job(job_id, network_isolated=True, guest_ip=guest_ip, attacker_ip=attacker_ip)

    try:
        run_command(cmd, job_id=job_id)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        logger.warning(
            "[%s] iptables isolation unavailable (non-fatal); guest retains network access. Detail: %s",
            job_id,
            exc,
        )
        with session_scope() as session:
            repo = repo_cls(session)
            if repo.get_job(job_id) is not None:
                repo.add_event(
                    job_id,
                    "isolation_failed",
                    f"Requested network isolation could NOT be applied for guest {guest_ip}: {exc}",
                )
        return False

    msg = f"Network isolation applied: guest {guest_ip} blocked from forwarded traffic"
    if attacker_ip:
        msg += f"; attacker {attacker_ip} ↔ target traffic permitted"
    with session_scope() as session:
        repo = repo_cls(session)
        repo.update_job(job_id, network_isolated=True)
        repo.add_event(job_id, "waiting_for_capture", msg)

    logger.info("[%s] network isolation applied for guest %s (attacker: %s)", job_id, guest_ip, attacker_ip)
    return True


def flush_guest_filesystems(deps: Any, job_id: str, guest_ip: str, guest_user: str) -> None:
    """Write back the guest page cache so a disk frozen next holds files just written.

    ``deps`` is the Job's dependency container; its key, runner and settings are
    read at call time like every other production wiring closure.
    """
    ssh_priv, _ = deps.get_active_key_paths()
    deps.run_command(
        [
            "ssh",
            "-i",
            str(ssh_priv),
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "ConnectTimeout=30",
            f"{guest_user}@{guest_ip}",
            "sync",
        ],
        cwd=deps.settings.base_dir,
        job_id=job_id,
    )


def remove_network_isolation(
    session_scope,
    repo_cls,
    run_command,
    logger,
    job_id: str,
    guest_ip: str,
    attacker_ip: str | None = None,
) -> bool:
    """Remove previously applied network isolation rules via the privileged helper.

    Missing rules are harmless; failed removal retains durable cleanup intent.
    """
    import subprocess

    from ..virtualization.host_helpers import network_isolation_admin_helper

    helper = network_isolation_admin_helper()
    cmd = ["sudo", "-n", str(helper), "remove", job_id, guest_ip]
    if attacker_ip:
        cmd.append(attacker_ip)

    try:
        # A cancelled Job must not terminate the command removing its rules.
        run_command(cmd)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        logger.warning(
            "[%s] iptables isolation removal failed (non-fatal): %s",
            job_id,
            exc,
        )
        return False

    with session_scope() as session:
        repo = repo_cls(session)
        repo.update_job(job_id, network_isolated=False)

    logger.info("[%s] network isolation removed for guest %s", job_id, guest_ip)
    return True


def cleanup_job_network_isolation(
    session_scope: Any, repo_cls: Any, run_command: Any, logger: Any, job_id: str
) -> bool:
    """Reconcile persisted firewall custody without preventing VM teardown."""
    canonical_id = job_id.removesuffix("-attacker")
    try:
        with session_scope() as session:
            job = repo_cls(session).get_job(canonical_id)
            if job is None or not job.network_isolated:
                return True
            guest_ip, attacker_ip = job.guest_ip, job.attacker_ip
        if not guest_ip:
            raise RuntimeError("network cleanup requires the recorded guest IP")
        return remove_network_isolation(
            session_scope, repo_cls, run_command, logger, canonical_id, guest_ip, attacker_ip=attacker_ip
        )
    except Exception as exc:
        logger.warning("[%s] network isolation cleanup remains pending: %s", canonical_id, exc)
        return False
