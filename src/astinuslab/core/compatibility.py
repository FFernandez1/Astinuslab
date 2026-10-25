from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

_REDHAT_FAMILY = {"fedora", "centos", "rocky", "almalinux", "rhel"}
_DEBIAN_FAMILY = {"ubuntu", "debian", "kali", "parrot"}


def normalize_compatibility(raw: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(raw, dict):
        return {"include": [], "exclude": []}
    return {
        "include": [_normalize_selector(item) for item in raw.get("include", []) if isinstance(item, dict)],
        "exclude": [_normalize_selector(item) for item in raw.get("exclude", []) if isinstance(item, dict)],
    }


def workload_compatibility_for_target(
    workload: dict[str, Any],
    target: dict[str, Any],
    workload_map: dict[str, dict[str, Any]] | None = None,
) -> dict[str, str | None]:
    if workload.get("is_stack"):
        source = workload_map or {}
        for component_id in workload.get("components", []):
            component = source.get(component_id)
            if component is None:
                return _result("incompatible", f"Missing component '{component_id}' in workload catalog")
            result = workload_compatibility_for_target(component, target, workload_map=source)
            if result["compatibility_status"] != "compatible":
                reason = result["compatibility_reason"] or "component compatibility is not declared"
                component_status = result["compatibility_status"] or "incompatible"
                return _result(component_status, f"Component '{component_id}' blocked this stack: {reason}")
    return _declared_workload_compatibility(workload, target)


def runtime_target_from_metadata(target: dict[str, Any], metadata: dict[str, str | None]) -> dict[str, Any]:
    distro = (metadata.get("distro_id") or target.get("distro") or "unknown").lower()
    version = str(metadata.get("distro_version") or target.get("version") or "unknown").lower()
    if distro in _REDHAT_FAMILY:
        family = "redhat"
    elif distro in _DEBIAN_FAMILY:
        family = "debian"
    else:
        family = target.get("family") or "debian"
    return {
        **target,
        "distro": distro,
        "version": version,
        "family": family,
        "arch": metadata.get("arch") or target.get("arch"),
        "source": "detected" if metadata.get("distro_id") else "catalog",
    }


def runtime_workload_compatibility_for_metadata(
    workload: dict[str, Any],
    target: dict[str, Any],
    metadata: dict[str, str | None],
    workload_map: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    runtime_target = runtime_target_from_metadata(target, metadata)
    result = workload_compatibility_for_target(workload, runtime_target, workload_map=workload_map)
    return {**result, "runtime_target": runtime_target}


def compatibility_fingerprint(workload: dict[str, Any], *, base_dir: Path | None = None) -> str:
    payload = {
        "id": workload.get("id"),
        "install_script": workload.get("install_script"),
        "install_script_identity": _install_script_identity(workload.get("install_script"), base_dir),
        "components": workload.get("components", []),
        "compatibility": workload.get("compatibility", {}),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _install_script_identity(rel_path: Any, base_dir: Path | None) -> object:
    if not rel_path or base_dir is None:
        return str(rel_path or "")
    path = base_dir / str(rel_path)
    try:
        stat = path.stat()
    except OSError:
        return str(rel_path)
    return {"path": str(rel_path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _declared_workload_compatibility(workload: dict[str, Any], target: dict[str, Any]) -> dict[str, str | None]:
    compatibility = workload.get("compatibility") or {}
    include = compatibility.get("include", [])
    exclude = compatibility.get("exclude", [])

    for selector in exclude:
        matched, _reason = _selector_matches_target(selector, target)
        if matched:
            return _result("incompatible", f"Excluded for {_describe_selector(selector)}")

    if include:
        disk_reason: str | None = None
        for selector in include:
            matched, reason = _selector_matches_target(selector, target)
            if matched:
                return _result("compatible", f"Declared compatible for {_describe_selector(selector)}")
            if reason and "requires at least" in reason:
                disk_reason = reason
        if disk_reason:
            return _result("incompatible", disk_reason)
        return _result(
            "incompatible",
            f"Target {target.get('distro')} {target.get('version')} is not in the workload compatibility matrix",
        )

    if exclude:
        return _result("compatible", "Compatible because no exclusion rule matched this target")

    if workload.get("is_stack"):
        return _result("compatible", "All stack components are declared compatible with this target")

    return _result("unknown", "Compatibility is not declared for this workload")


def _normalize_selector(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "target_ids": _selector_values(raw, "target_id", "target_ids"),
        "families": _selector_values(raw, "family", "families"),
        "distros": _selector_values(raw, "distro", "distros"),
        "versions": _selector_values(raw, "version", "versions"),
        "version_min": _optional_text(raw.get("version_min")),
        "version_max": _optional_text(raw.get("version_max")),
        "arches": _selector_values(raw, "arch", "arches"),
        "min_disk_gb": _optional_int(raw.get("min_disk_gb")),
    }


def _selector_matches_target(selector: dict[str, Any], target: dict[str, Any]) -> tuple[bool, str | None]:
    if not _selector_has_constraints(selector):
        return False, None

    target_id = str(target.get("id", "")).lower()
    family = str(target.get("family", "")).lower()
    distro = str(target.get("distro", "")).lower()
    version = str(target.get("version", "")).lower()
    arch = str(target.get("arch", "")).lower()

    if selector["target_ids"] and target_id not in selector["target_ids"]:
        return False, None
    if selector["families"] and family not in selector["families"]:
        return False, None
    if selector["distros"] and distro not in selector["distros"]:
        return False, None
    if selector["versions"] and version not in selector["versions"]:
        return False, None
    if selector["arches"] and arch not in selector["arches"]:
        return False, None
    if selector["version_min"] and not _version_at_least(version, selector["version_min"]):
        return False, None
    if selector["version_max"] and not _version_at_most(version, selector["version_max"]):
        return False, None
    if selector["min_disk_gb"] is not None:
        disk_gb = _target_disk_gb(target)
        if disk_gb is None or disk_gb < selector["min_disk_gb"]:
            actual = "unknown" if disk_gb is None else f"{disk_gb}GB"
            return False, f"Workload requires at least {selector['min_disk_gb']}GB disk; target has {actual}"
    return True, None


def _selector_has_constraints(selector: dict[str, Any]) -> bool:
    return any(
        [
            selector.get("target_ids"),
            selector.get("families"),
            selector.get("distros"),
            selector.get("versions"),
            selector.get("version_min"),
            selector.get("version_max"),
            selector.get("arches"),
            selector.get("min_disk_gb") is not None,
        ]
    )


def _selector_values(raw: dict[str, Any], *keys: str) -> list[str]:
    result: list[str] = []
    for key in keys:
        value = raw.get(key)
        if value is None:
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            text = str(item).strip().lower()
            if text:
                result.append(text)
    return result


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _target_disk_gb(target: dict[str, Any]) -> int | None:
    value = target.get("disk_gb")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _version_tuple(value: str | None) -> tuple[int, ...]:
    if not value:
        return ()
    return tuple(int(part) for part in re.findall(r"\d+", str(value))[:4])


def _version_at_least(version: str, minimum: str) -> bool:
    parsed = _version_tuple(version)
    return bool(parsed) and parsed >= _version_tuple(minimum)


def _version_at_most(version: str, maximum: str) -> bool:
    parsed = _version_tuple(version)
    return bool(parsed) and parsed <= _version_tuple(maximum)


def _describe_selector(selector: dict[str, Any]) -> str:
    parts: list[str] = []
    if selector["target_ids"]:
        parts.append("target " + ", ".join(selector["target_ids"]))
    if selector["families"]:
        parts.append("family " + ", ".join(selector["families"]))
    if selector["distros"]:
        parts.append("distro " + ", ".join(selector["distros"]))
    if selector["versions"]:
        parts.append("version " + ", ".join(selector["versions"]))
    if selector["version_min"]:
        parts.append(f"version >= {selector['version_min']}")
    if selector["version_max"]:
        parts.append(f"version <= {selector['version_max']}")
    if selector["arches"]:
        parts.append("arch " + ", ".join(selector["arches"]))
    if selector["min_disk_gb"] is not None:
        parts.append(f"disk >= {selector['min_disk_gb']}GB")
    return " / ".join(parts) if parts else "an empty selector"


def _result(status: str, reason: str | None) -> dict[str, str | None]:
    return {"compatibility_status": status, "compatibility_reason": reason}
