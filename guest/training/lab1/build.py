#!/usr/bin/env python3
"""Build the private, self-contained Lab 1 qcow2 image."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

IMAGE_ID = "training-dvwa-single"
DVWA_COMMIT = "3663d72c4c4f51ac5bcd9f0c8a16b9d890383347"
DVWA_SHA256 = "80713919db387f36ecc96abf273dca6fc5631804d03df612e7816490b7b6cee6"
VIRTUAL_SIZE_BYTES = 16 * 1024**3
PACKAGES = (
    "apache2",
    "ca-certificates",
    "curl",
    "libapache2-mod-php",
    "mariadb-server",
    "php",
    "php-gd",
    "php-mbstring",
    "php-mysql",
    "php-xml",
    "qemu-guest-agent",
)
HERE = Path(__file__).resolve().parent
BUNDLE_FILES = (
    "install_image.sh",
    "firstboot_setup.sh",
    "readiness.sh",
    "astinuslab-training-dvwa-setup.service",
    "astinuslab-training-readiness.service",
    "astinuslab-training-observation-clear.service",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, check=True, text=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "no command output").strip()
        raise RuntimeError(f"{Path(command[0]).name} failed ({exc.returncode}): {detail}") from exc


def _image_info(qemu_img: str, image: Path) -> dict[str, Any]:
    result = _run([qemu_img, "info", "--output=json", str(image)])
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("qemu-img returned invalid JSON") from exc
    if info.get("format") != "qcow2":
        raise ValueError("prepared Lab 1 image is not qcow2")
    if external_field := _external_reference_field(info):
        raise ValueError(f"prepared Lab 1 image has an external dependency: {external_field}")
    virtual_size = info.get("virtual-size")
    if not isinstance(virtual_size, int) or virtual_size < VIRTUAL_SIZE_BYTES:
        raise ValueError("prepared Lab 1 image is smaller than 16 GiB")
    return info


def _external_reference_field(value: object) -> str | None:
    unsafe_fields = {"backing-filename", "full-backing-filename", "data-file", "data-file-raw"}
    if isinstance(value, dict):
        for key, item in value.items():
            if key in unsafe_fields and item not in (None, ""):
                return key
            if nested := _external_reference_field(item):
                return nested
    elif isinstance(value, list):
        for item in value:
            if nested := _external_reference_field(item):
                return nested
    return None


def _sidecar(*, built_at: str) -> dict[str, Any]:
    return {
        "arch": "x86_64",
        "build_id": IMAGE_ID,
        "cloud_init_profile": "ubuntu",
        "default_username": "ubuntu",
        "detected_at": built_at,
        "distro": "ubuntu",
        "family": "debian",
        "inject_network_config": False,
        "inspect_error": None,
        "kernel_release": None,
        "machine_type": "pc",
        "os_variant": "ubuntu24.04",
        "schema_version": 1,
        "source": "user",
        "user_edited_fields": ["cloud_init_profile", "inject_network_config"],
        "version": "24.04",
    }


def build_image(
    *,
    source: Path,
    destination: Path,
    dvwa_archive: Path,
    expected_dvwa_sha256: str = DVWA_SHA256,
    qemu_img: str = "qemu-img",
    virt_customize: str = "virt-customize",
) -> dict[str, Any]:
    source = source.resolve()
    destination = destination.resolve()
    dvwa_archive = dvwa_archive.resolve()
    sidecar = destination.with_suffix(destination.suffix + ".meta.json")
    if not source.is_file():
        raise FileNotFoundError(source)
    if not dvwa_archive.is_file():
        raise FileNotFoundError(dvwa_archive)
    if destination.suffix != ".qcow2" or destination.stem != IMAGE_ID:
        raise ValueError(f"destination must be named {IMAGE_ID}.qcow2")
    if destination.exists():
        raise FileExistsError(destination)
    actual_dvwa_sha256 = _sha256(dvwa_archive)
    if actual_dvwa_sha256 != expected_dvwa_sha256:
        raise ValueError(f"DVWA archive SHA-256 mismatch: expected {expected_dvwa_sha256}, got {actual_dvwa_sha256}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_image = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    temporary_sidecar = sidecar.with_name(f".{sidecar.name}.{uuid4().hex}.tmp")
    try:
        with tempfile.TemporaryDirectory(prefix="astinuslab-lab1-") as temporary:
            bundle = Path(temporary) / "lab1"
            bundle.mkdir()
            for name in BUNDLE_FILES:
                shutil.copy2(HERE / name, bundle / name)
            shutil.copy2(dvwa_archive, bundle / "dvwa.tar.gz")

            _run([qemu_img, "convert", "-p", "-O", "qcow2", str(source), str(temporary_image)])
            _run([qemu_img, "resize", str(temporary_image), str(VIRTUAL_SIZE_BYTES)])
            _run(
                [
                    virt_customize,
                    "-a",
                    str(temporary_image),
                    "--network",
                    "--install",
                    ",".join(PACKAGES),
                    "--mkdir",
                    "/opt/astinuslab-training",
                    "--copy-in",
                    f"{bundle}:/opt/astinuslab-training",
                    "--run-command",
                    f"/opt/astinuslab-training/lab1/install_image.sh {expected_dvwa_sha256}",
                ]
            )
            info = _image_info(qemu_img, temporary_image)

        built_at = datetime.now(UTC).isoformat(timespec="seconds")
        temporary_sidecar.write_text(json.dumps(_sidecar(built_at=built_at), indent=2) + "\n", encoding="utf-8")
        os.replace(temporary_image, destination)
        os.replace(temporary_sidecar, sidecar)
    except Exception:
        temporary_image.unlink(missing_ok=True)
        temporary_sidecar.unlink(missing_ok=True)
        raise

    return {
        "image_id": IMAGE_ID,
        "built_at": built_at,
        "source": {"path": str(source), "sha256": _sha256(source)},
        "dvwa": {
            "commit": DVWA_COMMIT,
            "sha256": actual_dvwa_sha256,
            "path": str(dvwa_archive),
        },
        "packages": list(PACKAGES),
        "output": {
            "path": str(destination),
            "sha256": _sha256(destination),
            "size_bytes": destination.stat().st_size,
            "virtual_size_bytes": info["virtual-size"],
            "format": info["format"],
            "backing_filename": info.get("backing-filename"),
            "data_file": info.get("data-file"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="Ubuntu 24.04 cloud image")
    parser.add_argument("--dvwa-archive", required=True, type=Path, help="Pinned DVWA commit tarball")
    parser.add_argument("--output", required=True, type=Path, help=f"Destination {IMAGE_ID}.qcow2")
    args = parser.parse_args()
    report = build_image(source=args.source, destination=args.output, dvwa_archive=args.dvwa_archive)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
