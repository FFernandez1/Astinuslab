"""Passive, bounded inspection and deterministic writing of ``.astinuslab`` archives.

The archive reader deliberately never extracts members.  It validates the ZIP
directory first, then reads each declared member through a bounded stream while
checking its digest and size against ``manifest.json``.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tempfile
import unicodedata
import zipfile
import zlib
from collections.abc import Buffer, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, BinaryIO, cast

from pydantic import ValidationError

from .canonical import canonical_bytes, canonical_sha256
from .models import AssetReference, LabDefinition, LabManifest, ManifestPayload, normalize_legacy_definition_payload

PACKAGE_ARCHIVE_MEDIA_TYPE = "application/vnd.astinuslab.lab+zip"
PACKAGE_MAX_ARCHIVE_BYTES = 40 * 1024**3
PACKAGE_MAX_MEMBER_BYTES = 16 * 1024**3
PACKAGE_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
PACKAGE_MAX_DEFINITION_BYTES = 2 * 1024 * 1024
PACKAGE_MAX_RUNBOOK_BYTES = 2 * 1024 * 1024
PACKAGE_MAX_TOTAL_UNCOMPRESSED_BYTES = 40 * 1024**3
PACKAGE_MAX_MEMBERS = 4096
PACKAGE_MAX_COMPRESSION_RATIO = 1000.0
PACKAGE_MAX_SIGNATURE_BYTES = 1 * 1024 * 1024
PACKAGE_MAX_INSPECTED_PAYLOAD_BYTES = 512 * 1024**2
PACKAGE_INLINE_PAYLOAD_BYTES = 8 * 1024**2
PACKAGE_MAX_PATH_LENGTH = 1024
PACKAGE_MAX_PATH_DEPTH = 16
PACKAGE_READ_CHUNK_BYTES = 1024 * 1024
PACKAGE_REQUIRED_MEMBERS = frozenset({"manifest.json", "lab.json", "experience/runbook.md"})
PACKAGE_SIGNATURE_MEMBERS = frozenset({"signatures/manifest.ed25519", "signatures/creator.pub"})

# Short aliases make the limits easy for an application embedding this module
# to override in tests or in a local policy layer.
MAX_ARCHIVE_BYTES = PACKAGE_MAX_ARCHIVE_BYTES
MAX_MEMBER_BYTES = PACKAGE_MAX_MEMBER_BYTES
MAX_TOTAL_UNCOMPRESSED_BYTES = PACKAGE_MAX_TOTAL_UNCOMPRESSED_BYTES
MAX_COMPRESSION_RATIO = PACKAGE_MAX_COMPRESSION_RATIO
MAX_INSPECTED_PAYLOAD_BYTES = PACKAGE_MAX_INSPECTED_PAYLOAD_BYTES


class PackageArchiveError(ValueError):
    """Raised when an archive violates the passive package contract."""


class _DuplicateJSONKey(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _safe_member_name(name: str) -> str:
    """Validate a ZIP member name without normalising attacker input."""
    if (
        not name
        or len(name) > PACKAGE_MAX_PATH_LENGTH
        or "\\" in name
        or name.startswith("/")
        or name.startswith("//")
        or any(
            ord(char) < 0x20
            or ord(char) == 0x7F
            or 0xD800 <= ord(char) <= 0xDFFF
            or unicodedata.category(char) in {"Cc", "Cf"}
            for char in name
        )
    ):
        raise PackageArchiveError(f"unsafe package path: {name}")
    parts = name.split("/")
    if len(parts) > PACKAGE_MAX_PATH_DEPTH or any(not part or part in {".", ".."} or ":" in part for part in parts):
        raise PackageArchiveError(f"unsafe package path: {name}")
    if Path(name).is_absolute():
        raise PackageArchiveError(f"unsafe package path: {name}")
    # A normalized spelling and case-folded spelling identify the same logical
    # package path for portable filesystems.  The caller compares these keys.
    return name


def _path_collision_key(name: str) -> tuple[str, str]:
    return unicodedata.normalize("NFC", name), unicodedata.normalize("NFC", name).casefold()


def _member_mode(info: zipfile.ZipInfo) -> int:
    mode = (info.external_attr >> 16) & 0o170000
    if mode in {stat.S_IFLNK, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, stat.S_IFSOCK}:
        raise PackageArchiveError(f"unsupported package member type: {info.filename}")
    return mode


def _check_member_header(info: zipfile.ZipInfo, *, max_member_bytes: int = PACKAGE_MAX_MEMBER_BYTES) -> None:
    _safe_member_name(info.filename)
    _member_mode(info)
    if info.is_dir() or info.filename.endswith("/"):
        raise PackageArchiveError(f"unsafe package path: {info.filename}")
    if info.flag_bits & 0x1:
        raise PackageArchiveError(f"encrypted package member is not supported: {info.filename}")
    if info.file_size < 0 or info.compress_size < 0:
        raise PackageArchiveError(f"invalid package member size: {info.filename}")
    if info.file_size > max_member_bytes:
        raise PackageArchiveError(f"package member too large: {info.filename}")
    if info.file_size and (
        info.compress_size == 0 or info.file_size / info.compress_size > PACKAGE_MAX_COMPRESSION_RATIO
    ):
        raise PackageArchiveError(f"package member compression ratio too high: {info.filename}")


@dataclass
class _ReadBudget:
    maximum: int
    retained: int = 0

    def reserve(self, size: int, filename: str) -> None:
        if self.retained + size > self.maximum:
            raise PackageArchiveError(f"inspected package payload is too large: {filename}")
        self.retained += size


@dataclass(frozen=True, slots=True)
class AssetDescriptor:
    """A verified embedded asset that can be reopened from an immutable archive.

    The archive path is intentionally supplied by the caller that owns its
    staging lifetime.  Inspection never copies, publishes, or retains large
    payload bytes.  Calling :meth:`open` opens a fresh bounded ZIP member, so
    descriptors remain usable after the inspection ``ZipFile`` has closed.
    """

    archive_path: Path
    path: str
    sha256: str
    size_bytes: int
    media_type: str

    def open(self) -> BinaryIO:
        return open_asset(self.archive_path, self)


class _VerifiedZipMember(io.RawIOBase):
    """A ZIP member stream which owns the archive handle and bounds reads."""

    def __init__(self, archive: zipfile.ZipFile, member: BinaryIO, descriptor: AssetDescriptor) -> None:
        self._archive = archive
        self._member = member
        self._descriptor = descriptor
        self._digest = hashlib.sha256()
        self._total = 0
        self._finished = False

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def writable(self) -> bool:
        return False

    def readinto(self, buffer: Buffer) -> int:
        view = memoryview(buffer)
        data = self.read(len(view))
        view[: len(data)] = data
        return len(data)

    def read(self, size: int = -1) -> bytes:
        if self._finished:
            return b""
        if size == 0:
            return b""
        remaining = self._descriptor.size_bytes - self._total
        if remaining < 0:
            raise PackageArchiveError(f"package member size mismatch: {self._descriptor.path}")
        requested = remaining if size is None or size < 0 else min(size, remaining)
        try:
            chunk = self._member.read(requested)
        except (OSError, RuntimeError, ValueError, zipfile.BadZipFile, EOFError, zlib.error) as exc:
            self.close()
            raise PackageArchiveError(f"invalid package member: {self._descriptor.path}") from exc
        if not chunk:
            self._finish()
            return b""
        self._total += len(chunk)
        self._digest.update(chunk)
        if self._total == self._descriptor.size_bytes:
            self._finish()
        return chunk

    def _finish(self) -> None:
        if self._finished:
            return
        try:
            # Ask ZIP for one more byte: a malicious header must not let a
            # member carry bytes beyond its declared size.
            extra = self._member.read(1)
        except (OSError, RuntimeError, ValueError, zipfile.BadZipFile, EOFError, zlib.error) as exc:
            self.close()
            raise PackageArchiveError(f"invalid package member: {self._descriptor.path}") from exc
        if extra or self._total != self._descriptor.size_bytes or self._digest.hexdigest() != self._descriptor.sha256:
            self.close()
            raise PackageArchiveError(f"manifest digest or size mismatch: {self._descriptor.path}")
        self._finished = True

    def close(self) -> None:
        if self.closed:
            return
        try:
            self._member.close()
        finally:
            self._archive.close()
            self._finished = True
            super().close()


class _BoundedBytesIO(io.BytesIO):
    """Seekable output buffer that refuses to exceed the archive byte limit."""

    def __init__(self, maximum: int) -> None:
        super().__init__()
        self._maximum = maximum
        self._high_water = 0

    def write(self, data: Any) -> int:
        end = self.tell() + len(data)
        if max(self._high_water, end) > self._maximum:
            raise PackageArchiveError("package archive is too large")
        written = super().write(data)
        self._high_water = max(self._high_water, end)
        return written


def _bounded_read(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    maximum: int,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
    budget: _ReadBudget | None = None,
    retain: bool = True,
) -> bytes:
    if info.file_size > maximum:
        raise PackageArchiveError(f"package member too large: {info.filename}")
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    try:
        with archive.open(info, "r") as stream:
            while True:
                chunk = stream.read(min(PACKAGE_READ_CHUNK_BYTES, maximum - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    raise PackageArchiveError(f"package member too large: {info.filename}")
                if retain and budget is not None:
                    budget.reserve(len(chunk), info.filename)
                digest.update(chunk)
                if retain:
                    chunks.append(chunk)
    except PackageArchiveError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile, EOFError, ValueError, zlib.error) as exc:
        raise PackageArchiveError(f"invalid package member: {info.filename}") from exc
    if total != info.file_size or (expected_size is not None and total != expected_size):
        raise PackageArchiveError(f"package member size mismatch: {info.filename}")
    if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
        raise PackageArchiveError(f"manifest digest mismatch: {info.filename}")
    return b"".join(chunks) if retain else b""


def _strict_json(payload: bytes, member_name: str) -> dict[str, Any]:
    try:
        text = payload.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError) as exc:
        raise PackageArchiveError(f"invalid JSON in package member: {member_name}") from exc
    if not isinstance(value, dict):
        raise PackageArchiveError(f"package member must contain a JSON object: {member_name}")
    return value


def _validate_manifest_payloads(
    manifest: LabManifest,
    infos_by_name: Mapping[str, zipfile.ZipInfo],
) -> dict[str, ManifestPayload]:
    listed: dict[str, ManifestPayload] = {}
    for payload in manifest.payloads:
        if payload.path in listed:
            raise PackageArchiveError(f"duplicate manifest payload path: {payload.path}")
        if payload.path in PACKAGE_SIGNATURE_MEMBERS or payload.path == "manifest.json":
            raise PackageArchiveError(f"manifest cannot inventory signature or manifest: {payload.path}")
        if payload.path not in infos_by_name:
            raise PackageArchiveError(f"manifest declares missing payload: {payload.path}")
        listed[payload.path] = payload

    actual = {name for name in infos_by_name if name != "manifest.json" and name not in PACKAGE_SIGNATURE_MEMBERS}
    if actual != set(listed):
        undeclared = sorted(actual - set(listed))
        missing = sorted(set(listed) - actual)
        if undeclared:
            raise PackageArchiveError(f"undeclared package payload: {undeclared[0]}")
        raise PackageArchiveError(f"manifest declares missing payload: {missing[0]}")
    if not PACKAGE_REQUIRED_MEMBERS.issubset(infos_by_name):
        missing_member = sorted(PACKAGE_REQUIRED_MEMBERS - set(infos_by_name))[0]
        raise PackageArchiveError(f"package missing required member: {missing_member}")
    return listed


def _validate_embedded_asset_path(name: str) -> str:
    prefix = "assets/sha256/"
    digest = name.removeprefix(prefix)
    if not name.startswith(prefix) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise PackageArchiveError(f"invalid embedded asset path: {name}")
    return digest


def _validate_payload_kind(name: str) -> None:
    if name in {"lab.json", "experience/runbook.md"}:
        return
    if name.startswith("assets/"):
        _validate_embedded_asset_path(name)
    elif name.startswith("experience/"):
        if name == "experience/runbook.md":
            return
        if len(name.split("/")) < 2:
            raise PackageArchiveError(f"invalid supporting file path: {name}")
    else:
        raise PackageArchiveError(f"unsupported package payload path: {name}")


def _validate_embedded_asset_references(definition: LabDefinition, listed: Mapping[str, ManifestPayload]) -> None:
    """Bind embedded boot layers to their declared immutable asset contract.

    Older unlocked packages had no asset-reference surface, so they remain
    readable.  Once a definition claims an embedded boot layer, every embedded
    member must be declared by that layer and carry the exact digest, size,
    media type, license, and redistribution confirmation needed for sharing.
    """

    references: dict[str, AssetReference] = {}
    for vm in definition.vms:
        reference = vm.effective_boot_layer
        if reference is not None and reference.embedded:
            if not reference.redistribution_confirmed:
                raise PackageArchiveError(f"embedded asset redistribution is not confirmed: {vm.id}")
            path = f"assets/sha256/{reference.sha256}"
            previous = references.get(path)
            if previous is not None and previous != reference:
                raise PackageArchiveError(f"embedded asset references disagree: {path}")
            references[path] = reference
    if not references:
        if legacy_embedded := [path for path in listed if path.startswith("assets/sha256/")]:
            if not _is_explicit_legacy_asset_definition(definition):
                raise PackageArchiveError(
                    f"embedded asset has no declared asset reference: {sorted(legacy_embedded)[0]}"
                )
        return
    embedded = {path: payload for path, payload in listed.items() if path.startswith("assets/sha256/")}
    if set(embedded) != set(references):
        missing = sorted(set(references) - set(embedded))
        orphan = sorted(set(embedded) - set(references))
        name = missing[0] if missing else orphan[0]
        raise PackageArchiveError(f"embedded asset has no declared asset reference: {name}")
    for path, reference in references.items():
        payload = embedded[path]
        if (
            payload.sha256 != reference.sha256
            or payload.size_bytes != reference.size_bytes
            or payload.media_type != reference.media_type
        ):
            raise PackageArchiveError(f"embedded asset metadata mismatch: {path}")


def _is_explicit_legacy_asset_definition(definition: LabDefinition) -> bool:
    """Limit unreferenced assets to the old unlocked v1 representation."""

    return definition.reproducibility == "unlocked" and all(
        vm.base_image is None and vm.effective_boot_layer is None and vm.workload is None for vm in definition.vms
    )


def _validate_embedded_asset_paths_for_write(
    definition: LabDefinition,
    asset_metadata: Mapping[str, tuple[int, str]],
    *,
    allow_legacy_unreferenced_assets: bool,
) -> None:
    """Apply the same asset-reference policy before an archive is emitted."""

    references: dict[str, AssetReference] = {}
    for vm in definition.vms:
        reference = vm.effective_boot_layer
        if reference is None or not reference.embedded:
            continue
        path = f"assets/sha256/{reference.sha256}"
        previous = references.get(path)
        if previous is not None and previous != reference:
            raise PackageArchiveError(f"embedded asset references disagree: {path}")
        references[path] = reference
    if not references:
        if not asset_metadata:
            return
        if allow_legacy_unreferenced_assets and _is_explicit_legacy_asset_definition(definition):
            return
        raise PackageArchiveError(f"embedded asset has no declared asset reference: {sorted(asset_metadata)[0]}")
    if set(references) != set(asset_metadata):
        missing = sorted(set(references) - set(asset_metadata))
        orphan = sorted(set(asset_metadata) - set(references))
        raise PackageArchiveError(f"embedded asset has no declared asset reference: {(missing or orphan)[0]}")
    for path, reference in references.items():
        if not reference.redistribution_confirmed:
            raise PackageArchiveError(f"embedded asset redistribution is not confirmed: {path}")
        size_bytes, media_type = asset_metadata[path]
        if size_bytes != reference.size_bytes or media_type != reference.media_type:
            raise PackageArchiveError(f"embedded asset metadata mismatch: {path}")


def _embedded_media_types(definition: LabDefinition) -> dict[str, str]:
    return {
        f"assets/sha256/{reference.sha256}": reference.media_type
        for vm in definition.vms
        if (reference := vm.effective_boot_layer) is not None and reference.embedded
    }


def _reject_path_collisions(names: Mapping[str, bytes]) -> None:
    collision_keys: dict[str, str] = {}
    for name in names:
        normalized, folded = _path_collision_key(name)
        if normalized in collision_keys or folded in collision_keys:
            previous = collision_keys.get(normalized) or collision_keys.get(folded, "")
            raise PackageArchiveError(f"package member path collision: {previous} / {name}")
        collision_keys[normalized] = name
        collision_keys[folded] = name


@dataclass(frozen=True)
class ArchiveInspection:
    """Validated package documents and in-memory payloads.

    Payload bytes are retained only in the caller's process; inspection never
    writes them to a filesystem.  ``assets`` is keyed by its archive path.
    """

    manifest: LabManifest
    definition: LabDefinition
    runbook: str
    assets: Mapping[str, bytes]
    asset_descriptors: Mapping[str, AssetDescriptor]
    signatures: Mapping[str, bytes]
    supporting_files: Mapping[str, bytes]
    _payloads: Mapping[str, bytes]

    @property
    def manifest_digest(self) -> str:
        return canonical_sha256(self.manifest)

    @property
    def payloads(self) -> Mapping[str, bytes]:
        return self._payloads

    def open_asset(self, path: str) -> BinaryIO:
        """Open a fresh, verified stream for one embedded asset."""

        descriptor = self.asset_descriptors.get(path)
        if descriptor is None:
            raise KeyError(path)
        return descriptor.open()


def _open_source(source: bytes | bytearray | memoryview | BinaryIO | Path) -> tuple[BinaryIO, bool]:
    if isinstance(source, (bytes, bytearray, memoryview)):
        return io.BytesIO(bytes(source)), True
    if isinstance(source, Path):
        try:
            handle = source.open("rb")
        except OSError as exc:
            raise PackageArchiveError("cannot open package archive") from exc
        return handle, True
    if hasattr(source, "read"):
        return source, False
    raise TypeError("package archive must be bytes, a path, or a binary stream")


def _immutable_archive_path(source: bytes | bytearray | memoryview | BinaryIO | Path) -> Path | None:
    """Return a caller-owned immutable path suitable for descriptor reopening.

    This deliberately does not copy uploads.  The import/staging protocol owns
    that boundary; callers passing a path here assert its immutable lifetime.
    """

    if not isinstance(source, Path):
        return None
    try:
        inspected = source.lstat()
    except OSError as exc:
        raise PackageArchiveError("cannot open package archive") from exc
    if stat.S_ISLNK(inspected.st_mode) or not stat.S_ISREG(inspected.st_mode):
        raise PackageArchiveError("package archive must be a regular non-symlink file")
    return source.absolute()


def open_asset(archive_path: Path, descriptor: AssetDescriptor) -> BinaryIO:
    """Reopen one verified asset member from a caller-owned immutable archive."""

    path = Path(archive_path)
    if path != descriptor.archive_path:
        raise PackageArchiveError("asset descriptor archive path mismatch")
    archive: zipfile.ZipFile | None = None
    try:
        inspected = path.lstat()
        if stat.S_ISLNK(inspected.st_mode) or not stat.S_ISREG(inspected.st_mode):
            raise PackageArchiveError("package archive must be a regular non-symlink file")
        archive = zipfile.ZipFile(path, "r", allowZip64=True)
        info = archive.getinfo(descriptor.path)
        _check_member_header(info)
        if info.file_size != descriptor.size_bytes:
            raise PackageArchiveError(f"package member size mismatch: {descriptor.path}")
        member = archive.open(info, "r")
    except PackageArchiveError:
        if archive is not None:
            archive.close()
        raise
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile, KeyError) as exc:
        if archive is not None:
            archive.close()
        raise PackageArchiveError(f"cannot open package member: {descriptor.path}") from exc
    if archive is None:
        raise PackageArchiveError(f"cannot open package member: {descriptor.path}")
    return cast(BinaryIO, _VerifiedZipMember(archive, cast(BinaryIO, member), descriptor))


def inspect_archive(
    source: bytes | bytearray | memoryview | BinaryIO | Path,
    *,
    temp_root: Path | None = None,
    max_archive_bytes: int | None = None,
    max_asset_bytes: int | None = None,
    inline_payload_bytes: int | None = None,
    retain_assets: bool = True,
) -> ArchiveInspection:
    """Validate without extraction; read-only metadata callers may discard asset bytes."""
    del temp_root  # Kept as an explicit API affordance: inspection is filesystem-free.
    archive_limit = PACKAGE_MAX_ARCHIVE_BYTES if max_archive_bytes is None else max_archive_bytes
    inline_limit = PACKAGE_INLINE_PAYLOAD_BYTES if inline_payload_bytes is None else inline_payload_bytes
    asset_limit = PACKAGE_MAX_MEMBER_BYTES if max_asset_bytes is None else max_asset_bytes
    if isinstance(archive_limit, bool) or not isinstance(archive_limit, int) or archive_limit < 1:
        raise ValueError("max_archive_bytes must be a positive integer")
    if isinstance(inline_limit, bool) or not isinstance(inline_limit, int) or inline_limit < 0:
        raise ValueError("inline_payload_bytes must be a non-negative integer")
    if isinstance(asset_limit, bool) or not isinstance(asset_limit, int) or asset_limit < 1:
        raise ValueError("max_asset_bytes must be a positive integer")
    immutable_path = _immutable_archive_path(source)
    handle, should_close = _open_source(source)
    try:
        try:
            current_position = handle.tell()
            handle.seek(0, 2)
            archive_size = handle.tell()
            handle.seek(current_position)
        except (AttributeError, OSError):
            archive_size = 0
        if archive_size > archive_limit:
            raise PackageArchiveError("package archive is too large")
        try:
            archive = zipfile.ZipFile(handle, "r", allowZip64=True)
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            raise PackageArchiveError("invalid package archive") from exc
        with archive:
            infos = archive.infolist()
            if len(infos) > PACKAGE_MAX_MEMBERS:
                raise PackageArchiveError("package archive contains too many members")
            infos_by_name: dict[str, zipfile.ZipInfo] = {}
            collision_keys: dict[str, str] = {}
            total_uncompressed = 0
            budget = _ReadBudget(PACKAGE_MAX_INSPECTED_PAYLOAD_BYTES)
            for info in infos:
                _check_member_header(
                    info,
                    max_member_bytes=asset_limit if info.filename.startswith("assets/") else PACKAGE_MAX_MEMBER_BYTES,
                )
                collision_key = _path_collision_key(info.filename)
                if info.filename in infos_by_name:
                    raise PackageArchiveError(f"duplicate package member: {info.filename}")
                if collision_key[0] in collision_keys or collision_key[1] in collision_keys:
                    previous = collision_keys.get(collision_key[0]) or collision_keys.get(collision_key[1], "")
                    raise PackageArchiveError(f"package member path collision: {previous} / {info.filename}")
                collision_keys[collision_key[0]] = info.filename
                collision_keys[collision_key[1]] = info.filename
                if info.filename.startswith("signatures/") and info.filename not in PACKAGE_SIGNATURE_MEMBERS:
                    raise PackageArchiveError(f"unexpected package signature entry: {info.filename}")
                total_uncompressed += info.file_size
                if total_uncompressed > PACKAGE_MAX_TOTAL_UNCOMPRESSED_BYTES:
                    raise PackageArchiveError("package uncompressed payload is too large")
                infos_by_name[info.filename] = info

            missing = sorted(PACKAGE_REQUIRED_MEMBERS - set(infos_by_name))
            if missing:
                raise PackageArchiveError(f"package missing required member: {missing[0]}")
            manifest_info = infos_by_name["manifest.json"]
            manifest_payload = _bounded_read(
                archive,
                manifest_info,
                PACKAGE_MAX_MANIFEST_BYTES,
                budget=budget,
            )
            manifest_data = _strict_json(manifest_payload, "manifest.json")
            try:
                manifest = LabManifest.model_validate(manifest_data)
            except ValidationError as exc:
                raise PackageArchiveError("invalid lab manifest") from exc
            if manifest_payload != canonical_bytes(manifest):
                raise PackageArchiveError("manifest.json is not canonical JSON")
            listed = _validate_manifest_payloads(manifest, infos_by_name)

            definition_info = infos_by_name["lab.json"]
            definition_payload = _bounded_read(
                archive,
                definition_info,
                PACKAGE_MAX_DEFINITION_BYTES,
                budget=budget,
            )
            raw_definition = _strict_json(definition_payload, "lab.json")
            normalized_definition = normalize_legacy_definition_payload(raw_definition)
            if normalized_definition != raw_definition:
                # Compatibility normalization may only change the unprovable
                # locked claim. Validate the original document as an unlocked
                # model first so Pydantic coercions (for example, "2" -> 2)
                # cannot hide behind the compatibility path.
                legacy_payload = {**raw_definition, "reproducibility": "unlocked"}
                try:
                    legacy_definition = LabDefinition.model_validate(legacy_payload)
                except ValidationError as exc:
                    raise PackageArchiveError("invalid lab definition") from exc
                legacy_canonical = json.dumps(
                    legacy_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
                ).encode("utf-8")
                if legacy_canonical != canonical_bytes(legacy_definition):
                    raise PackageArchiveError("lab.json is not canonical JSON")
            try:
                definition = LabDefinition.model_validate(normalized_definition)
            except ValidationError as exc:
                raise PackageArchiveError("invalid lab definition") from exc
            # A pre-remediation locked v1 document is canonical as written but
            # is returned as unlocked because it cannot prove stable bytes.
            raw_canonical = json.dumps(
                raw_definition, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            ).encode("utf-8")
            if definition_payload != canonical_bytes(definition) and (
                normalized_definition == raw_definition or definition_payload != raw_canonical
            ):
                raise PackageArchiveError("lab.json is not canonical JSON")
            if definition.package_id != manifest.package_id or definition.version != manifest.version:
                raise PackageArchiveError("manifest and lab definition identity mismatch")

            for name in listed:
                _validate_payload_kind(name)
            _validate_embedded_asset_references(definition, listed)
            lab_entry = listed["lab.json"]
            if (
                len(definition_payload) != lab_entry.size_bytes
                or hashlib.sha256(definition_payload).hexdigest() != lab_entry.sha256
            ):
                raise PackageArchiveError("manifest digest or size mismatch: lab.json")
            verified: dict[str, bytes] = {"lab.json": definition_payload}
            descriptors: dict[str, AssetDescriptor] = {}
            for name, payload in listed.items():
                if name == "lab.json":
                    continue
                is_asset = name.startswith("assets/sha256/")
                maximum = (
                    PACKAGE_MAX_RUNBOOK_BYTES
                    if name == "experience/runbook.md"
                    else asset_limit
                    if is_asset
                    else PACKAGE_MAX_MEMBER_BYTES
                )
                retain = not is_asset or (
                    retain_assets and (payload.size_bytes <= inline_limit or immutable_path is None)
                )
                content = _bounded_read(
                    archive,
                    infos_by_name[name],
                    maximum,
                    expected_size=payload.size_bytes,
                    expected_sha256=payload.sha256,
                    budget=budget if retain else None,
                    retain=retain,
                )
                if is_asset:
                    expected = _validate_embedded_asset_path(name)
                    if payload.sha256 != expected:
                        raise PackageArchiveError(f"embedded asset digest mismatch: {name}")
                    if immutable_path is not None and not retain:
                        descriptors[name] = AssetDescriptor(
                            archive_path=immutable_path,
                            path=name,
                            sha256=payload.sha256,
                            size_bytes=payload.size_bytes,
                            media_type=payload.media_type,
                        )
                if retain:
                    verified[name] = content

            try:
                runbook = verified["experience/runbook.md"].decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise PackageArchiveError("runbook is not valid UTF-8") from exc
            assets = {name: content for name, content in verified.items() if name.startswith("assets/sha256/")}
            supporting_files = {
                name: content
                for name, content in verified.items()
                if name.startswith("experience/") and name != "experience/runbook.md"
            }
            signatures = {
                name: _bounded_read(archive, info, PACKAGE_MAX_SIGNATURE_BYTES, budget=budget)
                for name, info in infos_by_name.items()
                if name in PACKAGE_SIGNATURE_MEMBERS
            }
            all_payloads = {"manifest.json": manifest_payload, **verified}
            return ArchiveInspection(
                manifest=manifest,
                definition=definition,
                runbook=runbook,
                assets=MappingProxyType(assets),
                asset_descriptors=MappingProxyType(descriptors),
                signatures=MappingProxyType(signatures),
                supporting_files=MappingProxyType(supporting_files),
                _payloads=MappingProxyType(all_payloads),
            )
    finally:
        if should_close:
            handle.close()


def _asset_path(key: str, content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()
    if key.startswith("assets/sha256/"):
        path = key
    elif len(key) == 64 and all(char in "0123456789abcdef" for char in key):
        path = f"assets/sha256/{key}"
    else:
        path = f"assets/sha256/{digest}"
    if path != f"assets/sha256/{digest}":
        raise PackageArchiveError(f"embedded asset digest mismatch: {path}")
    return path


def _deflated_size(content: bytes) -> int:
    compressor = zlib.compressobj(zlib.Z_DEFAULT_COMPRESSION, zlib.DEFLATED, -15)
    return len(compressor.compress(content) + compressor.flush())


def _zip_info(name: str, content: bytes, *, force_stored: bool = False) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    deflated_size = _deflated_size(content)
    if force_stored or (
        content and (deflated_size == 0 or len(content) / deflated_size > PACKAGE_MAX_COMPRESSION_RATIO)
    ):
        info.compress_type = zipfile.ZIP_STORED
    else:
        info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (0o100640 & 0xFFFF) << 16
    info.create_system = 3
    info.extra = b""
    info.comment = b""
    return info


def write_archive(
    definition: LabDefinition,
    runbook: str | bytes,
    assets: Mapping[str, bytes] | None = None,
    *,
    signatures: Mapping[str, bytes] | None = None,
    supporting_files: Mapping[str, bytes] | None = None,
    required_capabilities: tuple[str, ...] = (),
    manifest_override: LabManifest | None = None,
    allow_legacy_unreferenced_assets: bool = False,
    max_archive_bytes: int | None = None,
    max_asset_bytes: int | None = None,
) -> bytes:
    """Write an unsigned (or explicitly signed) deterministic ZIP64 package."""
    if not isinstance(definition, LabDefinition):
        raise TypeError("definition must be a LabDefinition")
    if isinstance(runbook, str):
        try:
            runbook_bytes = runbook.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise PackageArchiveError("runbook is not valid UTF-8") from exc
    elif isinstance(runbook, bytes):
        try:
            runbook.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise PackageArchiveError("runbook is not valid UTF-8") from exc
        runbook_bytes = runbook
    else:
        raise TypeError("runbook must be text or bytes")
    if not runbook_bytes:
        raise PackageArchiveError("runbook must not be empty")
    if len(runbook_bytes) > PACKAGE_MAX_RUNBOOK_BYTES:
        raise PackageArchiveError("runbook is too large")
    embedded_media_types = _embedded_media_types(definition)
    asset_limit = PACKAGE_MAX_MEMBER_BYTES if max_asset_bytes is None else max_asset_bytes
    if isinstance(asset_limit, bool) or not isinstance(asset_limit, int) or asset_limit < 1:
        raise ValueError("max_asset_bytes must be a positive integer")
    asset_payloads: dict[str, bytes] = {}
    for key, content in (assets or {}).items():
        if not isinstance(key, str) or not isinstance(content, bytes):
            raise TypeError("asset keys must be strings and values must be bytes")
        path = _asset_path(key, content)
        if len(content) > asset_limit:
            raise PackageArchiveError(f"package member too large: {path}")
        if path in asset_payloads:
            raise PackageArchiveError(f"duplicate embedded asset: {path}")
        asset_payloads[path] = content
    _validate_embedded_asset_paths_for_write(
        definition,
        {
            path: (len(content), embedded_media_types.get(path, "application/octet-stream"))
            for path, content in asset_payloads.items()
        },
        allow_legacy_unreferenced_assets=allow_legacy_unreferenced_assets,
    )

    payloads: dict[str, bytes] = {
        "lab.json": canonical_bytes(definition),
        "experience/runbook.md": runbook_bytes,
    }
    for name, content in (supporting_files or {}).items():
        _safe_member_name(name)
        if not name.startswith("experience/") or name == "experience/runbook.md":
            raise PackageArchiveError(f"invalid supporting file path: {name}")
        if not isinstance(content, bytes):
            raise TypeError("supporting file values must be bytes")
        if not content:
            raise PackageArchiveError(f"supporting file must not be empty: {name}")
        if len(content) > PACKAGE_MAX_MEMBER_BYTES:
            raise PackageArchiveError(f"package member too large: {name}")
        if name in payloads:
            raise PackageArchiveError(f"duplicate package payload: {name}")
        payloads[name] = content
    payloads.update(asset_payloads)
    _reject_path_collisions(payloads)
    if manifest_override is None:
        try:
            manifest = LabManifest(
                package_id=definition.package_id,
                version=definition.version,
                required_capabilities=required_capabilities,
                payloads=tuple(
                    ManifestPayload(
                        path=name,
                        sha256=hashlib.sha256(content).hexdigest(),
                        size_bytes=len(content),
                        media_type="text/markdown"
                        if name == "experience/runbook.md"
                        else "application/json"
                        if name == "lab.json"
                        else embedded_media_types.get(name, "application/octet-stream"),
                    )
                    for name, content in sorted(payloads.items())
                ),
            )
        except ValidationError as exc:
            raise PackageArchiveError("could not build package manifest") from exc
    else:
        if not isinstance(manifest_override, LabManifest):
            raise TypeError("manifest_override must be a LabManifest")
        manifest = manifest_override
        if required_capabilities and tuple(required_capabilities) != manifest.required_capabilities:
            raise PackageArchiveError("manifest and required capabilities mismatch")
        if manifest.package_id != definition.package_id or manifest.version != definition.version:
            raise PackageArchiveError("manifest and lab definition identity mismatch")
        manifest_payloads = {payload.path: payload for payload in manifest.payloads}
        if set(manifest_payloads) != set(payloads):
            raise PackageArchiveError("manifest payload inventory mismatch")
        for name, content in payloads.items():
            entry = manifest_payloads[name]
            if entry.sha256 != hashlib.sha256(content).hexdigest() or entry.size_bytes != len(content):
                raise PackageArchiveError(f"manifest digest or size mismatch: {name}")
            declared_media_type = embedded_media_types.get(name)
            if declared_media_type is not None and entry.media_type != declared_media_type:
                raise PackageArchiveError(f"embedded asset metadata mismatch: {name}")
    payloads_with_manifest = {"manifest.json": canonical_bytes(manifest), **payloads}
    for name, content in (signatures or {}).items():
        if name not in PACKAGE_SIGNATURE_MEMBERS:
            raise PackageArchiveError(f"unexpected package signature entry: {name}")
        if not isinstance(content, bytes):
            raise TypeError("signature values must be bytes")
        if len(content) > PACKAGE_MAX_SIGNATURE_BYTES:
            raise PackageArchiveError(f"signature entry too large: {name}")
        payloads_with_manifest[name] = content

    _reject_path_collisions(payloads_with_manifest)
    if len(payloads_with_manifest) > PACKAGE_MAX_MEMBERS:
        raise PackageArchiveError("package archive contains too many members")
    for name, content in payloads_with_manifest.items():
        if len(content) > PACKAGE_MAX_MEMBER_BYTES:
            raise PackageArchiveError(f"package member too large: {name}")
    total_uncompressed = sum(len(content) for content in payloads_with_manifest.values())
    if total_uncompressed > PACKAGE_MAX_TOTAL_UNCOMPRESSED_BYTES:
        raise PackageArchiveError("package uncompressed payload is too large")
    if total_uncompressed > PACKAGE_MAX_INSPECTED_PAYLOAD_BYTES:
        raise PackageArchiveError("inspected package payload is too large")
    if len(payloads_with_manifest["manifest.json"]) > PACKAGE_MAX_MANIFEST_BYTES:
        raise PackageArchiveError("package member too large: manifest.json")
    if len(payloads_with_manifest["lab.json"]) > PACKAGE_MAX_DEFINITION_BYTES:
        raise PackageArchiveError("package member too large: lab.json")

    archive_limit = PACKAGE_MAX_ARCHIVE_BYTES if max_archive_bytes is None else max_archive_bytes
    if isinstance(archive_limit, bool) or not isinstance(archive_limit, int) or archive_limit < 1:
        raise ValueError("max_archive_bytes must be a positive integer")
    output = _BoundedBytesIO(archive_limit)
    try:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for name in sorted(payloads_with_manifest):
                media_type = embedded_media_types.get(name, "")
                archive.writestr(
                    _zip_info(name, payloads_with_manifest[name], force_stored="qcow2" in media_type),
                    payloads_with_manifest[name],
                )
    except PackageArchiveError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, zlib.error) as exc:
        raise PackageArchiveError("could not write package archive") from exc
    result = output.getvalue()
    if len(result) > archive_limit:
        raise PackageArchiveError("package archive is too large")
    return result


class _LimitedSink:
    """Forward a seekable ZIP sink while enforcing an archive high-water mark."""

    def __init__(self, sink: BinaryIO, maximum: int) -> None:
        self._sink = sink
        self._maximum = maximum
        self._high_water = 0

    def write(self, data: bytes) -> int:
        position = self.tell()
        if max(self._high_water, position + len(data)) > self._maximum:
            raise PackageArchiveError("package archive is too large")
        written = self._sink.write(data)
        self._high_water = max(self._high_water, self.tell())
        return len(data) if written is None else written

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sink, name)


def _stream_source(source: BinaryIO | Path | bytes | bytearray | memoryview) -> tuple[BinaryIO, bool]:
    if isinstance(source, (bytes, bytearray, memoryview)):
        return io.BytesIO(bytes(source)), True
    if isinstance(source, Path):
        try:
            descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            inspected = os.fstat(descriptor)
            if stat.S_ISLNK(inspected.st_mode) or not stat.S_ISREG(inspected.st_mode):
                os.close(descriptor)
                raise PackageArchiveError("asset source must be a regular non-symlink file")
            return os.fdopen(descriptor, "rb"), True
        except PackageArchiveError:
            raise
        except OSError as exc:
            raise PackageArchiveError("could not open asset source") from exc
    if hasattr(source, "read"):
        return source, False
    raise TypeError("asset streams must contain binary streams, paths, or bytes")


def _spool_asset_stream(
    key: str,
    source: BinaryIO | Path | bytes | bytearray | memoryview,
    directory: Path,
    max_asset_bytes: int = PACKAGE_MAX_MEMBER_BYTES,
) -> tuple[str, Path, int]:
    """Copy an asset source once while discovering and enforcing its identity."""

    if key.startswith("assets/sha256/"):
        _validate_embedded_asset_path(key)
        expected_path = key
    elif len(key) == 64 and all(character in "0123456789abcdef" for character in key):
        expected_path = f"assets/sha256/{key}"
    else:
        expected_path = None
    handle, should_close = _stream_source(source)
    fd, name = tempfile.mkstemp(prefix=".astinuslab-asset-", dir=directory)
    temporary = Path(name)
    digest = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(fd, "wb") as target:
            while True:
                try:
                    chunk = handle.read(min(PACKAGE_READ_CHUNK_BYTES, max_asset_bytes - total + 1))
                except (OSError, ValueError) as exc:
                    raise PackageArchiveError("could not read asset stream") from exc
                if chunk is None:
                    raise PackageArchiveError("asset stream returned no bytes")
                if not isinstance(chunk, bytes):
                    try:
                        chunk = bytes(chunk)
                    except (TypeError, ValueError) as exc:
                        raise PackageArchiveError("asset stream returned invalid bytes") from exc
                if not chunk:
                    break
                total += len(chunk)
                if total > max_asset_bytes:
                    raise PackageArchiveError("package member too large: asset stream")
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        if not total:
            raise PackageArchiveError("embedded asset must not be empty")
        result_digest = digest.hexdigest()
        path = f"assets/sha256/{result_digest}"
        if expected_path is not None and expected_path != path:
            raise PackageArchiveError(f"embedded asset digest mismatch: {expected_path}")
        return path, temporary, total
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        if should_close:
            handle.close()


def write_archive_to(
    sink: BinaryIO,
    definition: LabDefinition,
    runbook: str | bytes,
    assets: Mapping[str, bytes] | None = None,
    *,
    asset_streams: Mapping[str, AssetDescriptor | BinaryIO | Path | bytes | bytearray | memoryview] | None = None,
    signatures: Mapping[str, bytes] | None = None,
    supporting_files: Mapping[str, bytes] | None = None,
    required_capabilities: tuple[str, ...] = (),
    manifest_override: LabManifest | None = None,
    allow_legacy_unreferenced_assets: bool = False,
    max_archive_bytes: int | None = None,
    max_asset_bytes: int | None = None,
) -> None:
    """Write a deterministic package to ``sink`` without materialising asset streams.

    Stream sources are spooled to private temporary files only long enough to
    establish their mandatory digest/size inventory.  No archive is published
    or renamed here; sink ownership and its lifecycle remain with the caller.
    """

    if not hasattr(sink, "write") or not hasattr(sink, "tell"):
        raise TypeError("sink must be a seekable binary stream")
    if not asset_streams:
        sink.write(
            write_archive(
                definition,
                runbook,
                assets,
                signatures=signatures,
                supporting_files=supporting_files,
                required_capabilities=required_capabilities,
                manifest_override=manifest_override,
                allow_legacy_unreferenced_assets=allow_legacy_unreferenced_assets,
                max_archive_bytes=max_archive_bytes,
                max_asset_bytes=max_asset_bytes,
            )
        )
        return
    if not isinstance(definition, LabDefinition):
        raise TypeError("definition must be a LabDefinition")
    asset_limit = PACKAGE_MAX_MEMBER_BYTES if max_asset_bytes is None else max_asset_bytes
    if isinstance(asset_limit, bool) or not isinstance(asset_limit, int) or asset_limit < 1:
        raise ValueError("max_asset_bytes must be a positive integer")
    if isinstance(runbook, str):
        runbook_bytes = runbook.encode("utf-8", errors="strict")
    elif isinstance(runbook, bytes):
        runbook.decode("utf-8", errors="strict")
        runbook_bytes = runbook
    else:
        raise TypeError("runbook must be text or bytes")
    if not runbook_bytes or len(runbook_bytes) > PACKAGE_MAX_RUNBOOK_BYTES:
        raise PackageArchiveError("runbook must not be empty or too large")

    inline_assets: dict[str, bytes] = {}
    for key, content in (assets or {}).items():
        if not isinstance(key, str) or not isinstance(content, bytes):
            raise TypeError("asset keys must be strings and values must be bytes")
        path = _asset_path(key, content)
        if len(content) > asset_limit:
            raise PackageArchiveError(f"package member too large: {path}")
        if path in inline_assets:
            raise PackageArchiveError(f"duplicate embedded asset: {path}")
        inline_assets[path] = content
    payloads: dict[str, bytes] = {"lab.json": canonical_bytes(definition), "experience/runbook.md": runbook_bytes}
    for name, content in (supporting_files or {}).items():
        _safe_member_name(name)
        if not name.startswith("experience/") or name == "experience/runbook.md" or not isinstance(content, bytes):
            raise PackageArchiveError(f"invalid supporting file path: {name}")
        payloads[name] = content
    payloads.update(inline_assets)

    with tempfile.TemporaryDirectory(prefix="astinuslab-write-") as temporary_root:
        embedded_media_types = _embedded_media_types(definition)
        staged: dict[str, tuple[Path, int, str]] = {}
        for key, source in asset_streams.items():
            if not isinstance(key, str):
                raise TypeError("asset stream keys must be strings")
            media_path = key if key.startswith("assets/sha256/") else f"assets/sha256/{key}"
            media_type = (
                source.media_type
                if isinstance(source, AssetDescriptor)
                else embedded_media_types.get(media_path, "application/octet-stream")
            )
            if isinstance(source, AssetDescriptor):
                stream_source = source.open()
                try:
                    path, temporary, size = _spool_asset_stream(key, stream_source, Path(temporary_root), asset_limit)
                finally:
                    stream_source.close()
            else:
                path, temporary, size = _spool_asset_stream(key, source, Path(temporary_root), asset_limit)
            if path in payloads or path in staged:
                raise PackageArchiveError(f"duplicate embedded asset: {path}")
            staged[path] = (temporary, size, media_type)
        _validate_embedded_asset_paths_for_write(
            definition,
            {
                **{
                    path: (len(content), embedded_media_types.get(path, "application/octet-stream"))
                    for path, content in inline_assets.items()
                },
                **{path: (size, media_type) for path, (_, size, media_type) in staged.items()},
            },
            allow_legacy_unreferenced_assets=allow_legacy_unreferenced_assets,
        )
        _reject_path_collisions({name: b"" for name in (*payloads, *staged)})
        if len(payloads) + len(staged) + 1 + len(signatures or {}) > PACKAGE_MAX_MEMBERS:
            raise PackageArchiveError("package archive contains too many members")
        entries = {
            name: ManifestPayload(
                path=name,
                sha256=hashlib.sha256(value).hexdigest(),
                size_bytes=len(value),
                media_type="text/markdown"
                if name == "experience/runbook.md"
                else "application/json"
                if name == "lab.json"
                else embedded_media_types.get(name, "application/octet-stream"),
            )
            for name, value in payloads.items()
        }
        entries.update(
            {
                name: ManifestPayload(path=name, sha256=name.rsplit("/", 1)[-1], size_bytes=size, media_type=media_type)
                for name, (_, size, media_type) in staged.items()
            }
        )
        manifest = manifest_override or LabManifest(
            package_id=definition.package_id,
            version=definition.version,
            required_capabilities=required_capabilities,
            payloads=tuple(entries[name] for name in sorted(entries)),
        )
        if manifest.package_id != definition.package_id or manifest.version != definition.version:
            raise PackageArchiveError("manifest and lab definition identity mismatch")
        if {entry.path for entry in manifest.payloads} != set(entries):
            raise PackageArchiveError("manifest payload inventory mismatch")
        # A manifest produced by an earlier inspection may carry deliberate
        # media types for lab/runbook/supporting documents. Preserve those
        # exact metadata values while still proving that the bytes have not
        # changed and that declared embedded assets retain their contract.
        for name, expected in entries.items():
            entry = next(payload for payload in manifest.payloads if payload.path == name)
            if entry.sha256 != expected.sha256 or entry.size_bytes != expected.size_bytes:
                raise PackageArchiveError(f"manifest digest or size mismatch: {name}")
            declared_media_type = embedded_media_types.get(name)
            if declared_media_type is not None and entry.media_type != declared_media_type:
                raise PackageArchiveError(f"embedded asset metadata mismatch: {name}")
        for name, content in (signatures or {}).items():
            if name not in PACKAGE_SIGNATURE_MEMBERS:
                raise PackageArchiveError(f"unexpected package signature entry: {name}")
            if not isinstance(content, bytes):
                raise TypeError("signature values must be bytes")
            if len(content) > PACKAGE_MAX_SIGNATURE_BYTES:
                raise PackageArchiveError(f"signature entry too large: {name}")
        all_bytes = {"manifest.json": canonical_bytes(manifest), **payloads, **(signatures or {})}
        for name, value in all_bytes.items():
            if name in PACKAGE_SIGNATURE_MEMBERS or name in {"manifest.json", *payloads}:
                if not isinstance(value, bytes):
                    raise TypeError("package payload values must be bytes")
            if name.startswith("signatures/") and name not in PACKAGE_SIGNATURE_MEMBERS:
                raise PackageArchiveError(f"unexpected package signature entry: {name}")
            if len(value) > PACKAGE_MAX_MEMBER_BYTES:
                raise PackageArchiveError(f"package member too large: {name}")
        _reject_path_collisions({name: b"" for name in (*all_bytes, *staged)})
        if len(all_bytes) + len(staged) > PACKAGE_MAX_MEMBERS:
            raise PackageArchiveError("package archive contains too many members")
        total = sum(len(value) for value in all_bytes.values()) + sum(size for _, size, _ in staged.values())
        if total > PACKAGE_MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise PackageArchiveError("package uncompressed payload is too large")
        if len(all_bytes["manifest.json"]) > PACKAGE_MAX_MANIFEST_BYTES:
            raise PackageArchiveError("package member too large: manifest.json")
        if len(payloads["lab.json"]) > PACKAGE_MAX_DEFINITION_BYTES:
            raise PackageArchiveError("package member too large: lab.json")
        archive_limit = PACKAGE_MAX_ARCHIVE_BYTES if max_archive_bytes is None else max_archive_bytes
        if isinstance(archive_limit, bool) or not isinstance(archive_limit, int) or archive_limit < 1:
            raise ValueError("max_archive_bytes must be a positive integer")
        output = _LimitedSink(sink, archive_limit)
        try:
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                for name in sorted(all_bytes):
                    archive.writestr(
                        _zip_info(name, all_bytes[name], force_stored="qcow2" in embedded_media_types.get(name, "")),
                        all_bytes[name],
                    )
                for name in sorted(staged):
                    temporary, size, _ = staged[name]
                    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_STORED
                    info.file_size = size
                    info.external_attr = (0o100640 & 0xFFFF) << 16
                    info.create_system = 3
                    with archive.open(info, "w") as target, temporary.open("rb") as source:
                        while chunk := source.read(PACKAGE_READ_CHUNK_BYTES):
                            target.write(chunk)
        except PackageArchiveError:
            raise
        except (OSError, ValueError, zipfile.BadZipFile, zlib.error) as exc:
            raise PackageArchiveError("could not write package archive") from exc


# Descriptive aliases for callers that use package rather than archive naming.
build_archive = write_archive
read_archive = inspect_archive
inspect_package = inspect_archive
write_package = write_archive
