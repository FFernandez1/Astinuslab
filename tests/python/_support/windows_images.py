"""Synthetic Windows provenance for temporary test files only."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from astinuslab.images.windows_bundle import WindowsPreparedBundle, parse_windows_bundle


def windows_bundle_declaration() -> dict[str, Any]:
    """Return a fresh public bundle declaration for synthetic test metadata."""
    return {
        "profile": "server2022-unattend-v1",
        "nonce": "db3396d32fa84103b4f7b29c27179dbb",
        "media": {
            "filename": "setup.iso",
            "size_bytes": 378880,
            "sha256": "04dcfbc060c22d41792e1613aa1ef50bca427eda2b4863af3949d247ef642e6d",
            "label": "ALDB3396D32FA8",
            "initializer_sha256": "bd9654aa7c6f651854e852051fe37de017d67bc514ba5eedc894766cdd52e019",
        },
    }


def create_windows_bundle_fixture(tmp_path: Path) -> tuple[Path, Path, Path, WindowsPreparedBundle]:
    """Create synthetic media in an isolated source/job pair; never a bootable ISO."""
    source = tmp_path / "images"
    job = tmp_path / "job"
    source.mkdir(mode=0o700)
    job.mkdir(mode=0o700)
    media = source / "setup.iso"
    media.write_bytes(b"synthetic public media")
    media.chmod(0o644)
    data = windows_bundle_declaration()
    data["media"].update(size_bytes=media.stat().st_size, sha256=hashlib.sha256(media.read_bytes()).hexdigest())
    return source, job, media, parse_windows_bundle(data)


def windows_payload(**overrides) -> dict:
    base = dict(
        schema_version=3,
        platform="windows",
        distro="windows-server",
        version="2022",
        family="windows",
        default_username="Administrator",
        os_variant="win2k22",
        machine_type="q35",
        kernel_release=None,
        arch="x86_64",
        cloud_init_profile=None,
        inject_network_config=False,
        edition="Standard Evaluation",
        build="20348",
        revision=5622,
        source="operator_prepared",
        generalization="generalized",
        shutdown_state="shut_off",
        virtual_size_bytes=40 * 1024**3,
        firmware={
            "machine": "q35",
            "firmware": "ovmf",
            "secure_boot": False,
            "tpm": False,
            "nvram": "per_instance_copy",
        },
        guest_access={
            "method": "openssh",
            "bootstrap": "qga",
            "key_in_image": False,
            "host_keys": "regenerate_per_instance",
        },
        windows_update={"policy": "optional", "pending_count": 0},
        sha256=hashlib.sha256(b"synthetic image; not a bootable VM").hexdigest(),
        detected_at="2026-09-10T00:00:00+00:00",
        first_boot=windows_bundle_declaration(),
        architecture="x86_64",
        user_edited_fields=[],
        inspect_error=None,
    )
    base.update(overrides)
    return base


def write_windows_image(path: Path, **overrides) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"synthetic image; not a bootable VM")
    overrides.setdefault("sha256", hashlib.sha256(path.read_bytes()).hexdigest())
    path.with_suffix(path.suffix + ".meta.json").write_text(json.dumps(windows_payload(**overrides)), encoding="utf-8")
    return path
