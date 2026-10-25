"""A bounded, content-addressed store for portable lab assets.

The store is deliberately small in scope: it publishes verified bytes under a
SHA-256-derived path and never interprets the bytes it stores.  All writes are
staged beside the final path and atomically published only after size and
digest verification.
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import os
import re
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .storage_custody import failure_message, secure_directory, secure_file

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_MAX_ASSET_BYTES = 16 * 1024**3
READ_CHUNK_BYTES = 1024 * 1024

type AssetSource = bytes | bytearray | memoryview | BinaryIO | Path


class ContentStoreError(ValueError):
    """Raised when an asset cannot be safely verified or published."""


class InvalidCachedAssetError(ContentStoreError):
    """A regular, single-link cache entry has a definite size or digest mismatch."""


@dataclass(frozen=True, slots=True)
class StoredAsset:
    """Immutable metadata for one verified cache entry."""

    sha256: str
    size_bytes: int
    path: Path
    from_cache: bool = False

    @property
    def digest(self) -> str:
        return self.sha256


def _validate_digest(value: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ContentStoreError("invalid SHA-256 digest")
    return value


def _validate_size(value: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContentStoreError("invalid asset size")
    if value > maximum:
        raise ContentStoreError("asset is too large")
    return value


def _is_symlink_or_nonregular(path: Path, st: os.stat_result) -> None:
    if stat.S_ISLNK(st.st_mode):
        raise ContentStoreError(f"asset cache entry is a symlink: {path}")
    if not stat.S_ISREG(st.st_mode):
        raise ContentStoreError(f"asset cache entry is not a regular file: {path}")


class ContentStore:
    """Store verified assets beneath a private SHA-256 directory tree."""

    def __init__(self, root: Path, *, max_bytes: int = DEFAULT_MAX_ASSET_BYTES) -> None:
        if isinstance(root, (str, bytes)):
            root = Path(root)
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ContentStoreError("invalid store size limit")
        self.root = Path(root)
        self.max_bytes = max_bytes
        # A process-local fingerprint is only an optimization. A changed inode,
        # size, mtime, or ctime always falls back to a full SHA-256 pass.
        self._verified_fingerprints: dict[Path, tuple[int, int, int, int, int]] = {}
        try:
            self._ensure_directory(self.root, "content store root")
            self._digest_root = self.root / "sha256"
            self._ensure_directory(self._digest_root, "content store directory")
        except ContentStoreError:
            raise
        except OSError as exc:
            raise ContentStoreError(failure_message("could not create content store", exc)) from exc

    def _path(self, digest: str) -> Path:
        digest = _validate_digest(digest)
        prefix = self._digest_root / digest[:2]
        try:
            self._ensure_directory(prefix, "content store bucket")
        except OSError as exc:
            raise ContentStoreError(failure_message("could not create content store bucket", exc)) from exc
        return prefix / digest

    def path_for(self, digest: str) -> Path:
        """Return the safe deterministic cache path for a digest."""

        return self._path(digest)

    def partial_path_for(self, digest: str) -> Path:
        """Return the private resumable-transfer path for a digest."""

        return self._path(digest).with_name(f".{digest}.partial")

    def partial_size(self, sha256: str, *, size_bytes: int) -> int:
        """Return a safe partial prefix length, or zero when none exists."""

        digest = _validate_digest(sha256)
        expected_size = _validate_size(size_bytes, self.max_bytes)
        path = self.partial_path_for(digest)
        try:
            inspected = path.lstat()
        except FileNotFoundError:
            return 0
        except OSError as exc:
            raise ContentStoreError(failure_message("could not inspect partial asset", exc)) from exc
        _is_symlink_or_nonregular(path, inspected)
        if inspected.st_size > expected_size:
            self._remove_regular(path, inspected)
            return 0
        return inspected.st_size

    @staticmethod
    def _ensure_directory(path: Path, label: str) -> None:
        try:
            ContentStore._reject_symlink_components(path)
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            inspected = path.lstat()
        except OSError as exc:
            raise ContentStoreError(failure_message(f"could not create {label}", exc)) from exc
        if stat.S_ISLNK(inspected.st_mode):
            raise ContentStoreError(f"{label} must not be a symlink")
        if not stat.S_ISDIR(inspected.st_mode):
            raise ContentStoreError(f"{label} is not a directory")
        try:
            secure_directory(path)
        except OSError as exc:
            raise ContentStoreError(failure_message(f"could not secure {label}", exc)) from exc

    @staticmethod
    def _reject_symlink_components(path: Path) -> None:
        absolute = path.absolute()
        current = Path(absolute.anchor)
        for component in absolute.parts[1:]:
            current /= component
            try:
                inspected = current.lstat()
            except FileNotFoundError:
                break
            except OSError as exc:
                raise ContentStoreError(failure_message("could not inspect content store path", exc)) from exc
            if stat.S_ISLNK(inspected.st_mode):
                raise ContentStoreError(f"content store path component is a symlink: {current}")

    @staticmethod
    def _open_regular(path: Path, *, flags: int = os.O_RDONLY) -> BinaryIO:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags | nofollow | getattr(os, "O_NONBLOCK", 0))
        except OSError as exc:
            if exc.errno in {getattr(os, "ELOOP", 40), 40}:
                raise ContentStoreError(f"asset cache entry is a symlink: {path}") from exc
            raise ContentStoreError(failure_message(f"could not open asset: {path}", exc)) from exc
        try:
            result = os.fstat(descriptor)
            _is_symlink_or_nonregular(path, result)
            return os.fdopen(descriptor, "rb")
        except ContentStoreError:
            os.close(descriptor)
            raise
        except OSError as exc:
            os.close(descriptor)
            raise ContentStoreError(failure_message(f"could not inspect asset: {path}", exc)) from exc

    def _verify_existing(self, path: Path, digest: str, size_bytes: int | None, *, remove_invalid: bool) -> bool:
        try:
            link_stat = path.lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ContentStoreError(failure_message(f"could not inspect asset cache entry: {path}", exc)) from exc
        _is_symlink_or_nonregular(path, link_stat)
        if link_stat.st_size > self.max_bytes:
            if remove_invalid:
                self._remove_regular(path, link_stat)
            return False
        if self._verified_fingerprints.get(path) == self._fingerprint(link_stat):
            return size_bytes is None or link_stat.st_size == size_bytes
        try:
            with self._open_regular(path) as handle:
                actual_size, actual_digest = self._stream_digest(handle, limit=self.max_bytes)
        except ContentStoreError:
            if remove_invalid:
                self._remove_regular(path, link_stat)
            raise
        if actual_digest != digest:
            if remove_invalid:
                self._remove_regular(path, link_stat)
            return False
        if size_bytes is not None and actual_size != size_bytes:
            # The named digest is valid; only this caller's reference disagrees.
            # Retain shared bytes even when they are unavailable to this caller.
            return False
        self._verified_fingerprints[path] = self._fingerprint(link_stat)
        return True

    @staticmethod
    def _fingerprint(inspected: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            inspected.st_dev,
            inspected.st_ino,
            inspected.st_size,
            inspected.st_mtime_ns,
            inspected.st_ctime_ns,
        )

    @staticmethod
    def _remove_regular(path: Path, expected_stat: os.stat_result) -> None:
        try:
            current = path.lstat()
            if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == (
                expected_stat.st_dev,
                expected_stat.st_ino,
            ):
                path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ContentStoreError(
                failure_message(f"could not remove invalid asset cache entry: {path}", exc)
            ) from exc

    def has(self, sha256: str, *, size_bytes: int | None = None) -> bool:
        """Return whether a cache entry is verified, removing stale regular files."""

        digest = _validate_digest(sha256)
        if size_bytes is not None:
            _validate_size(size_bytes, self.max_bytes)
        path = self._path(digest)
        return self._verify_existing(path, digest, size_bytes, remove_invalid=True)

    @contextmanager
    def locked_cached_asset(self, sha256: str, *, size_bytes: int) -> Iterator[StoredAsset | None]:
        """Verify immutable reuse without deleting or replacing any cached bytes.

        Hold the publication lock until the caller has committed its retention
        claim. Unlike ``has``/``open``, an invalid entry is preserved and refused;
        another operation may still own it. Only an absent entry yields None.
        """
        digest = _validate_digest(sha256)
        expected_size = _validate_size(size_bytes, self.max_bytes)
        path = self._path(digest)
        with self._digest_lock(path.parent, digest):
            yield self._inspect_cached_asset(path, digest, expected_size)

    def _inspect_cached_asset(self, path: Path, digest: str, expected_size: int) -> StoredAsset | None:
        try:
            named = path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ContentStoreError(failure_message("could not inspect cached asset", exc)) from exc
        _is_symlink_or_nonregular(path, named)
        if named.st_nlink != 1:
            raise ContentStoreError("cached asset must be a single-link regular file")
        if named.st_size != expected_size:
            raise InvalidCachedAssetError("asset cache entry size mismatch")
        try:
            with self._open_regular(path) as handle:
                opened = os.fstat(handle.fileno())
                if opened.st_nlink != 1 or self._fingerprint(opened) != self._fingerprint(named):
                    raise ContentStoreError("cached asset changed before verification")
                size, actual_digest = self._stream_digest(handle, limit=self.max_bytes)
                after = os.fstat(handle.fileno())
                current = path.lstat()
                if (
                    after.st_nlink != 1
                    or current.st_nlink != 1
                    or self._fingerprint(after) != self._fingerprint(opened)
                    or self._fingerprint(current) != self._fingerprint(opened)
                ):
                    raise ContentStoreError("cached asset changed during verification")
            if size != expected_size or actual_digest != digest:
                raise InvalidCachedAssetError("asset cache entry digest or size mismatch")
        except OSError as exc:
            raise ContentStoreError(failure_message("cached asset changed or could not be verified", exc)) from exc
        return StoredAsset(digest, expected_size, path, from_cache=True)

    def open(self, sha256: str, *, size_bytes: int | None = None) -> BinaryIO:
        """Open a verified entry for bounded consumption by the caller."""

        digest = _validate_digest(sha256)
        if size_bytes is not None:
            _validate_size(size_bytes, self.max_bytes)
        path = self._path(digest)
        try:
            link_stat = path.lstat()
        except FileNotFoundError as exc:
            raise ContentStoreError("asset is not present in content store") from exc
        _is_symlink_or_nonregular(path, link_stat)
        if link_stat.st_size > self.max_bytes:
            self._remove_regular(path, link_stat)
            raise ContentStoreError("asset cache entry is too large")
        handle = self._open_regular(path)
        try:
            opened = os.fstat(handle.fileno())
            if self._verified_fingerprints.get(path) == self._fingerprint(opened):
                actual_size = opened.st_size
            else:
                actual_size, actual_digest = self._stream_digest(handle, limit=self.max_bytes)
                if actual_digest != digest:
                    raise ContentStoreError("asset cache entry digest mismatch")
            self._verified_fingerprints[path] = self._fingerprint(opened)
            handle.seek(0)
        except ContentStoreError:
            self._remove_regular(path, link_stat)
            handle.close()
            raise
        except OSError as exc:
            handle.close()
            raise ContentStoreError(failure_message("could not read asset cache entry", exc)) from exc
        if size_bytes is not None and actual_size != size_bytes:
            # The digest was verified: this is inconsistent reference metadata,
            # not corrupt cache content. Close this read without unlinking it.
            handle.close()
            raise ContentStoreError("asset cache entry size mismatch")
        return handle

    def get(self, sha256: str, *, size_bytes: int | None = None) -> bytes:
        """Read one verified entry; the caller controls when bounded bytes are materialised."""

        with self.open(sha256, size_bytes=size_bytes) as handle:
            # A size-limited read preallocates its requested size, even for a
            # tiny file. Materialise only the bytes present, with bounded reads
            # and a ceiling check in case the file grows after verification.
            data = bytearray()
            while chunk := handle.read(min(READ_CHUNK_BYTES, self.max_bytes - len(data) + 1)):
                if len(data) + len(chunk) > self.max_bytes:
                    raise ContentStoreError("asset is too large")
                data.extend(chunk)
            return bytes(data)

    @staticmethod
    def _stream_digest(handle: BinaryIO, *, limit: int | None = None) -> tuple[int, str]:
        digest = hashlib.sha256()
        total = 0
        while True:
            try:
                chunk = handle.read(READ_CHUNK_BYTES)
            except (OSError, ValueError) as exc:
                raise ContentStoreError(failure_message("could not read asset stream", exc)) from exc
            if chunk is None:
                raise ContentStoreError("asset stream returned no bytes")
            if not isinstance(chunk, bytes):
                try:
                    chunk = bytes(chunk)
                except (TypeError, ValueError) as exc:
                    raise ContentStoreError("asset stream returned invalid bytes") from exc
            if not chunk:
                break
            total += len(chunk)
            if limit is not None and total > limit:
                raise ContentStoreError("asset is too large")
            digest.update(chunk)
        return total, digest.hexdigest()

    def put(
        self,
        source: AssetSource,
        *,
        sha256: str,
        size_bytes: int,
        reuse_existing: bool = True,
    ) -> StoredAsset:
        """Verify and atomically publish bytes from a stream or regular file."""

        digest = _validate_digest(sha256)
        expected_size = _validate_size(size_bytes, self.max_bytes)
        destination = self._path(digest)
        with self._digest_lock(destination.parent, digest):
            return self._put_unlocked(source, digest, expected_size, destination, reuse_existing)

    def put_resumable(
        self,
        source: AssetSource,
        *,
        sha256: str,
        size_bytes: int,
        offset: int | None = None,
        reset: bool = False,
    ) -> StoredAsset | None:
        """Append a verified transfer prefix and publish once it is complete.

        A source/read failure intentionally leaves only the bounded partial
        prefix under a private, digest-derived name.  The final content path is
        never visible until the complete size and SHA-256 digest verify.
        """

        digest = _validate_digest(sha256)
        expected_size = _validate_size(size_bytes, self.max_bytes)
        destination = self._path(digest)
        partial = self.partial_path_for(digest)
        with self._digest_lock(destination.parent, digest):
            if self._verify_existing(destination, digest, expected_size, remove_invalid=True):
                return StoredAsset(digest, expected_size, destination, from_cache=True)
            try:
                existing_stat = partial.lstat()
            except FileNotFoundError:
                existing_size = 0
                existing_stat = None
            except OSError as exc:
                raise ContentStoreError(failure_message("could not inspect partial asset", exc)) from exc
            if existing_stat is not None:
                _is_symlink_or_nonregular(partial, existing_stat)
                existing_size = existing_stat.st_size
                if existing_size > expected_size:
                    self._remove_regular(partial, existing_stat)
                    existing_size = 0
                elif reset:
                    self._remove_regular(partial, existing_stat)
                    existing_size = 0
            elif reset and offset not in {None, 0}:
                raise ContentStoreError("resumable transfer reset requires a zero offset")
            requested_offset = existing_size if offset is None else offset
            if (
                isinstance(requested_offset, bool)
                or not isinstance(requested_offset, int)
                or requested_offset < 0
                or requested_offset > expected_size
                or requested_offset != existing_size
            ):
                raise ContentStoreError("resumable transfer offset does not match partial content")

            source_handle: BinaryIO | None = None
            should_close = False
            descriptor: int | None = None
            try:
                if isinstance(source, (bytes, bytearray, memoryview)):
                    source_handle = _BytesReader(bytes(source))
                    should_close = True
                elif isinstance(source, Path):
                    source_handle = self._open_source_path(source)
                    should_close = True
                elif hasattr(source, "read"):
                    source_handle = source
                else:
                    raise TypeError("asset source must be bytes, a path, or a binary stream")

                flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                flags |= os.O_APPEND if requested_offset else 0
                descriptor = os.open(partial, flags, 0o600)
                secure_file(descriptor, partial)
                inspected = os.fstat(descriptor)
                if not stat.S_ISREG(inspected.st_mode) or inspected.st_size != existing_size:
                    raise ContentStoreError("partial asset changed while opening")
                with os.fdopen(descriptor, "ab" if requested_offset else "wb") as target:
                    descriptor = None
                    total = existing_size
                    while True:
                        try:
                            chunk = source_handle.read(min(READ_CHUNK_BYTES, expected_size - total + 1))
                        except (OSError, ValueError) as exc:
                            raise ContentStoreError(failure_message("could not read asset source", exc)) from exc
                        if chunk is None:
                            raise ContentStoreError("asset source returned no bytes")
                        if not isinstance(chunk, bytes):
                            try:
                                chunk = bytes(chunk)
                            except (TypeError, ValueError) as exc:
                                raise ContentStoreError("asset source returned invalid bytes") from exc
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > expected_size:
                            raise ContentStoreError("asset is too large")
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())

                if total != expected_size:
                    return None
                with self._open_regular(partial) as handle:
                    actual_size, actual_digest = self._stream_digest(handle, limit=self.max_bytes)
                if actual_size != expected_size:
                    raise ContentStoreError("asset size mismatch")
                if actual_digest != digest:
                    try:
                        partial.unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise ContentStoreError("asset digest mismatch")
                os.replace(partial, destination)
                self._fsync_directory(destination.parent)
                self._verified_fingerprints[destination] = self._fingerprint(destination.lstat())
                return StoredAsset(digest, expected_size, destination, from_cache=False)
            except ContentStoreError:
                raise
            except OSError as exc:
                raise ContentStoreError(failure_message("could not publish resumable asset", exc)) from exc
            finally:
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                if source_handle is not None and should_close:
                    source_handle.close()

    @contextmanager
    def _digest_lock(self, bucket: Path, digest: str) -> Iterator[None]:
        lock_path = bucket / f".{digest}.lock"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            secure_file(descriptor, lock_path)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError as exc:
            try:
                if descriptor is not None:
                    os.close(descriptor)
            except OSError:
                pass
            raise ContentStoreError(failure_message("could not lock asset cache entry", exc)) from exc
        try:
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(descriptor)

    def _put_unlocked(
        self,
        source: AssetSource,
        digest: str,
        expected_size: int,
        destination: Path,
        reuse_existing: bool,
    ) -> StoredAsset:
        existing_is_valid = self._verify_existing(destination, digest, expected_size, remove_invalid=True)
        if reuse_existing and existing_is_valid:
            return StoredAsset(digest, expected_size, destination, from_cache=True)

        source_handle: BinaryIO | None = None
        should_close = False
        temporary: Path | None = None
        try:
            if isinstance(source, (bytes, bytearray, memoryview)):
                source_handle = _BytesReader(bytes(source))
                should_close = True
            elif isinstance(source, Path):
                source_handle = self._open_source_path(source)
                should_close = True
            elif hasattr(source, "read"):
                source_handle = source
            else:
                raise TypeError("asset source must be bytes, a path, or a binary stream")

            fd, temporary_name = tempfile.mkstemp(prefix=".astinuslab-", dir=destination.parent)
            temporary = Path(temporary_name)
            try:
                secure_file(fd, temporary)
            except OSError:
                os.close(fd)
                raise
            digest_builder = hashlib.sha256()
            total = 0
            with os.fdopen(fd, "wb") as target:
                while True:
                    try:
                        chunk = source_handle.read(min(READ_CHUNK_BYTES, expected_size - total + 1))
                    except (OSError, ValueError) as exc:
                        raise ContentStoreError(failure_message("could not read asset source", exc)) from exc
                    if chunk is None:
                        raise ContentStoreError("asset source returned no bytes")
                    if not isinstance(chunk, bytes):
                        try:
                            chunk = bytes(chunk)
                        except (TypeError, ValueError) as exc:
                            raise ContentStoreError("asset source returned invalid bytes") from exc
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > expected_size or total > self.max_bytes:
                        raise ContentStoreError("asset is too large")
                    target.write(chunk)
                    digest_builder.update(chunk)
                if total != expected_size:
                    raise ContentStoreError("asset size mismatch")
                if digest_builder.hexdigest() != digest:
                    raise ContentStoreError("asset digest mismatch")
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, destination)
            temporary = None
            self._fsync_directory(destination.parent)
            self._verified_fingerprints[destination] = self._fingerprint(destination.lstat())
            return StoredAsset(digest, expected_size, destination, from_cache=False)
        except ContentStoreError:
            raise
        except OSError as exc:
            raise ContentStoreError(failure_message("could not publish asset", exc)) from exc
        finally:
            if source_handle is not None and should_close:
                source_handle.close()
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _open_source_path(source: Path) -> BinaryIO:
        try:
            descriptor = os.open(
                source,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            )
            result = os.fstat(descriptor)
            if stat.S_ISLNK(result.st_mode) or not stat.S_ISREG(result.st_mode):
                os.close(descriptor)
                if stat.S_ISLNK(result.st_mode):
                    raise ContentStoreError("asset source is a symlink")
                raise ContentStoreError("asset source is not a regular file")
            return os.fdopen(descriptor, "rb")
        except ContentStoreError:
            raise
        except OSError as exc:
            if exc.errno in {getattr(os, "ELOOP", 40), 40}:
                raise ContentStoreError("asset source is a symlink") from exc
            raise ContentStoreError(failure_message("could not open asset source", exc)) from exc

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError as exc:
            raise ContentStoreError(failure_message("could not open content store directory for fsync", exc)) from exc
        try:
            os.fsync(descriptor)
        except OSError as exc:
            raise ContentStoreError(failure_message("could not fsync content store directory", exc)) from exc
        finally:
            os.close(descriptor)

    put_asset = put


class _BytesReader(io.BytesIO):
    """Binary stream adapter for already-materialised byte payloads."""
