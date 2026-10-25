"""Passive inspection and transactional local import of portable lab packages."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import tempfile
import time
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..core.config import settings
from ..db.models import (
    LabContentObject,
    LabContentPublicationAttempt,
    LabInstance,
    LabPackage,
    LabPackageAsset,
    LabPackageInspection,
    LabPackagePublicationAttempt,
    LabPackageTombstone,
    LabPrepareAttempt,
    LabStorageLease,
    PlaygroundSession,
)
from ..db.transactions import reserve_sqlite_write
from .archive import ArchiveInspection, AssetDescriptor, PackageArchiveError, inspect_archive
from .canonical import canonical_bytes
from .content_store import ContentStore, ContentStoreError, StoredAsset
from .package_catalog import LabPackageRepository, PackageRecordConflictError
from .prepare_authority import PreparePackageBusy, PreparePackageLock, end_read_transaction, try_package_lock
from .signing import LocalTrustPolicy, SignatureResult, verify_archive
from .storage_custody import failure_message, secure_directory, secure_file

PACKAGE_STORE_DIRNAME = "lab-packages"
PACKAGE_CONTENT_DIRNAME = "lab-content"
PACKAGE_IMPORT_MAX_BYTES = 40 * 1024**3
PACKAGE_COPY_CHUNK_BYTES = 1024 * 1024
PACKAGE_STAGING_MAX_AGE_SECONDS = 24 * 60 * 60


class PackageImportError(ValueError):
    """Raised when a package cannot be safely imported into local state."""


class PackageImportConflictError(PackageImportError):
    """Raised when package identity/version is already registered with other bytes."""


class PackageInspectionExpiredError(PackageImportError):
    """Raised when an inert staged inspection is no longer safe to commit."""


class PackageDeletionConflictError(PackageImportError):
    """Raised when a retained package is actively used or leased."""


@dataclass(frozen=True, slots=True)
class ImportedPackage:
    """The inert record and verified embedded assets created by an import."""

    record: LabPackage
    inspection: ArchiveInspection
    signature: SignatureResult
    embedded_assets: tuple[StoredAsset, ...]


@dataclass(frozen=True, slots=True)
class StagedPackageInspection:
    """A persisted, immutable upload awaiting an explicit import commit."""

    id: str
    inspection: ArchiveInspection
    signature: SignatureResult
    expires_at: datetime


def _ensure_private_directory(path: Path) -> None:
    absolute = path if path.is_absolute() else Path.cwd() / path
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        created = False
        try:
            inspected = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            inspected = current.lstat()
            created = True
        except OSError as exc:
            raise PackageImportError(failure_message("could not inspect package storage path", exc)) from exc
        if stat.S_ISLNK(inspected.st_mode) or not stat.S_ISDIR(inspected.st_mode):
            raise PackageImportError("package storage path must contain only directories")
        if created or current == absolute:
            try:
                secure_directory(current)
            except OSError as exc:
                raise PackageImportError(failure_message("could not secure package storage directory", exc)) from exc


def _digest_file(path: Path, *, max_bytes: int = PACKAGE_IMPORT_MAX_BYTES) -> tuple[str, int]:
    try:
        inspected = path.lstat()
    except OSError as exc:
        raise PackageImportError("package source must be a regular non-symlink file") from exc
    if stat.S_ISLNK(inspected.st_mode) or not stat.S_ISREG(inspected.st_mode):
        raise PackageImportError("package source must be a regular non-symlink file")
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(PACKAGE_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise PackageImportError("package archive is too large")
                digest.update(chunk)
        current = path.lstat()
    except PackageImportError:
        raise
    except OSError as exc:
        raise PackageImportError(failure_message("could not read package source", exc)) from exc
    if (current.st_dev, current.st_ino) != (inspected.st_dev, inspected.st_ino):
        raise PackageImportError("package source changed while it was read")
    if total < 1:
        raise PackageImportError("package archive must not be empty")
    return digest.hexdigest(), total


def copy_digest_verified(
    source: Path, temp_sink: Path, *, max_bytes: int = PACKAGE_IMPORT_MAX_BYTES
) -> tuple[str, int]:
    """Copy ``source`` into a caller-owned temporary path while hashing it.

    This is deliberately a pure primitive: it creates no package records and
    never renames the supplied temporary path into a published location.  The
    durable import protocol is responsible for ownership and publication.
    """

    source_path = Path(source)
    sink_path = Path(temp_sink)
    try:
        source_stat = source_path.lstat()
    except OSError as exc:
        raise PackageImportError("package source must be a regular non-symlink file") from exc
    if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):
        raise PackageImportError("package source must be a regular non-symlink file")
    descriptor: int | None = None
    source_descriptor: int | None = None
    try:
        descriptor = os.open(
            sink_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        secure_file(descriptor, sink_path)
        source_descriptor = os.open(
            source_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        opened_source = os.fstat(source_descriptor)
        if not stat.S_ISREG(opened_source.st_mode) or (opened_source.st_dev, opened_source.st_ino) != (
            source_stat.st_dev,
            source_stat.st_ino,
        ):
            raise PackageImportError("package source changed while it was copied")
        digest = hashlib.sha256()
        total = 0
        with os.fdopen(descriptor, "wb") as target, os.fdopen(source_descriptor, "rb") as original:
            descriptor = None
            source_descriptor = None
            while True:
                chunk = original.read(PACKAGE_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise PackageImportError("package archive is too large")
                target.write(chunk)
                digest.update(chunk)
            target.flush()
            os.fsync(target.fileno())
        current = source_path.lstat()
        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns) != (
            source_stat.st_dev,
            source_stat.st_ino,
            source_stat.st_size,
            source_stat.st_mtime_ns,
            source_stat.st_ctime_ns,
        ):
            raise PackageImportError("package source changed while it was copied")
        if total < 1:
            raise PackageImportError("package archive must not be empty")
        return digest.hexdigest(), total
    except PackageImportError:
        raise
    except OSError as exc:
        raise PackageImportError(failure_message("could not copy package archive", exc)) from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if source_descriptor is not None:
            try:
                os.close(source_descriptor)
            except OSError:
                pass


def _copy_archive(source: Path, destination: Path, digest: str, size_bytes: int, *, max_bytes: int) -> bool:
    """Publish a source archive atomically; return whether destination was new."""

    if destination.exists() or destination.is_symlink():
        try:
            existing_digest, existing_size = _digest_file(destination, max_bytes=max_bytes)
        except PackageImportError as exc:
            raise PackageImportError("retained package path is not a valid regular file") from exc
        if existing_digest != digest or existing_size != size_bytes:
            raise PackageImportError("retained package path has an unexpected identity")
        return False

    temporary: Path | None = None
    published_stat: os.stat_result | None = None
    descriptor: int | None = None
    source_descriptor: int | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".astinuslab-import-", dir=destination.parent)
        temporary = Path(temporary_name)
        secure_file(descriptor, temporary)
        source_descriptor = os.open(
            source,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        source_stat = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_stat.st_mode):
            os.close(source_descriptor)
            source_descriptor = None
            raise PackageImportError("package source must be a regular non-symlink file")
        copied_digest = hashlib.sha256()
        copied_size = 0
        with os.fdopen(descriptor, "wb") as target, os.fdopen(source_descriptor, "rb") as original:
            descriptor = None
            source_descriptor = None
            while True:
                chunk = original.read(PACKAGE_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                copied_size += len(chunk)
                if copied_size > max_bytes:
                    raise PackageImportError("package archive is too large")
                target.write(chunk)
                copied_digest.update(chunk)
            target.flush()
            os.fsync(target.fileno())
        current_source = source.lstat()
        if (
            current_source.st_dev,
            current_source.st_ino,
            current_source.st_size,
            current_source.st_mtime_ns,
            current_source.st_ctime_ns,
        ) != (
            source_stat.st_dev,
            source_stat.st_ino,
            source_stat.st_size,
            source_stat.st_mtime_ns,
            source_stat.st_ctime_ns,
        ):
            raise PackageImportError("package source changed while it was copied")
        if copied_size != size_bytes or copied_digest.hexdigest() != digest:
            raise PackageImportError("package source changed while it was copied")
        os.replace(temporary, destination)
        published_stat = destination.lstat()
        temporary = None
        directory_fd = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        actual_digest, actual_size = _digest_file(destination, max_bytes=max_bytes)
        if actual_digest != digest or actual_size != size_bytes:
            _remove_if_same(destination, published_stat)
            raise PackageImportError("retained package archive changed while it was published")
    except PackageImportError:
        if published_stat is not None:
            _remove_if_same(destination, published_stat)
        raise
    except (OSError, ValueError) as exc:
        if published_stat is not None:
            _remove_if_same(destination, published_stat)
        raise PackageImportError(failure_message("could not retain package archive", exc)) from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if source_descriptor is not None:
            try:
                os.close(source_descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return True


@contextmanager
def _exclusive_package_lock(path: Path):
    """Serialize publication and registration for one archive identity."""

    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        secure_file(descriptor, path)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _remove_if_same(path: Path, expected: os.stat_result | None) -> None:
    if expected is None:
        return
    try:
        current = path.lstat()
        if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino):
            path.unlink()
    except OSError:
        pass


class LabPackageService:
    """Application service for passive inspection and local package import."""

    def __init__(
        self,
        *,
        package_root: Path | None = None,
        content_store: ContentStore | None = None,
    ) -> None:
        root = package_root or (settings.storage_dir / PACKAGE_STORE_DIRNAME)
        _ensure_private_directory(root)
        self.package_root = root
        self.archive_max_bytes = settings.packages_max_archive_bytes
        self._lock_root = Path(root) / ".locks"
        _ensure_private_directory(self._lock_root)
        self.content_store = content_store or ContentStore(
            settings.storage_dir / PACKAGE_CONTENT_DIRNAME,
            max_bytes=settings.packages_max_asset_bytes,
        )

    def cleanup_staging(self, *, max_age_seconds: int = PACKAGE_STAGING_MAX_AGE_SECONDS) -> int:
        """Remove stale interrupted upload/import files from managed storage."""

        if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, int) or max_age_seconds < 1:
            raise ValueError("staging age must be a positive integer")
        cutoff = time.time() - max_age_seconds
        removed = 0
        try:
            entries = list(self.package_root.iterdir())
        except OSError as exc:
            raise PackageImportError(failure_message("could not inspect package staging area", exc)) from exc
        for entry in entries:
            if not (entry.name.startswith(".astinuslab-upload-") or entry.name.startswith(".astinuslab-import-")):
                continue
            try:
                inspected = entry.lstat()
                if inspected.st_mtime >= cutoff:
                    continue
                if stat.S_ISDIR(inspected.st_mode):
                    continue
                entry.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                continue
            removed += 1
        return removed

    def inspect(self, source: Path) -> tuple[ArchiveInspection, SignatureResult]:
        source_path = Path(source)
        try:
            inspection = inspect_archive(
                source_path,
                max_archive_bytes=self.archive_max_bytes,
                max_asset_bytes=settings.packages_max_asset_bytes,
            )
            signature = verify_archive(inspection=inspection)
        except (PackageArchiveError, OSError, ValueError) as exc:
            raise PackageImportError(str(exc)) from exc
        return inspection, signature

    def inspect_experience(self, source: BinaryIO) -> ArchiveInspection:
        """Read bounded instructions from the caller's verified, leased descriptor."""
        return inspect_archive(
            source,
            max_archive_bytes=self.archive_max_bytes,
            max_asset_bytes=settings.packages_max_asset_bytes,
            retain_assets=False,
        )

    def get_staged_inspection(self, inspection_id: str, session: Session) -> LabPackageInspection:
        inspection = session.get(LabPackageInspection, inspection_id)
        if inspection is None:
            raise PackageImportError("package inspection not found")
        return inspection

    def reserve_inspection(
        self, session: Session, *, expires_in_seconds: int = PACKAGE_STAGING_MAX_AGE_SECONDS
    ) -> LabPackageInspection:
        if isinstance(expires_in_seconds, bool) or not isinstance(expires_in_seconds, int) or expires_in_seconds < 1:
            raise ValueError("inspection expiry must be a positive integer")
        inspection_id = f"lpi-{uuid4().hex[:16]}"
        staged_path = self.package_root / f".astinuslab-inspection-{inspection_id}.astinuslab"
        row = LabPackageInspection(
            id=inspection_id,
            staged_path=str(staged_path),
            state="reserved",
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in_seconds),
        )
        session.add(row)
        self._acquire_lease(
            session,
            kind="inspection",
            resource_id=inspection_id,
            owner_id=inspection_id,
            operation="inspection_stage",
            ttl_seconds=expires_in_seconds,
        )
        try:
            session.commit()
        except Exception as exc:
            session.rollback()
            raise PackageImportError(failure_message("could not reserve package inspection", exc)) from exc
        return row

    @staticmethod
    def inspection_partial_path(inspection: LabPackageInspection) -> Path:
        staged = Path(inspection.staged_path)
        return staged.with_name(f".{staged.name}.partial")

    def abandon_inspection(self, inspection_id: str, session: Session, *, error_summary: str) -> None:
        """Close a caller-aborted upload reservation without sweeping other files."""
        with self._inspection_lock(inspection_id):
            inspection = self.get_staged_inspection(inspection_id, session)
            if inspection.state not in {"reserved", "staged"}:
                return
            inspection.state = "failed"
            inspection.error_summary = error_summary[:1000]
            inspection.updated_at = datetime.now(UTC)
            self._release_lease(session, kind="inspection", resource_id=inspection.id, owner_id=inspection.id)
            session.commit()

    def finalize_staged_upload(
        self, inspection_id: str, session: Session, *, trust_policy: LocalTrustPolicy | None = None
    ) -> StagedPackageInspection:
        with self._inspection_lock(inspection_id):
            return self._finalize_staged_upload_locked(inspection_id, session, trust_policy=trust_policy)

    def _finalize_staged_upload_locked(
        self, inspection_id: str, session: Session, *, trust_policy: LocalTrustPolicy | None = None
    ) -> StagedPackageInspection:
        """Verify one reserved upload file and make its inspection durable."""
        row = self.get_staged_inspection(inspection_id, session)
        staged_path = Path(row.staged_path)
        partial = self.inspection_partial_path(row)
        try:
            digest, size = _digest_file(partial, max_bytes=self.archive_max_bytes)
            os.replace(partial, staged_path)
            directory_fd = os.open(self.package_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            inspection, signature = self.inspect(staged_path)
            if trust_policy is not None:
                signature = verify_archive(inspection=inspection, trust_policy=trust_policy)
            row.archive_sha256 = digest
            row.archive_size_bytes = size
            row.signature_status = signature.status.value
            row.creator_key_fingerprint = signature.fingerprint
            row.state = "staged"
            row.updated_at = datetime.now(UTC)
            session.commit()
            return StagedPackageInspection(row.id, inspection, signature, row.expires_at)
        except (PackageArchiveError, ContentStoreError, OSError, ValueError, PackageImportError) as exc:
            session.rollback()
            failed = session.get(LabPackageInspection, inspection_id)
            if failed is not None:
                failed.state = "failed"
                failed.error_summary = str(exc)[:1000]
                failed.updated_at = datetime.now(UTC)
                self._release_lease(session, kind="inspection", resource_id=failed.id, owner_id=failed.id)
                session.commit()
            if isinstance(exc, PackageImportError):
                raise
            raise PackageImportError(str(exc)) from exc

    def stage_inspection(
        self,
        source: Path,
        session: Session,
        *,
        trust_policy: LocalTrustPolicy | None = None,
        expires_in_seconds: int = PACKAGE_STAGING_MAX_AGE_SECONDS,
    ) -> StagedPackageInspection:
        """Copy an upload once into managed storage and persist an inert inspection.

        The reservation is committed before any archive bytes are made visible.
        This intentionally creates a small durable boundary: process death can
        leave a named ``reserved`` inspection, which reconciliation can remove,
        but cannot leave an anonymous retained package archive.
        """
        if isinstance(expires_in_seconds, bool) or not isinstance(expires_in_seconds, int) or expires_in_seconds < 1:
            raise ValueError("inspection expiry must be a positive integer")
        row = self.reserve_inspection(session, expires_in_seconds=expires_in_seconds)
        inspection_id = row.id
        staged_path = Path(row.staged_path)
        partial = self.inspection_partial_path(row)
        try:
            with self._inspection_lock(inspection_id):
                copy_digest_verified(Path(source), partial, max_bytes=self.archive_max_bytes)
            return self.finalize_staged_upload(inspection_id, session, trust_policy=trust_policy)
        except (PackageArchiveError, ContentStoreError, OSError, ValueError, PackageImportError) as exc:
            try:
                partial.unlink(missing_ok=True)
                staged_path.unlink(missing_ok=True)
            except OSError:
                pass
            session.rollback()
            failed = session.get(LabPackageInspection, inspection_id)
            if failed is not None:
                failed.state = "failed"
                failed.error_summary = str(exc)[:1000]
                failed.updated_at = datetime.now(UTC)
                self._release_lease(session, kind="inspection", resource_id=failed.id, owner_id=failed.id)
                session.commit()
            if isinstance(exc, PackageImportError):
                raise
            raise PackageImportError(str(exc)) from exc

    def _reuse_embedded_asset(
        self, session: Session, digest: str, size_bytes: int, *, owner_id: str
    ) -> StoredAsset | None:
        """Retain verified bytes under both digest locks before attaching an asset.

        An adoption pin is compatible with other readers, including verification
        and export. It never authorizes publication or removal of content.
        """
        recorded = session.get(LabContentObject, {"sha256": digest, "size_bytes": size_bytes}, populate_existing=True)
        if recorded is not None and Path(recorded.path) != self.content_store.path_for(digest):
            raise PackageImportError("cached content identity is bound to a different path")
        with self.content_store.locked_cached_asset(digest, size_bytes=size_bytes) as item:
            if item is None:
                return None
            reserve_sqlite_write(session)
            content = session.get(
                LabContentObject, {"sha256": digest, "size_bytes": size_bytes}, populate_existing=True
            )
            if content is not None and Path(content.path) != item.path:
                raise PackageImportError("cached content identity is bound to a different path")
            now = datetime.now(UTC).replace(tzinfo=None)
            pin = session.scalar(
                select(LabStorageLease).where(
                    LabStorageLease.resource_kind == "content",
                    LabStorageLease.resource_id == digest,
                    LabStorageLease.owner_id == owner_id,
                )
            )
            if pin is None:
                pin = LabStorageLease(
                    id=f"lpl-{uuid4().hex[:16]}",
                    resource_kind="content",
                    resource_id=digest,
                    owner_id=owner_id,
                    operation_kind="content_adopt",
                    expires_at=now + timedelta(hours=1),
                )
                session.add(pin)
            elif pin.operation_kind != "content_adopt":
                raise PackageImportError("content adoption owner has another operation lease")
            else:
                pin.expires_at = max(pin.expires_at, now + timedelta(hours=1))
            if content is None:
                content = LabContentObject(sha256=digest, size_bytes=size_bytes, path=str(item.path))
                session.add(content)
            content.filesystem_fingerprint = self._content_fingerprint(item.path)
            content.last_verified_at = content.last_used_at = now
            session.commit()
            return item

    def _cache_embedded_assets(
        self, session: Session, inspection: ArchiveInspection, *, adoption_owner_id: str
    ) -> tuple[list[StoredAsset], dict[str, StoredAsset]]:
        payloads = {payload.path: payload for payload in inspection.manifest.payloads}
        sources: dict[str, bytes | AssetDescriptor] = {path: content for path, content in inspection.assets.items()}
        sources.update(inspection.asset_descriptors)
        stored: list[StoredAsset] = []
        by_digest: dict[str, StoredAsset] = {}
        for asset_path, content in sources.items():
            expected = payloads.get(asset_path)
            if expected is None:
                raise PackageImportError(f"embedded asset is not declared: {asset_path}")
            attempt = LabContentPublicationAttempt(
                id=f"lcp-{uuid4().hex[:16]}",
                sha256=expected.sha256,
                size_bytes=expected.size_bytes,
                final_path=str(self.content_store.path_for(expected.sha256)),
                state="reserved",
            )
            try:
                with _exclusive_package_lock(self._resource_lock_path("content", expected.sha256)):
                    current = session.scalar(
                        select(LabContentPublicationAttempt).where(
                            LabContentPublicationAttempt.sha256 == expected.sha256,
                            LabContentPublicationAttempt.size_bytes == expected.size_bytes,
                            LabContentPublicationAttempt.state.in_(("reserved", "published")),
                        )
                    )
                    expected_path = self.content_store.path_for(expected.sha256)
                    if current is not None and Path(current.final_path).resolve(strict=False) != expected_path.resolve(
                        strict=False
                    ):
                        raise PackageImportError("content publication path is not the managed digest path")
                    item = self._reuse_embedded_asset(
                        session, expected.sha256, expected.size_bytes, owner_id=adoption_owner_id
                    )
                    if item is not None:
                        stored.append(item)
                        by_digest[item.sha256] = item
                        continue
                    owns_publication = current is None
                    if owns_publication:
                        # The journal row and lease are durable before
                        # ContentStore publishes its final digest path. A
                        # concurrent import reuses this owner under the same
                        # digest lock rather than creating a competing row.
                        session.add(attempt)
                    # A missing cache entry requires mutation authority, even
                    # when recovering an existing publication journal.
                    self._acquire_lease(
                        session,
                        kind="content",
                        resource_id=expected.sha256,
                        owner_id=attempt.id if current is None else current.id,
                        operation="content_publish",
                    )
                    session.commit()
                    if isinstance(content, AssetDescriptor):
                        with content.open() as asset_stream:
                            item = self.content_store.put(
                                asset_stream, sha256=expected.sha256, size_bytes=expected.size_bytes
                            )
                    else:
                        item = self.content_store.put(content, sha256=expected.sha256, size_bytes=expected.size_bytes)
                if owns_publication:
                    persisted = session.get(LabContentPublicationAttempt, attempt.id)
                    if persisted is None:
                        raise PackageImportError("content publication reservation disappeared")
                    persisted.state = "published"
                    persisted.updated_at = datetime.now(UTC)
                    session.commit()
                stored_row = session.get(LabContentObject, {"sha256": item.sha256, "size_bytes": item.size_bytes})
                if stored_row is None:
                    session.add(
                        LabContentObject(
                            sha256=item.sha256,
                            size_bytes=item.size_bytes,
                            path=str(item.path),
                            filesystem_fingerprint=self._content_fingerprint(item.path),
                            last_verified_at=datetime.now(UTC),
                            last_used_at=datetime.now(UTC),
                        )
                    )
                session.commit()
            except ContentStoreError as exc:
                session.rollback()
                failed = session.get(LabContentPublicationAttempt, attempt.id)
                if failed is not None:
                    failed.state = "failed"
                    failed.error_summary = str(exc)[:1000]
                    failed.updated_at = datetime.now(UTC)
                    self._release_lease(session, kind="content", resource_id=failed.sha256, owner_id=failed.id)
                    session.commit()
                raise PackageImportError(failure_message(f"could not cache embedded asset: {asset_path}", exc)) from exc
            stored.append(item)
            by_digest[item.sha256] = item
        return stored, by_digest

    def _identity_lock_path(self, package_id: str, version: str) -> Path:
        digest = hashlib.sha256(f"{package_id}@{version}".encode()).hexdigest()
        return self._lock_root / f"identity-{digest}.lock"

    def _archive_sweep_lock_path(self) -> Path:
        """Serialize final-archive publication with unclaimed-archive scans."""
        return self._lock_root / "archive-sweep.lock"

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @contextmanager
    def _archive_publication_lock(self, package_id: str, version: str):
        with _exclusive_package_lock(self._archive_sweep_lock_path()):
            with _exclusive_package_lock(self._identity_lock_path(package_id, version)):
                yield

    def _resource_lock_path(self, kind: str, resource_id: str) -> Path:
        digest = hashlib.sha256(f"{kind}:{resource_id}".encode()).hexdigest()
        return self._lock_root / f"{kind}-{digest}.lock"

    def try_prepare_package_lock(self, record_id: str) -> AbstractContextManager[PreparePackageLock]:
        """Nonblocking invocation custody shared with Prepare deletion/replay."""
        return try_package_lock(self._resource_lock_path("prepare-package", record_id), record_id)

    @contextmanager
    def content_lock(self, sha256: str):
        """Serialize package resolution with content publication and GC.

        The resolver itself protects the content-store inode.  This second,
        package-owned lock is the lock used by import reconciliation and GC,
        so the database ownership update cannot race a quarantine sweep.
        """

        with _exclusive_package_lock(self._resource_lock_path("content", sha256)):
            yield

    @contextmanager
    def _inspection_lock(self, inspection_id: str):
        with _exclusive_package_lock(self._resource_lock_path("inspection", inspection_id)):
            yield

    def _acquire_lease(
        self,
        session: Session,
        *,
        kind: str,
        resource_id: str,
        owner_id: str,
        operation: str,
        ttl_seconds: int = 60 * 60,
    ) -> None:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds < 1:
            raise ValueError("lease duration must be a positive integer")
        now = datetime.now(UTC).replace(tzinfo=None)
        live = session.scalar(
            select(LabStorageLease).where(
                LabStorageLease.resource_kind == kind,
                LabStorageLease.resource_id == resource_id,
                LabStorageLease.owner_id != owner_id,
                LabStorageLease.expires_at > now,
            )
        )
        if live is not None:
            raise PackageDeletionConflictError(f"{kind} is leased by an active operation")
        if (
            session.scalar(
                select(LabStorageLease.id).where(
                    LabStorageLease.resource_kind == kind,
                    LabStorageLease.resource_id == resource_id,
                    LabStorageLease.owner_id == owner_id,
                )
            )
            is None
        ):
            session.add(
                LabStorageLease(
                    id=f"lpl-{uuid4().hex[:16]}",
                    resource_kind=kind,
                    resource_id=resource_id,
                    owner_id=owner_id,
                    operation_kind=operation,
                    expires_at=now + timedelta(seconds=ttl_seconds),
                )
            )

    @staticmethod
    def _release_lease(session: Session, *, kind: str, resource_id: str, owner_id: str) -> None:
        for lease in session.scalars(
            select(LabStorageLease).where(
                LabStorageLease.resource_kind == kind,
                LabStorageLease.resource_id == resource_id,
                LabStorageLease.owner_id == owner_id,
            )
        ):
            session.delete(lease)

    def acquire_download_lease(self, record_id: str, session: Session) -> str:
        owner_id = f"download-{uuid4().hex[:16]}"
        self._acquire_lease(
            session, kind="package", resource_id=record_id, owner_id=owner_id, operation="package_download"
        )
        session.commit()
        return owner_id

    def release_download_lease(self, record_id: str, owner_id: str) -> None:
        from ..db import session_scope

        with session_scope() as session:
            self._release_lease(session, kind="package", resource_id=record_id, owner_id=owner_id)

    @staticmethod
    def _content_fingerprint(path: Path) -> str:
        item = path.stat()
        return f"{item.st_dev}:{item.st_ino}:{item.st_size}:{item.st_mtime_ns}:{item.st_ctime_ns}"

    def _record_asset_ownership(
        self, session: Session, record: LabPackage, inspection: ArchiveInspection, cached: dict[str, StoredAsset]
    ) -> None:
        payloads = {payload.path: payload for payload in inspection.manifest.payloads}
        boot_layers = {
            layer.sha256: layer for vm in inspection.definition.vms if (layer := vm.effective_boot_layer) is not None
        }
        for path, payload in payloads.items():
            existing = session.scalar(
                select(LabPackageAsset).where(
                    LabPackageAsset.package_record_id == record.id,
                    LabPackageAsset.logical_path == path,
                )
            )
            if existing is not None:
                continue
            stored = cached.get(payload.sha256)
            if (
                stored is not None
                and session.get(LabContentObject, {"sha256": stored.sha256, "size_bytes": stored.size_bytes}) is None
            ):
                session.add(
                    LabContentObject(
                        sha256=stored.sha256,
                        size_bytes=stored.size_bytes,
                        path=str(stored.path),
                        filesystem_fingerprint=self._content_fingerprint(stored.path),
                        last_verified_at=datetime.now(UTC),
                        last_used_at=datetime.now(UTC),
                    )
                )
            reference = boot_layers.get(payload.sha256)
            session.add(
                LabPackageAsset(
                    id=f"lpa-{uuid4().hex[:16]}",
                    package_record_id=record.id,
                    logical_path=path,
                    role="boot-layer" if reference is not None else "asset",
                    expected_sha256=payload.sha256,
                    expected_size_bytes=payload.size_bytes,
                    media_type=payload.media_type,
                    origin="embedded",
                    source_json="[]"
                    if reference is None
                    else json.dumps(list(reference.sources), separators=(",", ":")),
                    content_sha256=stored.sha256 if stored is not None else None,
                    content_size_bytes=stored.size_bytes if stored is not None else None,
                )
            )
        for digest, reference in boot_layers.items():
            if any(payload.sha256 == digest for payload in payloads.values()):
                continue
            path = f"assets/sha256/{digest}"
            if (
                session.scalar(
                    select(LabPackageAsset.id).where(
                        LabPackageAsset.package_record_id == record.id,
                        LabPackageAsset.logical_path == path,
                    )
                )
                is None
            ):
                session.add(
                    LabPackageAsset(
                        id=f"lpa-{uuid4().hex[:16]}",
                        package_record_id=record.id,
                        logical_path=path,
                        role="boot-layer",
                        expected_sha256=reference.sha256,
                        expected_size_bytes=reference.size_bytes,
                        media_type=reference.media_type,
                        origin="external",
                        source_json=json.dumps(list(reference.sources), separators=(",", ":")),
                    )
                )

    def _complete_content_publications(
        self, session: Session, cached: dict[str, StoredAsset], *, adoption_owner_id: str
    ) -> None:
        """Close content publication leases only after a package asset owns bytes."""
        for stored in cached.values():
            if (
                session.scalar(
                    select(LabPackageAsset.id).where(
                        LabPackageAsset.content_sha256 == stored.sha256,
                        LabPackageAsset.content_size_bytes == stored.size_bytes,
                    )
                )
                is None
            ):
                continue
            self._release_lease(session, kind="content", resource_id=stored.sha256, owner_id=adoption_owner_id)
            for attempt in session.scalars(
                select(LabContentPublicationAttempt).where(
                    LabContentPublicationAttempt.sha256 == stored.sha256,
                    LabContentPublicationAttempt.size_bytes == stored.size_bytes,
                    LabContentPublicationAttempt.state.in_(("reserved", "published")),
                )
            ):
                attempt.state = "completed"
                attempt.completed_at = datetime.now(UTC)
                attempt.updated_at = datetime.now(UTC)
                self._release_lease(session, kind="content", resource_id=attempt.sha256, owner_id=attempt.id)

    def _complete_published_attempt(
        self, attempt_id: str, session: Session, *, trust_policy: LocalTrustPolicy | None = None
    ) -> ImportedPackage:
        """Finish ownership for bytes published before a crash."""
        attempt = session.get(LabPackagePublicationAttempt, attempt_id)
        if attempt is None or attempt.state != "published":
            raise PackageImportError("package publication is not ready to complete")
        destination = Path(attempt.final_path)
        digest, _ = _digest_file(destination, max_bytes=self.archive_max_bytes)
        if digest != attempt.archive_sha256:
            raise PackageImportError("published package archive changed before completion")
        retained, signature = self.inspect(destination)
        if (retained.manifest.package_id, retained.manifest.version) != (attempt.package_id, attempt.version):
            raise PackageImportError("published package identity does not match its reservation")
        if trust_policy is not None:
            signature = verify_archive(inspection=retained, trust_policy=trust_policy)
        stored, by_digest = self._cache_embedded_assets(session, retained, adoption_owner_id=attempt.id)
        try:
            record = LabPackageRepository(session, archive_root=self.package_root).register_inspection(
                retained, archive_path=destination, signature=signature
            )
        except PackageRecordConflictError as exc:
            raise PackageImportConflictError(str(exc)) from exc
        self._record_asset_ownership(session, record, retained, by_digest)
        session.flush()
        self._complete_content_publications(session, by_digest, adoption_owner_id=attempt.id)
        completed = session.get(LabPackagePublicationAttempt, attempt_id)
        if completed is None:
            raise PackageImportError("package publication reservation disappeared")
        completed.package_record_id = record.id
        completed.state = "completed"
        completed.completed_at = datetime.now(UTC)
        completed.updated_at = datetime.now(UTC)
        self._release_lease(
            session,
            kind="package_identity",
            resource_id=f"{completed.package_id}@{completed.version}",
            owner_id=completed.id,
        )
        if completed.inspection_id is not None:
            inspection_row = self.get_staged_inspection(completed.inspection_id, session)
            inspection_row.state = "committed"
            inspection_row.updated_at = datetime.now(UTC)
            self._release_lease(session, kind="inspection", resource_id=inspection_row.id, owner_id=inspection_row.id)
        session.commit()
        return ImportedPackage(record, retained, signature, tuple(stored))

    def commit_inspection(
        self,
        inspection_id: str,
        session: Session,
        *,
        trust_policy: LocalTrustPolicy | None = None,
    ) -> ImportedPackage:
        with self._inspection_lock(inspection_id):
            return self._commit_inspection_locked(inspection_id, session, trust_policy=trust_policy)

    def _commit_inspection_locked(
        self,
        inspection_id: str,
        session: Session,
        *,
        trust_policy: LocalTrustPolicy | None = None,
    ) -> ImportedPackage:
        """Publish a previously staged inspection with a durable reservation."""
        row = self.get_staged_inspection(inspection_id, session)
        # SQLite does not preserve timezone information for ``DateTime``.
        # Store/compare UTC wall time consistently rather than mixing aware
        # Python values with naive values read back from SQLite.
        now = datetime.now(UTC).replace(tzinfo=None)
        if row.expires_at <= now or row.state == "expired":
            row.state = "expired"
            row.updated_at = now
            self._release_lease(session, kind="inspection", resource_id=row.id, owner_id=row.id)
            session.commit()
            raise PackageInspectionExpiredError("package inspection has expired")
        if row.state == "committed":
            raise PackageImportError("package inspection was already committed")
        if row.state != "staged" or row.archive_sha256 is None or row.archive_size_bytes is None:
            raise PackageImportError("package inspection is not ready to commit")
        staged_path = Path(row.staged_path)
        digest, size = _digest_file(staged_path, max_bytes=self.archive_max_bytes)
        if (digest, size) != (row.archive_sha256, row.archive_size_bytes):
            raise PackageImportError("staged package archive changed before commit")
        inspection, signature = self.inspect(staged_path)
        if trust_policy is not None:
            signature = verify_archive(inspection=inspection, trust_policy=trust_policy)
        filename = f"{inspection.manifest.package_id}-{inspection.manifest.version}-{digest}.astinuslab"
        destination = self.package_root / filename
        existing = LabPackageRepository(session, archive_root=self.package_root).get(
            inspection.manifest.package_id, inspection.manifest.version
        )
        if existing is not None:
            if existing.archive_sha256 != digest or existing.archive_size_bytes != size:
                raise PackageImportConflictError(
                    f"package {inspection.manifest.package_id}@{inspection.manifest.version} is already registered"
                )
            row.state = "committed"
            row.updated_at = datetime.now(UTC)
            self._release_lease(session, kind="inspection", resource_id=row.id, owner_id=row.id)
            session.commit()
            # A package imported before the provenance tables existed may be
            # encountered here first; attach only verifiable ownership.
            self._backfill_legacy_record(existing, session)
            # This staged copy belongs only to this inspection.  The original
            # immutable archive was already retained by the matching record.
            try:
                staged_stat = staged_path.lstat()
                if stat.S_ISREG(staged_stat.st_mode) and not stat.S_ISLNK(staged_stat.st_mode):
                    staged_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                # Reconciliation can retry a managed stale inspection; do not
                # turn an otherwise idempotent import into an inconsistent DB
                # operation merely because best-effort cleanup was interrupted.
                pass
            return ImportedPackage(existing, inspection, signature, ())
        completed_attempt = session.scalar(
            select(LabPackagePublicationAttempt).where(
                LabPackagePublicationAttempt.package_id == inspection.manifest.package_id,
                LabPackagePublicationAttempt.version == inspection.manifest.version,
                LabPackagePublicationAttempt.state == "completed",
            )
        )
        if completed_attempt is not None:
            if completed_attempt.archive_sha256 != digest:
                raise PackageImportConflictError(
                    f"package {inspection.manifest.package_id}@{inspection.manifest.version} has a finalized different archive"
                )
            raise PackageImportError("completed package publication is missing its package record; run reconciliation")
        attempt = LabPackagePublicationAttempt(
            id=f"lpp-{uuid4().hex[:16]}",
            inspection_id=row.id,
            archive_sha256=digest,
            package_id=inspection.manifest.package_id,
            version=inspection.manifest.version,
            staging_path=str(staged_path),
            final_path=str(destination),
            state="reserved",
        )
        session.add(attempt)
        self._acquire_lease(
            session,
            kind="package_identity",
            resource_id=f"{attempt.package_id}@{attempt.version}",
            owner_id=attempt.id,
            operation="package_commit",
        )
        try:
            session.commit()
        except Exception as exc:
            session.rollback()
            raise PackageImportConflictError("package publication is already reserved") from exc

        with self._archive_publication_lock(inspection.manifest.package_id, inspection.manifest.version):
            try:
                if destination.exists() or destination.is_symlink():
                    if _digest_file(destination, max_bytes=self.archive_max_bytes) != (digest, size):
                        raise PackageImportError("retained package path has an unexpected identity")
                else:
                    os.replace(staged_path, destination)
                    directory_fd = os.open(self.package_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                published_attempt = session.get(LabPackagePublicationAttempt, attempt.id)
                if published_attempt is None:
                    raise PackageImportError("package publication reservation disappeared")
                published_attempt.state = "published"
                published_attempt.updated_at = datetime.now(UTC)
                session.commit()
                return self._complete_published_attempt(attempt.id, session, trust_policy=trust_policy)
            except Exception as exc:
                session.rollback()
                failure = session.get(LabPackagePublicationAttempt, attempt.id)
                if failure is not None and failure.state != "completed":
                    # If rename succeeded but the immediately following DB
                    # write failed, the archive is owned by this durable
                    # reservation. Keep it published so reconciliation can
                    # safely complete it; never relabel owned bytes "failed".
                    try:
                        final_digest, _ = _digest_file(destination, max_bytes=self.archive_max_bytes)
                    except PackageImportError:
                        final_digest = None
                    failure.state = "published" if final_digest == digest else "failed"
                    failure.error_summary = str(exc)[:1000]
                    failure.updated_at = datetime.now(UTC)
                    if failure.state == "failed":
                        self._release_lease(
                            session,
                            kind="package_identity",
                            resource_id=f"{failure.package_id}@{failure.version}",
                            owner_id=failure.id,
                        )
                    session.commit()
                if isinstance(exc, PackageImportError):
                    raise
                raise PackageImportError(failure_message("could not commit package inspection", exc)) from exc

    def import_path(
        self,
        source: Path,
        session: Session,
        *,
        trust_policy: LocalTrustPolicy | None = None,
    ) -> ImportedPackage:
        """Compatibility wrapper for clients that do not use inspect/commit."""
        staged = self.stage_inspection(source, session, trust_policy=trust_policy)
        return self.commit_inspection(staged.id, session, trust_policy=trust_policy)

    def _backfill_legacy_record(self, record: LabPackage, session: Session) -> bool:
        """Conservatively attach provenance to a pre-journal package row."""
        if session.scalar(select(LabPackageAsset.id).where(LabPackageAsset.package_record_id == record.id)):
            return False
        try:
            archive_digest, archive_size = _digest_file(Path(record.archive_path), max_bytes=self.archive_max_bytes)
            inspection, _signature = self.inspect(Path(record.archive_path))
            if (
                (archive_digest, archive_size) != (record.archive_sha256, record.archive_size_bytes)
                or inspection.manifest.package_id != record.package_id
                or inspection.manifest.version != record.version
                or inspection.manifest_digest != record.manifest_sha256
                or canonical_bytes(inspection.manifest).decode("utf-8") != record.manifest_json
                or canonical_bytes(inspection.definition).decode("utf-8") != record.definition_json
                or inspection.definition.reproducibility != record.reproducibility
            ):
                return self._quarantine_legacy_record(
                    record, session, "legacy package record does not match retained archive"
                )
            _cached, by_digest = self._cache_embedded_assets(session, inspection, adoption_owner_id=record.id)
            self._record_asset_ownership(session, record, inspection, by_digest)
            session.flush()
            self._complete_content_publications(session, by_digest, adoption_owner_id=record.id)
            if (
                session.scalar(
                    select(LabPackagePublicationAttempt.id).where(
                        LabPackagePublicationAttempt.package_id == record.package_id,
                        LabPackagePublicationAttempt.version == record.version,
                        LabPackagePublicationAttempt.state == "completed",
                    )
                )
                is None
            ):
                session.add(
                    LabPackagePublicationAttempt(
                        id=f"lpp-legacy-{uuid4().hex[:12]}",
                        package_record_id=record.id,
                        package_id=record.package_id,
                        version=record.version,
                        archive_sha256=record.archive_sha256,
                        staging_path=record.archive_path,
                        final_path=record.archive_path,
                        state="completed",
                        completed_at=datetime.now(UTC),
                    )
                )
            session.commit()
            return True
        except (PackageImportError, ContentStoreError, OSError, ValueError):
            session.rollback()
            return False

    @staticmethod
    def _quarantine_legacy_record(record: LabPackage, session: Session, reason: str) -> bool:
        """Retain ambiguous historical bytes but remove a reproducibility claim."""
        session.execute(update(LabPackage).where(LabPackage.id == record.id).values(reproducibility="unlocked"))
        completed = session.scalar(
            select(LabPackagePublicationAttempt).where(
                LabPackagePublicationAttempt.package_record_id == record.id,
                LabPackagePublicationAttempt.state == "completed",
            )
        )
        if completed is not None:
            completed.error_summary = reason
            completed.updated_at = datetime.now(UTC)
        session.commit()
        return False

    def _reconcile_content_publications(self, session: Session) -> int:
        """Finish owned content or quarantine a crashed, unowned publication."""
        reconciled = 0
        attempts = list(
            session.scalars(
                select(LabContentPublicationAttempt).where(
                    LabContentPublicationAttempt.state.in_(("reserved", "published"))
                )
            )
        )
        for attempt in attempts:
            try:
                with _exclusive_package_lock(self._resource_lock_path("content", attempt.sha256)):
                    path = self.content_store.path_for(attempt.sha256)
                    if Path(attempt.final_path).resolve(strict=False) != path.resolve(strict=False):
                        attempt.state = "quarantined"
                        attempt.error_summary = "content publication path is not the managed digest path"
                        attempt.updated_at = datetime.now(UTC)
                        self._release_lease(session, kind="content", resource_id=attempt.sha256, owner_id=attempt.id)
                        session.commit()
                        continue
                    if not self.content_store.has(attempt.sha256, size_bytes=attempt.size_bytes):
                        attempt.state = "failed"
                        attempt.error_summary = "content bytes were not published"
                        attempt.updated_at = datetime.now(UTC)
                        self._release_lease(session, kind="content", resource_id=attempt.sha256, owner_id=attempt.id)
                        session.commit()
                        continue
                    has_package_owner = (
                        session.scalar(
                            select(LabPackageAsset.id).where(
                                LabPackageAsset.content_sha256 == attempt.sha256,
                                LabPackageAsset.content_size_bytes == attempt.size_bytes,
                            )
                        )
                        is not None
                    )
                    now = datetime.now(UTC).replace(tzinfo=None)
                    lease_is_live = (
                        session.scalar(
                            select(LabStorageLease.id).where(
                                LabStorageLease.resource_kind == "content",
                                LabStorageLease.resource_id == attempt.sha256,
                                LabStorageLease.owner_id == attempt.id,
                                LabStorageLease.expires_at > now,
                            )
                        )
                        is not None
                    )
                    content = session.get(
                        LabContentObject, {"sha256": attempt.sha256, "size_bytes": attempt.size_bytes}
                    )
                    if has_package_owner:
                        if content is None:
                            session.add(
                                LabContentObject(
                                    sha256=attempt.sha256,
                                    size_bytes=attempt.size_bytes,
                                    path=str(path),
                                    filesystem_fingerprint=self._content_fingerprint(path),
                                    last_verified_at=datetime.now(UTC),
                                    last_used_at=datetime.now(UTC),
                                )
                            )
                        attempt.state = "completed"
                        attempt.completed_at = datetime.now(UTC)
                        attempt.updated_at = datetime.now(UTC)
                        self._release_lease(session, kind="content", resource_id=attempt.sha256, owner_id=attempt.id)
                        session.commit()
                        reconciled += 1
                        continue
                    if lease_is_live:
                        if content is None:
                            session.add(
                                LabContentObject(
                                    sha256=attempt.sha256,
                                    size_bytes=attempt.size_bytes,
                                    path=str(path),
                                    filesystem_fingerprint=self._content_fingerprint(path),
                                    last_verified_at=datetime.now(UTC),
                                    last_used_at=datetime.now(UTC),
                                )
                            )
                        session.commit()
                        continue
                    # No package asset and no live publisher lease proves this
                    # was interrupted between content publication and package
                    # ownership. Remove the DB-only row, then let the locked
                    # unclaimed-content sweep quarantine the bytes.
                    if content is not None:
                        session.delete(content)
                    attempt.state = "quarantined"
                    attempt.error_summary = "content publication has no package owner"
                    attempt.updated_at = datetime.now(UTC)
                    self._release_lease(session, kind="content", resource_id=attempt.sha256, owner_id=attempt.id)
                    session.commit()
            except (ContentStoreError, OSError, ValueError):
                session.rollback()
                failed = session.get(LabContentPublicationAttempt, attempt.id)
                if failed is not None:
                    failed.state = "quarantined"
                    failed.error_summary = "content publication could not be verified"
                    failed.updated_at = datetime.now(UTC)
                    self._release_lease(session, kind="content", resource_id=failed.sha256, owner_id=failed.id)
                    session.commit()
        return reconciled

    def reconcile(self, session: Session) -> dict[str, int]:
        """Recover publication journal entries after process interruption.

        A ``published`` row names bytes that survived ``os.replace`` but not
        the package/content transaction; finish it.  A ``reserved`` row never
        made bytes visible, so release the canonical identity while preserving
        its staged inspection for an explicit retry.  Corrupt/unexpected
        published files are quarantined rather than silently adopted.
        """
        report = {"completed_publications": 0, "released_reservations": 0, "quarantined_publications": 0}
        attempts = list(
            session.scalars(
                select(LabPackagePublicationAttempt).where(
                    LabPackagePublicationAttempt.state.in_(
                        ("reserved", "published", "failed", "cleanup_incomplete", "completed")
                    )
                )
            )
        )
        for attempt in attempts:
            with self._archive_publication_lock(attempt.package_id, attempt.version):
                destination = Path(attempt.final_path)
                if attempt.state == "completed":
                    # This is the permanent identity ledger. Package deletion
                    # intentionally nulls its local record reference; it must
                    # never reopen a completed identity for reconciliation.
                    continue
                try:
                    has_owned_destination = (
                        destination.exists()
                        and _digest_file(destination, max_bytes=self.archive_max_bytes)[0] == attempt.archive_sha256
                    )
                except PackageImportError:
                    has_owned_destination = False
                if attempt.state in {"failed", "cleanup_incomplete"}:
                    if not has_owned_destination:
                        try:
                            metadata = destination.lstat()
                            if stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                                quarantine = self.package_root / f".astinuslab-quarantine-{attempt.id}.astinuslab"
                                if not quarantine.exists():
                                    os.replace(destination, quarantine)
                                    self._fsync_directory(quarantine.parent)
                                    report["quarantined_publications"] += 1
                        except FileNotFoundError:
                            pass
                        except OSError:
                            attempt.state = "cleanup_incomplete"
                            attempt.error_summary = "could not quarantine failed package archive"
                            attempt.updated_at = datetime.now(UTC)
                            session.commit()
                            continue
                        self._release_lease(
                            session,
                            kind="package_identity",
                            resource_id=f"{attempt.package_id}@{attempt.version}",
                            owner_id=attempt.id,
                        )
                        session.commit()
                        continue
                    attempt.state = "published"
                    attempt.updated_at = datetime.now(UTC)
                    session.commit()
                if attempt.state == "reserved":
                    if has_owned_destination:
                        attempt.state = "published"
                        attempt.updated_at = datetime.now(UTC)
                        session.commit()
                    else:
                        attempt.state = "failed"
                        attempt.error_summary = "interrupted before package archive publication"
                        attempt.updated_at = datetime.now(UTC)
                        self._release_lease(
                            session,
                            kind="package_identity",
                            resource_id=f"{attempt.package_id}@{attempt.version}",
                            owner_id=attempt.id,
                        )
                        session.commit()
                        report["released_reservations"] += 1
                        continue
                try:
                    self._complete_published_attempt(attempt.id, session)
                    report["completed_publications"] += 1
                except (PackageImportError, PackageRecordConflictError, ContentStoreError, OSError, ValueError) as exc:
                    session.rollback()
                    failed = session.get(LabPackagePublicationAttempt, attempt.id)
                    if failed is None:
                        continue
                    try:
                        if destination.exists() and not destination.is_symlink():
                            quarantine = self.package_root / f".astinuslab-quarantine-{failed.id}.astinuslab"
                            if not quarantine.exists():
                                os.replace(destination, quarantine)
                                self._fsync_directory(quarantine.parent)
                                report["quarantined_publications"] += 1
                        failed.state = "failed"
                        failed.error_summary = str(exc)[:1000]
                        failed.updated_at = datetime.now(UTC)
                        self._release_lease(
                            session,
                            kind="package_identity",
                            resource_id=f"{failed.package_id}@{failed.version}",
                            owner_id=failed.id,
                        )
                        session.commit()
                    except OSError:
                        session.rollback()
                        failed = session.get(LabPackagePublicationAttempt, attempt.id)
                        if failed is not None:
                            failed.state = "cleanup_incomplete"
                            failed.error_summary = str(exc)[:1000]
                            failed.updated_at = datetime.now(UTC)
                            session.commit()
        replayed_tombstones = 0
        tombstone_ids = list(
            session.scalars(
                select(LabPackageTombstone.id).where(LabPackageTombstone.state.in_(("pending", "cleanup_incomplete")))
            )
        )
        session.commit()
        for tombstone_id in tombstone_ids:
            tombstone = session.get(LabPackageTombstone, tombstone_id)
            if tombstone is not None and self._replay_tombstone(tombstone, session):
                replayed_tombstones += 1
        report["replayed_tombstones"] = replayed_tombstones
        report["completed_content_publications"] = self._reconcile_content_publications(session)
        # End the read transaction before scanning paths. Publishers commit
        # their reservations before acquiring the shared sweep lock, so this
        # gives the scan a current ledger snapshot and prevents a concurrent
        # final rename until the scan has made its decision.
        session.commit()
        quarantined_archives = 0
        with _exclusive_package_lock(self._archive_sweep_lock_path()):
            claimed_archives = (
                {Path(path).resolve(strict=False) for path in session.scalars(select(LabPackage.archive_path))}
                | {
                    Path(path).resolve(strict=False)
                    for path in session.scalars(
                        select(LabPackagePublicationAttempt.final_path).where(
                            LabPackagePublicationAttempt.state.in_(("reserved", "published", "completed"))
                        )
                    )
                }
                | {
                    Path(path).resolve(strict=False)
                    for path in session.scalars(
                        select(LabPackageInspection.staged_path).where(
                            LabPackageInspection.state.in_(("reserved", "staged"))
                        )
                    )
                }
            )
            for archive in self.package_root.glob("*.astinuslab"):
                # Quarantine is deliberately durable evidence for manual
                # inspection. It is not an import candidate and must never be
                # repeatedly renamed by later reconciliation passes.
                if archive.name.startswith(".astinuslab-quarantine-"):
                    continue
                try:
                    if archive.resolve(strict=True) in claimed_archives:
                        continue
                    metadata = archive.lstat()
                    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                        continue
                    quarantine = self.package_root / f".astinuslab-quarantine-unclaimed-{archive.name}"
                    if not quarantine.exists():
                        os.replace(archive, quarantine)
                        self._fsync_directory(quarantine.parent)
                        quarantined_archives += 1
                except OSError:
                    continue
        report["quarantined_archives"] = quarantined_archives
        # Start content checks from a fresh SQLite read transaction; each
        # candidate is then rechecked under its exact digest lock.
        session.commit()
        quarantined_content = 0
        content_root = self.content_store.root / "sha256"
        quarantine_root = self.content_store.root / "quarantine"
        try:
            quarantine_root.mkdir(mode=0o700, exist_ok=True)
            secure_directory(quarantine_root)
            for candidate in content_root.glob("*/*"):
                if len(candidate.name) != 64:
                    continue
                try:
                    expected = self.content_store.path_for(candidate.name)
                except ContentStoreError:
                    continue
                if candidate.resolve(strict=False) != expected.resolve(strict=False):
                    continue
                with _exclusive_package_lock(self._resource_lock_path("content", candidate.name)):
                    # Re-read claims under the same digest lock used by content
                    # publication/adoption, so a concurrent import cannot lose
                    # its newly-published content between a stale scan and move.
                    if (
                        session.scalar(
                            select(LabContentObject.sha256).where(
                                LabContentObject.sha256 == candidate.name,
                                LabContentObject.path == str(candidate),
                            )
                        )
                        is not None
                    ):
                        continue
                    if (
                        session.scalar(
                            select(LabContentPublicationAttempt.id).where(
                                LabContentPublicationAttempt.final_path == str(candidate),
                                LabContentPublicationAttempt.state.in_(("reserved", "published")),
                            )
                        )
                        is not None
                    ):
                        continue
                    metadata = candidate.lstat()
                    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                        continue
                    destination = quarantine_root / f"unclaimed-{candidate.name}"
                    if not destination.exists():
                        os.replace(candidate, destination)
                        self._fsync_directory(destination.parent)
                        quarantined_content += 1
        except OSError:
            pass
        report["quarantined_content"] = quarantined_content
        now = datetime.now(UTC).replace(tzinfo=None)
        expired_leases = list(session.scalars(select(LabStorageLease).where(LabStorageLease.expires_at <= now)))
        for lease in expired_leases:
            session.delete(lease)
        if expired_leases:
            session.commit()
        report["expired_leases"] = len(expired_leases)
        report["legacy_backfilled"] = sum(
            1 for record in list(session.scalars(select(LabPackage))) if self._backfill_legacy_record(record, session)
        )
        return report

    def _delete_terminal_instances(self, record_id: str, session: Session) -> None:
        """Detach terminal runtime intent before deleting an immutable package.

        Active instances are rejected by the caller.  Explicitly deleting the
        terminal rows makes package deletion work on both upgraded SQLite DBs
        and fresh DBs with FK enforcement enabled; FK cascades remove action
        rows rather than relying on a disabled SQLite default.
        """
        terminal = session.scalars(select(LabInstance).where(LabInstance.package_record_id == record_id)).all()
        for instance in terminal:
            session.delete(instance)
        session.flush()

    def _check_delete_protections(
        self,
        record_id: str,
        session: Session,
        *,
        package_lock: PreparePackageLock,
        tombstone_id: str | None = None,
    ) -> None:
        """Inspect current custody after reserving the writer, under package lock."""
        package_lock.require(record_id)
        if session.scalar(
            select(LabInstance.id).where(
                LabInstance.package_record_id == record_id,
                LabInstance.state != "destroyed",
            )
        ):
            raise PackageDeletionConflictError("lab package has an active instance")
        if session.scalar(select(PlaygroundSession.id).where(PlaygroundSession.lab_package_record_id == record_id)):
            raise PackageDeletionConflictError("lab package is associated with a Playground session")
        attempts = list(
            session.scalars(
                select(LabPrepareAttempt)
                .where(
                    LabPrepareAttempt.package_record_id == record_id,
                    LabPrepareAttempt.state.in_(("reserved", "running")),
                )
                .execution_options(populate_existing=True)
            )
        )
        # A free package lock proves running intent is abandoned. A fresh
        # reservation still has lease custody until it expires or is cancelled.
        abandoned = {attempt.id for attempt in attempts if attempt.state == "running"}
        now = datetime.now(UTC).replace(tzinfo=None)
        leases = session.scalars(
            select(LabStorageLease).where(
                LabStorageLease.resource_kind == "package",
                LabStorageLease.resource_id == record_id,
                LabStorageLease.expires_at > now,
            )
        )
        if any(lease.owner_id != tombstone_id and lease.owner_id not in abandoned for lease in leases):
            raise PackageDeletionConflictError("lab package is leased by an active operation")
        from .prepare_service import _terminalize_locked

        for attempt in attempts:
            attempt.cancel_requested = True
            _terminalize_locked(session, attempt, package_lock=package_lock, state="cancelled")

    def _replay_tombstone(self, tombstone: LabPackageTombstone, session: Session) -> bool:
        """Replay using package identity, also for startup/GC callers."""
        tombstone_id, record_id = tombstone.id, tombstone.package_record_id
        end_read_transaction(session)
        try:
            with self.try_prepare_package_lock(record_id) as lock:
                return self._replay_tombstone_locked(tombstone_id, session, package_lock=lock)
        except (PreparePackageBusy, PackageDeletionConflictError):
            session.rollback()
            return False

    def _replay_tombstone_locked(
        self,
        tombstone_id: str,
        session: Session,
        *,
        package_lock: PreparePackageLock,
    ) -> bool:
        reserve_sqlite_write(session)
        tombstone = session.get(LabPackageTombstone, tombstone_id, populate_existing=True)
        if tombstone is None or tombstone.state not in {"pending", "cleanup_incomplete"}:
            session.rollback()
            return False
        record_id = tombstone.package_record_id
        package_lock.require(record_id)
        self._check_delete_protections(record_id, session, package_lock=package_lock, tombstone_id=tombstone.id)
        archive, digest = Path(tombstone.archive_path), tombstone.archive_sha256
        # The durable tombstone excludes new Prepare/launch intent. Hashing and
        # unlinking hold file custody, never SQLite's single writer.
        session.commit()
        try:
            archived = archive.lstat()
            if stat.S_ISLNK(archived.st_mode) or not stat.S_ISREG(archived.st_mode):
                raise OSError("archive is not a managed regular file")
            if _digest_file(archive, max_bytes=self.archive_max_bytes)[0] != digest:
                raise OSError("archive identity no longer matches deletion tombstone")
            archive.unlink()
            self._fsync_directory(archive.parent)
        except FileNotFoundError:
            pass
        except OSError:
            reserve_sqlite_write(session)
            tombstone = session.get(LabPackageTombstone, tombstone_id, populate_existing=True)
            if tombstone is None:
                raise PackageImportError("package tombstone disappeared during deletion") from None
            tombstone.state = "cleanup_incomplete"
            session.commit()
            return False
        reserve_sqlite_write(session)
        package_lock.require(record_id)
        record = session.get(LabPackage, record_id, populate_existing=True)
        if record is not None:
            self._delete_terminal_instances(record.id, session)
            session.delete(record)
        tombstone = session.get(LabPackageTombstone, tombstone_id, populate_existing=True)
        if tombstone is None:
            raise PackageImportError("package tombstone disappeared during deletion") from None
        tombstone.state = "completed"
        tombstone.completed_at = datetime.now(UTC)
        self._release_lease(session, kind="package", resource_id=record_id, owner_id=tombstone_id)
        session.commit()
        return True

    def delete_package(
        self, record_id: str, session: Session, *, reason: str = "user requested package deletion"
    ) -> None:
        """Record deletion intent and retain package custody through unlink."""
        try:
            with self.try_prepare_package_lock(record_id) as lock:
                reserve_sqlite_write(session)
                record = session.get(LabPackage, record_id, populate_existing=True)
                if record is None:
                    raise PackageImportError("lab package not found")
                tombstone = session.scalar(
                    select(LabPackageTombstone)
                    .where(
                        LabPackageTombstone.package_record_id == record_id,
                        LabPackageTombstone.state.in_(("pending", "cleanup_incomplete")),
                    )
                    .execution_options(populate_existing=True)
                )
                self._check_delete_protections(
                    record_id, session, package_lock=lock, tombstone_id=tombstone.id if tombstone else None
                )
                if tombstone is None:
                    tombstone = LabPackageTombstone(
                        id=f"lpt-{uuid4().hex[:16]}",
                        package_record_id=record_id,
                        archive_path=record.archive_path,
                        archive_sha256=record.archive_sha256,
                        reason=reason,
                    )
                    session.add(tombstone)
                    session.flush()
                    self._acquire_lease(
                        session,
                        kind="package",
                        resource_id=record_id,
                        owner_id=tombstone.id,
                        operation="package_delete",
                    )
                tombstone_id = tombstone.id
                session.commit()
                if not self._replay_tombstone_locked(tombstone_id, session, package_lock=lock):
                    raise PackageImportError("could not remove retained package archive")
        except PreparePackageBusy as exc:
            raise PackageDeletionConflictError("lab package preparation is active") from exc

    def garbage_collect(self, session: Session, *, grace_seconds: int | None = None) -> dict[str, int]:
        """Reconcile expired inspections and remove unreferenced content safely."""
        recovered = self.reconcile(session)
        grace = settings.packages_gc_grace_seconds if grace_seconds is None else grace_seconds
        if isinstance(grace, bool) or not isinstance(grace, int) or grace < 0:
            raise ValueError("GC grace must be a non-negative integer")
        now = datetime.now(UTC).replace(tzinfo=None)
        expired_inspections = 0
        for inspection in list(
            session.scalars(
                select(LabPackageInspection).where(
                    LabPackageInspection.state.in_(("reserved", "staged")), LabPackageInspection.expires_at <= now
                )
            )
        ):
            with self._inspection_lock(inspection.id):
                current = session.get(LabPackageInspection, inspection.id)
                if current is None or current.state not in {"reserved", "staged"} or current.expires_at > now:
                    continue
                try:
                    path = Path(current.staged_path)
                    # These paths are derived from a durable inspection row;
                    # do not sweep arbitrary similarly named files.
                    for candidate_path in (path, path.with_name(f".{path.name}.partial")):
                        try:
                            candidate = candidate_path.lstat()
                            if stat.S_ISREG(candidate.st_mode) and not stat.S_ISLNK(candidate.st_mode):
                                candidate_path.unlink()
                                self._fsync_directory(candidate_path.parent)
                        except FileNotFoundError:
                            continue
                except FileNotFoundError:
                    pass
                except OSError:
                    continue
                current.state = "expired"
                current.updated_at = now
                self._release_lease(session, kind="inspection", resource_id=current.id, owner_id=current.id)
                expired_inspections += 1
        cutoff = now - timedelta(seconds=grace)
        removed_content = 0
        candidates = list(session.execute(select(LabContentObject.sha256, LabContentObject.size_bytes)))
        # Persist inspection expiry and finish the scan's read transaction before
        # waiting for a publisher. Each candidate gets a fresh, short transaction.
        session.commit()
        for digest, size_bytes in candidates:
            try:
                with _exclusive_package_lock(self._resource_lock_path("content", digest)):
                    end_read_transaction(session)
                    with session.begin():
                        # Export leases do not take this file lock. Reserve the DB
                        # writer before checking custody so a new lease cannot commit
                        # between the check and unlink. No hashing/streaming is done
                        # while holding the writer.
                        reserve_sqlite_write(session)
                        content = session.get(
                            LabContentObject, {"sha256": digest, "size_bytes": size_bytes}, populate_existing=True
                        )
                        if content is None or (content.last_used_at or content.created_at) > cutoff:
                            continue
                        if session.scalar(
                            select(LabPackageAsset.id).where(
                                LabPackageAsset.content_sha256 == digest,
                                LabPackageAsset.content_size_bytes == size_bytes,
                            )
                        ) or session.scalar(
                            select(LabStorageLease.id).where(
                                LabStorageLease.resource_kind == "content",
                                LabStorageLease.resource_id == digest,
                                LabStorageLease.expires_at > now,
                            )
                        ):
                            continue
                        path = Path(content.path)
                        expected = self.content_store.path_for(digest)
                        if path != expected:
                            continue
                        try:
                            candidate = path.lstat()
                            if stat.S_ISREG(candidate.st_mode) and not stat.S_ISLNK(candidate.st_mode):
                                path.unlink()
                                self._fsync_directory(path.parent)
                        except FileNotFoundError:
                            pass
                        session.delete(content)
                        removed_content += 1
            except (ContentStoreError, OSError):
                continue
        session.commit()
        return {
            "expired_inspections": expired_inspections,
            "removed_content": removed_content,
            "completed_publications": recovered["completed_publications"],
            "quarantined_publications": recovered["quarantined_publications"],
        }


def default_package_service() -> LabPackageService:
    return LabPackageService()
