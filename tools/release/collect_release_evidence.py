#!/usr/bin/env python3
"""Collect self-describing AstinusLab release evidence without backend imports."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4

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
    "artifact_id",
    "capture",
    "created_at_utc",
    "filename",
    "inputs",
    "integrity",
    "kind",
    "network",
    "path",
    "producer",
    "schema_version",
    "sha256",
    "sha256_sidecar_path",
    "size_bytes",
    "target",
}
_INTEGRITY_KEYS = {
    "domain_xml_excerpt",
    "domain_xml_sha256",
    "fake_mode",
    "fake_mode_flags",
    "raw_sha256",
}
# Must match astinuslab.core.fake_mode.fake_mode_flags() exactly: release
# evidence is rejected unless the manifest declares every fake flag the
# producer knows about and every one of them is false.
_FAKE_FLAG_KEYS = {"fake_libvirt", "fake_playground", "fake_orchestration"}
_HEX_SHA256 = re.compile(r"[0-9a-f]{64}")
_FULL_COMMIT = re.compile(r"[0-9a-f]{40}")
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1


class CollectionError(RuntimeError):
    """Release evidence cannot be collected safely or consistently."""


def _error(message: str) -> NoReturn:
    raise CollectionError(message)


def _run_git(repo_root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise CollectionError(detail)
    return result


def _require_absolute(path: Path, label: str) -> Path:
    path = path.expanduser()
    if not path.is_absolute():
        _error(f"{label} must be an absolute path")
    if ".." in path.parts:
        _error(f"{label} must not contain a parent reference")
    return path


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _paths_overlap(first: Path, second: Path) -> bool:
    return _path_is_within(first, second) or _path_is_within(second, first)


def _reject_symlink_components(path: Path, label: str, *, allow_missing_leaf: bool = False) -> None:
    path = _require_absolute(path, label)
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            item_stat = current.lstat()
        except FileNotFoundError:
            # Nothing below a missing component can exist, so no symlink is left to
            # reject; callers that require the path to exist refuse it here instead.
            if allow_missing_leaf:
                return
            _error(f"{label} does not exist")
        if stat.S_ISLNK(item_stat.st_mode):
            _error(f"{label} must not contain a symlink")


def _existing_directory(path: Path, label: str) -> Path:
    path = _require_absolute(path, label)
    _reject_symlink_components(path, label)
    try:
        item_stat = path.stat()
    except FileNotFoundError:
        _error(f"{label} directory does not exist")
    if not stat.S_ISDIR(item_stat.st_mode):
        _error(f"{label} must be a directory")
    return path.resolve(strict=True)


def _opened_target(fd: int, label: str) -> Path:
    proc_path = Path(f"/proc/self/fd/{fd}")
    try:
        return proc_path.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise CollectionError(f"{label} open-file identity could not be verified through /proc/self/fd") from exc


def _verify_open_path(path: Path, expected: Path, fd: int, item_stat: os.stat_result, label: str) -> None:
    try:
        path_after = path.lstat()
    except FileNotFoundError:
        _error(f"{label} changed while it was open")
    if stat.S_ISLNK(path_after.st_mode) or not stat.S_ISREG(path_after.st_mode):
        _error(f"{label} changed type while it was open")
    expected_identity = (item_stat.st_dev, item_stat.st_ino, item_stat.st_size, item_stat.st_mtime_ns)
    path_identity = (path_after.st_dev, path_after.st_ino, path_after.st_size, path_after.st_mtime_ns)
    if path_identity != expected_identity or _opened_target(fd, label) != expected:
        _error(f"{label} changed identity while it was open")


def _read_regular_bytes(path: Path, label: str, *, root: Path | None = None) -> tuple[bytes, Path]:
    path = _require_absolute(path, label)
    try:
        item_stat = path.lstat()
    except FileNotFoundError:
        _error(f"{label} does not exist")
    if stat.S_ISLNK(item_stat.st_mode):
        _error(f"{label} must not be a symlink")
    if not stat.S_ISREG(item_stat.st_mode):
        _error(f"{label} must be a regular file")

    expected = path.resolve(strict=True)
    if root is not None and not _path_is_within(expected, root):
        _error(f"{label} must stay inside storage root")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise CollectionError(f"{label} could not be opened as a regular non-symlink file") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            _error(f"{label} must be a regular file")
        opened = _opened_target(fd, label)
        if opened != expected:
            _error(f"{label} changed while it was opened")
        if root is not None and not _path_is_within(opened, root):
            _error(f"{label} opened outside storage root")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        raw = b"".join(chunks)
        if identity_before != identity_after or len(raw) != after.st_size:
            _error(f"{label} changed while it was read")
        _verify_open_path(path, expected, fd, after, label)
        return raw, opened
    finally:
        os.close(fd)


def _hash_regular_file(path: Path, label: str, *, root: Path) -> tuple[str, int, Path]:
    path = _require_absolute(path, label)
    try:
        item_stat = path.lstat()
    except FileNotFoundError:
        _error(f"{label} does not exist")
    if stat.S_ISLNK(item_stat.st_mode):
        _error(f"{label} must not be a symlink")
    if not stat.S_ISREG(item_stat.st_mode):
        _error(f"{label} must be a regular file")

    expected = path.resolve(strict=True)
    if not _path_is_within(expected, root):
        _error(f"{label} must stay inside storage root")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise CollectionError(f"{label} could not be opened as a regular non-symlink file") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            _error(f"{label} must be a regular file")
        opened = _opened_target(fd, label)
        if opened != expected or not _path_is_within(opened, root):
            _error(f"{label} opened outside storage root or changed identity")
        digest = hashlib.sha256()
        size_bytes = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size_bytes += len(chunk)
        after = os.fstat(fd)
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_before != identity_after or size_bytes != after.st_size:
            _error(f"{label} changed while it was hashed")
        _verify_open_path(path, expected, fd, after, label)
        return digest.hexdigest(), size_bytes, opened
    finally:
        os.close(fd)


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
        raise CollectionError(f"{label} must contain valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        _error(f"{label} must contain a JSON object")
    return value


def _git_source(repo_root: Path) -> dict[str, Any]:
    repo_root = _existing_directory(repo_root, "repository root")
    commit = _run_git(repo_root, "rev-parse", "HEAD").stdout.strip()
    if _FULL_COMMIT.fullmatch(commit) is None:
        _error("git rev-parse HEAD did not return a full 40-character lowercase SHA")
    dirty = _run_git(repo_root, "status", "--porcelain=v1", "--untracked-files=no").stdout
    if dirty:
        _error("tracked checkout must be clean before evidence collection")
    branch_result = _run_git(repo_root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    detached = branch_result.returncode != 0
    branch = "DETACHED" if detached else branch_result.stdout.strip()
    if not detached and not branch:
        _error("git branch name could not be resolved")
    return {
        "branch": branch,
        "commit": commit,
        "detached": detached,
        "tracked_clean": True,
    }


def default_evidence_root(repo_root: Path, *, environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    xdg_state = env.get("XDG_STATE_HOME")
    if xdg_state:
        base = Path(xdg_state).expanduser()
    else:
        home = env.get("HOME")
        if not home:
            home = str(Path.home())
        base = Path(home).expanduser() / ".local/state"
    result = _require_absolute(base / "astinuslab/release-evidence", "default evidence root").resolve()
    repo = Path(repo_root).expanduser().resolve()
    if _path_is_within(result, repo):
        _error("default evidence root must be outside repository")
    return result


def _validate_evidence_root(repo_root: Path, storage_root: Path, evidence_root: Path) -> Path:
    evidence_root = _require_absolute(evidence_root, "evidence root")
    canonical = evidence_root.resolve(strict=False)
    if _paths_overlap(canonical, storage_root):
        _error("evidence root and storage root must not overlap")
    repo = repo_root.resolve(strict=True)
    if canonical == repo or _path_is_within(repo, canonical):
        _error("evidence root must not contain the repository")
    if _path_is_within(canonical, repo):
        relative = canonical.relative_to(repo).as_posix()
        ignored = _run_git(
            repo,
            "check-ignore",
            "--quiet",
            "--no-index",
            f"{relative}/.astinuslab-evidence-probe",
            check=False,
        )
        if ignored.returncode != 0:
            _error("an evidence root inside repository must be ignored by git")
    _reject_symlink_components(evidence_root, "evidence root", allow_missing_leaf=True)
    return evidence_root


def _validate_waiver(value: object, row_number: int) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _WAIVER_KEYS:
        _error(f"row {row_number} waiver must contain exactly the documented fields")
    waiver_row = value.get("row")
    if type(waiver_row) is not int or waiver_row != row_number:
        _error(f"row {row_number} waiver row must be the matching integer")
    for key in _WAIVER_KEYS - {"row"}:
        item = value.get(key)
        if not isinstance(item, str) or not item.strip():
            _error(f"row {row_number} waiver field {key} must be nonempty")
    # Expiry is judged at validation time, but refuse a value the validator could
    # never parse rather than publishing a bundle that can never pass.
    expiry = value["expires_or_retest"].strip()
    try:
        if "T" in expiry or " " in expiry:
            parsed = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                _error(f"row {row_number} waiver expiry/retest datetime must include a timezone")
        else:
            date.fromisoformat(expiry)
    except ValueError as exc:
        raise CollectionError(f"row {row_number} waiver expiry/retest must be ISO-8601") from exc
    return dict(value)


def _record_field(record_type: str) -> str:
    normalized = record_type.replace("-", "_")
    normalized = _RECORD_FIELD_ALIASES.get(normalized, normalized)
    return f"{normalized}_id"


def _proof_source(
    path_text: object,
    label: str,
    *,
    forbidden_artifacts: set[Path],
) -> dict[str, Any]:
    if not isinstance(path_text, str) or not path_text:
        _error(f"{label} must name an absolute proof file")
    path = Path(path_text)
    if not path.is_absolute():
        _error(f"{label} must be an absolute path")
    try:
        resolved_before_read = path.resolve(strict=True)
    except FileNotFoundError:
        resolved_before_read = path
    if resolved_before_read in forbidden_artifacts:
        _error(f"{label} must not copy a raw artifact")
    raw, resolved = _read_regular_bytes(path, label)
    return {"bytes": raw, "name": resolved.name, "path": resolved}


def _validate_rows(
    rows_file: Path,
    *,
    command_log: Path | None,
    forbidden_artifacts: set[Path] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    rows_file = _require_absolute(rows_file, "rows file")
    rows_bytes, _ = _read_regular_bytes(rows_file, "rows file")
    document = _json_object(rows_bytes, "rows file")
    if set(document) != {"schema_version", "rows"}:
        _error("rows file contains missing or unexpected top-level keys")
    if document.get("schema_version") != ROWS_SCHEMA_VERSION:
        _error("rows file schema_version is not supported")
    rows_value = document.get("rows")
    if not isinstance(rows_value, list):
        _error("rows must be a list")
    numbers: list[int] = []
    for row in rows_value:
        if not isinstance(row, dict) or type(row.get("row")) is not int:
            _error("each workflow row must be an object with an integer row number")
        numbers.append(row["row"])
    if len(numbers) != 7 or sorted(numbers) != list(range(1, 8)) or len(set(numbers)) != 7:
        _error("rows must contain exactly one workflow row from 1 through 7; duplicates are forbidden")

    claimed_artifacts = {
        Path(row["artifact_path"]).resolve(strict=False)
        for row in rows_value
        if isinstance(row, dict)
        and isinstance(row.get("artifact_path"), str)
        and Path(row["artifact_path"]).is_absolute()
    }
    claimed_artifacts.update(forbidden_artifacts or set())

    validated: list[dict[str, Any]] = []
    any_command_reference = False
    record_ids: set[str] = set()
    for raw_row in sorted(rows_value, key=lambda item: item["row"]):
        number = raw_row["row"]
        if set(raw_row) != _ROW_KEYS:
            missing = sorted(_ROW_KEYS - set(raw_row))
            unexpected = sorted(set(raw_row) - _ROW_KEYS)
            _error(f"row {number} keys are invalid; missing={missing}, unexpected={unexpected}")
        driver = raw_row["driver"]
        if driver not in {"api", "ui"}:
            _error(f"row {number} driver must be api or ui")
        if number in _REQUIRED_UI_ROWS and driver != "ui":
            _error(f"row {number} must use ui driver")
        record_type = raw_row["record_type"]
        record_id = raw_row["record_id"]
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
        if raw_row["status"] != "completed":
            _error(f"row {number} status must be completed")

        triad_keys = ("artifact_path", "manifest_path", "sidecar_path")
        if number == 5:
            if any(raw_row[key] is not None for key in triad_keys):
                _error("row 5 must use a null artifact, manifest, and sidecar triad")
        else:
            for key in triad_keys:
                value = raw_row[key]
                if not isinstance(value, str) or not Path(value).is_absolute():
                    _error(f"row {number} {key} must be an absolute artifact reference")

        evidence_value = raw_row["evidence_paths"]
        if not isinstance(evidence_value, list):
            _error(f"row {number} evidence_paths must be a list")
        # Every row carries proof: UI rows may rely on their trace/screenshot set,
        # so only rows without UI proof (and row 5, which never has an artifact) must
        # supply an explicit evidence file.
        if not evidence_value and (number == 5 or driver != "ui"):
            _error(f"row {number} requires at least one evidence file")
        evidence_sources = [
            _proof_source(
                value,
                f"row {number} evidence proof",
                forbidden_artifacts=claimed_artifacts,
            )
            for value in evidence_value
        ]

        ui_value = raw_row["ui_proof"]
        ui_sources: dict[str, Any] | None = None
        if driver == "ui":
            if not isinstance(ui_value, dict) or set(ui_value) != _UI_PROOF_KEYS:
                _error(f"row {number} ui proof must contain trace and screenshot paths")
            screenshots = ui_value["screenshot_paths"]
            if not isinstance(screenshots, list) or not screenshots:
                _error(f"row {number} ui proof requires at least one screenshot")
            ui_sources = {
                "trace": _proof_source(
                    ui_value["trace_path"],
                    f"row {number} UI trace proof",
                    forbidden_artifacts=claimed_artifacts,
                ),
                "screenshots": [
                    _proof_source(
                        value,
                        f"row {number} UI screenshot proof",
                        forbidden_artifacts=claimed_artifacts,
                    )
                    for value in screenshots
                ],
            }
        elif ui_value is not None:
            _error(f"row {number} api driver must not claim UI proof")

        source_names = [source["name"] for source in evidence_sources]
        if ui_sources is not None:
            source_names.append(ui_sources["trace"]["name"])
            source_names.extend(source["name"] for source in ui_sources["screenshots"])
        if len(source_names) != len(set(source_names)):
            _error(f"row {number} proof basename collision")

        command_reference = raw_row["command_log_ref"]
        if command_reference is not None and (not isinstance(command_reference, str) or not command_reference.strip()):
            _error(f"row {number} command log reference must be nonempty")
        any_command_reference = any_command_reference or command_reference is not None

        validated.append(
            {
                **{key: raw_row[key] for key in _ROW_KEYS},
                "waiver": _validate_waiver(raw_row["waiver"], number),
                "_evidence_sources": evidence_sources,
                "_ui_sources": ui_sources,
            }
        )

    command_source: dict[str, Any] | None = None
    if command_log is None:
        if any_command_reference:
            _error("a referenced command log must be supplied")
    else:
        command_log = _require_absolute(Path(command_log), "operator command log")
        command_source = _proof_source(
            str(command_log),
            "operator command log",
            forbidden_artifacts=claimed_artifacts,
        )
        try:
            command_source["bytes"].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CollectionError("operator command log must be UTF-8") from exc
    return validated, command_source


def _discover_manifest_paths(storage_root: Path) -> list[Path]:
    manifests: list[Path] = []
    for directory, child_dirs, files in os.walk(storage_root, followlinks=False):
        for name in child_dirs:
            if (Path(directory) / name).is_symlink():
                _error("storage tree must not contain symlink directories")
        for filename in files:
            if filename.endswith(".manifest.json"):
                manifests.append(Path(directory) / filename)
    manifests.sort(key=lambda path: path.relative_to(storage_root).as_posix())
    if not manifests:
        _error("storage root contains no artifact manifests")
    return manifests


def _strict_sidecar(sidecar_bytes: bytes, artifact_name: str, digest: str) -> None:
    try:
        text = sidecar_bytes.decode("ascii")
    except UnicodeDecodeError as exc:
        raise CollectionError("sidecar format must be ASCII") from exc
    match = re.fullmatch(r"([0-9a-f]{64})  ([^\n]+)\n", text)
    if match is None:
        _error("sidecar format must be lowercase sha256, two spaces, filename, and newline")
    sidecar_digest, sidecar_name = match.groups()
    if sidecar_name != artifact_name:
        _error("sidecar filename does not match artifact filename")
    if sidecar_digest != digest:
        _error("sidecar digest does not match independently recomputed sha256")


def _validate_manifest_schema(manifest: dict[str, Any]) -> None:
    missing = _MANIFEST_REQUIRED_KEYS - set(manifest)
    if missing:
        _error(f"artifact manifest is missing required fields: {', '.join(sorted(missing))}")
    if type(manifest.get("schema_version")) is not str or manifest["schema_version"] != MANIFEST_SCHEMA_VERSION:
        _error("artifact manifest schema_version is not supported")
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
        raise CollectionError("artifact manifest created_at_utc must be a valid timestamp") from exc
    if parsed_created.tzinfo is None or parsed_created.utcoffset() != timedelta(0):
        _error("artifact manifest created_at_utc must be UTC")
    for field in ("target", "capture", "network"):
        value = manifest.get(field)
        if value is not None and not isinstance(value, dict):
            _error(f"artifact manifest {field} must be null or an object")
    if not isinstance(manifest.get("inputs"), list):
        _error("artifact manifest inputs must be a list")

    integrity = manifest.get("integrity")
    if not isinstance(integrity, dict) or set(integrity) != _INTEGRITY_KEYS:
        _error("artifact manifest integrity must contain exactly the producer schema fields")
    # The producer records the artifact digest and size at manifest top level; the
    # integrity block carries provenance of the raw capture and the domain XML.
    for field in ("raw_sha256", "domain_xml_sha256"):
        value = integrity.get(field)
        if value is not None and (not isinstance(value, str) or _HEX_SHA256.fullmatch(value) is None):
            _error(f"artifact manifest integrity {field} must be null or a lowercase sha256 digest")
    excerpt = integrity.get("domain_xml_excerpt")
    if excerpt is not None and not isinstance(excerpt, str):
        _error("artifact manifest integrity domain_xml_excerpt must be null or a string")
    if integrity.get("fake_mode") is not False:
        _error("manifest fake_mode must be explicitly false")
    flags = integrity.get("fake_mode_flags")
    if not isinstance(flags, dict) or set(flags) != _FAKE_FLAG_KEYS:
        _error("manifest fake_mode_flags must contain exactly all producer fake flags")
    if any(type(value) is not bool for value in flags.values()):
        _error("manifest fake_mode_flags must contain booleans")
    if any(flags.values()):
        _error("manifest contains a truthy fake_mode_flags fake flag")


def _manifest_input_paths(manifest: dict[str, Any], artifact_path: Path) -> tuple[list[Path], list[Path]]:
    memory_paths: list[Path] = []
    isf_paths: list[Path] = []
    kind = str(manifest.get("kind", "")).lower().replace("_", "-")
    if kind in {"memory", "memory-dump", "raw-memory"}:
        memory_paths.append(artifact_path)
    if kind == "isf":
        isf_paths.append(artifact_path)
    inputs = manifest.get("inputs", [])
    if not isinstance(inputs, list):
        _error("manifest inputs must be a list")
    for item in inputs:
        if not isinstance(item, dict):
            _error("manifest inputs must contain objects")
        item_kind = str(item.get("kind", "")).lower().replace("_", "-")
        item_path = item.get("path")
        if not isinstance(item_path, str) or not item_path:
            continue
        candidate = Path(item_path)
        if not candidate.is_absolute():
            candidate = artifact_path.parent / candidate
        candidate = candidate.resolve(strict=False)
        if item_kind in {"memory", "memory-dump", "raw-memory"}:
            memory_paths.append(candidate)
        elif item_kind == "isf":
            isf_paths.append(candidate)
    return memory_paths, isf_paths


def _validate_artifacts(storage_root: Path) -> tuple[list[dict[str, Any]], dict[Path, dict[str, Any]]]:
    inventory: list[dict[str, Any]] = []
    by_artifact: dict[Path, dict[str, Any]] = {}
    memory_paths: list[Path] = []
    isf_paths: list[Path] = []

    for manifest_path in _discover_manifest_paths(storage_root):
        manifest_bytes, manifest_resolved = _read_regular_bytes(manifest_path, "artifact manifest", root=storage_root)
        manifest = _json_object(manifest_bytes, "artifact manifest")
        suffix = ".manifest.json"
        artifact_path = manifest_path.with_name(manifest_path.name[: -len(suffix)])
        sidecar_path = artifact_path.with_name(f"{artifact_path.name}.sha256")
        digest, size_bytes, artifact_resolved = _hash_regular_file(artifact_path, "artifact", root=storage_root)
        sidecar_bytes, sidecar_resolved = _read_regular_bytes(sidecar_path, "artifact sidecar", root=storage_root)
        _validate_manifest_schema(manifest)

        manifest_path_value = manifest.get("path")
        if not isinstance(manifest_path_value, str) or not Path(manifest_path_value).is_absolute():
            _error("manifest artifact path must be absolute")
        if Path(manifest_path_value).resolve(strict=False) != artifact_resolved:
            _error("manifest artifact path does not match discovered artifact path")
        if manifest.get("filename") != artifact_resolved.name:
            _error("manifest filename does not match artifact filename")
        manifest_digest = manifest.get("sha256")
        if not isinstance(manifest_digest, str) or _HEX_SHA256.fullmatch(manifest_digest) is None:
            _error("manifest sha256 must be a lowercase 64-character digest")
        if manifest_digest != digest:
            _error("manifest sha256 digest does not match independently recomputed digest")
        manifest_size = manifest.get("size_bytes")
        if isinstance(manifest_size, bool) or not isinstance(manifest_size, int) or manifest_size != size_bytes:
            _error("manifest size_bytes does not match artifact size")
        sidecar_value = manifest.get("sha256_sidecar_path")
        if not isinstance(sidecar_value, str) or not Path(sidecar_value).is_absolute():
            _error("manifest sha256 sidecar path must be absolute")
        if Path(sidecar_value).resolve(strict=False) != sidecar_resolved:
            _error("manifest sha256 sidecar path does not match discovered sidecar path")

        _strict_sidecar(sidecar_bytes, artifact_resolved.name, digest)
        relative_artifact = artifact_resolved.relative_to(storage_root).as_posix()
        relative_manifest = manifest_resolved.relative_to(storage_root).as_posix()
        relative_sidecar = sidecar_resolved.relative_to(storage_root).as_posix()
        for label, value in (
            ("artifact relative path", relative_artifact),
            ("manifest relative path", relative_manifest),
            ("sidecar relative path", relative_sidecar),
        ):
            if any(ord(character) < 32 or ord(character) == 127 for character in value):
                _error(f"{label} contains unsafe control characters")
        public_record = {
            "artifact_path": relative_artifact,
            "bundle_manifest_path": f"manifests/{relative_manifest}",
            "bundle_sidecar_path": f"sidecars/{relative_sidecar}",
            "kind": manifest.get("kind"),
            "manifest_path": relative_manifest,
            "sha256": digest,
            "sidecar_path": relative_sidecar,
            "size_bytes": size_bytes,
        }
        internal_record = {
            **public_record,
            "_artifact_resolved": artifact_resolved,
            "_manifest_data": manifest,
            "_manifest_bytes": manifest_bytes,
            "_manifest_resolved": manifest_resolved,
            "_sidecar_bytes": sidecar_bytes,
            "_sidecar_resolved": sidecar_resolved,
        }
        inventory.append(internal_record)
        by_artifact[artifact_resolved] = internal_record
        manifest_memory, manifest_isf = _manifest_input_paths(manifest, artifact_resolved)
        memory_paths.extend(manifest_memory)
        isf_paths.extend(manifest_isf)

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

    inventory.sort(key=lambda item: item["artifact_path"])
    return inventory, by_artifact


def _evidence_sources(
    rows: list[dict[str, Any]],
    command_source: dict[str, Any] | None,
) -> list[tuple[str, dict[str, Any]]]:
    sources: list[tuple[str, dict[str, Any]]] = []
    for row in rows:
        number = row["row"]
        sources.extend((f"row {number} evidence proof", source) for source in row["_evidence_sources"])
        ui_sources = row["_ui_sources"]
        if ui_sources is not None:
            sources.append((f"row {number} UI trace proof", ui_sources["trace"]))
            sources.extend((f"row {number} UI screenshot proof", source) for source in ui_sources["screenshots"])
    if command_source is not None:
        sources.append(("operator command log", command_source))
    return sources


def _reject_raw_artifact_sources(
    rows: list[dict[str, Any]],
    command_source: dict[str, Any] | None,
    artifacts: list[dict[str, Any]],
) -> None:
    artifact_signatures = {(item["sha256"], item["size_bytes"]) for item in artifacts}
    artifact_paths = [item["_artifact_resolved"] for item in artifacts]
    for label, source in _evidence_sources(rows, command_source):
        source_signature = (hashlib.sha256(source["bytes"]).hexdigest(), len(source["bytes"]))
        if source_signature in artifact_signatures:
            _error(f"{label} must not copy raw artifact bytes")
        for artifact_path in artifact_paths:
            try:
                same_file = source["path"].samefile(artifact_path)
            except OSError:
                same_file = source["path"] == artifact_path
            if same_file:
                _error(f"{label} must not copy a raw artifact")


def _normalized_rows(
    rows: list[dict[str, Any]],
    storage_root: Path,
    by_artifact: dict[Path, dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    referenced_artifacts: set[tuple[int, int]] = set()
    for row in rows:
        number = row["row"]
        output = {
            key: row[key]
            for key in (
                "command_log_ref",
                "driver",
                "record_id",
                "record_type",
                "row",
                "status",
                "waiver",
            )
        }
        if number == 5:
            output.update(
                {
                    "artifact_path": None,
                    "manifest_path": None,
                    "sha256": None,
                    "sidecar_path": None,
                    "size_bytes": None,
                }
            )
        else:
            resolved_paths: dict[str, Path] = {}
            for key in ("artifact_path", "manifest_path", "sidecar_path"):
                source = Path(row[key])
                try:
                    resolved = source.resolve(strict=True)
                except FileNotFoundError:
                    _error(f"row {number} artifact reference does not exist")
                if not _path_is_within(resolved, storage_root):
                    _error(f"row {number} artifact reference must stay inside storage root")
                resolved_paths[key] = resolved
            record = by_artifact.get(resolved_paths["artifact_path"])
            if record is None:
                _error(f"row {number} artifact reference has no validated manifest")
            artifact_stat = resolved_paths["artifact_path"].stat()
            artifact_identity = (artifact_stat.st_dev, artifact_stat.st_ino)
            if artifact_identity in referenced_artifacts:
                _error(f"row {number} contains a duplicate artifact reference")
            referenced_artifacts.add(artifact_identity)
            if resolved_paths["manifest_path"] != record["_manifest_resolved"]:
                _error(f"row {number} manifest reference disagrees with artifact")
            if resolved_paths["sidecar_path"] != record["_sidecar_resolved"]:
                _error(f"row {number} sidecar reference disagrees with artifact")
            record_field = _record_field(row["record_type"])
            if record["_manifest_data"].get(record_field) != row["record_id"]:
                _error(f"row {number} record identity disagrees with artifact manifest")
            expected_kind = _EXPECTED_ARTIFACT_KINDS.get(number)
            if expected_kind is not None and record["kind"] != expected_kind:
                _error(f"row {number} artifact kind must be {expected_kind}")
            output.update(
                {
                    "artifact_path": record["artifact_path"],
                    "manifest_path": record["manifest_path"],
                    "sha256": record["sha256"],
                    "sidecar_path": record["sidecar_path"],
                    "size_bytes": record["size_bytes"],
                }
            )
        output["_evidence_sources"] = row["_evidence_sources"]
        output["_ui_sources"] = row["_ui_sources"]
        normalized.append(output)
    return normalized


def _secure_create_directory_tree(path: Path) -> None:
    path = _require_absolute(path, "evidence root")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path.anchor, flags)
    try:
        components = path.parts[1:]
        for index, component in enumerate(components):
            created = False
            try:
                os.mkdir(component, 0o700, dir_fd=fd)
                created = True
            except FileExistsError:
                pass
            child = os.open(component, flags, dir_fd=fd)
            try:
                child_stat = os.fstat(child)
                if not stat.S_ISDIR(child_stat.st_mode):
                    _error("evidence root component must be a directory")
                if created:
                    os.fchmod(child, 0o700)
                elif index == len(components) - 1 and stat.S_IMODE(child_stat.st_mode) != 0o700:
                    _error("existing evidence root must already use private mode 0700")
            except BaseException:
                os.close(child)
                raise
            os.close(fd)
            fd = child

    except OSError as exc:
        raise CollectionError(f"evidence root could not be created safely: {exc.strerror}") from exc
    finally:
        os.close(fd)


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _write_new(path: Path, data: bytes) -> None:
    _private_directory(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def _write_text(path: Path, text: str) -> None:
    _write_new(path, text.encode("utf-8"))


def _write_json(path: Path, value: object) -> None:
    _write_text(path, json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n")


def _normalize_bundle_modes(bundle: Path) -> None:
    """Make every generated directory private, including parents made recursively."""

    bundle.chmod(0o700)
    for path in bundle.rglob("*"):
        item_stat = path.lstat()
        if stat.S_ISLNK(item_stat.st_mode):
            _error("generated evidence bundle must not contain symlinks")
        if stat.S_ISDIR(item_stat.st_mode):
            path.chmod(0o700)
        elif stat.S_ISREG(item_stat.st_mode):
            path.chmod(0o600)
        else:
            _error("generated evidence bundle must contain only regular files and directories")


def _proof_digest(path: str, payload: bytes) -> dict[str, Any]:
    return {"path": path, "sha256": hashlib.sha256(payload).hexdigest(), "size_bytes": len(payload)}


def _copy_proofs(bundle: Path, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    output_rows: list[dict[str, Any]] = []
    inventory: list[dict[str, Any]] = []

    def store(destination: Path, payload: bytes) -> str:
        """Copy one proof and bind its bytes into the private proof inventory."""
        _write_new(bundle / destination, payload)
        relative = destination.as_posix()
        inventory.append(_proof_digest(relative, payload))
        return relative

    for row in rows:
        number = row["row"]
        output = {key: value for key, value in row.items() if not key.startswith("_")}
        evidence_paths: list[str] = []
        for source in row["_evidence_sources"]:
            destination = Path("proofs") / f"row-{number}" / source["name"]
            evidence_paths.append(store(destination, source["bytes"]))
        output["evidence_paths"] = evidence_paths

        ui_sources = row["_ui_sources"]
        if ui_sources is None:
            output["ui_proof"] = None
        else:
            trace_destination = Path("proofs") / f"row-{number}" / ui_sources["trace"]["name"]
            trace_path = store(trace_destination, ui_sources["trace"]["bytes"])
            screenshots: list[str] = []
            for source in ui_sources["screenshots"]:
                destination = Path("proofs") / f"row-{number}" / source["name"]
                screenshots.append(store(destination, source["bytes"]))
            output["ui_proof"] = {
                "screenshot_paths": screenshots,
                "trace_path": trace_path,
            }
        output_rows.append(output)
    return output_rows, inventory


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


def _safe_inventory(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = (
        "artifact_path",
        "bundle_manifest_path",
        "bundle_sidecar_path",
        "kind",
        "manifest_path",
        "sha256",
        "sidecar_path",
        "size_bytes",
    )
    return [{key: item[key] for key in keys} for item in artifacts]


def _revalidate_artifact_snapshot(storage_root: Path, artifacts: list[dict[str, Any]]) -> None:
    discovered = {path.resolve(strict=True) for path in _discover_manifest_paths(storage_root)}
    expected = {item["_manifest_resolved"] for item in artifacts}
    if discovered != expected:
        _error("artifact source inventory changed during evidence collection")
    for item in artifacts:
        digest, size_bytes, artifact_path = _hash_regular_file(
            item["_artifact_resolved"],
            "artifact source recheck",
            root=storage_root,
        )
        if artifact_path != item["_artifact_resolved"] or digest != item["sha256"] or size_bytes != item["size_bytes"]:
            _error("artifact source changed during evidence collection")
        manifest_bytes, manifest_path = _read_regular_bytes(
            item["_manifest_resolved"],
            "artifact manifest source recheck",
            root=storage_root,
        )
        sidecar_bytes, sidecar_path = _read_regular_bytes(
            item["_sidecar_resolved"],
            "artifact sidecar source recheck",
            root=storage_root,
        )
        if manifest_path != item["_manifest_resolved"] or manifest_bytes != item["_manifest_bytes"]:
            _error("artifact manifest source changed during evidence collection")
        if sidecar_path != item["_sidecar_resolved"] or sidecar_bytes != item["_sidecar_bytes"]:
            _error("artifact sidecar source changed during evidence collection")


def _bundle_timestamp(now: datetime | None) -> str:
    current = datetime.now(UTC) if now is None else now
    if current.tzinfo is None:
        _error("bundle timestamp must be timezone-aware")
    return current.astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ")


def _rename_no_replace(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(errno.EEXIST, "destination exists")
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError as exc:
        raise CollectionError("atomic no-replace publication is unavailable") from exc
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is not None:
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            _AT_FDCWD,
            os.fsencode(source),
            _AT_FDCWD,
            os.fsencode(destination),
            _RENAME_NOREPLACE,
        )
        if result == 0:
            return
        error_number = ctypes.get_errno()
        if error_number not in {errno.ENOSYS, errno.EINVAL}:
            raise OSError(error_number, os.strerror(error_number), destination)
    _error("atomic no-replace publication is unavailable")


def _remove_owned_tree(path: Path, identity: tuple[int, int]) -> None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if (
        not stat.S_ISDIR(current.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or (current.st_dev, current.st_ino) != identity
    ):
        _error("collector-owned output changed identity; refusing unsafe cleanup")
    shutil.rmtree(path)


def collect_bundle(
    repo_root: Path,
    storage_root: Path,
    evidence_root: Path,
    rows_file: Path,
    *,
    command_log: Path | None = None,
    now: datetime | None = None,
) -> Path:
    """Validate raw release evidence and atomically publish private/public projections."""

    repo_root = _existing_directory(Path(repo_root), "repository root")
    source = _git_source(repo_root)
    storage_root = _existing_directory(Path(storage_root), "storage root")
    evidence_root = _validate_evidence_root(repo_root, storage_root, Path(evidence_root))
    artifacts, by_artifact = _validate_artifacts(storage_root)
    rows, command_source = _validate_rows(
        Path(rows_file),
        command_log=command_log,
        forbidden_artifacts={item["_artifact_resolved"] for item in artifacts},
    )
    _reject_raw_artifact_sources(rows, command_source, artifacts)
    rows = _normalized_rows(rows, storage_root, by_artifact)

    bundle_name = f"{_bundle_timestamp(now)}-{source['commit']}"
    final_bundle = evidence_root / bundle_name
    _secure_create_directory_tree(evidence_root)
    if final_bundle.exists() or final_bundle.is_symlink():
        _error(f"evidence bundle collision: {bundle_name} already exists")
    temp_bundle = evidence_root / f".tmp-{uuid4().hex}"
    temp_identity: tuple[int, int] | None = None
    try:
        try:
            temp_bundle.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise CollectionError("temporary evidence directory collision") from exc
        created_stat = temp_bundle.lstat()
        temp_identity = (created_stat.st_dev, created_stat.st_ino)
        temp_bundle.chmod(0o700)
        output_rows, proof_inventory = _copy_proofs(temp_bundle, rows)

        for artifact in artifacts:
            _write_new(temp_bundle / artifact["bundle_manifest_path"], artifact["_manifest_bytes"])
            _write_new(temp_bundle / artifact["bundle_sidecar_path"], artifact["_sidecar_bytes"])

        if command_source is not None:
            _write_new(temp_bundle / "operator-command.log", command_source["bytes"])
            proof_inventory.append(_proof_digest("operator-command.log", command_source["bytes"]))
        proof_inventory.sort(key=lambda item: item["path"])

        private_inventory = _safe_inventory(artifacts)
        private_evidence = {
            "artifacts": private_inventory,
            "proofs": proof_inventory,
            "rows": output_rows,
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "source": source,
            "storage_root": str(storage_root),
        }
        public_rows = _public_rows(output_rows)
        public_evidence = {
            "rows": public_rows,
            "schema_version": PUBLIC_SCHEMA_VERSION,
            "source": {"commit": source["commit"]},
        }
        source_text = (
            f"commit={source['commit']}\n"
            f"branch={source['branch']}\n"
            f"detached={str(source['detached']).lower()}\n"
            "tracked_clean=true\n"
        )
        private_sums = _sha256_sums(private_inventory)
        public_sums = _public_sha256_sums(public_rows)
        _write_text(temp_bundle / "SOURCE_COMMIT.txt", source_text)
        _write_json(temp_bundle / "evidence.json", private_evidence)
        _write_text(temp_bundle / "SHA256SUMS.txt", private_sums)
        _write_text(temp_bundle / "HOST_WORKFLOW_CHECKLIST.md", _private_checklist(output_rows))
        _write_json(temp_bundle / "public/evidence-summary.json", public_evidence)
        _write_text(temp_bundle / "public/SHA256SUMS.txt", public_sums)
        _write_text(temp_bundle / "public/HOST_WORKFLOW_CHECKLIST.md", _public_checklist(public_rows))
        _normalize_bundle_modes(temp_bundle)

        _revalidate_artifact_snapshot(storage_root, artifacts)

        final_source = _git_source(repo_root)
        if final_source != source:
            _error("repository source changed during evidence collection")

        try:
            _rename_no_replace(temp_bundle, final_bundle)
        except FileExistsError as exc:
            raise CollectionError(f"evidence bundle collision: {bundle_name} already exists") from exc
        try:
            if _git_source(repo_root) != source:
                _error("repository source changed during evidence publication")
        except BaseException:
            if temp_identity is not None:
                _remove_owned_tree(final_bundle, temp_identity)
            raise
        return final_bundle
    except BaseException:
        if temp_identity is not None:
            _remove_owned_tree(temp_bundle, temp_identity)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Collect validated AstinusLab release evidence. ASTINUSLAB_STORAGE_DIR is required; "
            "ASTINUSLAB_EVIDENCE_DIR overrides the private evidence root."
        )
    )
    parser.add_argument("--rows-file", required=True, type=Path, help="authoritative seven-row JSON input")
    parser.add_argument("--command-log", type=Path, help="optional operator command log to copy")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parents[2]
    storage_value = os.environ.get("ASTINUSLAB_STORAGE_DIR")
    if not storage_value:
        print("error: ASTINUSLAB_STORAGE_DIR is required", file=sys.stderr)
        return 2
    evidence_value = os.environ.get("ASTINUSLAB_EVIDENCE_DIR")
    try:
        evidence_root = Path(evidence_value).expanduser() if evidence_value else default_evidence_root(repo_root)
        bundle = collect_bundle(
            repo_root,
            Path(storage_value).expanduser(),
            evidence_root,
            args.rows_file,
            command_log=args.command_log,
        )
    except CollectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(bundle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
