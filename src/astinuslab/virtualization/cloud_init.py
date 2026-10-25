from __future__ import annotations

from pathlib import Path


def resolve_cloud_init_profile(
    settings,
    *,
    target_distro: str | None,
    target_family: str | None,
    target_cloud_init_profile: str | None,
) -> str:
    template_dir = settings.base_dir / "guest" / "cloud-init"
    candidates = [target_cloud_init_profile, target_distro, target_family, "generic"]
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate:
            continue
        profile = candidate.strip().lower()
        if not profile or profile in seen:
            continue
        seen.add(profile)
        if (template_dir / f"user-data.{profile}.yaml").exists():
            return profile
    return "generic"


def render_target_user_data_for_profile(
    settings,
    *,
    profile: str,
    job_target_id: str,
    guest_user: str,
    ssh_public_key_path: Path,
    target_guest_mount_path: str | None,
) -> str:
    template_path = settings.base_dir / "guest" / "cloud-init" / f"user-data.{profile}.yaml"
    user_data = template_path.read_text(encoding="utf-8")
    public_key = ssh_public_key_path.read_text(encoding="utf-8").strip()
    user_data = user_data.replace("ssh-ed25519 REPLACE_WITH_LAB_PUBLIC_KEY", public_key)
    user_data = user_data.replace("REPLACE_GUEST_USER", guest_user)
    user_data = user_data.replace("REPLACE_TARGET_ID", job_target_id)
    return user_data.rstrip() + "\n"


def render_target_user_data(
    settings,
    *,
    job_target_id: str,
    guest_user: str,
    ssh_public_key_path: Path,
    target_distro: str | None,
    target_family: str | None,
    target_cloud_init_profile: str | None,
    target_guest_mount_path: str | None,
) -> str:
    profile = resolve_cloud_init_profile(
        settings,
        target_distro=target_distro,
        target_family=target_family,
        target_cloud_init_profile=target_cloud_init_profile,
    )
    return render_target_user_data_for_profile(
        settings,
        profile=profile,
        job_target_id=job_target_id,
        guest_user=guest_user,
        ssh_public_key_path=ssh_public_key_path,
        target_guest_mount_path=target_guest_mount_path,
    )


def render_target_meta_data(
    *,
    job_id: str,
    job_target_id: str,
    cache_instance_id: str | None,
) -> str:
    instance_id = cache_instance_id or f"astinuslab-{job_id}"
    local_hostname = f"astinuslab-{job_target_id}"
    return f"instance-id: {instance_id}\nlocal-hostname: {local_hostname}\n"


def render_target_network_config_for_profile(
    settings,
    *,
    profile: str,
) -> str | None:
    template_path = settings.base_dir / "guest" / "cloud-init" / f"network-config.{profile}.yaml"
    if not template_path.exists():
        return None
    return template_path.read_text(encoding="utf-8").rstrip() + "\n"


def render_target_network_config(
    settings,
    *,
    target_distro: str | None,
    target_family: str | None,
    target_cloud_init_profile: str | None,
) -> str | None:
    profile = resolve_cloud_init_profile(
        settings,
        target_distro=target_distro,
        target_family=target_family,
        target_cloud_init_profile=target_cloud_init_profile,
    )
    return render_target_network_config_for_profile(settings, profile=profile)
