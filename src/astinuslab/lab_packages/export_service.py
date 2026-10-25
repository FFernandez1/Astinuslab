"""Creator-side export of portable lab packages.

The export service is intentionally independent from the HTTP layer.  It owns
the two important boundaries in a locked export:

* the starting bytes are captured, hashed, and published to the content store
  before they are put in an archive; and
* the exact locked definition is read from a verified clean-run attestation,
  rather than reconstructed from a live Playground at download time.

All archive writing is streamed into a caller-owned staging file.  The service
does not return archive bytes, which keeps a disk image from becoming a second
copy in the Python process.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO, cast
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core import fake_mode
from ..core.config import settings
from ..db.models import LabPackageAttestation, LabPackageExportAttempt, LabStorageLease
from .archive import PackageArchiveError, inspect_archive, write_archive_to
from .canonical import canonical_sha256
from .content_store import ContentStore, ContentStoreError, StoredAsset
from .models import (
    AssetReference,
    LabDefinition,
    LabImageSpec,
    LabVM,
    LabWorkloadSpec,
)
from .storage_custody import secure_directory, secure_file

EXPORT_CHUNK_BYTES = 1024 * 1024
EXPORT_RESERVATION_SECONDS = 60 * 60
EXPORT_LEASE_SECONDS = 60 * 60


class PackageExportError(ValueError):
    """Raised when an export cannot make a truthful package."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ExportVersionConflict(PackageExportError):
    def __init__(self, message: str) -> None:
        super().__init__("version_conflict", message)


@dataclass(frozen=True, slots=True)
class ExportArtifact:
    attempt_id: str
    package_id: str
    version: str
    path: Path
    archive_sha256: str
    archive_size_bytes: int
    definition_sha256: str


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PackageExportError("invalid_snapshot", "draft snapshot contains unsupported JSON") from exc


def _hash_source(source: bytes | bytearray | memoryview | BinaryIO | Path, *, maximum: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    should_close = False
    handle: BinaryIO
    if isinstance(source, (bytes, bytearray, memoryview)):
        handle = io.BytesIO(bytes(source))
        should_close = True
    elif isinstance(source, Path):
        try:
            inspected = source.lstat()
            if stat.S_ISLNK(inspected.st_mode) or not stat.S_ISREG(inspected.st_mode):
                raise PackageExportError("unsafe_asset", "captured layer must be a regular file")
            descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            opened = os.fstat(descriptor)
            if stat.S_ISLNK(opened.st_mode) or not stat.S_ISREG(opened.st_mode):
                os.close(descriptor)
                raise PackageExportError("unsafe_asset", "captured layer must be a regular file")
            handle = os.fdopen(descriptor, "rb")
            should_close = True
        except PackageExportError:
            raise
        except OSError as exc:
            raise PackageExportError("asset_unavailable", "captured layer is unavailable") from exc
    elif hasattr(source, "read"):
        handle = source
    else:
        raise PackageExportError("invalid_asset", "captured layer source is invalid")
    try:
        while True:
            chunk = handle.read(min(EXPORT_CHUNK_BYTES, maximum - total + 1))
            if chunk is None:
                raise PackageExportError("invalid_asset", "captured layer stream returned no bytes")
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise PackageExportError("asset_too_large", "captured layer is too large")
            digest.update(chunk)
    except PackageExportError:
        raise
    except (OSError, ValueError) as exc:
        raise PackageExportError("asset_unavailable", "captured layer could not be read") from exc
    finally:
        if should_close:
            handle.close()
    if total < 1:
        raise PackageExportError("empty_asset", "captured layer must not be empty")
    return digest.hexdigest(), total


def _safe_layer_source(
    source: bytes | bytearray | memoryview | BinaryIO | Path,
) -> bytes | bytearray | memoryview | Path | BinaryIO:
    """Return a source accepted by ContentStore after identity validation."""

    return source


def materialize_boot_layer(
    content_store: ContentStore,
    source: bytes | bytearray | memoryview | BinaryIO | Path,
    *,
    media_type: str = "application/x-qcow2",
    maximum: int | None = None,
) -> tuple[StoredAsset, str]:
    """Hash and publish one clean layer, returning immutable cache metadata."""

    limit = content_store.max_bytes if maximum is None else maximum
    # ContentStore verifies while publishing.  Seekable streams can be rewound
    # for that second pass; a pipe-like stream is spooled once to a private
    # temporary file so EOF is never handed to the store.
    source_for_store: bytes | bytearray | memoryview | Path | BinaryIO = source
    temporary: Path | None = None
    if hasattr(source, "read") and not isinstance(source, (bytes, bytearray, memoryview, Path)):
        seekable = getattr(source, "seekable", lambda: False)()
        if seekable:
            try:
                source.seek(0)
            except (OSError, ValueError):
                seekable = False
        if not seekable:
            descriptor, name = tempfile.mkstemp(prefix="astinuslab-layer-")
            temporary = Path(name)
            digest = hashlib.sha256()
            total = 0
            open_descriptor: int | None = descriptor
            spooled = False
            try:
                with os.fdopen(descriptor, "wb") as staged:
                    open_descriptor = None
                    while True:
                        chunk = source.read(min(EXPORT_CHUNK_BYTES, limit - total + 1))
                        if chunk is None:
                            raise PackageExportError("invalid_asset", "captured layer stream returned no bytes")
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > limit:
                            raise PackageExportError("asset_too_large", "captured layer is too large")
                        staged.write(chunk)
                        digest.update(chunk)
                    staged.flush()
                    os.fsync(staged.fileno())
                if total < 1:
                    raise PackageExportError("empty_asset", "captured layer must not be empty")
                digest_value, size = digest.hexdigest(), total
                source_for_store = temporary
                spooled = True
            finally:
                if open_descriptor is not None:
                    try:
                        os.close(open_descriptor)
                    except OSError:
                        pass
                # A rejected layer never reaches the publication block below, so
                # its spool file has to be removed here. Leaving it behind would
                # strand up to `limit` bytes of captured guest disk in the system
                # temporary directory for every refused export.
                if not spooled:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
                    temporary = None
        else:
            digest_value, size = _hash_source(source, maximum=limit)
            source.seek(0)
    else:
        digest_value, size = _hash_source(source, maximum=limit)
    try:
        stored = content_store.put(_safe_layer_source(source_for_store), sha256=digest_value, size_bytes=size)
    except ContentStoreError as exc:
        raise PackageExportError("asset_publish_failed", "could not publish captured boot layer") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return stored, media_type


def image_spec_from_catalog(image_id: str, image: Mapping[str, object]) -> LabImageSpec:
    """Build immutable base-image provenance from an existing catalog entry."""

    path_value = image.get("path")
    if not isinstance(path_value, str) or not path_value:
        raise PackageExportError("base_image_unavailable", f"base image is unavailable: {image_id}")
    path = Path(path_value)
    digest, size = _hash_source(path, maximum=settings.packages_max_asset_bytes)

    def _string(key: str) -> str | None:
        value = image.get(key)
        return value if isinstance(value, str) else None

    architecture = _string("architecture") or "x86_64"
    firmware = _string("firmware") or "bios"
    if architecture != "x86_64" or firmware not in {"bios", "uefi"}:
        raise PackageExportError("unsupported_base_image", f"base image has unsupported runtime facts: {image_id}")
    return LabImageSpec(
        image_id=image_id,
        sha256=digest,
        size_bytes=size,
        media_type=_string("media_type") or "application/x-qcow2",
        architecture=architecture,  # type: ignore[arg-type]
        firmware=firmware,  # type: ignore[arg-type]
        distro=_string("distro"),
        version=_string("version"),
        username=_string("username"),
        cloud_init=bool(image.get("cloud_init", False)),
        cloud_init_profile=_string("cloud_init_profile"),
        machine_type=_string("machine_type"),
        os_variant=_string("os_variant"),
        family=_string("family"),
        image_format=_string("image_format"),
        filesystem_type=_string("filesystem_type"),
        network_profile=_string("network_profile"),
        inject_network_config=cast(bool | None, image.get("inject_network_config"))
        if isinstance(image.get("inject_network_config"), bool)
        else None,
    )


def build_locked_definition(
    definition: LabDefinition,
    *,
    boot_layers: Mapping[str, AssetReference],
    base_images: Mapping[str, LabImageSpec] | None = None,
    embed_layers: bool = True,
    license: str | None = None,
    redistribution_confirmed: bool = False,
) -> LabDefinition:
    """Bind independently captured boot bytes and base-image provenance."""

    if not boot_layers:
        raise PackageExportError("boot_layer_missing", "locked export requires a clean boot layer for every VM")
    if embed_layers and not redistribution_confirmed:
        raise PackageExportError("redistribution_unconfirmed", "embedding requires redistribution confirmation")
    base_by_vm = base_images or {}
    vms: list[LabVM] = []
    for vm in definition.vms:
        layer = boot_layers.get(vm.id)
        if layer is None:
            raise PackageExportError("boot_layer_missing", f"locked export has no clean layer for VM {vm.id}")
        base = base_by_vm.get(vm.id) or vm.base_image
        if base is None:
            raise PackageExportError("base_image_missing", f"locked export has no base image provenance for VM {vm.id}")
        if embed_layers and not layer.embedded:
            layer = layer.model_copy(update={"embedded": True, "redistribution_confirmed": redistribution_confirmed})
        elif not embed_layers and layer.embedded:
            layer = layer.model_copy(update={"embedded": False, "redistribution_confirmed": redistribution_confirmed})
        workload = vm.workload or LabWorkloadSpec(
            workload_baked=True,
            immutable_inputs={"workload_id": vm.workload_id} if vm.workload_id else {},
        )
        workload = workload.model_copy(update={"workload_baked": True})
        vms.append(
            vm.model_copy(
                update={
                    "base_image": base,
                    "boot_layer": layer,
                    "clean_layer": None,
                    "workload": workload,
                }
            )
        )
    if any(network.reachability is None for network in definition.networks):
        raise PackageExportError(
            "ambiguous_reachability",
            "locked export requires explicit isolated or NAT reachability for every network",
        )
    networks = tuple(definition.networks)
    # A new definition is reconstructed to ensure the locked validator runs
    # after all independent identities have been bound.
    payload = definition.model_dump(mode="json")
    payload.update(
        {
            "reproducibility": "locked",
            "license": license or definition.license,
            "networks": [network.model_dump(mode="json") for network in networks],
            "vms": [vm.model_dump(mode="json") for vm in vms],
        }
    )
    try:
        return LabDefinition.model_validate(payload)
    except ValueError as exc:
        raise PackageExportError("invalid_locked_definition", "locked definition is not complete") from exc


def reserve_export_version(
    session: Session,
    *,
    package_id: str,
    version: str,
    definition_sha256: str,
    staging_path: Path,
    expires_in_seconds: int = EXPORT_RESERVATION_SECONDS,
) -> LabPackageExportAttempt:
    """Reserve ``package_id@version`` before any archive bytes are written."""

    now = _utc_now()
    live = session.scalars(
        select(LabPackageExportAttempt).where(
            LabPackageExportAttempt.package_id == package_id,
            LabPackageExportAttempt.version == version,
            LabPackageExportAttempt.state.in_(("reserved", "published")),
        )
    ).all()
    for row in live:
        if row.expires_at is not None and row.expires_at <= now:
            row.state = "failed"
        else:
            raise ExportVersionConflict(f"package version is already being exported: {package_id}@{version}")
    completed = session.scalar(
        select(LabPackageExportAttempt).where(
            LabPackageExportAttempt.package_id == package_id,
            LabPackageExportAttempt.version == version,
            LabPackageExportAttempt.state == "completed",
        )
    )
    if completed is not None:
        if completed.definition_sha256 == definition_sha256:
            raise ExportVersionConflict(f"package version is finalized: {package_id}@{version}")
        raise ExportVersionConflict(f"finalized package version cannot be reused: {package_id}@{version}")
    # Imported package records are also permanent identity claims.
    from .package_catalog import LabPackageRepository

    existing = LabPackageRepository(session, archive_root=settings.storage_dir).get(package_id, version)
    if existing is not None:
        raise ExportVersionConflict(f"package version is already registered: {package_id}@{version}")
    attempt = LabPackageExportAttempt(
        id=f"lpe-{uuid4().hex[:16]}",
        package_id=package_id,
        version=version,
        definition_sha256=definition_sha256,
        staging_path=str(staging_path),
        state="reserved",
        expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=expires_in_seconds),
    )
    session.add(attempt)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise ExportVersionConflict(f"package version is already reserved: {package_id}@{version}") from exc
    return attempt


def _digest_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(EXPORT_CHUNK_BYTES):
                total += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise PackageExportError("archive_unavailable", "staged package archive is unavailable") from exc
    return digest.hexdigest(), total


def export_locked(
    session: Session,
    *,
    definition: LabDefinition,
    runbook: str,
    content_store: ContentStore,
    embedded_layers: Mapping[str, AssetReference] | None = None,
    supporting_files: Mapping[str, bytes] | None = None,
    attestation: LabPackageAttestation | None = None,
    draft_supporting_files: Mapping[str, str] | None = None,
    required_capabilities: tuple[str, ...] = (),
    package_root: Path | None = None,
) -> ExportArtifact:
    """Write a verified locked archive to durable staging and finalize its journal."""

    if fake_mode.fake_mode_active():
        raise PackageExportError("fake_mode_unsupported", "locked export is unavailable in fake mode")
    if definition.reproducibility != "locked":
        raise PackageExportError("not_locked", "only a locked definition can receive a verified export")
    definition_digest = canonical_sha256(definition)
    if attestation is not None and attestation.locked_definition_sha256 != definition_digest:
        raise PackageExportError("attestation_mismatch", "attestation does not match the locked definition")
    root = Path(package_root or settings.storage_dir / "lab-packages")
    root.mkdir(parents=True, exist_ok=True)
    secure_directory(root)
    if embedded_layers == {} and any(vm.boot_layer is not None and vm.boot_layer.embedded for vm in definition.vms):
        raise PackageExportError(
            "embedding_required",
            "this attestation contains embedded clean-start layers; export them or verify an external-layer draft",
        )
    export_token = uuid4().hex
    final_path = root / f"{definition.package_id}-{definition.version}-{export_token}.astinuslab"
    staging = root / f".{final_path.name}.partial"
    attempt = reserve_export_version(
        session,
        package_id=definition.package_id,
        version=definition.version,
        definition_sha256=definition_digest,
        # The journal owns the eventual final path before any bytes are
        # written.  A crash after os.replace but before the state transition
        # can therefore be recovered without an untracked archive.
        staging_path=final_path,
    )
    # The reservation is durable before the first filesystem byte is written.
    # A restart can therefore distinguish a dead export from an anonymous
    # archive and safely reclaim the former.
    session.commit()
    layer_refs = (
        {vm.id: vm.boot_layer for vm in definition.vms if vm.boot_layer is not None and vm.boot_layer.embedded}
        if embedded_layers is None
        else embedded_layers
    )
    streams: dict[str, BinaryIO] = {}
    digest = ""
    size = 0
    published = False

    # The package service's lease helper is intentionally private because it
    # also raises package-deletion-specific exceptions.  Export has a simpler
    # contract: make the digest visible to GC for the life of this attempt,
    # then release that ownership after the terminal transition.
    def lease_digest(digest: str) -> None:
        now = _utc_now()
        existing = session.scalar(
            select(LabStorageLease).where(
                LabStorageLease.resource_kind == "content",
                LabStorageLease.resource_id == digest,
                LabStorageLease.owner_id == attempt.id,
            )
        )
        if existing is None:
            session.add(
                LabStorageLease(
                    id=f"lpl-{uuid4().hex[:16]}",
                    resource_kind="content",
                    resource_id=digest,
                    operation_kind="package_export",
                    owner_id=attempt.id,
                    expires_at=now + timedelta(seconds=EXPORT_LEASE_SECONDS),
                )
            )

    def lease_content(reference: AssetReference) -> None:
        lease_digest(reference.sha256)

    def release_content_leases() -> None:
        leases = session.scalars(
            select(LabStorageLease).where(
                LabStorageLease.resource_kind == "content",
                LabStorageLease.owner_id == attempt.id,
            )
        ).all()
        for lease in leases:
            session.delete(lease)

    try:
        archive_supporting: dict[str, bytes] = dict(supporting_files or {})
        for path, expected_digest in (draft_supporting_files or {}).items():
            if (
                not isinstance(path, str)
                or not path.startswith("experience/")
                or path == "experience/runbook.md"
                or not isinstance(expected_digest, str)
                or len(expected_digest) != 64
                or any(char not in "0123456789abcdef" for char in expected_digest)
                or path not in archive_supporting
                or hashlib.sha256(archive_supporting[path]).hexdigest() != expected_digest
            ):
                raise PackageExportError("supporting_file_mismatch", f"draft supporting file is not exact: {path}")
        if attestation is not None:
            try:
                runtime = json.loads(attestation.runtime_json)
                if not isinstance(runtime, dict):
                    raise PackageExportError("invalid_attestation", "attestation runtime facts are invalid")
                fake_flags = runtime.get("fake_mode")
                if isinstance(fake_flags, dict) and any(bool(value) for value in fake_flags.values()):
                    raise PackageExportError("fake_mode_unsupported", "synthetic clean-run evidence cannot be exported")
                verification_metadata = {
                    "attestation_id": attestation.id,
                    "input_draft_sha256": attestation.input_draft_sha256,
                    "locked_definition_sha256": attestation.locked_definition_sha256,
                    "input_digests": json.loads(attestation.input_digests_json),
                    "runtime": runtime,
                    "verification_result": json.loads(attestation.result_json),
                    "draft_supporting_files": dict(draft_supporting_files or {}),
                }
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise PackageExportError("invalid_attestation", "attestation payload is invalid") from exc
            verification_metadata["attestation_sha256"] = hashlib.sha256(
                _canonical_json(verification_metadata).encode("utf-8")
            ).hexdigest()
            archive_supporting["experience/attestation.json"] = json.dumps(
                verification_metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        embedded_references = [(vm_id, reference) for vm_id, reference in layer_refs.items() if reference.embedded]
        for _vm_id, reference in embedded_references:
            lease_content(reference)
        for _path, digest_ref in (draft_supporting_files or {}).items():
            lease_digest(digest_ref)
        # Do not leave a crash window where the export journal is durable but
        # its content leases are only in an uncommitted request transaction.
        session.commit()
        for vm_id, reference in embedded_references:
            try:
                streams[f"assets/sha256/{reference.sha256}"] = content_store.open(
                    reference.sha256, size_bytes=reference.size_bytes
                )
            except ContentStoreError as exc:
                raise PackageExportError("boot_layer_unavailable", f"boot layer is not cached: {vm_id}") from exc
        descriptor = os.open(staging, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w+b") as sink:
            secure_file(sink.fileno(), staging)
            write_archive_to(
                sink,
                definition,
                runbook,
                asset_streams=streams,
                supporting_files=archive_supporting,
                required_capabilities=required_capabilities,
                max_archive_bytes=settings.packages_max_archive_bytes,
                max_asset_bytes=settings.packages_max_asset_bytes,
            )
            sink.flush()
            os.fsync(sink.fileno())
        digest, size = _digest_file(staging)
        os.replace(staging, final_path)
        directory_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        published = True
        attempt.staging_path = str(final_path)
        attempt.archive_sha256 = digest
        attempt.state = "published"
        # This is the durable hand-off: a restart can inspect the named final
        # file and either complete or quarantine the export.
        session.commit()
        checked = inspect_archive(
            final_path,
            max_archive_bytes=settings.packages_max_archive_bytes,
            max_asset_bytes=settings.packages_max_asset_bytes,
        )
        if checked.definition != definition or checked.runbook != runbook:
            raise PackageExportError("archive_mismatch", "staged archive does not match the locked definition")
        # A separate terminal transition makes a crash between publication
        # and completion visible to reconciliation.
        attempt.state = "completed"
        attempt.completed_at = _utc_now()
        attempt.expires_at = None
        session.commit()
        with session.begin():
            release_content_leases()
    except (PackageArchiveError, OSError, PackageExportError, ContentStoreError):
        attempt.state = "failed"
        attempt.staging_path = str(final_path if published else staging)
        try:
            session.commit()
        except Exception:
            session.rollback()
        # Once the journal says ``published`` the rename has already made the
        # final path visible.  If post-publication inspection or the terminal
        # DB transition fails, remove that exact path as part of this owned
        # reservation's compensation; deleting only ``staging`` would leave
        # an untracked archive behind.
        for owned_path in (final_path if published else staging, staging):
            try:
                owned_path.unlink(missing_ok=True)
            except OSError:
                pass
        # Failed exports must not pin content until the lease timeout.  The
        # archive journal still retains the exact path for reconciliation.
        try:
            with session.begin():
                release_content_leases()
        except Exception:
            session.rollback()
        raise
    finally:
        for stream in streams.values():
            stream.close()
    return ExportArtifact(
        attempt_id=attempt.id,
        package_id=definition.package_id,
        version=definition.version,
        path=final_path,
        archive_sha256=digest,
        archive_size_bytes=size,
        definition_sha256=definition_digest,
    )


def stream_file(path: Path, *, chunk_size: int = EXPORT_CHUNK_BYTES):
    """Yield a staged archive without materialising it in a response body."""

    try:
        with path.open("rb") as handle:
            while chunk := handle.read(chunk_size):
                yield chunk
    except OSError as exc:
        raise PackageExportError("archive_unavailable", "staged package archive is unavailable") from exc


def reconcile_exports(session: Session, *, package_root: Path | None = None) -> dict[str, int]:
    """Recover export rows interrupted between filesystem and DB transitions."""

    root = Path(package_root or settings.storage_dir / "lab-packages").resolve()
    now = _utc_now()
    completed = 0
    failed = 0
    rows = list(
        session.scalars(
            select(LabPackageExportAttempt).where(
                LabPackageExportAttempt.state.in_(("reserved", "published")),
            )
        )
    )
    for attempt in rows:
        path = Path(attempt.staging_path) if attempt.staging_path else None
        if path is not None and not path.resolve(strict=False).is_relative_to(root):
            # Never inspect or adopt an archive outside the configured package
            # root, even if a corrupted/stale row names one.
            path = None
        live_reservation = attempt.expires_at is None or attempt.expires_at > now
        if live_reservation and path is not None and path.exists() and path.is_file() and not path.is_symlink():
            try:
                digest, size = _digest_file(path)
                # A reserved attempt has no archive digest yet.  A published
                # attempt has one and must match it before recovery can adopt
                # the file.  In both cases inspect the archive and bind the
                # recovered definition to the journal's immutable digest.
                if attempt.state == "published" and digest != attempt.archive_sha256:
                    raise PackageExportError("archive_mismatch", "published archive digest changed")
                inspected = inspect_archive(
                    path,
                    max_archive_bytes=settings.packages_max_archive_bytes,
                    max_asset_bytes=settings.packages_max_asset_bytes,
                )
                if canonical_sha256(inspected.definition) != attempt.definition_sha256:
                    raise PackageExportError(
                        "archive_mismatch", "recovered archive definition does not match reservation"
                    )
                attempt.archive_sha256 = digest
                attempt.state = "completed"
                attempt.completed_at = now
                attempt.expires_at = None
                for lease in session.scalars(
                    select(LabStorageLease).where(
                        LabStorageLease.resource_kind == "content",
                        LabStorageLease.owner_id == attempt.id,
                    )
                ).all():
                    session.delete(lease)
                completed += 1
                continue
            except (OSError, PackageArchiveError, PackageExportError, ValueError):
                pass
        if attempt.expires_at is not None and attempt.expires_at > now:
            continue
        attempt.state = "failed"
        failed += 1
        for lease in session.scalars(
            select(LabStorageLease).where(
                LabStorageLease.resource_kind == "content",
                LabStorageLease.owner_id == attempt.id,
            )
        ).all():
            session.delete(lease)
        if path is not None:
            try:
                candidates = (path, path.with_name(f".{path.name}.partial"))
                for candidate in candidates:
                    if (
                        candidate.resolve(strict=False).is_relative_to(root)
                        and candidate.is_file()
                        and not candidate.is_symlink()
                    ):
                        candidate.unlink()
            except OSError:
                pass
    session.commit()
    return {"completed": completed, "failed": failed}
