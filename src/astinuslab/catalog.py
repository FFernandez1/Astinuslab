"""Catalog loading and normalization for targets, workloads, attackers, networks, and scenarios.

The catalog merges curated YAML with discovered local images and hides host-specific fallback rules from callers.
"""

from __future__ import annotations

import copy
import io
import logging
import os
import re
import stat
import subprocess
import tempfile
import unicodedata
import zipfile
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .core.compatibility import (
    normalize_compatibility as _parse_compatibility,
)
from .core.compatibility import (
    workload_compatibility_for_target as _compat_workload_compatibility_for_target,
)
from .core.config import settings

logger = logging.getLogger(__name__)

CATALOG_OSINFO_TIMEOUT_SECONDS = 30
_ATTACK_TECHNIQUE_RE = re.compile(r"^T\d{4}(\.\d{3})?$")


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


class ScenarioPackageError(ValueError):
    """Raised when a shareable scenario package is malformed or unsafe."""


_PACKAGE_METADATA_NAME = "metadata.yaml"
_PACKAGE_SCENARIO_NAME = "scenario.yaml"
_PACKAGE_RESOURCE_FIELDS = {"vcpus", "memory_mb", "disk_gb"}
PACKAGE_MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_PACKAGE_MAX_FILES = 128
_PACKAGE_MAX_MEMBER_BYTES = 10 * 1024 * 1024
_PACKAGE_MAX_TOTAL_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
_PACKAGE_MAX_COMPRESSION_RATIO = 1000.0


def _safe_package_member(name: str) -> str:
    member = name
    path = PurePosixPath(member)
    parts = member.split("/")
    if (
        not member
        or "\\" in member
        or member.startswith("/")
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
        or any(":" in part for part in parts)
    ):
        raise ScenarioPackageError(f"unsafe package path: {name}")
    return member


def _validate_package_info(info: zipfile.ZipInfo) -> None:
    _safe_package_member(info.filename)
    if info.is_dir():
        raise ScenarioPackageError(f"unsafe package path: {info.filename}")
    if info.flag_bits & 0x1:
        raise ScenarioPackageError("encrypted scenario package members are not supported")
    mode = (info.external_attr >> 16) & 0o170000
    if mode in {stat.S_IFLNK, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, stat.S_IFSOCK}:
        raise ScenarioPackageError(f"unsupported scenario package member type: {info.filename}")
    if info.file_size > _PACKAGE_MAX_MEMBER_BYTES:
        raise ScenarioPackageError(f"scenario package member too large: {info.filename}")
    if info.file_size and (
        info.compress_size == 0 or info.file_size / info.compress_size > _PACKAGE_MAX_COMPRESSION_RATIO
    ):
        raise ScenarioPackageError(f"scenario package member compression ratio too high: {info.filename}")


def _safe_base_relative_file(rel_path: str) -> Path:
    member = _safe_package_member(rel_path)
    candidate = (settings.base_dir / member).resolve()
    base = settings.base_dir.resolve()
    if candidate == base or base not in candidate.parents:
        raise ScenarioPackageError(f"unsafe script path: {rel_path}")
    if not candidate.is_file():
        raise ScenarioPackageError(f"script not found: {rel_path}")
    return candidate


def build_scenario_package(
    recipe: dict[str, Any],
    *,
    metadata: dict[str, Any] | None = None,
    script_paths: list[str] | None = None,
) -> bytes:
    """Bundle a Playground recipe, package metadata, and optional repo-relative scripts as a zip."""
    package_metadata = {
        "schema_version": 1,
        "package_type": "astinuslab.playground.scenario",
        **(metadata or {}),
    }
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(_PACKAGE_METADATA_NAME, yaml.safe_dump(package_metadata, sort_keys=True))
        archive.writestr(_PACKAGE_SCENARIO_NAME, yaml.safe_dump(recipe, sort_keys=False))
        for rel_path in script_paths or []:
            member = _safe_package_member(rel_path)
            source = _safe_base_relative_file(member)
            archive.write(source, member)
    return payload.getvalue()


def read_scenario_package(data: bytes) -> dict[str, Any]:
    """Parse and validate a shareable scenario package zip without writing files."""
    if len(data) > PACKAGE_MAX_UPLOAD_BYTES:
        raise ScenarioPackageError("scenario package zip is too large")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ScenarioPackageError("invalid scenario package zip") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > _PACKAGE_MAX_FILES:
            raise ScenarioPackageError("scenario package contains too many files")
        total_uncompressed = 0
        seen_names: dict[str, str] = {}
        for info in infos:
            _validate_package_info(info)
            normalized = unicodedata.normalize("NFC", info.filename)
            folded = normalized.casefold()
            if normalized in seen_names or folded in seen_names:
                raise ScenarioPackageError(f"duplicate scenario package member: {info.filename}")
            seen_names[normalized] = info.filename
            seen_names[folded] = info.filename
            total_uncompressed += info.file_size
            if total_uncompressed > _PACKAGE_MAX_TOTAL_UNCOMPRESSED_BYTES:
                raise ScenarioPackageError("scenario package uncompressed payload is too large")
        names = [info.filename.replace("\\", "/") for info in infos]
        if _PACKAGE_SCENARIO_NAME not in names:
            raise ScenarioPackageError("scenario package missing scenario.yaml")
        try:
            recipe_payload = archive.read(_PACKAGE_SCENARIO_NAME)
            metadata_payload = archive.read(_PACKAGE_METADATA_NAME) if _PACKAGE_METADATA_NAME in names else None
        except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
            raise ScenarioPackageError("invalid scenario package zip") from exc
        try:
            recipe = yaml.safe_load(recipe_payload) or {}
            metadata = yaml.safe_load(metadata_payload) if metadata_payload is not None else {}
        except yaml.YAMLError as exc:
            raise ScenarioPackageError("scenario package contains invalid YAML") from exc
        if not isinstance(recipe, dict):
            raise ScenarioPackageError("scenario.yaml must contain a mapping")
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise ScenarioPackageError("metadata.yaml must contain a mapping")
        scripts = [name for name in names if name not in {_PACKAGE_METADATA_NAME, _PACKAGE_SCENARIO_NAME}]
        return {"metadata": metadata, "recipe": recipe, "scripts": scripts}


def apply_scenario_package_resource_overrides(
    recipe: dict[str, Any],
    overrides: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    """Return recipe copy with per-VM import-time machine resource overrides applied by label."""
    updated = copy.deepcopy(recipe)
    if not overrides:
        return updated
    vms = updated.get("vms")
    if not isinstance(vms, list):
        raise ScenarioPackageError("scenario recipe must contain a vms list before applying resource overrides")
    by_label = {vm.get("label"): vm for vm in vms if isinstance(vm, dict) and vm.get("label")}
    for label, resources in overrides.items():
        if label not in by_label:
            raise ScenarioPackageError(f"resource override references unknown VM: {label}")
        if not isinstance(resources, dict):
            raise ScenarioPackageError(f"resource override for {label} must be an object")
        for key, value in resources.items():
            if key not in _PACKAGE_RESOURCE_FIELDS:
                raise ScenarioPackageError(f"unsupported resource override for {label}: {key}")
            by_label[label][key] = value
    return updated


def _preferred_catalog_path(path: Path) -> Path:
    if path.name.endswith(".example.yaml"):
        preferred = path.with_name(path.name.replace(".example.yaml", ".yaml"))
        if preferred.exists():
            return preferred
    elif not path.exists() and path.suffix == ".yaml":
        example = path.with_name(f"{path.stem}.example.yaml")
        if example.exists():
            return example
    return path


def _catalog_stamp(path: Path) -> tuple:
    try:
        stat = os.stat(path)
    except OSError:
        return (str(path), None)
    return (str(path), stat.st_mtime_ns, stat.st_size)


# ── Image directory auto-discovery ─────────────────────────────────────────────


def invalidate_catalog_caches() -> None:
    """Clear process-local catalog caches after catalog or image metadata changes."""
    _load_targets_cached.cache_clear()
    _load_distro_profiles.cache_clear()
    _load_attackers_cached.cache_clear()
    _load_network_nodes_cached.cache_clear()
    _load_workloads_cached.cache_clear()
    _load_scenarios_cached.cache_clear()


def _osinfo_variants(cache_dir: Path) -> set[str]:
    env = {**os.environ, "XDG_CACHE_HOME": str(cache_dir)}
    try:
        result = subprocess.run(
            ["virt-install", "--osinfo", "list"],
            text=True,
            capture_output=True,
            check=True,
            env=env,
            timeout=CATALOG_OSINFO_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return {"generic"}

    supported: set[str] = set()
    for line in result.stdout.splitlines():
        token = line.strip().split(",", 1)[0].strip()
        if token:
            supported.add(token)
    supported.add("generic")
    return supported


@lru_cache(maxsize=1)
def _supported_os_variants() -> set[str]:
    """Return osinfo short IDs supported by the local virt-install.

    The on-disk cache is an optimisation. Under separate runtime identities the
    shared scratch directory belongs to the runner, so the API cannot write it;
    failing the call there would turn an optional cache into an HTTP 500 on
    every VM spawn. Fall back to a private cache instead of degrading the
    variant list, which would silently change the osinfo choice for a guest.
    """
    cache_dir = settings.storage_dir / "tmp" / "osinfo-cache"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        usable = os.access(cache_dir, os.W_OK | os.X_OK)
    except OSError:
        usable = False
    if usable:
        return _osinfo_variants(cache_dir)
    with tempfile.TemporaryDirectory(prefix="astinuslab-osinfo-") as private_cache:
        return _osinfo_variants(Path(private_cache))


def is_windows_family(family: str | None) -> bool:
    """Return True when a target/distro family denotes a Windows guest.

    Windows guests take a different provisioning, guest-access, and Volatility 3
    symbol path than the Linux default, so callers branch on this.
    """
    return family is not None and family.strip().lower() == "windows"


def _resolve_os_variant(requested: str | None, distro: str | None = None) -> str:
    """Return a locally supported os_variant, falling back safely if needed."""
    supported = _supported_os_variants()
    if requested and requested in supported:
        return requested

    if distro:
        distro_unknown = f"{distro}-unknown"
        if distro_unknown in supported:
            return distro_unknown

    return "generic"


@lru_cache(maxsize=1)
def _load_distro_profiles() -> dict:
    catalog_dir = settings.data_dir / "config" / "catalog"
    # Prefer the live profiles; fall back to the committed example (profiles.yaml
    # is gitignored and absent in CI), mirroring the other catalog loaders.
    profile_path = catalog_dir / "profiles.yaml"
    if not profile_path.exists():
        profile_path = catalog_dir / "profiles.example.yaml"
    if not profile_path.exists():
        return {"profiles": []}
    with profile_path.open() as f:
        return yaml.safe_load(f)


def _infer_from_filename(filename: str) -> dict[str, Any]:
    name = filename.lower()
    profiles_data = _load_distro_profiles()
    profiles = profiles_data.get("profiles", [])

    for profile in profiles:
        distro = profile.get("id")
        family = profile.get("family", "generic")
        default_user = profile.get("default_username", "ubuntu")
        cloud_init_profile = profile.get("cloud_init_profile")

        for rule in profile.get("rules", []):
            pattern = rule.get("pattern")
            match = re.search(pattern, name)
            if match:
                version = rule.get("version", "unknown")
                if not version or version == "unknown":
                    if match.groups():
                        version = match.group(1)

                os_variant = rule.get("os_variant", "generic")
                machine_type = rule.get("machine_type")
                filesystem_type = rule.get("filesystem_type")

                resolved_os_variant = _resolve_os_variant(os_variant, distro=distro)

                result = {
                    "distro": distro,
                    "version": version,
                    "family": family,
                    "default_username": default_user,
                    "os_variant": resolved_os_variant,
                    "machine_type": machine_type,
                    "cloud_init_profile": cloud_init_profile,
                    "inject_network_config": None,
                }
                if filesystem_type:
                    result["filesystem_type"] = filesystem_type
                return result

    # Strict fallback for unknown images
    return {
        "distro": "unknown",
        "version": "unknown",
        "family": "generic",
        "default_username": "root",
        "os_variant": "generic",
        "machine_type": None,
        "cloud_init_profile": "generic",
        "inject_network_config": None,
    }


def _scan_images_dir(images_dir: Path) -> list[dict[str, Any]]:
    """Return a target entry for every .qcow2/.img file found in images_dir."""
    if not images_dir.exists():
        return []
    targets: list[dict[str, Any]] = []
    for path in sorted(images_dir.iterdir()):
        from .images.image_inspection import BOOTABLE_IMAGE_EXTENSIONS

        if path.suffix.lower() not in BOOTABLE_IMAGE_EXTENSIONS:
            continue
        from .images.image_inspection import UnverifiedImageMetadata, classify_image_preflight, ensure_metadata

        meta_obj = ensure_metadata(path)
        preflight = classify_image_preflight(path, meta_obj)
        is_windows = getattr(meta_obj, "platform", None) == "windows"
        meta = {
            "distro": meta_obj.distro,
            "version": meta_obj.version,
            "default_username": meta_obj.default_username,
            "os_variant": meta_obj.os_variant,
            "machine_type": meta_obj.machine_type,
            "family": meta_obj.family,
            "cloud_init_profile": meta_obj.cloud_init_profile,
            "inject_network_config": meta_obj.inject_network_config,
            "platform": getattr(meta_obj, "platform", None),
            "arch": meta_obj.arch,
        }
        source_type = path.suffix.lstrip(".")
        target_id = re.sub(r"[^a-z0-9-]", "-", path.stem.lower()).strip("-")[:48]
        if meta["distro"] == "unknown":
            cleaned = re.sub(r"[-_.]+", " ", path.stem).strip()
            display_name = f"{cleaned}  [{path.name}]"
        else:
            display_name = f"{meta['distro']} {meta['version']}  [{path.name}]"
        targets.append(
            {
                "id": target_id,
                "distro": meta["distro"],
                "version": meta["version"],
                "family": meta["family"],
                "platform": meta["platform"],
                "enabled": not isinstance(meta_obj, UnverifiedImageMetadata)
                and (not is_windows or bool(preflight["reusable"])),
                "image_available": True,
                "source_type": source_type,
                "supports_cloud_image": True,
                "supports_iso_autoinstall": False,
                "image_ref": path.name,
                "os_variant": meta["os_variant"],
                "machine_type": meta["machine_type"],
                "cloud_init_profile": meta["cloud_init_profile"],
                "inject_network_config": meta["inject_network_config"],
                "default_username": meta["default_username"],
                "arch": meta["arch"],
                "disk_gb": 40 if is_windows else 20,
                "cloud_init": False if is_windows else bool(meta["cloud_init_profile"]),
                "reusable": bool(preflight["reusable"]),
                "isf_supported": bool(preflight["isf_supported"]),
                "preflight_reasons": list(preflight["reasons"]),
                "destroy_vm_after_job": True,
                "keep_failed_vm": False,
                "display_name": display_name,
            }
        )
    return targets


# ── Targets ────────────────────────────────────────────────────────────────────


@lru_cache(maxsize=2)  # process-scoped cache; catalog stamp revalidates YAML changes
def _load_targets_cached(stamp: tuple) -> list[dict[str, Any]]:
    """Return enabled target catalog entries plus discovered local bootable images."""
    del stamp
    # Load curated YAML catalog (may not exist)
    yaml_items: list[dict[str, Any]] = []
    catalog_path = _preferred_catalog_path(settings.targets_catalog_path)
    if catalog_path.exists():
        raw = _load_yaml(catalog_path)
        for item in raw.get("targets", []):
            if not item.get("id"):
                continue
            distro = item.get("distro", {})
            image = item.get("image", {})
            virtualization = item.get("virtualization", {})
            lifecycle = item.get("lifecycle", {})
            source_type = image.get("source_type", "unknown")
            image_ref = image.get("image_ref")
            image_available = bool(image_ref and (settings.images_dir / image_ref).exists())
            d = distro.get("slug", "unknown")
            v = distro.get("version", "unknown")
            resolved_os_variant = _resolve_os_variant(virtualization.get("os_variant"), distro=d)
            dump_output = item.get("dump_output", {})
            yaml_items.append(
                {
                    "id": item["id"],
                    "distro": d,
                    "version": v,
                    "family": distro.get("family", "debian"),
                    "enabled": item.get("enabled", False) and image_available,
                    "image_available": image_available,
                    "source_type": source_type,
                    "supports_cloud_image": source_type in {"qcow2", "img"},
                    "supports_iso_autoinstall": False,
                    "image_ref": image_ref,
                    "os_variant": resolved_os_variant,
                    "machine_type": virtualization.get("machine_type"),
                    "network_profile": virtualization.get("network_profile"),
                    "filesystem_type": virtualization.get("filesystem_type") or image.get("filesystem_type"),
                    "cloud_init_profile": image.get("cloud_init_profile"),
                    "inject_network_config": image.get("inject_network_config"),
                    "vcpu": virtualization.get("vcpu"),
                    "memory_mb": virtualization.get("memory_mb"),
                    "disk_gb": virtualization.get("disk_gb"),
                    "default_username": image.get("default_username", "ubuntu"),
                    "cloud_init": image.get("cloud_init", False),
                    "destroy_vm_after_job": lifecycle.get("destroy_vm_after_job", True),
                    "keep_failed_vm": lifecycle.get("keep_failed_vm", False),
                    "guest_mount_path": dump_output.get("guest_mount_path"),
                    "display_name": f"{d} {v}  [{image_ref}]" if image_ref else f"{d} {v}",
                }
            )

    # Scan images directory and add any image not already covered by YAML
    yaml_refs = {item["image_ref"] for item in yaml_items if item.get("image_ref")}
    yaml_ids = {item["id"] for item in yaml_items}
    scanned = _scan_images_dir(settings.images_dir)
    for entry in scanned:
        if entry["image_ref"] not in yaml_refs and entry["id"] not in yaml_ids:
            yaml_items.append(entry)

    return yaml_items


def load_targets() -> list[dict[str, Any]]:
    """Return enabled target catalog entries plus discovered local bootable images."""
    catalog_path = _preferred_catalog_path(settings.targets_catalog_path)
    return _load_targets_cached(_catalog_stamp(catalog_path))


def _normalize_playground_target_image(entry: dict[str, Any]) -> dict[str, Any]:
    image_ref = entry.get("image_ref", "")
    return {
        "id": entry["id"],
        "label": entry.get("display_name") or f"{entry.get('distro', 'unknown')} {entry.get('version', 'unknown')}",
        "role": "target",
        "image_ref": image_ref,
        "path": entry.get("path", str(settings.images_dir / image_ref) if image_ref else ""),
        "format": entry.get("format") or entry.get("source_type", "qcow2"),
        "architecture": entry.get("architecture", entry.get("arch", "x86_64")),
        "username": entry.get("default_username", ""),
        "distro": entry.get("distro", "unknown"),
        "version": entry.get("version", "unknown"),
        "family": entry.get("family", "generic"),
        "disk_gb": entry.get("disk_gb"),
    }


def _normalize_playground_attacker_image(entry: dict[str, Any]) -> dict[str, Any]:
    image_ref = entry.get("image_ref", "")
    return {
        "id": entry["id"],
        "label": entry.get("display_name", entry["id"]),
        "role": "attacker",
        "image_ref": image_ref,
        "path": str(settings.images_dir / image_ref) if image_ref else "",
        "format": "qcow2",
        "architecture": "x86_64",
        "username": entry.get("username", "kali"),
        "cloud_init": bool(entry.get("cloud_init", False)),
    }


@lru_cache(maxsize=2)  # process-scoped cache; catalog stamp revalidates YAML changes
def _load_network_nodes_cached(stamp: tuple) -> list[dict[str, Any]]:
    """Return network/appliance images available for Playground topology roles."""
    del stamp
    catalog_path = _preferred_catalog_path(settings.network_nodes_catalog_path)
    if not catalog_path.exists():
        return []
    raw = _load_yaml(catalog_path)
    nodes: list[dict[str, Any]] = []
    for item in raw.get("network_nodes", []):
        image_ref = item.get("image_ref")
        image_available = bool(image_ref and (settings.images_dir / image_ref).exists())
        nodes.append({**item, "image_available": image_available})
    return nodes


def load_network_nodes() -> list[dict[str, Any]]:
    """Return network/appliance images available for Playground topology roles."""
    catalog_path = _preferred_catalog_path(settings.network_nodes_catalog_path)
    return _load_network_nodes_cached(_catalog_stamp(catalog_path))


def _normalize_playground_network_image(entry: dict[str, Any]) -> dict[str, Any]:
    image_ref = entry.get("image_ref", "")
    return {
        "id": entry["id"],
        "label": entry.get("display_name", entry["id"]),
        "role": "network",
        "image_ref": image_ref,
        "path": str(settings.images_dir / image_ref) if image_ref else "",
        "format": entry.get("format", "qcow2"),
        "architecture": entry.get("architecture", "x86_64"),
        "username": entry.get("username", "root"),
        "os_variant": entry.get("os_variant", "generic"),
        "cloud_init": bool(entry.get("cloud_init", False)),
        "function_tags": entry.get("function_tags", []),
        "defaults": entry.get("defaults", {}),
        "disk_gb": (entry.get("defaults") or {}).get("disk_gb"),
    }


def list_playground_images_for_role(role: str = "target") -> list[dict[str, Any]]:
    """Filter normalized Playground images by role while preserving role-specific metadata."""
    if role in {"target", "vm"}:
        return [_normalize_playground_target_image(entry) for entry in list_playground_usable_images()]
    if role == "attacker":
        return [
            _normalize_playground_attacker_image(entry)
            for entry in load_attackers()
            if entry.get("image_available", True)
        ]
    if role == "network":
        return [
            _normalize_playground_network_image(entry)
            for entry in load_network_nodes()
            if entry.get("enabled", True) and entry.get("image_available", True)
        ]
    raise ValueError(f"unsupported playground image role: {role}")


# ── Scenarios ──────────────────────────────────────────────────────────────────


def _read_script_content(rel_path: str) -> str | None:
    """Return file content of a guest script, or None if missing."""
    path = settings.base_dir / rel_path
    if path.exists():
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return None
    return None


def _parse_single_workload(item: dict[str, Any]) -> dict[str, Any]:
    display_name = item.get("display_name", item["id"])
    services = item.get("services", [])
    return {
        "id": item["id"],
        "display_name": display_name,
        "tags": item.get("tags", []),
        "install_script": item.get("install_script"),
        "health_check": item.get("health_check", {}),
        "services": services,
        "service_labels": _service_labels(services, display_name),
        "cve_ids": item.get("cve_ids", []),
        "cve_context": item.get("cve_context"),
        "network_isolation": item.get("network_isolation", False),
        "is_stack": False,
        "components": [],
        "attacker_vm": item.get("attacker_vm"),
        "compatibility": _parse_compatibility(item.get("compatibility")),
    }


@lru_cache(maxsize=2)  # process-scoped cache; catalog stamp revalidates YAML changes
def _load_workloads_cached(stamp: tuple) -> list[dict[str, Any]]:
    """Load and normalize workload definitions used by manual-trigger labs and Playground provisioning."""
    del stamp
    catalog_path = _preferred_catalog_path(settings.workloads_catalog_path)
    if not catalog_path.exists():
        return []
    raw = _load_yaml(catalog_path)
    singles: dict[str, dict[str, Any]] = {}
    result: list[dict[str, Any]] = []

    for item in raw.get("workloads", []):
        if not item.get("enabled", True):
            continue
        if item.get("type") == "stack":
            continue  # stacks resolved in second pass
        entry = _parse_single_workload(item)
        singles[entry["id"]] = entry
        result.append(entry)

    for item in raw.get("workloads", []):
        if not item.get("enabled", True) or item.get("type") != "stack":
            continue
        component_ids: list[str] = item.get("components", [])
        # Aggregate services and CVEs from components
        agg_services: list[dict] = []
        agg_cve_ids: list[str] = []
        seen_cves: set[str] = set()
        net_isolation = False
        for cid in component_ids:
            comp = singles.get(cid)
            if comp is None:
                continue
            agg_services.extend(comp.get("services", []))
            for cve in comp.get("cve_ids", []):
                if cve not in seen_cves:
                    seen_cves.add(cve)
                    agg_cve_ids.append(cve)
            if comp.get("network_isolation"):
                net_isolation = True

        services = item.get("services") or agg_services
        display_name = item.get("display_name", item["id"])
        result.append(
            {
                "id": item["id"],
                "display_name": display_name,
                "tags": item.get("tags", []),
                "install_script": None,
                "health_check": {},  # per-component health checks run during provisioning
                "services": services,
                "service_labels": _service_labels(services, display_name),
                "cve_ids": item.get("cve_ids") or agg_cve_ids,
                "cve_context": item.get("cve_context"),
                "network_isolation": item.get("network_isolation", net_isolation),
                "is_stack": True,
                "components": component_ids,
                "attacker_vm": item.get("attacker_vm"),
                "compatibility": _parse_compatibility(item.get("compatibility")),
            }
        )

    return result


def load_workloads() -> list[dict[str, Any]]:
    """Load and normalize workload definitions used by manual-trigger labs and Playground provisioning."""
    catalog_path = _preferred_catalog_path(settings.workloads_catalog_path)
    return _load_workloads_cached(_catalog_stamp(catalog_path))


def target_by_id(target_id: str) -> dict[str, Any] | None:
    return next((t for t in load_targets() if t["id"] == target_id), None)


def workload_by_id(workload_id: str) -> dict[str, Any] | None:
    return next((w for w in load_workloads() if w["id"] == workload_id), None)


def annotate_workloads_for_target(
    target_id: str | None,
    workloads: list[dict[str, Any]] | None = None,
    *,
    only_compatible: bool = False,
) -> list[dict[str, Any]]:
    """Attach compatibility status/reason to workload entries for a selected target."""
    source = list(workloads if workloads is not None else load_workloads())
    if not target_id:
        return source

    target = target_by_id(target_id)
    if target is None:
        return []

    workload_map = {workload["id"]: workload for workload in source}
    annotated = [
        {
            **workload,
            **workload_compatibility_for_target(workload, target, workload_map=workload_map),
        }
        for workload in source
    ]
    if only_compatible:
        return [workload for workload in annotated if workload["compatibility_status"] == "compatible"]
    return annotated


def workload_compatibility_for_target(
    workload: dict[str, Any],
    target: dict[str, Any],
    workload_map: dict[str, dict[str, Any]] | None = None,
) -> dict[str, str | None]:
    """Evaluate workload-to-target compatibility using normalized catalog metadata."""
    source = workload_map or {item["id"]: item for item in load_workloads()}
    return _compat_workload_compatibility_for_target(workload, target, workload_map=source)


def search_workloads(query: str, workloads: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Search workload names, tags, services, CVEs, and version labels for UI filtering."""
    source = list(workloads if workloads is not None else load_workloads())
    terms = [part.lower() for part in query.split() if part.strip()]
    if not terms:
        return source
    result: list[dict[str, Any]] = []
    for workload in source:
        haystack = _workload_search_text(workload)
        if all(term in haystack for term in terms):
            result.append(workload)
    return result


def validate_workload_catalog(workloads: list[dict[str, Any]] | None = None) -> list[str]:
    """Return operator-facing validation errors for malformed workload catalog entries."""
    source = workloads if workloads is not None else load_workloads()
    by_id = {item["id"]: item for item in source}
    errors: list[str] = []
    for workload in source:
        compatibility = workload.get("compatibility") or {}
        if not compatibility.get("include") and not compatibility.get("exclude"):
            errors.append(f"workload {workload['id']} has no compatibility rules")
        if workload.get("is_stack"):
            for component_id in workload.get("components", []):
                if component_id not in by_id:
                    errors.append(f"workload {workload['id']} references missing component {component_id}")
        else:
            script = workload.get("install_script")
            if not script:
                errors.append(f"workload {workload['id']} has no install_script")
            elif not (settings.base_dir / script).exists():
                errors.append(f"workload {workload['id']} install_script not found: {script}")
    return errors


def workloads_by_cve(cve_id: str) -> list[dict[str, Any]]:
    """Return workloads that list the given CVE ID (case-insensitive)."""
    needle = cve_id.upper()
    return [w for w in load_workloads() if needle in [c.upper() for c in w.get("cve_ids", [])]]


def all_cve_ids() -> list[str]:
    """Return sorted deduplicated list of all CVE IDs across all workloads."""
    seen: set[str] = set()
    result: list[str] = []
    for w in load_workloads():
        for cve in w.get("cve_ids", []):
            upper = cve.upper()
            if upper not in seen:
                seen.add(upper)
                result.append(upper)
    return sorted(result)


def all_service_names() -> list[str]:
    """Return distinct service names for workload filtering and UI facet lists."""
    seen: set[str] = set()
    result: list[str] = []
    for workload in load_workloads():
        for service in workload.get("services", []):
            name = str(service.get("name", "")).strip()
            if not name:
                continue
            normalized = name.lower()
            if normalized in seen:
                continue
            seen.add(normalized)
            result.append(name)
    return sorted(result, key=str.lower)


def _service_labels(services: list[dict[str, Any]], display_name: str) -> list[str]:
    result: list[str] = []
    for service in services:
        label = _service_label(service, display_name)
        if label:
            result.append(label)
    return result


def _service_label(service: dict[str, Any], display_name: str) -> str:
    name = str(service.get("name", "")).strip()
    if not name:
        return ""
    version = str(service.get("version", "")).strip()
    if not version:
        version = _infer_service_version(name, display_name)
    return f"{name} {version}".strip()


def _infer_service_version(service_name: str, display_name: str) -> str:
    if not display_name:
        return ""
    match = re.search(rf"{re.escape(service_name)}\s+([0-9][A-Za-z0-9._-]*)", display_name, flags=re.IGNORECASE)
    return match.group(1) if match else ""


def _workload_search_text(workload: dict[str, Any]) -> str:
    parts = [
        str(workload.get("id", "")),
        str(workload.get("display_name", "")),
        str(workload.get("cve_context", "")),
    ]
    parts.extend(str(tag) for tag in workload.get("tags", []))
    parts.extend(str(cve) for cve in workload.get("cve_ids", []))
    parts.extend(str(component) for component in workload.get("components", []))
    parts.extend(str(label) for label in workload.get("service_labels", []))
    for service in workload.get("services", []):
        parts.extend(
            [
                str(service.get("name", "")),
                str(service.get("version", "")),
                str(service.get("port", "")),
                str(service.get("url_path", "")),
            ]
        )
    return " ".join(parts).lower()


@lru_cache(maxsize=2)  # process-scoped cache; catalog stamp revalidates YAML changes
def _load_attackers_cached(stamp: tuple) -> list[dict[str, Any]]:
    """Return attacker VM entries.

    Merges two sources (YAML catalog takes priority):
    1. ``config/catalog/attackers.yaml`` explicit entries
    2. Auto-detected attacker images in the images directory (kali, parrot, …)
    """
    del stamp
    result: list[dict[str, Any]] = []
    seen_refs: set[str] = set()

    # ── Explicit YAML entries ────────────────────────────────────────────────
    if settings.attackers_catalog_path.exists():
        raw = _load_yaml(settings.attackers_catalog_path)
        for item in raw.get("attackers", []):
            if not item.get("enabled", True):
                continue
            image_ref = item.get("image_ref") or ""
            image_available = bool(image_ref and (settings.images_dir / image_ref).exists())
            result.append(
                {
                    "id": item["id"],
                    "display_name": item.get("display_name", item["id"]),
                    "image_ref": image_ref,
                    "username": item.get("username", "kali"),
                    "os_variant": item.get("os_variant", "generic"),
                    "cloud_init": bool(item.get("cloud_init", False)),
                    "image_available": image_available,
                }
            )
            if image_ref:
                seen_refs.add(image_ref)

    # ── Auto-detect from images directory ───────────────────────────────────
    if settings.images_dir.exists():
        for path in sorted(settings.images_dir.iterdir()):
            if path.suffix not in {".qcow2", ".img"}:
                continue
            if path.name in seen_refs:
                continue
            meta = _infer_from_filename(path.name)
            if meta.get("cloud_init_profile") != "attacker":
                continue
            distro = meta["distro"]
            version = meta["version"]
            entry_id = re.sub(r"[^a-z0-9-]", "-", path.stem.lower()).strip("-")[:48]
            display_name = f"{distro.capitalize()} {version}  [{path.name}]"
            result.append(
                {
                    "id": entry_id,
                    "display_name": display_name,
                    "image_ref": path.name,
                    "username": meta.get("default_username", "user"),
                    "os_variant": meta["os_variant"],
                    "image_available": True,
                }
            )
            seen_refs.add(path.name)

    return result


def load_attackers() -> list[dict[str, Any]]:
    """Return attacker VM entries.

    Merges two sources (YAML catalog takes priority):
    1. ``config/catalog/attackers.yaml`` explicit entries
    2. Auto-detected attacker images in the images directory (kali, parrot, …)
    """
    return _load_attackers_cached(_catalog_stamp(settings.attackers_catalog_path))


def attacker_by_id(attacker_id: str) -> dict[str, Any] | None:
    return next((a for a in load_attackers() if a["id"] == attacker_id), None)


def _target_image_path(entry: dict[str, Any]) -> Path | None:
    image_ref = entry.get("image_ref") or ""
    raw_path = entry.get("path")
    path = Path(raw_path) if raw_path else settings.images_dir / str(image_ref)
    try:
        root = settings.images_dir.resolve()
        resolved = path.resolve()
    except OSError:
        return None
    if not resolved.is_relative_to(root):
        return None
    return path


def _has_required_image_fields(entry: dict[str, Any]) -> bool:
    """Return True if a normalized target entry has fields required for Playground boot."""
    if not entry.get("id"):
        return False
    if not entry.get("image_ref"):
        return False
    if not entry.get("source_type"):
        return False
    if not entry.get("supports_cloud_image", True):
        return False
    if not entry.get("distro"):
        return False
    if not entry.get("default_username"):
        return False
    if _target_image_path(entry) is None:
        return False
    return True


def list_playground_usable_images() -> list[dict[str, Any]]:
    """Return catalog images usable in playground mode.

    Skips scenario validation: an entry needs only normalized boot fields
    (id, image_ref, source_type, distro, default_username, local path).
    Scenario/workload fields are not required.

    Returns normalized flat dicts (mirroring the shape produced by
    load_targets()) so callers can access top-level keys such as
    ``entry["path"]``, ``entry["format"]``, ``entry["architecture"]``,
    and ``entry["default_username"]`` without digging into nested sub-dicts.

    Returns [] if the catalog path does not exist or is empty.
    """
    catalog_path = _preferred_catalog_path(settings.targets_catalog_path)
    if not catalog_path.exists():
        return []
    result: list[dict[str, Any]] = []
    for entry in load_targets():
        if not entry.get("enabled"):
            continue
        if not entry.get("image_available"):
            continue
        if not _has_required_image_fields(entry):
            continue
        image_ref = entry.get("image_ref", "")
        path = _target_image_path(entry)
        if path is None:
            continue
        result.append(
            {
                "id": entry["id"],
                "path": str(path),
                "format": entry.get("format") or entry.get("source_type", "qcow2"),
                "architecture": entry.get("architecture", entry.get("arch", "x86_64")),
                "default_username": entry.get("default_username", ""),
                # preserve additional fields used elsewhere
                "image_ref": image_ref,
                "source_type": entry.get("source_type", "qcow2"),
                "distro": entry.get("distro", "unknown"),
                "version": entry.get("version", "unknown"),
                "family": entry.get("family", "debian"),
                "enabled": entry.get("enabled", False),
                "cloud_init": bool(entry.get("cloud_init", False)),
            }
        )
    return result


@lru_cache(maxsize=2)  # process-scoped cache; catalog stamp revalidates YAML changes
def _load_scenarios_cached(stamp: tuple) -> list[dict[str, Any]]:
    """Return scenario catalog entries with inline script content for post-boot execution modes."""
    del stamp
    catalog_path = _preferred_catalog_path(settings.scenarios_catalog_path)
    raw = _load_yaml(catalog_path)
    result = []
    for item in raw.get("scenarios", []):
        script_cfg = item.get("script", {})
        script_path = script_cfg.get("path") if script_cfg.get("enabled") else None
        script_content = _read_script_content(script_path) if script_path else None
        raw_attack = item.get("attack", []) or []
        attack_techniques = []
        for tech in raw_attack:
            tech_id = str(tech).strip().upper()
            if _ATTACK_TECHNIQUE_RE.fullmatch(tech_id):
                attack_techniques.append(tech_id)
            else:
                logger.warning("scenario %s: ignoring invalid ATT&CK technique id: %r", item["id"], tech)
        result.append(
            {
                "id": item["id"],
                "mode": item["mode"],
                "description": item["description"],
                "enabled": item.get("enabled", False),
                "script_path": script_path,
                "script_content": script_content,
                "script": script_cfg,
                "attack_techniques": attack_techniques,
            }
        )
    return result


def load_scenarios() -> list[dict[str, Any]]:
    """Return scenario catalog entries with inline script content for post-boot execution modes."""
    catalog_path = _preferred_catalog_path(settings.scenarios_catalog_path)
    return _load_scenarios_cached(_catalog_stamp(catalog_path))


# Compatibility for older tests/callers that explicitly clear loader caches.
load_targets.cache_clear = _load_targets_cached.cache_clear  # type: ignore[attr-defined]
load_network_nodes.cache_clear = _load_network_nodes_cached.cache_clear  # type: ignore[attr-defined]
load_workloads.cache_clear = _load_workloads_cached.cache_clear  # type: ignore[attr-defined]
load_attackers.cache_clear = _load_attackers_cached.cache_clear  # type: ignore[attr-defined]
load_scenarios.cache_clear = _load_scenarios_cached.cache_clear  # type: ignore[attr-defined]


def runtime_workload_context_or_raise(
    job_id: str,
    workload: dict,
    target: dict,
    metadata: dict[str, str | None],
    workload_map: dict[str, dict] | None = None,
) -> dict:
    """Re-check workload compatibility against the *detected* guest metadata; raise on mismatch."""
    from .core.compatibility import runtime_workload_compatibility_for_metadata

    source = workload_map or {item["id"]: item for item in load_workloads()}
    result = runtime_workload_compatibility_for_metadata(workload, target, metadata, workload_map=source)
    runtime_target = result["runtime_target"]
    if result["compatibility_status"] != "compatible":
        reason = result["compatibility_reason"] or "compatibility is not declared"
        raise RuntimeError(
            f"Workload '{workload['id']}' is incompatible with detected guest "
            f"{runtime_target.get('distro')} {runtime_target.get('version')} "
            f"for target '{target.get('id')}' in job {job_id}: {reason}"
        )
    return runtime_target
