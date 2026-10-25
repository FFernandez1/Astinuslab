"""Persistence for inert, locally imported ``.astinuslab`` packages.

This module records package identity and verified documents only.  Registering a
row never launches a VM, creates a libvirt object, or executes package content.
The retained archive path is managed by the package service; this repository
checks that it points to a regular, non-symlink file before recording it.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core.config import settings
from ..db.models import LabPackage
from .archive import ArchiveInspection, PackageArchiveError, inspect_archive
from .canonical import canonical_bytes
from .signing import SignatureResult, SignatureStatus

ARCHIVE_HASH_CHUNK_BYTES = 1024 * 1024


class PackageRecordConflictError(ValueError):
    """Raised when an immutable package identity is registered with new bytes."""


def _descriptor_inventory(inspection: ArchiveInspection) -> dict[str, tuple[str, int, str]]:
    return {
        path: (descriptor.sha256, descriptor.size_bytes, descriptor.media_type)
        for path, descriptor in inspection.asset_descriptors.items()
    }


def _archive_identity(path: Path) -> tuple[str, int]:
    try:
        inspected = path.lstat()
    except OSError as exc:
        raise PackageRecordConflictError("archive path must reference a regular non-symlink file") from exc
    if not path.is_file() or path.is_symlink():
        raise PackageRecordConflictError("archive path must reference a regular non-symlink file")

    digest = hashlib.sha256()
    total = 0
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        before = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (inspected.st_dev, inspected.st_ino) or not stat.S_ISREG(before.st_mode):
            raise PackageRecordConflictError("archive changed while it was being registered")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            while True:
                chunk = handle.read(ARCHIVE_HASH_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > settings.packages_max_archive_bytes:
                    raise PackageRecordConflictError("archive is too large")
                digest.update(chunk)
        current = path.lstat()
        after = current
    except OSError as exc:
        raise PackageRecordConflictError("could not read retained package archive") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ):
        raise PackageRecordConflictError("archive changed while it was being registered")
    if total < 1:
        raise PackageRecordConflictError("archive must not be empty")
    return digest.hexdigest(), total


def _safe_archive_path(path: Path, root: Path) -> str:
    absolute = path if path.is_absolute() else Path.cwd() / path
    managed_root = root.resolve(strict=True)
    if not managed_root.is_dir() or root.is_symlink():
        raise PackageRecordConflictError("package archive root must be a regular directory")
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            inspected = current.lstat()
        except OSError as exc:
            raise PackageRecordConflictError("could not inspect retained package path") from exc
        if stat.S_ISLNK(inspected.st_mode):
            raise PackageRecordConflictError("archive path must not contain symlink components")
    absolute = Path.cwd() / path if not path.is_absolute() else path
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise PackageRecordConflictError("archive path must reference a regular non-symlink file") from exc
    if not resolved.is_relative_to(managed_root):
        raise PackageRecordConflictError("archive path must be inside managed package storage")
    return str(resolved)


def _same_identity(
    record: LabPackage,
    *,
    archive_sha256: str,
    archive_size_bytes: int,
    manifest_sha256: str,
    manifest_json: str,
    definition_json: str,
) -> bool:
    return (
        record.archive_sha256 == archive_sha256
        and record.archive_size_bytes == archive_size_bytes
        and record.manifest_sha256 == manifest_sha256
        and record.manifest_json == manifest_json
        and record.definition_json == definition_json
    )


class LabPackageRepository:
    """CRUD operations for immutable local package records."""

    def __init__(self, session: Session, *, archive_root: Path) -> None:
        self.session = session
        self.archive_root = Path(archive_root)

    def get(self, package_id: str, version: str) -> LabPackage | None:
        return self.session.scalar(
            select(LabPackage).where(LabPackage.package_id == package_id, LabPackage.version == version)
        )

    def get_by_id(self, record_id: str) -> LabPackage | None:
        return self.session.get(LabPackage, record_id)

    def list_packages(self, *, limit: int | None = None, offset: int = 0) -> list[LabPackage]:
        query = select(LabPackage).order_by(LabPackage.imported_at.desc(), LabPackage.id.desc())
        if offset:
            query = query.offset(offset)
        if limit is not None:
            if limit < 1:
                raise ValueError("limit must be positive")
            query = query.limit(limit)
        return list(self.session.scalars(query))

    def register_inspection(
        self,
        inspection: ArchiveInspection,
        *,
        archive_path: Path,
        signature: SignatureResult | None = None,
    ) -> LabPackage:
        """Register a validated inspection, idempotently by package identity and bytes."""

        if not isinstance(inspection, ArchiveInspection):
            raise TypeError("inspection must be an ArchiveInspection")
        path = Path(archive_path)
        archive_sha256, archive_size_bytes = _archive_identity(path)
        safe_path = _safe_archive_path(path, self.archive_root)
        try:
            retained = inspect_archive(
                Path(safe_path),
                max_archive_bytes=settings.packages_max_archive_bytes,
                max_asset_bytes=settings.packages_max_asset_bytes,
            )
        except (PackageArchiveError, OSError, ValueError) as exc:
            raise PackageRecordConflictError("retained archive could not be revalidated") from exc
        if (
            retained.manifest != inspection.manifest
            or retained.definition != inspection.definition
            or retained.runbook != inspection.runbook
            or dict(retained.assets) != dict(inspection.assets)
            or _descriptor_inventory(retained) != _descriptor_inventory(inspection)
            or dict(retained.supporting_files) != dict(inspection.supporting_files)
            or dict(retained.signatures) != dict(inspection.signatures)
        ):
            raise PackageRecordConflictError("retained archive does not match inspected package")
        signature_result = signature or SignatureResult(SignatureStatus.UNSIGNED)
        if not isinstance(signature_result, SignatureResult):
            raise TypeError("signature must be a SignatureResult")
        if signature_result.status is SignatureStatus.UNSIGNED and signature_result.fingerprint is not None:
            raise ValueError("unsigned package cannot have a creator key fingerprint")

        manifest_json = canonical_bytes(inspection.manifest).decode("utf-8")
        definition_json = canonical_bytes(inspection.definition).decode("utf-8")
        capabilities_json = json.dumps(
            list(inspection.manifest.required_capabilities),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        existing = self.get(inspection.manifest.package_id, inspection.manifest.version)
        if existing is not None:
            if not _same_identity(
                existing,
                archive_sha256=archive_sha256,
                archive_size_bytes=archive_size_bytes,
                manifest_sha256=inspection.manifest_digest,
                manifest_json=manifest_json,
                definition_json=definition_json,
            ):
                raise PackageRecordConflictError(
                    f"package {inspection.manifest.package_id}@{inspection.manifest.version} is already registered"
                ) from None
            return existing

        record = LabPackage(
            id=f"lp-{uuid4().hex[:16]}",
            package_id=inspection.manifest.package_id,
            version=inspection.manifest.version,
            title=inspection.definition.title,
            description=inspection.definition.description,
            creator=inspection.definition.creator,
            license=inspection.definition.license,
            reproducibility=inspection.definition.reproducibility,
            archive_path=safe_path,
            archive_sha256=archive_sha256,
            archive_size_bytes=archive_size_bytes,
            manifest_sha256=inspection.manifest_digest,
            manifest_json=manifest_json,
            definition_json=definition_json,
            required_capabilities_json=capabilities_json,
            signature_status=signature_result.status.value,
            creator_key_fingerprint=signature_result.fingerprint,
        )
        try:
            with self.session.begin_nested():
                self.session.add(record)
                self.session.flush()
        except IntegrityError:
            raced = self.get(inspection.manifest.package_id, inspection.manifest.version)
            if raced is not None:
                if _same_identity(
                    raced,
                    archive_sha256=archive_sha256,
                    archive_size_bytes=archive_size_bytes,
                    manifest_sha256=inspection.manifest_digest,
                    manifest_json=manifest_json,
                    definition_json=definition_json,
                ):
                    return raced
                raise PackageRecordConflictError(
                    f"package {inspection.manifest.package_id}@{inspection.manifest.version} is already registered"
                ) from None
            raise
        return record

    def touch_last_used(self, record_id: str) -> LabPackage:
        record = self.get_by_id(record_id)
        if record is None:
            raise ValueError(f"package record not found: {record_id}")
        record.last_used_at = datetime.now(UTC)
        self.session.add(record)
        self.session.flush()
        return record
