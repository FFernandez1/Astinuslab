"""Playground cloud-init seed: what a lab VM reads on its first boot.

Split out of `playground/vm_lifecycle.py`, which was 959 lines against a 900
ceiling. This is the separable piece, and it is separable because it is the only
part of the spawn path that produces a *file the guest reads* rather than a
host-side resource the database has to account for. Everything else in
`vm_lifecycle.py` is custody: a disk that exists, a domain that may be defined,
a row that has to agree with both. Nothing here holds custody of anything. If it
raises, the spawn has not reached libvirt yet and the caller's cleanup removes
the staging directory along with the rest.

Not to be confused with `astinuslab.virtualization.cloud_init`, which resolves
which cloud-init *template profile* a job's target should boot from. This module
writes the three seed files a playground VM is handed directly -- `user-data`,
`meta-data` and `network-config` -- and names the seed ISO built from them.

What the seed grants is why the directory mode matters. `user-data` installs the
guest's authorized key and gives that account passwordless sudo, so the staging
directory is created 0700 explicitly rather than by mkdir's default: installed
workers run under `UMask=0007`, which would otherwise make it group-writable, and
the ACL helper refuses a group-writable child by design. The directory is also
created through `po._create_directory_no_follow`, so a symlink planted where the
staging directory belongs is refused instead of written through.

`settings`, the guest-user lookup and the no-follow directory creation are reached
through the orchestrator module object (`po.<name>`) rather than imported by name,
for the reason every sibling in this package does it: `orchestrator` is what the
suite patches, and a by-name import would bind the original and silently ignore a
patched value. `vm_lifecycle` reaches this module the same way, by attribute on
the module object, so a case patching `cloud_init_seed._write_playground_cloud_init`
still intercepts the call the spawn path makes.
"""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

from ..storage.keystore import get_active_key_paths
from . import orchestrator as po


class _PlaygroundCloudInitPaths(TypedDict, total=False):
    seed_iso_path: Path
    user_data_path: Path
    meta_data_path: Path
    network_config_path: Path


def _playground_cloud_init_enabled(image: dict) -> bool:
    return bool(image.get("cloud_init"))


def _write_playground_cloud_init(
    *,
    job_id: str,
    vm_id: str,
    label: str,
    image: dict,
    disk_path: Path,
) -> _PlaygroundCloudInitPaths:
    cloud_init_dir = disk_path.parent / f"{disk_path.stem}-cloud-init"
    po._create_directory_no_follow(disk_path.parent, cloud_init_dir.name, final_mode=0o700)
    _ssh_priv, ssh_pub = get_active_key_paths()
    public_key = ssh_pub.read_text(encoding="utf-8").strip()
    guest_user = po._playground_guest_user(image)

    user_data_path = cloud_init_dir / "user-data"
    meta_data_path = cloud_init_dir / "meta-data"
    network_config_path = cloud_init_dir / "network-config"
    seed_iso_path = disk_path.with_name(f"{disk_path.stem}-seed.iso")

    user_data_path.write_text(
        "\n".join(
            [
                "#cloud-config",
                "preserve_hostname: false",
                "users:",
                "  - default",
                f"  - name: {guest_user}",
                "    sudo: ALL=(ALL) NOPASSWD:ALL",
                "    groups: users, adm",
                "    shell: /bin/bash",
                "    lock_passwd: true",
                "    ssh_authorized_keys:",
                f"      - {public_key}",
                "ssh_pwauth: false",
                "disable_root: true",
                "packages:",
                "  - qemu-guest-agent",
                "runcmd:",
                "  - systemctl enable --now qemu-guest-agent || /usr/sbin/qemu-ga -d",
                "",
            ]
        ),
        encoding="utf-8",
    )
    meta_data_path.write_text(
        f"instance-id: astinuslab-{job_id}-{vm_id}\nlocal-hostname: {label}\n",
        encoding="utf-8",
    )
    network_config_path.write_text(
        'version: 2\nethernets:\n  default_nic:\n    match:\n      name: "e*"\n    dhcp4: true\n    dhcp6: false\n',
        encoding="utf-8",
    )
    return {
        "seed_iso_path": seed_iso_path,
        "user_data_path": user_data_path,
        "meta_data_path": meta_data_path,
        "network_config_path": network_config_path,
    }
