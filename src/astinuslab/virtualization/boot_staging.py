from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from ..core import fake_mode
from ..core.config import settings
from .host_helpers import ensure_evidence_dir, storage_root


def build_guest_env(
    *,
    guest_user: str,
    target_distro: str | None,
    target_family: str | None,
    target_machine_type: str | None,
    target_filesystem_type: str | None,
    target_network_profile: str | None,
    target_cloud_init_profile: str | None,
    target_inject_network_config: bool | None,
    target_guest_mount_path: str | None,
    vm_vcpus: int | None,
    vm_memory_mb: int | None,
    vm_disk_gb: int | None,
    cache_image: Path | None,
    cache_instance_id: str | None,
    libvirt_uri: str,
    libvirt_network: str,
) -> dict[str, str]:
    guest_env: dict[str, str] = {"GUEST_USER": guest_user}
    if target_distro:
        guest_env["TARGET_DISTRO"] = target_distro
    if target_family:
        guest_env["TARGET_FAMILY"] = target_family
    if target_machine_type:
        guest_env["VIRT_MACHINE_TYPE"] = target_machine_type
    if target_filesystem_type:
        guest_env["FILESYSTEM_TYPE"] = target_filesystem_type
    if vm_vcpus:
        guest_env["VM_VCPUS"] = str(vm_vcpus)
    if vm_memory_mb:
        guest_env["VM_MEMORY_MB"] = str(vm_memory_mb)
    if vm_disk_gb:
        guest_env["VM_DISK_GB"] = str(vm_disk_gb)
    guest_env["LIBVIRT_URI"] = libvirt_uri
    guest_env["LIBVIRT_NETWORK"] = target_network_profile or libvirt_network
    if target_cloud_init_profile:
        guest_env["CLOUD_INIT_PROFILE"] = target_cloud_init_profile
    if target_inject_network_config is not None:
        guest_env["CLOUD_INIT_NETWORK_CONFIG"] = "1" if target_inject_network_config else "0"
    if target_guest_mount_path:
        guest_env["GUEST_MOUNT_PATH"] = target_guest_mount_path
    if cache_image:
        guest_env["WORKLOAD_CACHE_IMAGE"] = str(cache_image)
    if cache_instance_id:
        guest_env["WORKLOAD_CACHE_INSTANCE_ID"] = cache_instance_id
    guest_env.setdefault("VIRTIOFS_MOUNT_TAG", "astinuslab_out")
    return guest_env


def collect_guest_metadata_with_fallback(
    *,
    logger,
    job_id: str,
    vm_name: str,
    libvirt_uri: str,
    guest_ip: str,
    ssh_key: Path,
    guest_user: str,
    collect_via_qga: Callable[..., dict],
    wait_for_ssh: Callable[..., object],
    collect_via_ssh: Callable[..., dict],
) -> dict:
    try:
        metadata = collect_via_qga(vm_name, libvirt_uri)
        metadata["source"] = metadata.get("source") or "qga"
    except Exception as exc:
        logger.warning("[%s] QGA metadata collection failed; trying SSH fallback: %s", job_id, exc)
        wait_for_ssh(job_id, guest_ip, ssh_key, guest_user)
        metadata = collect_via_ssh(guest_ip, ssh_key, guest_user)
        metadata["source"] = metadata.get("source") or "ssh"
    return metadata


def install_backing_image(
    *,
    job_id: str,
    image_path: Path,
    cache_image: Path | None,
    images_dir: Path,
    workload_cache_dir: Path,
    run_command: Callable[..., object],
    storage_admin_helper: Callable[[], Path],
) -> tuple[Path, Path]:
    backing_source = cache_image or image_path
    storage_root = settings.libvirt_image_base_dir
    base_image_dir = storage_root / "base"
    job_storage_dir = storage_root / job_id
    helper = storage_admin_helper()
    run_command(
        ["sudo", "-n", str(helper), "ensure_dirs", str(base_image_dir), str(job_storage_dir)],
        job_id=job_id,
    )
    backing_image = base_image_dir / backing_source.name
    allowed_src_prefix = str(workload_cache_dir.resolve()) if cache_image else str(Path(images_dir).resolve())
    run_command(
        [
            "sudo",
            "-n",
            str(helper),
            "install_base_image",
            allowed_src_prefix,
            str(backing_source),
            str(backing_image),
        ],
        job_id=job_id,
    )
    return backing_image, job_storage_dir


def write_guest_metadata_json(path: Path, job_id: str, metadata: dict) -> None:
    doc = {
        "distro_id": metadata.get("distro_id") or "",
        "distro_version": metadata.get("distro_version") or "",
        "pretty_name": f"{metadata.get('distro_id') or 'linux'} {metadata.get('distro_version') or ''}".strip(),
        "hostname": f"astinuslab-{job_id[:16]}",
        "arch": metadata.get("arch") or "x86_64",
        "kernel_release": metadata.get("kernel_release") or "",
        "kernel_version": "",
        "source": metadata.get("source") or "qga",
    }
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def stage_target_cloud_init(
    *,
    cloud_init_dir: Path,
    job_id: str,
    job_target_id: str,
    guest_user: str,
    ssh_public_key_path: Path,
    target_distro: str | None,
    target_family: str | None,
    target_cloud_init_profile: str | None,
    target_inject_network_config: bool | None,
    target_guest_mount_path: str | None,
    cache_instance_id: str | None,
    render_target_user_data: Callable[..., str],
    render_target_meta_data: Callable[..., str],
    render_target_network_config: Callable[..., str | None],
) -> tuple[Path, Path, Path | None]:
    user_data_path = cloud_init_dir / "user-data"
    meta_data_path = cloud_init_dir / "meta-data"
    network_config_path: Path | None = None
    user_data_path.write_text(
        render_target_user_data(
            job_target_id=job_target_id,
            guest_user=guest_user,
            ssh_public_key_path=ssh_public_key_path,
            target_distro=target_distro,
            target_family=target_family,
            target_cloud_init_profile=target_cloud_init_profile,
            target_guest_mount_path=target_guest_mount_path,
        ),
        encoding="utf-8",
    )
    meta_data_path.write_text(
        render_target_meta_data(
            job_id=job_id,
            job_target_id=job_target_id,
            cache_instance_id=cache_instance_id,
        ),
        encoding="utf-8",
    )
    if target_inject_network_config is not False:
        network_config = render_target_network_config(
            target_distro=target_distro,
            target_family=target_family,
            target_cloud_init_profile=target_cloud_init_profile,
        )
        if network_config is not None:
            network_config_path = cloud_init_dir / "network-config"
            network_config_path.write_text(network_config, encoding="utf-8")
    return user_data_path, meta_data_path, network_config_path


def capture_and_persist_guest_metadata(
    *,
    logger,
    job_id: str,
    vm_name: str,
    guest_ip: str,
    libvirt_uri: str,
    ssh_key: Path,
    guest_user: str,
    dump_dir: Path,
    vm_disk_gb: int | None,
    wait_for_guest_agent: Callable[..., None],
    verify_guest_disk_capacity: Callable[..., None],
    collect_via_qga: Callable[..., dict],
    wait_for_ssh: Callable[..., None],
    collect_via_ssh: Callable[..., dict],
    write_metadata_json: Callable[[Path, str, dict], None],
) -> dict:
    wait_for_guest_agent(job_id, vm_name, libvirt_uri)
    if vm_disk_gb:
        verify_guest_disk_capacity(job_id, vm_name, libvirt_uri, vm_disk_gb)
    metadata = collect_guest_metadata_with_fallback(
        logger=logger,
        job_id=job_id,
        vm_name=vm_name,
        libvirt_uri=libvirt_uri,
        guest_ip=guest_ip,
        ssh_key=ssh_key,
        guest_user=guest_user,
        collect_via_qga=collect_via_qga,
        wait_for_ssh=wait_for_ssh,
        collect_via_ssh=collect_via_ssh,
    )
    dump_dir.mkdir(parents=True, exist_ok=True)
    try:
        write_metadata_json(dump_dir / "guest-metadata.json", job_id, metadata)
    except Exception as exc:
        logger.warning("[%s] failed to write guest-metadata.json (non-fatal): %s", job_id, exc)
    return metadata


def stage_boot_runtime_paths(
    *,
    job_id: str,
    base_tmp_dir: Path,
    serial_log_dir: Path,
    run_command: Callable[..., object],
    acl_helper_resolver: Callable[[], Path],
) -> tuple[Path, Path, Path]:
    """Prepare the job's tmp/cloud-init/share directories.

    No virtiofsd is started here: the domain declares a libvirt-managed virtiofs
    device, so libvirt spawns the daemon with the domain and reaps it on destroy.
    """
    tmp_root = base_tmp_dir / job_id
    cloud_init_dir = tmp_root / "cloud-init"
    share_dir = tmp_root / "virtiofs-staging"
    ensure_evidence_dir(tmp_root, storage_root(settings))
    tmp_root.chmod(0o755)
    # The service umask permits group write, but this child can belong to the
    # runner's primary group rather than the policy-approved storage group.
    ensure_evidence_dir(cloud_init_dir, storage_root(settings))
    share_dir.mkdir(parents=True, exist_ok=True)
    share_dir.chmod(0o755)
    serial_log_dir.mkdir(parents=True, exist_ok=True)

    if not fake_mode.FAKE_LIBVIRT:
        acl_helper = acl_helper_resolver()
        run_command(
            ["sudo", "-n", str(acl_helper), str(tmp_root)],
            job_id=job_id,
        )

    return tmp_root, cloud_init_dir, share_dir
