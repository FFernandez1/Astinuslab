from __future__ import annotations

import hashlib
import json
import logging
import shutil
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path

from ...core.output_parsers import parse_keyed_output as _parse_keyed_output_impl
from ...forensics.capture_support import acquire_and_publish as _acquire_and_publish_impl
from ...forensics.capture_support import dispatch_boot_capture_acquisition as _dispatch_boot_capture_acquisition_impl
from ...forensics.capture_support import teardown_post_capture as _teardown_post_capture_impl
from ...images.image_inspection import WindowsImageMetadata, load_sidecar, require_reusable_windows_image
from ...images.windows_bundle import parse_windows_bundle, stage_windows_media, verify_windows_disk
from ...jobs.job_control import ACTION_BY_JOB
from ...jobs.job_state_support import finalize_action_tracking as _finalize_action_tracking_impl
from ...jobs.job_state_support import handle_runner_cleanup_exception as _handle_runner_cleanup_exception_impl
from ...jobs.job_state_support import persist_guest_access_and_metadata as _persist_guest_access_and_metadata_impl
from ...jobs.job_state_support import should_preserve_failed_vm as _should_preserve_failed_vm_impl
from ...orchestration.context import JobRunContext
from ...orchestration.deps import JobRunDeps
from ...orchestration.support import mark_action_running as _mark_action_running_impl
from ...orchestration.support import mark_job_preparing_image as _mark_job_preparing_image_impl
from ...virtualization.boot_staging import build_guest_env as _build_guest_env_impl
from ...virtualization.boot_staging import (
    capture_and_persist_guest_metadata as _capture_and_persist_guest_metadata_impl,
)
from ...virtualization.boot_staging import install_backing_image as _install_backing_image_impl
from ...virtualization.boot_staging import stage_boot_runtime_paths as _stage_boot_runtime_paths_impl
from ...virtualization.host_helpers import fix_libvirt_path_acl_helper
from ...virtualization.uefi import WindowsFirmwareConfig, assert_runtime_directory_unowned
from ...virtualization.windows_runtime import WindowsBootstrapResult
from ...workloads.workload_cache_support import workload_cache_dir

logger = logging.getLogger(__name__)

# Boot-capture flow. Dependencies are resolved through an explicit ``JobRunDeps``
# container (see orchestration.deps.py): every entrypoint accepts ``deps=`` so tests can
# inject overrides instead of monkeypatching module globals.


def target_guest_mac(job_id: str) -> str:
    digest = hashlib.sha256(job_id.encode("utf-8")).digest()
    return f"52:54:00:{digest[0]:02x}:{digest[1]:02x}:{digest[2]:02x}"


def is_windows_target(target: dict) -> bool:
    """Return true only for an explicit, reusable Windows Server target."""
    if target.get("family") != "windows" or target.get("distro") != "windows-server":
        return False
    if target.get("platform") != "windows" or target.get("reusable") is not True:
        return False
    if target.get("version") not in (None, "2022"):
        return False
    if target.get("os_variant") not in (None, "win2k22"):
        return False
    return target.get("cloud_init") is not True


def windows_computer_name(job_id: str) -> str:
    digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest().upper()
    return f"AL-{digest[:12]}"


def boot_and_prepare_windows(
    job_id: str,
    image_path: Path,
    os_variant: str,
    job_target_id: str,
    guest_user: str = "Administrator",
    target_distro: str | None = "windows-server",
    target_family: str | None = "windows",
    target_machine_type: str | None = "q35",
    target_network_profile: str | None = None,
    target_cloud_init_profile: str | None = None,
    target_inject_network_config: bool | None = False,
    vm_vcpus: int | None = None,
    vm_memory_mb: int | None = None,
    vm_disk_gb: int | None = None,
    target_guest_mount_path: str | None = None,
    target_filesystem_type: str | None = None,
    cache_image: Path | None = None,
    cache_instance_id: str | None = None,
    keep_failed_vm: bool = False,
    deps: JobRunDeps | None = None,
) -> tuple[str, dict]:
    """Boot one validated Windows base without Linux cloud-init staging."""
    del os_variant, job_target_id, target_machine_type, target_cloud_init_profile
    del target_inject_network_config, target_guest_mount_path, cache_instance_id
    deps = deps if deps is not None else JobRunDeps.default()
    phase = "setup"
    conn = None
    lifecycle = None
    boot_result = None
    vm_name = deps.runtime_vm_name(job_id, "target")
    try:
        phase = "preflight"
        image_path = require_reusable_windows_image(image_path, allowed_dir=Path(deps.settings.images_dir))
        if cache_image is not None:
            cache_image = require_reusable_windows_image(cache_image, allowed_dir=workload_cache_dir(deps.settings))
        selected_source = cache_image or image_path
        source_metadata = load_sidecar(selected_source)
        if not isinstance(source_metadata, WindowsImageMetadata):
            raise ValueError("Windows prepared source metadata unavailable")
        bundle = parse_windows_bundle(source_metadata.first_boot)
        phase = "setup"
        deps.require_noninteractive_sudo()
        if deps.is_cancel_requested(job_id):
            raise RuntimeError("cancel_requested")
        ssh_priv, ssh_pub = deps.get_active_key_paths()
        public_key = ssh_pub.read_text(encoding="utf-8").strip()

        phase = "runtime_paths"
        tmp_root, _cloud_init_dir, share_dir = _stage_boot_runtime_paths_impl(
            job_id=job_id,
            base_tmp_dir=deps.settings.storage_dir / "tmp",
            serial_log_dir=deps.settings.serial_log_dir,
            run_command=deps.run_command,
            acl_helper_resolver=fix_libvirt_path_acl_helper,
        )
        phase = "install_backing_image"
        backing_image, job_storage_dir = _install_backing_image_impl(
            job_id=job_id,
            image_path=image_path,
            cache_image=cache_image,
            images_dir=Path(deps.settings.images_dir),
            workload_cache_dir=workload_cache_dir(deps.settings),
            run_command=deps.run_command,
            storage_admin_helper=deps.libvirt_storage_admin_helper,
        )

        phase = "disk_provenance"
        verify_windows_disk(backing_image, source_metadata.sha256)
        phase = "runtime_resources"
        conn = deps.open_libvirt_connection()
        assert_runtime_directory_unowned(conn, job_storage_dir)
        phase = "nvram"
        firmware = WindowsFirmwareConfig()
        nvram_path = job_storage_dir / f"{vm_name}.vars"
        with nvram_path.open("xb") as destination, Path(firmware.nvram_template).open("rb") as template:
            shutil.copyfileobj(template, destination)
        nvram_path.chmod(0o644)

        phase = "setup_media"
        prepared_media = stage_windows_media(bundle, source_dir=selected_source.parent, runtime_dir=job_storage_dir)

        phase = "boot"
        lifecycle = deps.vm_lifecycle()
        guest_env = _build_guest_env_impl(
            guest_user=guest_user,
            target_distro=target_distro,
            target_family=target_family,
            target_machine_type="q35",
            target_filesystem_type=target_filesystem_type,
            target_network_profile=target_network_profile,
            target_cloud_init_profile=None,
            target_inject_network_config=False,
            target_guest_mount_path=None,
            vm_vcpus=vm_vcpus or 4,
            vm_memory_mb=vm_memory_mb or 8192,
            vm_disk_gb=vm_disk_gb or 40,
            cache_image=cache_image,
            cache_instance_id=None,
            libvirt_uri=deps.settings.libvirt_uri,
            libvirt_network=deps.settings.libvirt_network,
        )
        boot_result = lifecycle.boot(
            conn,
            vm_name=vm_name,
            memory_mb=vm_memory_mb or 8192,
            vcpus=vm_vcpus or 4,
            backing_image=backing_image,
            disk_path=deps.job_disk_image_path(job_id, "windows-server"),
            seed_iso_path=None,
            user_data_path=None,
            meta_data_path=None,
            disk_size_gb=vm_disk_gb or 40,
            network_config_path=None,
            virtiofs_socket=tmp_root / "virtiofsd.sock",
            share_dir=share_dir,
            virtiofs_mount_tag=guest_env["VIRTIOFS_MOUNT_TAG"],
            network_name=guest_env["LIBVIRT_NETWORK"],
            guest_mac=deps.target_guest_mac(job_id),
            machine_type="q35",
            cwd=deps.settings.base_dir,
            cancel_requested=lambda: deps.is_cancel_requested(job_id),
            ip_resolver=None,
            preserve_on_failure=keep_failed_vm,
            serial_log_path=deps.settings.serial_log_dir / f"astinuslab-{job_id}-serial.log",
            filesystem_type=target_filesystem_type or "virtiofs",
            job_id=job_id,
            firmware=firmware,
            nvram_path=nvram_path,
            prepared_media=prepared_media,
        )

        phase = "wait_qga"
        deps.wait_for_guest_agent(job_id, vm_name, deps.settings.libvirt_uri)
        phase = "wait_windows_setup"
        setup_boot_id = deps.wait_for_windows_setup(
            deps.settings.libvirt_uri,
            vm_name,
            cancel_requested=lambda: deps.is_cancel_requested(job_id),
        )
        phase = "prepare_windows_guest"
        if deps.is_cancel_requested(job_id):
            raise RuntimeError("cancel_requested")
        preparation = deps.prepare_windows_guest(deps.settings.libvirt_uri, vm_name, bundle, setup_boot_id)
        if preparation.boot_id != setup_boot_id:
            raise RuntimeError("Windows preparation boot identity changed")
        phase = "bootstrap"
        bootstrap: WindowsBootstrapResult = deps.bootstrap_windows_guest(
            deps.settings.libvirt_uri,
            vm_name,
            public_key,
            windows_computer_name(job_id),
            expected_boot_id=preparation.boot_id,
        )
        phase = "wait_windows_identity"
        deps.wait_for_windows_identity(
            deps.settings.libvirt_uri,
            vm_name,
            bootstrap,
            cancel_requested=lambda: deps.is_cancel_requested(job_id),
        )

        phase = "resolve_ip"
        guest_ip = deps.parse_guest_ip(
            job_id,
            libvirt_uri=deps.settings.libvirt_uri,
            libvirt_network=guest_env["LIBVIRT_NETWORK"],
        )
        phase = "verify_host_key"
        known_hosts_path = tmp_root / "known_hosts"
        deps.record_verified_ssh_host_key(
            guest_ip,
            known_hosts_path,
            bootstrap.ssh_host_key_fingerprint,
        )
        phase = "wait_ssh"
        deps.wait_for_guest_ssh(
            job_id,
            guest_ip,
            ssh_priv,
            guest_user,
            known_hosts_path=known_hosts_path,
            guest_platform="windows",
        )
        metadata = {
            "distro_id": "windows-server",
            "distro_version": "2022",
            "kernel_release": None,
            "arch": "x86_64",
            "source": "qga",
            "computer_name": bootstrap.computer_name,
            "ssh_host_key_fingerprint": bootstrap.ssh_host_key_fingerprint,
            "bootstrap_phase": "completed",
            "machine_sid_prefix": preparation.machine_sid_prefix,
            "answer_descriptor_hash": preparation.answer_descriptor_hash,
        }
        phase = "persist_metadata"
        _persist_guest_access_and_metadata_impl(
            deps.session_scope,
            deps.job_repository_cls,
            deps.sync_runtime_record,
            job_id=job_id,
            vm_name=vm_name,
            guest_ip=guest_ip,
            metadata=metadata,
        )
        metadata_dir = deps.settings.storage_dir / "dumps" / job_id
        metadata_dir.mkdir(parents=True, exist_ok=True)
        deps.write_guest_metadata_json(metadata_dir / "guest-metadata.json", job_id, metadata)
        runtime_metadata_path = tmp_root / "windows-runtime.json"
        runtime_metadata_path.write_text(
            json.dumps(
                {
                    "computer_name": bootstrap.computer_name,
                    "ssh_host_key_fingerprint": bootstrap.ssh_host_key_fingerprint,
                    "bootstrap_phase": "completed",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return guest_ip, metadata
    except Exception as exc:
        if lifecycle is not None and conn is not None and boot_result is not None and not keep_failed_vm:
            try:
                lifecycle.destroy(conn, vm_name)
            except Exception as cleanup_exc:
                raise RuntimeError(f"Windows boot failed during {phase}: {exc}; cleanup failed: {cleanup_exc}") from exc
        raise RuntimeError(f"Windows boot failed during {phase}: {exc}") from exc
    finally:
        if conn is not None:
            close_conn = getattr(conn, "close", None)
            if callable(close_conn):
                close_conn()


def boot_and_prepare(
    job_id: str,
    image_path: Path,
    os_variant: str,
    job_target_id: str,
    guest_user: str = "ubuntu",
    target_distro: str | None = None,
    target_family: str | None = None,
    target_platform: str | None = None,
    target_reusable: bool | None = None,
    target_version: str | None = None,
    target_machine_type: str | None = None,
    target_network_profile: str | None = None,
    target_cloud_init_profile: str | None = None,
    target_inject_network_config: bool | None = None,
    vm_vcpus: int | None = None,
    vm_memory_mb: int | None = None,
    vm_disk_gb: int | None = None,
    target_guest_mount_path: str | None = None,
    target_filesystem_type: str | None = None,
    cache_image: Path | None = None,
    cache_instance_id: str | None = None,
    keep_failed_vm: bool = False,
    deps: JobRunDeps | None = None,
) -> tuple[str, dict]:
    """
    Shared Phase 1: boot VM, detect guest IP and kernel.
    Returns (guest_ip, metadata).
    """
    deps = deps if deps is not None else JobRunDeps.default()

    windows_target = is_windows_target(
        {
            "platform": target_platform,
            "reusable": target_reusable,
            "family": target_family,
            "distro": target_distro,
            "version": target_version,
            "os_variant": os_variant,
            "cloud_init": False if target_inject_network_config is False else None,
        }
    )
    if (
        target_platform == "windows" or target_family == "windows" or target_distro == "windows-server"
    ) and not windows_target:
        raise ValueError("Windows target is not a validated reusable image")
    if windows_target:
        return boot_and_prepare_windows(
            job_id,
            image_path,
            os_variant,
            job_target_id,
            guest_user=guest_user,
            target_distro=target_distro,
            target_family=target_family,
            target_machine_type=target_machine_type,
            target_network_profile=target_network_profile,
            target_cloud_init_profile=target_cloud_init_profile,
            target_inject_network_config=target_inject_network_config,
            vm_vcpus=vm_vcpus,
            vm_memory_mb=vm_memory_mb,
            vm_disk_gb=vm_disk_gb,
            target_guest_mount_path=target_guest_mount_path,
            target_filesystem_type=target_filesystem_type,
            cache_image=cache_image,
            cache_instance_id=cache_instance_id,
            keep_failed_vm=keep_failed_vm,
            deps=deps,
        )

    deps.require_noninteractive_sudo()
    if deps.is_cancel_requested(job_id):
        raise RuntimeError("cancel_requested")

    guest_env = _build_guest_env_impl(
        guest_user=guest_user,
        target_distro=target_distro,
        target_family=target_family,
        target_machine_type=target_machine_type,
        target_filesystem_type=target_filesystem_type,
        target_network_profile=target_network_profile,
        target_cloud_init_profile=target_cloud_init_profile,
        target_inject_network_config=target_inject_network_config,
        target_guest_mount_path=target_guest_mount_path,
        vm_vcpus=vm_vcpus,
        vm_memory_mb=vm_memory_mb,
        vm_disk_gb=vm_disk_gb,
        cache_image=cache_image,
        cache_instance_id=cache_instance_id,
        libvirt_uri=deps.settings.libvirt_uri,
        libvirt_network=deps.settings.libvirt_network,
    )
    _ssh_priv, _ssh_pub = deps.get_active_key_paths()

    tmp_root, cloud_init_dir, share_dir = _stage_boot_runtime_paths_impl(
        job_id=job_id,
        base_tmp_dir=deps.settings.storage_dir / "tmp",
        serial_log_dir=deps.settings.serial_log_dir,
        run_command=deps.run_command,
        acl_helper_resolver=fix_libvirt_path_acl_helper,
    )

    user_data_path, meta_data_path, network_config_path = deps.stage_target_cloud_init(
        cloud_init_dir=cloud_init_dir,
        job_id=job_id,
        job_target_id=job_target_id,
        guest_user=guest_user,
        ssh_public_key_path=_ssh_pub,
        target_distro=target_distro,
        target_family=target_family,
        target_cloud_init_profile=target_cloud_init_profile,
        target_inject_network_config=target_inject_network_config,
        target_guest_mount_path=target_guest_mount_path,
        cache_instance_id=cache_instance_id,
    )

    backing_image, job_storage_dir = _install_backing_image_impl(
        job_id=job_id,
        image_path=image_path,
        cache_image=cache_image,
        images_dir=Path(deps.settings.images_dir),
        workload_cache_dir=workload_cache_dir(deps.settings),
        run_command=deps.run_command,
        storage_admin_helper=deps.libvirt_storage_admin_helper,
    )

    resolved_vcpus = vm_vcpus or 2
    resolved_memory_mb = vm_memory_mb or 4096
    lifecycle = deps.vm_lifecycle()
    conn = deps.open_libvirt_connection()
    libvirt_network = guest_env["LIBVIRT_NETWORK"]
    try:
        boot_result = lifecycle.boot(
            conn,
            vm_name=deps.runtime_vm_name(job_id, "target"),
            memory_mb=resolved_memory_mb,
            vcpus=resolved_vcpus,
            backing_image=backing_image,
            disk_path=deps.job_disk_image_path(job_id, job_target_id),
            seed_iso_path=job_storage_dir / "seed.iso",
            user_data_path=user_data_path,
            meta_data_path=meta_data_path,
            disk_size_gb=vm_disk_gb,
            network_config_path=network_config_path,
            virtiofs_socket=tmp_root / "virtiofsd.sock",
            share_dir=share_dir,
            virtiofs_mount_tag=guest_env["VIRTIOFS_MOUNT_TAG"],
            network_name=libvirt_network,
            guest_mac=deps.target_guest_mac(job_id),
            machine_type=target_machine_type,
            cwd=deps.settings.base_dir,
            cancel_requested=lambda: deps.is_cancel_requested(job_id),
            ip_resolver=lambda: deps.parse_guest_ip(
                job_id,
                libvirt_uri=deps.settings.libvirt_uri,
                libvirt_network=libvirt_network,
            ),
            preserve_on_failure=keep_failed_vm,
            serial_log_path=(deps.settings.serial_log_dir / f"astinuslab-{job_id}-serial.log"),
            filesystem_type=target_filesystem_type or "virtiofs",
            job_id=job_id,
        )
        guest_ip = boot_result.guest_ip
        metadata = _capture_and_persist_guest_metadata_impl(
            logger=logger,
            job_id=job_id,
            vm_name=boot_result.vm_name,
            guest_ip=guest_ip,
            libvirt_uri=deps.settings.libvirt_uri,
            ssh_key=_ssh_priv,
            guest_user=guest_user,
            dump_dir=deps.settings.storage_dir / "dumps" / job_id,
            vm_disk_gb=vm_disk_gb,
            wait_for_guest_agent=deps.wait_for_guest_agent,
            verify_guest_disk_capacity=deps.verify_guest_disk_capacity,
            collect_via_qga=deps.collect_guest_metadata_via_qga,
            wait_for_ssh=deps.wait_for_guest_ssh,
            collect_via_ssh=deps.collect_guest_metadata_via_ssh,
            write_metadata_json=deps.write_guest_metadata_json,
        )
    finally:
        close_conn = getattr(conn, "close", None)
        if callable(close_conn):
            close_conn()

    _persist_guest_access_and_metadata_impl(
        deps.session_scope,
        deps.job_repository_cls,
        deps.sync_runtime_record,
        job_id=job_id,
        vm_name=boot_result.vm_name,
        guest_ip=guest_ip,
        metadata=metadata,
    )

    # QGA can answer before sshd accepts connections; the share mount uses SSH.
    deps.wait_for_guest_ssh(job_id, guest_ip, _ssh_priv, guest_user)
    deps.run_command(
        [
            "bash",
            str(deps.settings.base_dir / "ops" / "host" / "mount_output_share_remote.sh"),
            guest_ip,
            str(_ssh_priv),
        ],
        cwd=deps.settings.base_dir,
        job_id=job_id,
        extra_env=guest_env,
    )

    return guest_ip, metadata


def run_boot_capture_job(
    job_id: str,
    action_id: int | None,
    *,
    run_post_script: bool,
    deps: JobRunDeps | None = None,
) -> None:
    ctx = JobRunContext.create(job_id, action_id, deps)
    deps = ctx.deps

    _mark_action_running_impl(deps.session_scope, deps.job_repository_cls, ACTION_BY_JOB, ctx.job_id, ctx.action_id)

    keep_failed_vm = False
    action_payload = deps.load_action_payload(ctx.action_id)
    isf_only = bool(action_payload.get("isf_only"))
    with deps.job_cleanup_manager(ctx.job_id) as cleanup:
        try:
            params = deps.load_job_params(ctx.job_id)
            target, scenario, guest_user, keep_failed_vm = deps.resolve_target_execution_context(
                params.target_id, params.scenario_id
            )

            deps.check_host_disk_space(ctx.job_id)

            _mark_job_preparing_image_impl(deps.session_scope, deps.job_repository_cls, ctx.job_id)

            image_path, effective_vcpus, effective_memory_mb, effective_disk_gb = deps.prepare_boot_context(
                target,
                params.image_path,
                params.vm_vcpus,
                params.vm_memory_mb,
                params.vm_disk_gb,
            )

            guest_ip, metadata = deps.boot_and_prepare(
                ctx.job_id,
                image_path,
                target.get("os_variant") or "generic",
                params.target_id,
                guest_user=guest_user,
                target_distro=target.get("distro"),
                target_family=target.get("family"),
                target_platform=target.get("platform"),
                target_reusable=target.get("reusable"),
                target_version=target.get("version"),
                target_machine_type=target.get("machine_type"),
                target_network_profile=target.get("network_profile"),
                target_cloud_init_profile=target.get("cloud_init_profile"),
                target_inject_network_config=target.get("inject_network_config"),
                target_filesystem_type=target.get("filesystem_type"),
                vm_vcpus=effective_vcpus,
                vm_memory_mb=effective_memory_mb,
                vm_disk_gb=effective_disk_gb,
                target_guest_mount_path=target.get("guest_mount_path"),
                keep_failed_vm=keep_failed_vm,
            )
            kernel_release = metadata.get("kernel_release")

            if run_post_script:
                tmp_script_path = deps.run_post_boot_script(
                    ctx.job_id, guest_ip, guest_user, scenario or {}, params.custom_script
                )
                cleanup.tmp_script_path = tmp_script_path

            _dispatch_boot_capture_acquisition_impl(
                run_isf_only_fn=deps.run_isf_only_job,
                acquire_and_publish_fn=deps.acquire_and_publish,
                isf_only=isf_only,
                job_id=ctx.job_id,
                action_id=ctx.action_id,
                target_id=params.target_id,
                guest_ip=guest_ip,
                runtime_vm_name=deps.runtime_vm_name(ctx.job_id, "target"),
                distro=target["distro"],
                version=target["version"],
                kernel_release=kernel_release,
                guest_user=guest_user,
                capture_type=params.capture_type,
            )

        except Exception as exc:
            _handle_runner_cleanup_exception_impl(
                deps.handle_exception,
                _should_preserve_failed_vm_impl,
                logger,
                cleanup,
                exc,
                ctx.job_id,
                ctx.action_id,
                keep_failed_vm,
            )


def acquire_and_publish(
    job_id,
    target_id,
    guest_ip,
    vm_name,
    distro,
    version,
    action_id,
    *,
    kernel_release=None,
    guest_user="ubuntu",
    capture_type="memory",
    deps: JobRunDeps | None = None,
) -> None:
    deps = deps if deps is not None else JobRunDeps.default()

    from ...forensics.memory_dump import MemoryDumper

    libvirt = import_module("libvirt")
    memory_dumper = MemoryDumper(libvirt_module=libvirt)
    conn = deps.open_libvirt_connection()
    try:
        _acquire_and_publish_impl(
            session_scope=deps.session_scope,
            repo_cls=deps.job_repository_cls,
            get_active_key_paths=deps.get_active_key_paths,
            run_command=deps.run_command,
            settings=deps.settings,
            parse_keyed_output=_parse_keyed_output_impl,
            check_host_disk_space=deps.check_host_disk_space,
            is_cancel_requested=deps.is_cancel_requested,
            job_disk_image_path=deps.job_disk_image_path,
            memory_dumper=memory_dumper,
            conn=conn,
            job_id=job_id,
            target_id=target_id,
            guest_ip=guest_ip,
            vm_name=vm_name,
            distro=distro,
            version=version,
            action_id=action_id,
            kernel_release=kernel_release,
            guest_user=guest_user,
            capture_type=capture_type,
        )
    finally:
        close_fn = getattr(conn, "close", None)
        if callable(close_fn):
            close_fn()


def acquire_memory_dump(job_id: str, action_id: int | None = None, deps: JobRunDeps | None = None) -> None:
    deps = deps if deps is not None else JobRunDeps.default()

    if action_id is not None:
        ACTION_BY_JOB[job_id] = action_id
        with deps.session_scope() as session:
            repo = deps.job_repository_cls(session)
            repo.update_action(action_id, status="running", started_at=datetime.now(UTC))

    keep_failed_vm = False
    try:
        params = deps.load_job_params(job_id)
        target = deps.target_by_id(params.target_id)
        guest_user = target.get("default_username", "ubuntu")
        keep_failed_vm = bool(target.get("keep_failed_vm", False))

        runtime_state = deps.load_capture_runtime_state(job_id)
        guest_ip = runtime_state["guest_ip"]
        kernel_release = runtime_state["kernel_release"]
        attacker_ip = runtime_state["attacker_ip"]
        network_isolated = runtime_state["network_isolated"]
        attacker_vm_present = runtime_state["attacker_vm_present"]

        capture_type = params.capture_type
        if action_id is not None:
            with deps.session_scope() as session:
                action = deps.job_repository_cls(session).get_action(action_id)
                if action and action.payload:
                    payload = json.loads(action.payload)
                    if "capture_type" in payload:
                        capture_type = payload["capture_type"]

        deps.acquire_and_publish(
            job_id,
            params.target_id,
            guest_ip or "",
            deps.runtime_vm_name(job_id, "target"),
            target["distro"],
            target["version"],
            action_id,
            kernel_release=kernel_release,
            guest_user=guest_user,
            capture_type=capture_type,
        )

        _teardown_post_capture_impl(
            logger=logger,
            job_id=job_id,
            guest_ip=guest_ip,
            attacker_ip=attacker_ip,
            network_isolated=network_isolated,
            attacker_vm_present=attacker_vm_present,
            remove_network_isolation=deps.remove_network_isolation,
            teardown_attacker_vm=deps.teardown_attacker_vm,
            teardown_vm=deps.teardown_vm,
        )
    except Exception as exc:
        deps.handle_exception(exc, job_id, action_id)
        # Failed captures tear down the target VM by default. keep_failed_vm
        # preserves it for debugging and still emits the preservation log.
        if not _should_preserve_failed_vm_impl(logger, exc, job_id, keep_failed_vm):
            try:
                deps.teardown_vm(job_id)
            except Exception as teardown_exc:
                logger.warning("[%s] failed to teardown target VM after failed capture: %s", job_id, teardown_exc)
    finally:
        _finalize_action_tracking_impl(ACTION_BY_JOB, job_id)


def _complete_isf_only_job(deps: JobRunDeps, job_id: str, action_id: int | None, summary: str) -> None:
    now = datetime.now(UTC)
    with deps.session_scope() as session:
        repo = deps.job_repository_cls(session)
        repo.transition_job_status(job_id, "completed", finished_at=now)
        repo.add_event(job_id, "completed", f"ISF-only job completed. {summary}")
        if action_id is not None:
            repo.update_action(action_id, status="completed", finished_at=now, active_pid=None)


def run_isf_only_job(job_id: str, action_id: int | None, deps: JobRunDeps | None = None) -> None:
    deps = deps if deps is not None else JobRunDeps.default()

    from ...forensics.isf_preflight import PreflightError, check_host_prerequisites
    from ...forensics.vol3_support import run_vol3_isf_only

    try:
        check_host_prerequisites(deps.settings.libvirt_uri)
    except PreflightError as exc:
        logger.error("[%s] preflight failed: %s", job_id, exc)
        raise

    with deps.session_scope() as session:
        repo = deps.job_repository_cls(session)
        repo.transition_job_status(job_id, "generating_isf")
        repo.add_event(job_id, "generating_isf", "Generating Volatility 3 ISF...")

    success, summary = run_vol3_isf_only(job_id)
    if not success:
        raise RuntimeError(summary)
    _complete_isf_only_job(deps, job_id, action_id, summary)


def run_immediate_job(job_id: str, action_id: int | None = None, deps: JobRunDeps | None = None) -> None:
    run_boot_capture_job(job_id, action_id, run_post_script=False, deps=deps)


def run_post_script_job(job_id: str, action_id: int | None = None, deps: JobRunDeps | None = None) -> None:
    run_boot_capture_job(job_id, action_id, run_post_script=True, deps=deps)
