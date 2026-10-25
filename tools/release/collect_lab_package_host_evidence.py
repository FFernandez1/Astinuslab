#!/usr/bin/env python3
"""Create and compare private two-host evidence for a locked lab package.

This tool deliberately has no AstinusLab imports.  A real-host lifecycle test
or operator run supplies a small JSON record describing the observed launch,
reset, teardown, topology, and checkpoint results.  The tool binds that record
to the exact package archive and candidate commit, records host facts, and
compares only the reproducibility surface between two hosts.

The resulting manifest is private evidence.  It may contain host software
versions and operator supplied identifiers, so callers must keep it outside
the checkout and must not publish it without review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

SCHEMA_VERSION = "astinuslab-lab-package-host-evidence/v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_HOST_LABELS = {"host-a", "host-b"}
_MAX_ARCHIVE_BYTES = 40 * 1024**3
_MAX_DOCUMENT_BYTES = 2 * 1024**2
_MAX_ASSET_BYTES = 16 * 1024**3
_MAX_LIFECYCLE_BYTES = 2 * 1024**2


class EvidenceError(ValueError):
    """The evidence input is incomplete, unsafe, or inconsistent."""


def _fail(message: str) -> NoReturn:
    raise EvidenceError(message)


def _read_regular(path: Path, *, label: str, maximum: int) -> bytes:
    try:
        metadata = path.lstat()
    except OSError:
        _fail(f"{label} is unavailable: {path}")
    if not metadata or not path.is_file() or path.is_symlink():
        _fail(f"{label} must be a regular non-symlink file: {path}")
    if metadata.st_size > maximum:
        _fail(f"{label} is too large: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise EvidenceError(f"could not read {label}") from exc


def _sha256_file(path: Path) -> tuple[str, int]:
    try:
        metadata = path.lstat()
        if not path.is_file() or path.is_symlink():
            _fail(f"package archive must be a regular non-symlink file: {path}")
        if metadata.st_size > _MAX_ARCHIVE_BYTES:
            _fail("package archive is too large")
        digest = hashlib.sha256()
        total = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                total += len(chunk)
                if total > _MAX_ARCHIVE_BYTES:
                    _fail("package archive is too large")
                digest.update(chunk)
        return digest.hexdigest(), total
    except EvidenceError:
        raise
    except OSError as exc:
        raise EvidenceError("could not read package archive") from exc


def _json_document(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise EvidenceError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        _fail(f"{label} must be a JSON object")
    return value


def _digest_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _package_surface(archive_path: Path) -> dict[str, Any]:
    archive_sha256, archive_size_bytes = _sha256_file(archive_path)
    try:
        with zipfile.ZipFile(archive_path, "r", allowZip64=True) as archive:
            names = set(archive.namelist())
            required = {"manifest.json", "lab.json"}
            if not required.issubset(names):
                _fail("package archive is missing manifest.json or lab.json")
            if len(names) != len(archive.namelist()):
                _fail("package archive contains duplicate entries")
            for member in ("manifest.json", "lab.json", "experience/runbook.md"):
                if archive.getinfo(member).file_size > _MAX_DOCUMENT_BYTES:
                    _fail("package document is too large")
            runbook_raw = archive.read("experience/runbook.md")
            manifest_raw = archive.read("manifest.json")
            definition_raw = archive.read("lab.json")
            if len(manifest_raw) > _MAX_DOCUMENT_BYTES or len(definition_raw) > _MAX_DOCUMENT_BYTES:
                _fail("package manifest or definition is too large")
            manifest = _json_document(manifest_raw, label="manifest.json")
            definition = _json_document(definition_raw, label="lab.json")
            package_id = definition.get("package_id")
            version = definition.get("version")
            reproducibility = definition.get("reproducibility")
            if not all(isinstance(value, str) and value for value in (package_id, version)):
                _fail("lab.json has no package identity")
            if reproducibility != "locked":
                _fail("host evidence requires a locked package")
            if manifest.get("package_id") != package_id or manifest.get("version") != version:
                _fail("manifest and lab definition identity disagree")
            assets: list[dict[str, Any]] = []
            for name in sorted(names):
                if not name.startswith("assets/sha256/"):
                    continue
                expected = name.removeprefix("assets/sha256/")
                if not _SHA256.fullmatch(expected):
                    _fail(f"invalid embedded asset name: {name}")
                info = archive.getinfo(name)
                if info.file_size > _MAX_ASSET_BYTES:
                    _fail(f"embedded asset is too large: {name}")
                digest = hashlib.sha256()
                total = 0
                with archive.open(info, "r") as stream:
                    while chunk := stream.read(1024 * 1024):
                        total += len(chunk)
                        if total > _MAX_ASSET_BYTES:
                            _fail(f"embedded asset is too large: {name}")
                        digest.update(chunk)
                actual = digest.hexdigest()
                if actual != expected or total != info.file_size:
                    _fail(f"embedded asset digest or size mismatch: {name}")
                assets.append({"sha256": actual, "size_bytes": total})
    except EvidenceError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile, KeyError) as exc:
        raise EvidenceError("could not inspect package archive") from exc
    return {
        "package_id": package_id,
        "version": version,
        "archive_sha256": archive_sha256,
        "archive_size_bytes": archive_size_bytes,
        "definition_sha256": _digest_bytes(definition_raw),
        "manifest_sha256": _digest_bytes(manifest_raw),
        "asset_digests": assets,
        "runbook_sha256": _digest_bytes(runbook_raw),
        "contract": _definition_contract(definition),
    }


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"lifecycle {label} must be a non-empty string")
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        _fail(f"{label} must be a SHA-256 digest")
    return value


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object")
    return value


def _positive(value: Any, label: str) -> int:
    if type(value) is not int or value < 1:
        _fail(f"{label} must be positive")
    return value


def _ordered(rows: Any, keys: tuple[str, ...], label: str) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        _fail(f"{label} must be a list")
    seen: set[tuple[Any, ...]] = set()
    for row in rows:
        _object(row, label)
        identity = tuple(row.get(key) for key in keys)
        if any(not isinstance(item, (str, int)) or isinstance(item, bool) for item in identity):
            _fail(f"{label} has invalid identity")
        if identity in seen:
            _fail(f"{label} has duplicate identity")
        seen.add(identity)
    return sorted(rows, key=lambda row: tuple(str(row[key]) for key in keys))


def _definition_contract(definition: dict[str, Any]) -> dict[str, Any]:
    vms = _ordered(definition.get("vms"), ("id",), "definition VMs")
    networks = _ordered(definition.get("networks"), ("id",), "definition networks")
    if not vms or not networks:
        _fail("locked definition requires VMs and networks")
    layers = []
    readiness = []
    nics = []
    for vm in vms:
        layer = _object(vm.get("boot_layer") or vm.get("clean_layer"), "boot layer")
        layers.append(
            {
                "vm_id": vm["id"],
                "sha256": _sha(layer.get("sha256"), "boot layer"),
                "size_bytes": _positive(layer.get("size_bytes"), "boot layer size"),
            }
        )
        workload = vm.get("workload") or {}
        readiness.append({"vm_id": vm["id"], "probes": workload.get("readiness_probes", [])})
        for ordinal, network in enumerate(vm.get("network_ids", [])):
            if network not in {item["id"] for item in networks}:
                _fail("definition NIC refers to an unknown network")
            nics.append({"vm_id": vm["id"], "network_id": network, "ordinal": ordinal})
    checkpoints = definition.get("experience", {}).get("checkpoints", [])
    if not isinstance(checkpoints, list):
        _fail("definition checkpoints must be a list")
    return {
        "boot_layers": layers,
        "topology": {
            "networks": [{"id": n["id"], "subnet_cidr": n["subnet_cidr"], "isolated": n["isolated"]} for n in networks],
            "nics": nics,
        },
        "readiness": readiness,
        "checkpoints": checkpoints,
    }


_RESIDUE_KEYS = ("domains", "networks", "overlays", "reservations", "temporary_paths")


def _no_residue(value: Any, label: str) -> None:
    row = _object(value, label)
    for kind in _RESIDUE_KEYS:
        if row.get(kind) != []:
            _fail(f"{label} residue: {kind} must be an observed empty list")


def _validate_lifecycle(value: Any, package: dict[str, Any]) -> dict[str, Any]:
    value = _object(value, "lifecycle evidence")
    contract = _object(package.get("contract"), "archive contract")
    for name, expected in (("launch", "ready"), ("reset", "ready"), ("teardown", "destroyed")):
        row = _object(value.get(name), name)
        if row.get("state") != expected:
            _fail(f"lifecycle {name}.state must be {expected}")
    for name in ("launch", "reset"):
        row = value[name]
        layers = _ordered(row.get("boot_layers"), ("vm_id",), f"{name} boot layers")
        for layer in layers:
            _sha(layer.get("sha256"), "observed boot layer")
            _positive(layer.get("size_bytes"), "observed boot layer size")
        if layers != contract["boot_layers"]:
            _fail(f"{name} boot layers differ from locked archive")
        topology = _object(row.get("topology"), f"{name} topology")
        normalized = {
            "networks": _ordered(topology.get("networks"), ("id",), "networks"),
            "nics": _ordered(topology.get("nics"), ("vm_id", "ordinal"), "NICs"),
        }
        if normalized != contract["topology"]:
            _fail(f"{name} logical topology differs from locked archive")
        readiness = _ordered(row.get("readiness"), ("vm_id",), f"{name} readiness")
        expected_readiness = [{**item, "passed": True} for item in contract["readiness"]]
        expected_checkpoints = [{"operation": item, "passed": True} for item in contract["checkpoints"]]
        if readiness != expected_readiness or any(item.get("passed") is not True for item in readiness):
            _fail(f"{name} readiness is missing, failed or inconsistent")
        if row.get("checkpoints") != expected_checkpoints or any(
            item.get("passed") is not True for item in row["checkpoints"]
        ):
            _fail(f"{name} declared checkpoint is missing, failed or inconsistent")
    _no_residue(value["reset"].get("previous_session_residue"), "reset")
    _no_residue(value["teardown"], "teardown")
    retained = value["teardown"].get("retained_asset_digests")
    if not isinstance(retained, list):
        _fail("teardown retained_asset_digests must be a list")
    for digest in retained:
        _sha(digest, "retained asset")
    if not {a["sha256"] for a in package["asset_digests"]}.issubset(retained):
        _fail("teardown does not prove retention of every packaged asset")
    return value


def _validate_provenance(
    value: Any, *, candidate_sha: str, archive_sha256: str, lifecycle_sha256: str
) -> dict[str, Any]:
    value = _object(value, "provenance")
    if value.get("candidate_sha") != candidate_sha or value.get("archive_sha256") != archive_sha256:
        _fail("provenance candidate or archive differs")
    for key in ("host_id_sha256", "installation_id_sha256", "identity_receipt_sha256"):
        _sha(value.get(key), key)
    for key in ("installed_source", "installed_helpers"):
        row = _object(value.get(key), key)
        if row.get("verified") is not True:
            _fail(f"{key} verification is missing")
        _sha(row.get("manifest_sha256"), key)
    if value["installed_source"].get("candidate_sha") != candidate_sha:
        _fail("installed source candidate differs")
    command = _object(value.get("command"), "command result")
    for key in ("exit_code", "failed", "skipped"):
        if type(command.get(key)) is not int or command[key] != 0:
            _fail("command must pass without failures or skips")
    _positive(command.get("passed"), "command passed count")
    _sha(command.get("receipt_sha256"), "command receipt")
    if command.get("lifecycle_sha256") != lifecycle_sha256:
        _fail("command receipt is not bound to lifecycle bytes")
    return value


def _command_output(command: list[str]) -> str | None:
    try:
        result = subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (result.stdout or result.stderr).strip()
    return output[:2048] if output else None


def _os_release() -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            key, separator, raw = line.partition("=")
            if separator and key and key.isidentifier():
                result[key] = raw.strip().strip('"')[:256]
    except OSError:
        pass
    return result


def _host_facts() -> dict[str, Any]:
    release = _os_release()
    return {
        "machine": platform.machine(),
        "system": platform.system(),
        "kernel": platform.release(),
        "os": {
            "id": release.get("ID"),
            "version_id": release.get("VERSION_ID"),
            "pretty_name": release.get("PRETTY_NAME"),
        },
        "libvirt": _command_output(["virsh", "-c", "qemu:///system", "version"]),
        "qemu_img": _command_output(["qemu-img", "--version"]),
    }


def _canonical(value: Any) -> Any:
    """Return JSON-compatible data with deterministic dictionary ordering."""
    if isinstance(value, dict):
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def _reproducibility_surface(package: dict[str, Any], lifecycle: dict[str, Any]) -> dict[str, Any]:
    # Validation already requires agreement with every archive contract field.
    # Runtime IDs, paths, host identity and timing never enter this surface.
    return _canonical(
        {
            "package": package,
            "launch": package["contract"],
            "reset": package["contract"],
            "teardown": {
                "state": lifecycle["teardown"]["state"],
                "retained_asset_digests": sorted(lifecycle["teardown"]["retained_asset_digests"]),
            },
        }
    )


def create_manifest(
    *,
    archive_path: Path,
    lifecycle_path: Path,
    provenance_path: Path,
    candidate_sha: str,
    host_label: str,
    output_path: Path,
) -> dict[str, Any]:
    if not _COMMIT.fullmatch(candidate_sha):
        _fail("candidate SHA must be a full 40-character lowercase commit")
    if host_label not in _HOST_LABELS:
        _fail("host label must be host-a or host-b")
    lifecycle_raw = _read_regular(lifecycle_path, label="lifecycle evidence", maximum=_MAX_LIFECYCLE_BYTES)
    package = _package_surface(archive_path)
    lifecycle = _validate_lifecycle(_json_document(lifecycle_raw, label="lifecycle evidence"), package)
    provenance = _validate_provenance(
        _json_document(
            _read_regular(provenance_path, label="provenance", maximum=_MAX_LIFECYCLE_BYTES), label="provenance"
        ),
        candidate_sha=candidate_sha,
        archive_sha256=package["archive_sha256"],
        lifecycle_sha256=_digest_bytes(lifecycle_raw),
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "contract_revision": 2,
        "lifecycle_sha256": _digest_bytes(lifecycle_raw),
        "provenance": provenance,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "candidate_sha": candidate_sha,
        "host_label": host_label,
        "host": _host_facts(),
        "package": package,
        "lifecycle": lifecycle,
        "reproducibility_surface": _reproducibility_surface(package, lifecycle),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.partial")
    try:
        temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, output_path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise EvidenceError("could not publish evidence manifest") from exc
    return manifest


def _load_manifest(path: Path) -> dict[str, Any]:
    raw = _read_regular(path, label="evidence manifest", maximum=_MAX_LIFECYCLE_BYTES * 2)
    value = _json_document(raw, label="evidence manifest")
    if value.get("schema_version") != SCHEMA_VERSION:
        _fail("unsupported evidence manifest schema")
    if not _COMMIT.fullmatch(value.get("candidate_sha", "")):
        _fail("evidence manifest candidate SHA is invalid")
    if value.get("host_label") not in _HOST_LABELS:
        _fail("evidence manifest host label is invalid")
    if not isinstance(value.get("reproducibility_surface"), dict):
        _fail("evidence manifest has no reproducibility surface")
    if value.get("contract_revision") != 2:
        _fail("legacy evidence lacks the complete observation contract")
    package = _object(value.get("package"), "package")
    lifecycle = _validate_lifecycle(value.get("lifecycle"), package)
    _validate_provenance(
        value.get("provenance"),
        candidate_sha=value["candidate_sha"],
        archive_sha256=package["archive_sha256"],
        lifecycle_sha256=_sha(value.get("lifecycle_sha256"), "lifecycle"),
    )
    if value["reproducibility_surface"] != _reproducibility_surface(package, lifecycle):
        _fail("stored reproducibility surface contradicts observations")
    return value


def compare_manifests(first_path: Path, second_path: Path, *, candidate_sha: str | None = None) -> dict[str, Any]:
    first = _load_manifest(first_path)
    second = _load_manifest(second_path)
    if first["host_label"] == second["host_label"]:
        _fail("two-host comparison requires distinct host labels")
    for key in ("host_id_sha256", "installation_id_sha256"):
        if first["provenance"][key] == second["provenance"][key]:
            _fail("P2 requires distinct hosts and installations, not relabeled receipts")
    if first["candidate_sha"] != second["candidate_sha"]:
        _fail("host evidence candidate SHAs disagree")
    if candidate_sha is not None and first["candidate_sha"] != candidate_sha:
        _fail("evidence candidate SHA does not match requested candidate")
    if first["reproducibility_surface"] != second["reproducibility_surface"]:
        _fail("host reproducibility surfaces differ")
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_sha": first["candidate_sha"],
        "hosts": sorted([first["host_label"], second["host_label"]]),
        "surface_sha256": _digest_bytes(
            json.dumps(first["reproducibility_surface"], sort_keys=True, separators=(",", ":")).encode()
        ),
        "result": "PASS",
    }


def _time(value: Any, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        _fail(f"{label} must be a finite non-negative monotonic time")
    return float(value)


def _timed_phases(value: Any, names: tuple[str, ...], *, negative: bool = False) -> dict[str, Any]:
    row = _object(value, "timed workflow")
    start = _time(row.get("start_monotonic"), "workflow start")
    end = _time(row.get("end_monotonic"), "workflow end")
    phases = row.get("phases")
    if not isinstance(phases, list) or [p.get("name") for p in phases if isinstance(p, dict)] != list(names):
        _fail("timing must cover every phase in order")
    cursor = start
    for phase in phases:
        begins = _time(phase.get("start_monotonic"), "phase start")
        finishes = _time(phase.get("end_monotonic"), "phase end")
        if begins != cursor or finishes <= begins:
            _fail("phase timings have a gap, overlap or inconsistent timestamps")
        expected = "expected_failure" if negative and phase["name"] == "prepare" else "passed"
        if phase.get("outcome") != expected:
            _fail("phase failed or has an inconsistent outcome")
        cursor = finishes
    if end != cursor:
        _fail("total workflow time omits phases or is inconsistent")
    if row.get("included_work") != ["hashing", "queue_waits", "readiness"]:
        _fail("timings must include hashing, queue waits and readiness")
    return row


def _offline_network(value: Any, start: float, end: float) -> None:
    row = _object(value, "network restriction")
    applied = _time(row.get("applied_monotonic"), "network apply")
    restored = _time(row.get("restored_monotonic"), "network restore")
    if not applied <= start < end <= restored:
        _fail("network restriction does not cover the complete workflow")
    for key in ("applied_receipt_sha256", "restored_receipt_sha256", "continuous_receipt_sha256"):
        _sha(row.get(key), key)
    probes = row.get("probes")
    if not isinstance(probes, list) or len(probes) != 4:
        _fail("network evidence requires DNS and TCP probes before and after the workflow")
    identities = set()
    for probe in probes:
        probe = _object(probe, "external-connectivity probe")
        kind, stage = probe.get("kind"), probe.get("stage")
        if kind not in ("dns", "tcp") or stage not in ("before", "after") or probe.get("blocked") is not True:
            _fail("external-connectivity probe did not demonstrate blocking")
        identities.add((kind, stage))
        stamp = _time(probe.get("monotonic"), "probe time")
        if not (applied <= stamp <= start if stage == "before" else end <= stamp <= restored):
            _fail("external-connectivity probe time is inconsistent")
        _sha(probe.get("receipt_sha256"), "external probe receipt")
    if len(identities) != 4:
        _fail("external-connectivity probe coverage is incomplete")


def validate_p3(path: Path, *, candidate_sha: str) -> dict[str, Any]:
    value = _json_document(_read_regular(path, label="P3 receipt", maximum=_MAX_LIFECYCLE_BYTES), label="P3 receipt")
    if (
        value.get("schema_version") != SCHEMA_VERSION
        or value.get("kind") != "offline-performance"
        or value.get("contract_revision") != 2
    ):
        _fail("unsupported P3 evidence contract")
    if not _COMMIT.fullmatch(candidate_sha) or value.get("candidate_sha") != candidate_sha:
        _fail("P3 candidate SHA differs")
    _sha(value.get("archive_sha256"), "P3 archive")
    for key in ("host_id_sha256", "installation_id_sha256"):
        _sha(value.get(key), key)
    profile = _object(value.get("profile"), "reference host profile")
    if (profile.get("os_id"), profile.get("os_version"), profile.get("architecture")) != ("ubuntu", "24.04", "x86_64"):
        _fail("P3 requires Ubuntu 24.04 x86_64")
    for key in (
        "cpu_threads",
        "memory_mb",
        "free_disk_bytes",
        "guest_memory_mb",
        "vm_count",
        "compressed_content_bytes",
    ):
        _positive(profile.get(key), key)
    if profile["cpu_threads"] < 4 or profile["memory_mb"] < 8192 or profile["free_disk_bytes"] < 40 * 1024**3:
        _fail("host profile is below reference requirements")
    if (
        profile["guest_memory_mb"] > 4096
        or profile["vm_count"] > 2
        or profile["compressed_content_bytes"] > 10 * 1024**3
    ):
        _fail("package exceeds the bounded reference profile")
    _sha(profile.get("receipt_sha256"), "profile receipt")
    creator = _timed_phases(value.get("creator"), ("build", "verify", "export"))
    if creator.get("candidate_sha") != candidate_sha or creator.get("archive_sha256") != value["archive_sha256"]:
        _fail("creator candidate/archive differs")
    if creator.get("source_verified") is not True or creator.get("helpers_verified") is not True:
        _fail("creator installed source/helper verification is missing")
    for key in ("installed_source_manifest_sha256", "installed_helper_manifest_sha256"):
        _sha(creator.get(key), key)
    _sha(creator.get("receipt_sha256"), "creator receipt")
    if creator.get("excluded_work") != ["initial_asset_downloads", "custom_runbook_authoring"]:
        _fail("creator exclusions exceed the acceptance contract")
    creator_seconds = creator["end_monotonic"] - creator["start_monotonic"]
    if creator_seconds >= 600:
        _fail("creator missed the under-600-second target")
    runs = value.get("runs")
    if (
        not isinstance(runs, list)
        or len(runs) != 3
        or {r.get("cache_mode") for r in runs if isinstance(r, dict)} != {"cold", "cached", "unavailable"}
    ):
        _fail("P3 requires cold-cache, cached and unavailable-asset control runs")
    recipient_seconds = None
    lifecycle_digests: set[str] = set()
    command_digests: set[str] = set()
    required_layers: set[str] = set()
    negative_cache: set[str] = set()
    control_package: dict[str, Any] | None = None
    reference_contract: dict[str, Any] | None = None
    unavailable_digest = ""
    for run in runs:
        negative = run["cache_mode"] == "unavailable"
        names = (
            ("inspect", "import", "prepare", "teardown")
            if negative
            else ("inspect", "import", "prepare", "preflight", "launch", "checkpoints", "reset", "teardown")
        )
        _timed_phases(run, names, negative=negative)
        if run.get("excluded_work") != []:
            _fail("recipient timings may not exclude work")
        _sha(run.get("command_receipt_sha256"), "run command receipt")
        _sha(run.get("cache_receipt_sha256"), "cache inventory receipt")
        cache = run.get("cache_before")
        if not isinstance(cache, list):
            _fail("cache inventory must list observed content digests")
        for digest in cache:
            _sha(digest, "cached asset")
        _offline_network(run.get("network"), run["start_monotonic"], run["end_monotonic"])
        if negative:
            if (
                run.get("failure_code") != "required_asset_unavailable"
                or type(run.get("downloads_attempted")) is not int
                or run["downloads_attempted"] != 0
            ):
                _fail("unavailable-asset control must fail explicitly without a download")
            _no_residue(run.get("residue"), "unavailable-asset control")
            negative_cache = set(cache)
            control_package = _package_surface(Path(_require_string(run.get("control_archive"), "control archive")))
            unavailable_digest = _sha(run.get("unavailable_asset_sha256"), "unavailable asset")
            if (
                run.get("control_archive_sha256") != control_package["archive_sha256"]
                or run.get("reference_archive_sha256") != value["archive_sha256"]
            ):
                _fail("unavailable control archive binding differs")
            observation = {
                key: run[key]
                for key in (
                    "control_archive_sha256",
                    "reference_archive_sha256",
                    "unavailable_asset_sha256",
                    "failure_code",
                    "downloads_attempted",
                    "residue",
                )
            }
            provenance = _validate_provenance(
                run.get("provenance"),
                candidate_sha=candidate_sha,
                archive_sha256=control_package["archive_sha256"],
                lifecycle_sha256=_digest_bytes(json.dumps(observation, sort_keys=True, separators=(",", ":")).encode()),
            )
            if any(provenance[key] != value[key] for key in ("host_id_sha256", "installation_id_sha256")):
                _fail("unavailable control host/installation differs")
            digest = provenance["command"]["receipt_sha256"]
            if digest != run["command_receipt_sha256"] or digest in command_digests:
                _fail("unavailable control command is unbound or reused")
            command_digests.add(digest)
            continue
        manifest = _load_manifest(Path(_require_string(run.get("manifest"), "manifest path")))
        lifecycle_digest = manifest["lifecycle_sha256"]
        command_digest = manifest["provenance"]["command"]["receipt_sha256"]
        if command_digest != run["command_receipt_sha256"]:
            _fail("offline/timing command receipt differs from lifecycle command")
        if lifecycle_digest in lifecycle_digests or command_digest in command_digests:
            _fail("cold and cached runs require independent lifecycle and command receipts")
        lifecycle_digests.add(lifecycle_digest)
        command_digests.add(command_digest)
        required_layers = {layer["sha256"] for layer in manifest["package"]["contract"]["boot_layers"]}
        reference_contract = manifest["package"]["contract"]
        if run["cache_mode"] == "cold":
            embedded = {asset["sha256"] for asset in manifest["package"]["asset_digests"]}
            if cache or not required_layers.issubset(embedded):
                _fail("cold run requires an empty cache and fully embedded package")
        elif not required_layers.issubset(cache):
            _fail("cached run lacks required immutable content")
        if (
            manifest["candidate_sha"] != candidate_sha
            or manifest["package"]["archive_sha256"] != value["archive_sha256"]
        ):
            _fail("offline lifecycle candidate/archive differs")
        if any(manifest["provenance"][key] != value[key] for key in ("host_id_sha256", "installation_id_sha256")):
            _fail("offline lifecycle host/installation differs")
        if run["cache_mode"] == "cached":
            recipient_seconds = run["phases"][4]["end_monotonic"] - run["start_monotonic"]
            if recipient_seconds >= 300:
                _fail("recipient missed the under-300-second target")
    if control_package is None or control_package["contract"] != reference_contract:
        _fail("unavailable-asset control is not the reference lab contract")
    embedded_control = {asset["sha256"] for asset in control_package["asset_digests"]}
    if unavailable_digest not in required_layers or unavailable_digest in negative_cache | embedded_control:
        _fail("unavailable-asset control has no missing required asset")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "offline-performance",
        "candidate_sha": candidate_sha,
        "archive_sha256": value["archive_sha256"],
        "creator_seconds": creator_seconds,
        "recipient_seconds": recipient_seconds,
        "result": "PASS",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="create one host's private evidence manifest")
    create.add_argument("--archive", type=Path, required=True, help="locked .astinuslab archive")
    create.add_argument("--lifecycle", type=Path, required=True, help="JSON lifecycle observation")
    create.add_argument(
        "--provenance", type=Path, required=True, help="private installed identity and command receipts"
    )
    create.add_argument("--candidate-sha", required=True, help="full frozen candidate commit SHA")
    create.add_argument("--host-label", choices=sorted(_HOST_LABELS), required=True)
    create.add_argument("--output", type=Path, required=True)
    compare = commands.add_parser("compare", help="compare host A and host B reproducibility surfaces")
    compare.add_argument("--host-a", type=Path, required=True)
    compare.add_argument("--host-b", type=Path, required=True)
    compare.add_argument("--candidate-sha")
    p3 = commands.add_parser("validate-p3", help="validate private offline/performance receipts")
    p3.add_argument("--receipt", type=Path, required=True)
    p3.add_argument("--candidate-sha", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create":
            manifest = create_manifest(
                archive_path=args.archive,
                lifecycle_path=args.lifecycle,
                provenance_path=args.provenance,
                candidate_sha=args.candidate_sha,
                host_label=args.host_label,
                output_path=args.output,
            )
            print(
                json.dumps(
                    {
                        "result": "PASS",
                        "output": str(args.output),
                        "surface_sha256": _digest_bytes(
                            json.dumps(
                                manifest["reproducibility_surface"], sort_keys=True, separators=(",", ":")
                            ).encode()
                        ),
                    },
                    sort_keys=True,
                )
            )
        elif args.command == "validate-p3":
            print(json.dumps(validate_p3(args.receipt, candidate_sha=args.candidate_sha), sort_keys=True))
        else:
            print(
                json.dumps(
                    compare_manifests(args.host_a, args.host_b, candidate_sha=args.candidate_sha), sort_keys=True
                )
            )
    except EvidenceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
