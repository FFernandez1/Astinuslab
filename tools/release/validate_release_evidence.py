#!/usr/bin/env python3
"""Validate a private AstinusLab release-evidence bundle fail-closed."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

ROWS_SCHEMA_VERSION = "astinuslab-release-evidence-rows/v1"
EVIDENCE_SCHEMA_VERSION = "astinuslab-release-evidence/v1"
PUBLIC_SCHEMA_VERSION = "astinuslab-release-evidence-public/v1"
MANIFEST_SCHEMA_VERSION = "artifact-manifest/v1"

_ROW_KEYS = {
    "artifact_path",
    "command_log_ref",
    "driver",
    "evidence_paths",
    "manifest_path",
    "record_id",
    "record_type",
    "row",
    "sidecar_path",
    "status",
    "ui_proof",
    "waiver",
}
_EVIDENCE_ROW_KEYS = _ROW_KEYS | {"sha256", "size_bytes"}
_WAIVER_KEYS = {
    "row",
    "reason",
    "impact",
    "owner",
    "decision_owner",
    "expires_or_retest",
}
_UI_PROOF_KEYS = {"trace_path", "screenshot_paths"}
_REQUIRED_UI_ROWS = {4, 6, 7}
_RECORD_TYPE = re.compile(r"[a-z][a-z0-9_-]*")
_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}")
_RECORD_FIELD_ALIASES = {"playground": "session"}
_EXPECTED_RECORD_TYPES = {
    1: "job",
    2: "job",
    3: "job",
    4: "job",
    5: "workload",
    6: "acquisition",
    7: "acquisition",
}
_EXPECTED_ARTIFACT_KINDS = {1: "memory", 2: "disk", 6: "memory", 7: "disk"}
_MANIFEST_REQUIRED_KEYS = {
    "schema_version",
    "artifact_id",
    "producer",
    "kind",
    "path",
    "filename",
    "sha256",
    "sha256_sidecar_path",
    "size_bytes",
    "created_at_utc",
    "target",
    "capture",
    "network",
    "integrity",
    "inputs",
}
_INTEGRITY_KEYS = {
    "raw_sha256",
    "domain_xml_sha256",
    "domain_xml_excerpt",
    "fake_mode",
    "fake_mode_flags",
}
# Must match astinuslab.core.fake_mode.fake_mode_flags() exactly: release
# evidence is rejected unless the manifest declares every fake flag the
# producer knows about and every one of them is false.
_FAKE_FLAG_KEYS = {"fake_libvirt", "fake_playground", "fake_orchestration"}
_ARTIFACT_KEYS = {
    "artifact_path",
    "bundle_manifest_path",
    "bundle_sidecar_path",
    "kind",
    "manifest_path",
    "sha256",
    "sidecar_path",
    "size_bytes",
}
_SOURCE_KEYS = {"branch", "commit", "detached", "tracked_clean"}
_PUBLIC_ROW_KEYS = {"artifact_path", "driver", "row", "sha256", "size_bytes", "status"}
_ROOT_REQUIRED = {
    "HOST_WORKFLOW_CHECKLIST.md",
    "SHA256SUMS.txt",
    "SOURCE_COMMIT.txt",
    "evidence.json",
    "manifests",
    "proofs",
    "public",
    "sidecars",
}
_ROOT_ALLOWED = _ROOT_REQUIRED | {"operator-command.log"}
_PUBLIC_FILES = {
    "HOST_WORKFLOW_CHECKLIST.md",
    "SHA256SUMS.txt",
    "evidence-summary.json",
}
_HEX_SHA256 = re.compile(r"[0-9a-f]{64}")
_FULL_COMMIT = re.compile(r"[0-9a-f]{40}")
_BUNDLE_NAME = re.compile(r"(\d{8}T\d{6}\.\d{6}Z)-([0-9a-f]{40})")


class ValidationError(RuntimeError):
    """Evidence bundle violates release contract."""


def _error(message: str) -> NoReturn:
    raise ValidationError(message)


def _json_object(raw: bytes, label: str) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                _error(f"{label} contains duplicate JSON key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> NoReturn:
        _error(f"{label} contains non-finite JSON constant {value}")

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"{label} must contain valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        _error(f"{label} must contain a JSON object")
    return value


def _safe_relative(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        _error(f"{label} must be a nonempty relative path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or "." in relative.parts:
        _error(f"{label} must be a safe relative path without parent references")
    if any(not part for part in relative.parts):
        _error(f"{label} contains an invalid empty path component")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        _error(f"{label} contains unsafe control characters")
    return relative.as_posix()


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _paths_overlap(first: Path, second: Path) -> bool:
    return _path_within(first, second) or _path_within(second, first)


def _expected_tree_entries(root_name: str, file_paths: set[str]) -> set[str]:
    expected = {root_name}
    for value in file_paths:
        path = PurePosixPath(value)
        expected.add(path.as_posix())
        for parent in path.parents:
            if parent == PurePosixPath("."):
                break
            expected.add(parent.as_posix())
    return expected


def _actual_tree_entries(bundle: Path, root_name: str) -> set[str]:
    root = bundle / root_name
    return {root_name} | {path.relative_to(bundle).as_posix() for path in root.rglob("*")}


def _reject_symlink_components(path: Path, label: str) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            item_stat = current.lstat()
        except FileNotFoundError:
            _error(f"{label} does not exist")
        if stat.S_ISLNK(item_stat.st_mode):
            _error(f"{label} must not contain symlink path components")


def _opened_target(fd: int, label: str) -> Path:
    try:
        return Path(f"/proc/self/fd/{fd}").resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise ValidationError(f"{label} open-file identity could not be verified through /proc/self/fd") from exc


def _verify_post_read_path(
    path: Path,
    *,
    fd: int,
    opened: Path,
    after: os.stat_result,
    root: Path,
    label: str,
) -> None:
    if _opened_target(fd, label) != opened:
        _error(f"{label} changed open-file identity while it was read")
    try:
        path_stat = path.lstat()
        path_resolved = path.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise ValidationError(f"{label} changed identity after it was read") from exc
    path_identity = (path_stat.st_dev, path_stat.st_ino, path_stat.st_size, path_stat.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if not stat.S_ISREG(path_stat.st_mode) or path_identity != after_identity or path_resolved != opened:
        _error(f"{label} changed identity after it was read")
    if not _path_within(path_resolved, root):
        _error(f"{label} escaped its approved root")


def _regular_file(path: Path, label: str, *, root: Path) -> tuple[bytes, Path]:
    try:
        item_stat = path.lstat()
    except FileNotFoundError:
        _error(f"missing referenced {label}")
    if stat.S_ISLNK(item_stat.st_mode):
        _error(f"{label} must not be a symlink")
    if not stat.S_ISREG(item_stat.st_mode):
        _error(f"{label} must be a regular file")
    resolved = path.resolve(strict=True)
    if not _path_within(resolved, root):
        _error(f"{label} resolves outside its approved root")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ValidationError(f"{label} could not be opened safely") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            _error(f"{label} must be a regular file")
        opened = _opened_target(fd, label)
        if opened != resolved or not _path_within(opened, root):
            _error(f"{label} changed identity or escaped its approved root")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if before_identity != after_identity:
            _error(f"{label} changed while it was read")
        _verify_post_read_path(path, fd=fd, opened=opened, after=after, root=root, label=label)
        return b"".join(chunks), opened
    finally:
        os.close(fd)


def _bundle_file(bundle: Path, relative: object, label: str) -> tuple[bytes, Path]:
    safe = _safe_relative(relative, label)
    return _regular_file(bundle.joinpath(*PurePosixPath(safe).parts), label, root=bundle)


def _storage_file(storage: Path, relative: object, label: str) -> tuple[bytes, Path]:
    safe = _safe_relative(relative, label)
    return _regular_file(storage.joinpath(*PurePosixPath(safe).parts), label, root=storage)


def _hash_storage_file(storage: Path, relative: object, label: str) -> tuple[str, int, Path]:
    safe = _safe_relative(relative, label)
    path = storage.joinpath(*PurePosixPath(safe).parts)
    try:
        item_stat = path.lstat()
    except FileNotFoundError:
        _error(f"missing referenced {label}")
    if stat.S_ISLNK(item_stat.st_mode) or not stat.S_ISREG(item_stat.st_mode):
        _error(f"{label} must be a regular non-symlink file")
    resolved = path.resolve(strict=True)
    if not _path_within(resolved, storage):
        _error(f"{label} resolves outside storage root")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ValidationError(f"{label} could not be opened safely") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            _error(f"{label} must be a regular file")
        opened = _opened_target(fd, label)
        if opened != resolved or not _path_within(opened, storage):
            _error(f"{label} changed identity or escaped storage root")
        digest = hashlib.sha256()
        size_bytes = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size_bytes += len(chunk)
        after = os.fstat(fd)
        before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if before_identity != after_identity or size_bytes != after.st_size:
            _error(f"{label} changed while it was hashed")
        _verify_post_read_path(path, fd=fd, opened=opened, after=after, root=storage, label=label)
        return digest.hexdigest(), size_bytes, opened
    finally:
        os.close(fd)


def _strict_sidecar(raw: bytes, *, filename: str, digest: str) -> None:
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValidationError("sidecar format must be ASCII") from exc
    match = re.fullmatch(r"([0-9a-f]{64})  ([^\n]+)\n", text)
    if match is None:
        _error("sidecar format is invalid")
    sidecar_digest, sidecar_filename = match.groups()
    if sidecar_filename != filename:
        _error("sidecar filename disagrees with artifact")
    if sidecar_digest != digest:
        _error("sidecar sha256 digest disagrees with artifact")


def _source_commit(bundle: Path, evidence_source: object, candidate_sha: str | None) -> dict[str, Any]:
    if not isinstance(evidence_source, dict) or set(evidence_source) != _SOURCE_KEYS:
        _error("evidence source object is malformed")
    commit = evidence_source.get("commit")
    if not isinstance(commit, str) or _FULL_COMMIT.fullmatch(commit) is None:
        _error("evidence source commit must be a full 40-character lowercase SHA")
    branch = evidence_source.get("branch")
    if not isinstance(branch, str) or not branch:
        _error("evidence source branch is missing")
    detached = evidence_source.get("detached")
    if type(detached) is not bool:
        _error("evidence source detached must be boolean")
    if evidence_source.get("tracked_clean") is not True:
        _error("evidence source must record tracked_clean=true")
    if detached and branch != "DETACHED":
        _error("detached source must record branch=DETACHED")
    if not detached and branch == "DETACHED":
        _error("attached source cannot record branch=DETACHED")

    source_raw, _ = _bundle_file(bundle, "SOURCE_COMMIT.txt", "SOURCE_COMMIT.txt")
    try:
        source_text = source_raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("SOURCE_COMMIT.txt must be UTF-8") from exc
    expected_source_text = f"commit={commit}\nbranch={branch}\ndetached={str(detached).lower()}\ntracked_clean=true\n"
    if source_text != expected_source_text:
        _error("SOURCE_COMMIT.txt disagrees with evidence source")

    name_match = _BUNDLE_NAME.fullmatch(bundle.name)
    if name_match is None:
        _error("evidence bundle name must contain UTC timestamp and full commit SHA")
    try:
        datetime.strptime(name_match.group(1), "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValidationError("evidence bundle name contains an invalid UTC timestamp") from exc
    if name_match.group(2) != commit:
        _error("evidence bundle commit does not match SOURCE_COMMIT")
    if candidate_sha is not None:
        if _FULL_COMMIT.fullmatch(candidate_sha) is None:
            _error("candidate SHA must be a full 40-character lowercase commit")
        if candidate_sha != commit:
            _error("evidence source commit does not match candidate SHA")
    return dict(evidence_source)


def _parse_expiry(value: str, now: datetime) -> None:
    normalized = value.strip()
    try:
        if "T" in normalized or " " in normalized:
            parsed_datetime = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
            if parsed_datetime.tzinfo is None:
                _error("waiver expiry/retest datetime must include timezone")
            if parsed_datetime.astimezone(UTC) < now.astimezone(UTC):
                _error("waiver expiry/retest date is expired")
        else:
            parsed_date = date.fromisoformat(normalized)
            if parsed_date < now.astimezone(UTC).date():
                _error("waiver expiry/retest date is expired")
    except ValueError as exc:
        raise ValidationError("waiver expiry/retest date must be ISO-8601") from exc


def _validate_waiver(value: object, row_number: int, now: datetime) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _WAIVER_KEYS:
        _error(f"row {row_number} waiver must contain every required field")
    waiver_row = value.get("row")
    if type(waiver_row) is not int or waiver_row != row_number:
        _error(f"row {row_number} waiver row must be the matching integer")
    for key in _WAIVER_KEYS - {"row"}:
        item = value.get(key)
        if not isinstance(item, str) or not item.strip():
            _error(f"row {row_number} waiver field {key} must be nonempty")
    _parse_expiry(value["expires_or_retest"], now)
    return dict(value)


def _validate_artifact_manifest(
    manifest: dict[str, Any],
    *,
    artifact_path: Path,
    sidecar_path: Path,
    digest: str,
    size_bytes: int,
    inventory_kind: object,
) -> tuple[list[Path], list[Path]]:
    missing = _MANIFEST_REQUIRED_KEYS - set(manifest)
    if missing:
        _error(f"artifact manifest is missing required fields: {', '.join(sorted(missing))}")
    if type(manifest.get("schema_version")) is not str or manifest["schema_version"] != MANIFEST_SCHEMA_VERSION:
        _error("artifact manifest schema_version is invalid")
    artifact_id = manifest.get("artifact_id")
    if artifact_id is not None and (not isinstance(artifact_id, str) or not artifact_id.strip()):
        _error("artifact manifest artifact_id must be null or a nonempty string")
    for field in ("producer", "kind"):
        value = manifest.get(field)
        if not isinstance(value, str) or not value.strip():
            _error(f"artifact manifest {field} must be a nonempty string")
    created = manifest.get("created_at_utc")
    if not isinstance(created, str) or not created.strip():
        _error("artifact manifest created_at_utc must be a timestamp string")
    try:
        parsed_created = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("artifact manifest created_at_utc must be a valid timestamp") from exc
    if parsed_created.tzinfo is None or parsed_created.utcoffset() != timedelta(0):
        _error("artifact manifest created_at_utc must be UTC")
    for field in ("target", "capture", "network"):
        value = manifest.get(field)
        if value is not None and not isinstance(value, dict):
            _error(f"artifact manifest {field} must be null or an object")
    inputs = manifest.get("inputs")
    if not isinstance(inputs, list):
        _error("artifact manifest inputs must be a list")
    if manifest.get("kind") != inventory_kind:
        _error("artifact manifest kind disagrees with evidence inventory")
    manifest_path = manifest.get("path")
    if not isinstance(manifest_path, str) or not Path(manifest_path).is_absolute():
        _error("artifact manifest path must be absolute")
    if Path(manifest_path).resolve(strict=False) != artifact_path:
        _error("artifact manifest path disagrees with storage artifact")
    if manifest.get("filename") != artifact_path.name:
        _error("artifact manifest filename disagrees with storage artifact")
    if manifest.get("sha256") != digest:
        _error("artifact manifest sha256 disagrees with recomputed digest")
    manifest_size = manifest.get("size_bytes")
    if isinstance(manifest_size, bool) or not isinstance(manifest_size, int) or manifest_size != size_bytes:
        _error("artifact manifest size disagrees with storage artifact")
    sidecar_value = manifest.get("sha256_sidecar_path")
    if not isinstance(sidecar_value, str) or not Path(sidecar_value).is_absolute():
        _error("artifact manifest sidecar path must be absolute")
    if Path(sidecar_value).resolve(strict=False) != sidecar_path:
        _error("artifact manifest sidecar path disagrees with storage sidecar")
    integrity = manifest.get("integrity")
    if not isinstance(integrity, dict) or set(integrity) != _INTEGRITY_KEYS:
        _error("artifact manifest integrity must contain exactly the producer schema fields")
    # The producer records the artifact digest and size at manifest top level (checked
    # above); the integrity block carries raw-capture and domain XML provenance.
    for field in ("raw_sha256", "domain_xml_sha256"):
        value = integrity.get(field)
        if value is not None and (not isinstance(value, str) or _HEX_SHA256.fullmatch(value) is None):
            _error(f"artifact manifest integrity {field} must be null or a lowercase sha256 digest")
    excerpt = integrity.get("domain_xml_excerpt")
    if excerpt is not None and not isinstance(excerpt, str):
        _error("artifact manifest integrity domain_xml_excerpt must be null or a string")
    if integrity.get("fake_mode") is not False:
        _error("artifact manifest fake_mode must be false")
    flags = integrity.get("fake_mode_flags")
    if not isinstance(flags, dict) or set(flags) != _FAKE_FLAG_KEYS:
        _error("artifact manifest fake_mode_flags must contain exactly all producer fake flags")
    if any(type(flag) is not bool for flag in flags.values()):
        _error("artifact manifest fake_mode_flags must contain booleans")
    if any(bool(flag) for flag in flags.values()):
        _error("artifact manifest contains a truthy fake mode flag")

    memory_paths: list[Path] = []
    isf_paths: list[Path] = []
    kind = str(manifest.get("kind", "")).lower().replace("_", "-")
    if kind in {"memory", "memory-dump", "raw-memory"}:
        memory_paths.append(artifact_path)
    elif kind == "isf":
        isf_paths.append(artifact_path)
    for item in inputs:
        if not isinstance(item, dict):
            _error("artifact manifest inputs must contain objects")
        input_kind = str(item.get("kind", "")).lower().replace("_", "-")
        input_path = item.get("path")
        if not isinstance(input_path, str) or not input_path:
            continue
        candidate = Path(input_path)
        if not candidate.is_absolute():
            candidate = artifact_path.parent / candidate
        candidate = candidate.resolve(strict=False)
        if input_kind in {"memory", "memory-dump", "raw-memory"}:
            memory_paths.append(candidate)
        elif input_kind == "isf":
            isf_paths.append(candidate)
    return memory_paths, isf_paths


def _validate_artifacts(
    bundle: Path,
    storage: Path,
    artifacts_value: object,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    if not isinstance(artifacts_value, list) or not artifacts_value:
        _error("evidence artifacts must be a nonempty list")
    artifacts: list[dict[str, Any]] = []
    by_path: dict[str, dict[str, Any]] = {}
    memory_paths: list[Path] = []
    isf_paths: list[Path] = []
    for index, artifact in enumerate(artifacts_value, start=1):
        if not isinstance(artifact, dict) or set(artifact) != _ARTIFACT_KEYS:
            _error(f"artifact inventory row {index} has invalid keys")
        record = dict(artifact)
        for key in (
            "artifact_path",
            "bundle_manifest_path",
            "bundle_sidecar_path",
            "manifest_path",
            "sidecar_path",
        ):
            record[key] = _safe_relative(record[key], f"artifact {index} {key}")
        if record["bundle_manifest_path"] != f"manifests/{record['manifest_path']}":
            _error("bundle manifest path disagrees with source manifest path")
        if record["bundle_sidecar_path"] != f"sidecars/{record['sidecar_path']}":
            _error("bundle sidecar path disagrees with source sidecar path")
        digest = record.get("sha256")
        if not isinstance(digest, str) or _HEX_SHA256.fullmatch(digest) is None:
            _error("artifact inventory sha256 must be a lowercase digest")
        size_value = record.get("size_bytes")
        if isinstance(size_value, bool) or not isinstance(size_value, int) or size_value < 0:
            _error("artifact inventory size_bytes is invalid")
        if record["artifact_path"] in by_path:
            _error("artifact inventory contains duplicate artifact paths")

        computed_digest, computed_size, artifact_path = _hash_storage_file(
            storage, record["artifact_path"], "source artifact"
        )
        if computed_digest != digest or computed_size != size_value:
            _error("source artifact digest or size disagrees with evidence inventory")
        source_manifest_raw, source_manifest_path = _storage_file(storage, record["manifest_path"], "source manifest")
        source_sidecar_raw, source_sidecar_path = _storage_file(storage, record["sidecar_path"], "source sidecar")
        copied_manifest_raw, _ = _bundle_file(bundle, record["bundle_manifest_path"], "copied manifest")
        copied_sidecar_raw, _ = _bundle_file(bundle, record["bundle_sidecar_path"], "copied sidecar")
        if copied_manifest_raw != source_manifest_raw:
            _error("copied manifest disagrees with source manifest")
        if copied_sidecar_raw != source_sidecar_raw:
            _error("copied sidecar disagrees with source sidecar")
        manifest = _json_object(copied_manifest_raw, "copied manifest")
        manifest_memory, manifest_isf = _validate_artifact_manifest(
            manifest,
            artifact_path=artifact_path,
            sidecar_path=source_sidecar_path,
            digest=digest,
            size_bytes=size_value,
            inventory_kind=record.get("kind"),
        )
        _strict_sidecar(copied_sidecar_raw, filename=artifact_path.name, digest=digest)
        expected_manifest_path = artifact_path.with_name(f"{artifact_path.name}.manifest.json")
        expected_sidecar_path = artifact_path.with_name(f"{artifact_path.name}.sha256")
        if source_manifest_path != expected_manifest_path:
            _error("source manifest path disagrees with artifact filename")
        if source_sidecar_path != expected_sidecar_path:
            _error("source sidecar path disagrees with artifact filename")
        artifact_stat = artifact_path.stat()
        record["_artifact_identity"] = (artifact_stat.st_dev, artifact_stat.st_ino)
        record["_artifact_resolved"] = artifact_path
        record["_manifest_resolved"] = source_manifest_path
        record["_sidecar_resolved"] = source_sidecar_path
        record["_manifest_bytes"] = source_manifest_raw
        record["_sidecar_bytes"] = source_sidecar_raw
        memory_paths.extend(manifest_memory)
        isf_paths.extend(manifest_isf)
        artifacts.append(record)
        by_path[record["artifact_path"]] = {**record, "_manifest_data": manifest}

    if [item["artifact_path"] for item in artifacts] != sorted(by_path):
        _error("artifact inventory must be sorted by artifact path")

    discovered_manifests: set[str] = set()
    for directory, directories, files in os.walk(storage, followlinks=False):
        for name in directories:
            if (Path(directory) / name).is_symlink():
                _error("storage tree must not contain symlink directories")
        for filename in files:
            if not filename.endswith(".manifest.json"):
                continue
            path = Path(directory) / filename
            _storage_file(
                storage,
                path.relative_to(storage).as_posix(),
                "discovered source manifest",
            )
            discovered_manifests.add(path.relative_to(storage).as_posix())
    expected_manifests = {item["manifest_path"] for item in artifacts}
    if discovered_manifests != expected_manifests:
        _error("source manifest discovery disagrees with evidence artifact inventory")

    expected_bundle_manifests = {item["bundle_manifest_path"] for item in artifacts}
    expected_bundle_sidecars = {item["bundle_sidecar_path"] for item in artifacts}
    actual_bundle_manifests = _actual_tree_entries(bundle, "manifests")
    actual_bundle_sidecars = _actual_tree_entries(bundle, "sidecars")
    if actual_bundle_manifests != _expected_tree_entries("manifests", expected_bundle_manifests):
        _error("copied manifest inventory contains missing or unexpected files")
    if actual_bundle_sidecars != _expected_tree_entries("sidecars", expected_bundle_sidecars):
        _error("copied sidecar inventory contains missing or unexpected files")

    for memory_path in memory_paths:
        for isf_path in isf_paths:
            overlap = _paths_overlap(memory_path, isf_path)
            if not overlap and memory_path.exists() and isf_path.exists():
                try:
                    overlap = memory_path.samefile(isf_path)
                except OSError:
                    overlap = False
            if overlap:
                _error("ISF path and raw memory path overlap")
    return artifacts, by_path


def _record_field(record_type: str) -> str:
    normalized = record_type.replace("-", "_")
    normalized = _RECORD_FIELD_ALIASES.get(normalized, normalized)
    return f"{normalized}_id"


def _reject_raw_artifact_bytes(
    raw: bytes,
    label: str,
    artifact_signatures: set[tuple[str, int]],
) -> None:
    signature = (hashlib.sha256(raw).hexdigest(), len(raw))
    if signature in artifact_signatures:
        _error(f"{label} must not contain copied raw artifact bytes")


_PROOF_KEYS = {"path", "sha256", "size_bytes"}


def _validate_proof_inventory(proofs_value: object) -> dict[str, tuple[str, int]]:
    """Read the recorded proof digests that bind copied proof bytes to evidence.json."""
    if not isinstance(proofs_value, list):
        _error("evidence proofs must be a list")
    inventory: dict[str, tuple[str, int]] = {}
    order: list[str] = []
    for index, entry in enumerate(proofs_value, start=1):
        if not isinstance(entry, dict) or set(entry) != _PROOF_KEYS:
            _error(f"proof inventory row {index} has invalid keys")
        path = _safe_relative(entry["path"], f"proof inventory row {index} path")
        digest = entry["sha256"]
        if not isinstance(digest, str) or _HEX_SHA256.fullmatch(digest) is None:
            _error(f"proof inventory row {index} sha256 must be a lowercase digest")
        size = entry["size_bytes"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            _error(f"proof inventory row {index} size_bytes is invalid")
        if path in inventory:
            _error("proof inventory contains duplicate paths")
        inventory[path] = (digest, size)
        order.append(path)
    if order != sorted(order):
        _error("proof inventory must be sorted by path")
    return inventory


def _validate_rows(
    bundle: Path,
    rows_value: object,
    artifacts: dict[str, dict[str, Any]],
    *,
    now: datetime,
    proofs: dict[str, tuple[str, int]],
) -> list[dict[str, Any]]:
    if not isinstance(rows_value, list):
        _error("evidence rows must be a list")
    numbers: list[int] = []
    for row in rows_value:
        if not isinstance(row, dict) or type(row.get("row")) is not int:
            _error("each evidence row must contain an integer row number")
        numbers.append(row["row"])
    if len(numbers) != 7 or sorted(numbers) != list(range(1, 8)) or len(set(numbers)) != 7:
        _error("rows must contain exactly one row from 1 through 7; duplicates are forbidden")

    artifact_signatures = {(item["sha256"], item["size_bytes"]) for item in artifacts.values()}
    used_proofs: set[str] = set()

    def read_proof(relative: str, label: str) -> bytes:
        """Read a copied proof and bind its bytes to the recorded proof digest."""
        raw, _ = _bundle_file(bundle, relative, label)
        _reject_raw_artifact_bytes(raw, label, artifact_signatures)
        recorded = proofs.get(relative)
        if recorded is None:
            _error(f"{label} is absent from the recorded proof inventory")
        if (hashlib.sha256(raw).hexdigest(), len(raw)) != recorded:
            _error(f"{label} bytes disagree with the recorded proof digest")
        used_proofs.add(relative)
        return raw

    command_log_path = bundle / "operator-command.log"
    command_log: str | None = None
    if command_log_path.exists() or command_log_path.is_symlink():
        command_raw = read_proof("operator-command.log", "operator command log")
        try:
            command_log = command_raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError("operator command log must be UTF-8") from exc

    rows: list[dict[str, Any]] = []
    referenced_proofs: set[str] = set()
    record_ids: set[str] = set()
    referenced_artifacts: set[tuple[int, int]] = set()
    for row in sorted(rows_value, key=lambda item: item["row"]):
        number = row["row"]
        if set(row) != _EVIDENCE_ROW_KEYS:
            _error(f"row {number} evidence keys are invalid")
        if row.get("driver") not in {"api", "ui"}:
            _error(f"row {number} driver must be api or ui")
        if number in _REQUIRED_UI_ROWS and row.get("driver") != "ui":
            _error(f"row {number} must be driven from ui")
        if row.get("status") != "completed":
            _error(f"row {number} status must be completed")
        record_type = row.get("record_type")
        record_id = row.get("record_id")
        if not isinstance(record_type, str) or _RECORD_TYPE.fullmatch(record_type) is None:
            _error(f"row {number} record_type must be a lowercase identifier")
        expected_record_type = _EXPECTED_RECORD_TYPES[number]
        if record_type != expected_record_type:
            _error(f"row {number} record_type must be {expected_record_type}")
        if not isinstance(record_id, str) or _RECORD_ID.fullmatch(record_id) is None:
            _error(f"row {number} record_id must be a safe nonempty identifier")
        if record_id in record_ids:
            _error(f"row {number} contains a duplicate record identity")
        record_ids.add(record_id)
        _validate_waiver(row.get("waiver"), number, now)

        if number == 5:
            for key in ("artifact_path", "manifest_path", "sidecar_path", "sha256", "size_bytes"):
                if row.get(key) is not None:
                    _error("row 5 must not claim an artifact triad")
        else:
            artifact_path = _safe_relative(row.get("artifact_path"), f"row {number} artifact path")
            manifest_path = _safe_relative(row.get("manifest_path"), f"row {number} manifest path")
            sidecar_path = _safe_relative(row.get("sidecar_path"), f"row {number} sidecar path")
            artifact = artifacts.get(artifact_path)
            if artifact is None:
                _error(f"row {number} artifact is absent from validated inventory")
            artifact_identity = artifact["_artifact_identity"]
            if artifact_identity in referenced_artifacts:
                _error(f"row {number} contains a duplicate artifact reference")
            referenced_artifacts.add(artifact_identity)
            record_field = _record_field(row["record_type"])
            if artifact["_manifest_data"].get(record_field) != row["record_id"]:
                _error(f"row {number} record identity disagrees with artifact manifest")
            expected_kind = _EXPECTED_ARTIFACT_KINDS.get(number)
            if expected_kind is not None and artifact["kind"] != expected_kind:
                _error(f"row {number} artifact kind must be {expected_kind}")
            expected = {
                "manifest_path": artifact["manifest_path"],
                "sha256": artifact["sha256"],
                "sidecar_path": artifact["sidecar_path"],
                "size_bytes": artifact["size_bytes"],
            }
            for key, expected_value in expected.items():
                row_value = {
                    "manifest_path": manifest_path,
                    "sidecar_path": sidecar_path,
                }.get(key, row.get(key))
                if row_value != expected_value:
                    _error(f"row {number} {key} disagrees with validated artifact")

        evidence_paths = row.get("evidence_paths")
        if not isinstance(evidence_paths, list):
            _error(f"row {number} requires an evidence proof list")
        # Mirror of the collector rule: UI rows may rely on their trace/screenshot
        # set, every other row must carry an explicit evidence file.
        if not evidence_paths and (number == 5 or row.get("driver") != "ui"):
            _error(f"row {number} requires at least one evidence file")
        for proof_path in evidence_paths:
            safe = _safe_relative(proof_path, f"row {number} evidence proof")
            if safe in referenced_proofs:
                _error(f"row {number} contains a duplicate proof reference")
            read_proof(safe, f"row {number} evidence proof")
            referenced_proofs.add(safe)

        ui_proof = row.get("ui_proof")
        if row.get("driver") == "ui":
            if not isinstance(ui_proof, dict) or set(ui_proof) != _UI_PROOF_KEYS:
                _error(f"row {number} ui proof is missing")
            trace = _safe_relative(ui_proof.get("trace_path"), f"row {number} UI trace")
            if trace in referenced_proofs:
                _error(f"row {number} contains a duplicate proof reference")
            screenshots = ui_proof.get("screenshot_paths")
            if not isinstance(screenshots, list) or not screenshots:
                _error(f"row {number} UI screenshot proof is missing")
            read_proof(trace, f"row {number} UI trace")
            referenced_proofs.add(trace)
            for screenshot in screenshots:
                safe = _safe_relative(screenshot, f"row {number} UI screenshot")
                if safe in referenced_proofs:
                    _error(f"row {number} contains a duplicate proof reference")
                read_proof(safe, f"row {number} UI screenshot")
                referenced_proofs.add(safe)
        elif ui_proof is not None:
            _error(f"row {number} api evidence must not claim UI proof")

        command_reference = row.get("command_log_ref")
        if command_reference is not None:
            if not isinstance(command_reference, str) or not command_reference.strip():
                _error(f"row {number} command log reference is invalid")
            if command_log is None:
                _error(f"row {number} references a missing operator command log")
            if command_reference not in command_log:
                _error(f"row {number} command log reference is absent from copied log")
        rows.append(dict(row))

    actual_proofs = _actual_tree_entries(bundle, "proofs")
    if actual_proofs != _expected_tree_entries("proofs", referenced_proofs):
        _error("proof inventory contains missing or unexpected referenced evidence")
    if used_proofs != set(proofs):
        _error("recorded proof inventory contains unreferenced or missing proof entries")
    return rows


def _markdown_cell(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _private_checklist(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Host-Verified Workflow Evidence",
        "",
        "> Generated from `evidence.json`; do not edit this rendered view.",
        "",
        "| # | Driver | Record | Status | Artifact | Manifest | Sidecar | SHA-256 | Size bytes | UI/backend evidence | Command log ref | Waiver |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        waiver = "None" if row["waiver"] is None else "Recorded"
        artifact = row["artifact_path"] or "N/A"
        manifest = row["manifest_path"] or "N/A"
        sidecar = row["sidecar_path"] or "N/A"
        digest = row["sha256"] or "N/A"
        size = row["size_bytes"] if row["size_bytes"] is not None else "N/A"
        record = f"{row['record_type']}:{row['record_id']}"
        evidence = list(row["evidence_paths"])
        ui_proof = row["ui_proof"]
        if ui_proof is not None:
            evidence.append(ui_proof["trace_path"])
            evidence.extend(ui_proof["screenshot_paths"])
        evidence_text = ", ".join(evidence) or "N/A"
        command_reference = row["command_log_ref"] or "N/A"
        lines.append(
            "| "
            + " | ".join(
                _markdown_cell(value)
                for value in (
                    row["row"],
                    row["driver"],
                    record,
                    row["status"],
                    artifact,
                    manifest,
                    sidecar,
                    digest,
                    size,
                    evidence_text,
                    command_reference,
                    waiver,
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def _public_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "artifact_path": None if row["artifact_path"] is None else f"row-{row['row']}/artifact",
            "driver": row["driver"],
            "row": row["row"],
            "sha256": row["sha256"],
            "size_bytes": row["size_bytes"],
            "status": row["status"],
        }
        for row in rows
    ]


def _public_checklist(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Host-Verified Workflow Evidence Summary",
        "",
        "> Sanitized projection generated from private `evidence.json`.",
        "",
        "| # | Driver | Status | Artifact | SHA-256 | Size bytes |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['row']} | {row['driver']} | {row['status']} | "
            f"{row['artifact_path'] or 'N/A'} | {row['sha256'] or 'N/A'} | "
            f"{row['size_bytes'] if row['size_bytes'] is not None else 'N/A'} |"
        )
    return "\n".join(lines) + "\n"


def _sha256_sums(artifacts: list[dict[str, Any]]) -> str:
    return "".join(f"{item['sha256']}  {item['artifact_path']}\n" for item in artifacts)


def _public_sha256_sums(rows: list[dict[str, Any]]) -> str:
    return "".join(f"{row['sha256']}  {row['artifact_path']}\n" for row in rows if row["artifact_path"] is not None)


def _validate_rendered_outputs(
    bundle: Path,
    source: dict[str, Any],
    rows: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
) -> None:
    private_checklist_raw, _ = _bundle_file(bundle, "HOST_WORKFLOW_CHECKLIST.md", "private rendered checklist")
    if private_checklist_raw != _private_checklist(rows).encode():
        _error("private rendered checklist disagrees with evidence.json")
    sums = _sha256_sums(artifacts)
    private_sums_raw, _ = _bundle_file(bundle, "SHA256SUMS.txt", "private SHA256SUMS")
    if private_sums_raw != sums.encode():
        _error("private SHA256SUMS disagrees with evidence.json")

    public = bundle / "public"
    try:
        public_stat = public.lstat()
    except FileNotFoundError:
        _error("public evidence directory is missing")
    if stat.S_ISLNK(public_stat.st_mode) or not stat.S_ISDIR(public_stat.st_mode):
        _error("public evidence path must be a non-symlink directory")
    actual_public = {path.name for path in public.iterdir()}
    if actual_public != _PUBLIC_FILES:
        _error("public evidence contains missing or unexpected output files")

    public_rows = _public_rows(rows)
    public_expected = {
        "rows": public_rows,
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "source": {"commit": source["commit"]},
    }
    public_raw, _ = _bundle_file(bundle, "public/evidence-summary.json", "public evidence summary")
    public_actual = _json_object(public_raw, "public evidence summary")
    if set(public_actual) != {"rows", "schema_version", "source"}:
        _error("public evidence summary contains private or unexpected fields")
    if public_actual != public_expected:
        _error("public evidence summary disagrees with private evidence.json")
    if any(not isinstance(row, dict) or set(row) != _PUBLIC_ROW_KEYS for row in public_actual["rows"]):
        _error("public evidence rows contain private or unexpected fields")

    public_checklist_raw, _ = _bundle_file(bundle, "public/HOST_WORKFLOW_CHECKLIST.md", "public rendered checklist")
    if public_checklist_raw != _public_checklist(public_rows).encode():
        _error("public rendered checklist disagrees with evidence.json")
    public_sums_raw, _ = _bundle_file(bundle, "public/SHA256SUMS.txt", "public SHA256SUMS")
    if public_sums_raw != _public_sha256_sums(public_rows).encode():
        _error("public SHA256SUMS disagrees with evidence.json")


def _validate_bundle_layout(bundle: Path) -> Path:
    bundle = Path(bundle).expanduser()
    if not bundle.is_absolute():
        bundle = (Path.cwd() / bundle).absolute()
    _reject_symlink_components(bundle, "evidence bundle")
    try:
        bundle_stat = bundle.lstat()
    except FileNotFoundError:
        _error("evidence bundle does not exist")
    if stat.S_ISLNK(bundle_stat.st_mode) or not stat.S_ISDIR(bundle_stat.st_mode):
        _error("evidence bundle must be a non-symlink directory")
    if stat.S_IMODE(bundle_stat.st_mode) != 0o700:
        _error("evidence bundle root must remain private mode 0700")
    bundle = bundle.resolve(strict=True)
    names = {path.name for path in bundle.iterdir()}
    if not _ROOT_REQUIRED <= names:
        _error("evidence bundle is missing required files or directories")
    if not names <= _ROOT_ALLOWED:
        _error("evidence bundle contains unexpected root entries")
    for path in bundle.rglob("*"):
        item_stat = path.lstat()
        if stat.S_ISLNK(item_stat.st_mode):
            _error("evidence bundle must not contain symlinks")
        if stat.S_ISDIR(item_stat.st_mode):
            expected_mode = 0o700
        elif stat.S_ISREG(item_stat.st_mode):
            expected_mode = 0o600
        else:
            _error("evidence bundle must contain only regular files and directories")
        if stat.S_IMODE(item_stat.st_mode) != expected_mode:
            _error("evidence bundle contains unexpected permissions; all outputs must remain private")
    return bundle


def _bundle_snapshot(bundle: Path) -> dict[str, tuple[str, int]]:
    snapshot: dict[str, tuple[str, int]] = {}
    for path in sorted(bundle.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(bundle).as_posix()
        raw, _ = _bundle_file(bundle, relative, "bundle snapshot file")
        snapshot[relative] = (hashlib.sha256(raw).hexdigest(), len(raw))
    return snapshot


def _revalidate_artifact_snapshot(storage: Path, artifacts: list[dict[str, Any]]) -> None:
    discovered: set[Path] = set()
    for directory, directories, files in os.walk(storage, followlinks=False):
        for name in directories:
            if (Path(directory) / name).is_symlink():
                _error("storage tree must not contain symlink directories")
        for filename in files:
            if filename.endswith(".manifest.json"):
                discovered.add((Path(directory) / filename).resolve(strict=True))
    if discovered != {item["_manifest_resolved"] for item in artifacts}:
        _error("artifact source inventory changed during evidence validation")
    for item in artifacts:
        digest, size_bytes, artifact_path = _hash_storage_file(
            storage,
            item["artifact_path"],
            "source artifact recheck",
        )
        if artifact_path != item["_artifact_resolved"] or digest != item["sha256"] or size_bytes != item["size_bytes"]:
            _error("source artifact changed during evidence validation")
        manifest_raw, manifest_path = _storage_file(storage, item["manifest_path"], "source manifest recheck")
        sidecar_raw, sidecar_path = _storage_file(storage, item["sidecar_path"], "source sidecar recheck")
        if manifest_path != item["_manifest_resolved"] or manifest_raw != item["_manifest_bytes"]:
            _error("source manifest changed during evidence validation")
        if sidecar_path != item["_sidecar_resolved"] or sidecar_raw != item["_sidecar_bytes"]:
            _error("source sidecar changed during evidence validation")


def validate_bundle(
    bundle: Path,
    *,
    candidate_sha: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate one private bundle and return a machine-readable PASS summary."""

    bundle = _validate_bundle_layout(Path(bundle))
    initial_bundle_snapshot = _bundle_snapshot(bundle)
    current = datetime.now(UTC) if now is None else now
    if current.tzinfo is None:
        _error("validation timestamp must be timezone-aware")
    current = current.astimezone(UTC)

    evidence_raw, _ = _bundle_file(bundle, "evidence.json", "evidence.json")
    evidence = _json_object(evidence_raw, "evidence.json")
    if set(evidence) != {"artifacts", "proofs", "rows", "schema_version", "source", "storage_root"}:
        _error("evidence.json contains missing or unexpected fields")
    if evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        _error("evidence.json schema_version is invalid")
    source = _source_commit(bundle, evidence.get("source"), candidate_sha)

    storage_value = evidence.get("storage_root")
    if not isinstance(storage_value, str) or not Path(storage_value).is_absolute():
        _error("evidence storage_root must be absolute")
    storage_path = Path(storage_value)
    _reject_symlink_components(storage_path, "evidence storage_root")
    try:
        storage_stat = storage_path.lstat()
    except FileNotFoundError:
        _error("evidence storage_root does not exist")
    if stat.S_ISLNK(storage_stat.st_mode) or not stat.S_ISDIR(storage_stat.st_mode):
        _error("evidence storage_root must be a non-symlink directory")
    storage = storage_path.resolve(strict=True)
    if _paths_overlap(storage, bundle):
        _error("evidence storage root and bundle must not overlap")

    artifacts, artifact_map = _validate_artifacts(bundle, storage, evidence.get("artifacts"))
    proofs = _validate_proof_inventory(evidence.get("proofs"))
    rows = _validate_rows(bundle, evidence.get("rows"), artifact_map, now=current, proofs=proofs)
    _validate_rendered_outputs(bundle, source, rows, artifacts)
    _revalidate_artifact_snapshot(storage, artifacts)
    _validate_bundle_layout(bundle)
    if _bundle_snapshot(bundle) != initial_bundle_snapshot:
        _error("evidence bundle changed during validation")
    return {
        "artifacts": len(artifacts),
        "commit": source["commit"],
        "rows": len(rows),
        "verdict": "PASS",
    }


def _git_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def _current_head(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        env=_git_env(),
        text=True,
    )
    commit = result.stdout.strip()
    if result.returncode != 0 or _FULL_COMMIT.fullmatch(commit) is None:
        _error("current candidate SHA could not be resolved from repository")
    status = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain=v1", "--untracked-files=no"],
        check=False,
        capture_output=True,
        env=_git_env(),
        text=True,
    )
    if status.returncode != 0 or status.stdout:
        _error("current candidate worktree has dirty tracked files")
    return commit


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate a private AstinusLab release-evidence bundle")
    parser.add_argument("EVIDENCE_BUNDLE", type=Path, help="private evidence bundle directory")
    parser.add_argument(
        "--candidate-sha",
        help="expected full candidate SHA; defaults to current repository HEAD",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parents[2]
    try:
        current_head = _current_head(repo_root)
        candidate_sha = args.candidate_sha or current_head
        if candidate_sha != current_head:
            _error("requested candidate SHA does not match current repository HEAD")
        result = validate_bundle(args.EVIDENCE_BUNDLE, candidate_sha=candidate_sha)
        if _current_head(repo_root) != candidate_sha:
            _error("current repository source changed during evidence validation")
    except ValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"PASS commit={result['commit']} rows={result['rows']} artifacts={result['artifacts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
