"""Explicit public setup media for the reviewed operator-prepared profile.

No ISO generation, registration or certification. A declaration is not evidence
of guest acceptance. The shared source is read only; every job owns its copy.
"""

from __future__ import annotations

import hashlib
import os
import pwd
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..virtualization.windows_unattend import build_unattended_setup

PROFILE = "server2022-unattend-v1"
_HEX = re.compile(r"[0-9a-f]{64}")
_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.iso")


@dataclass(frozen=True)
class WindowsPreparedBundle:
    profile: str
    nonce: str
    media_filename: str
    media_size: int
    media_sha256: str
    media_label: str
    initializer_sha256: str


def parse_windows_bundle(value: object) -> WindowsPreparedBundle:
    """Select code-owned policy, never arbitrary ACLs or executable strings."""
    try:
        if type(value) is not dict or set(value) != {"profile", "nonce", "media"}:
            raise ValueError("shape")
        profile, nonce, media = value["profile"], value["nonce"], value["media"]
        if profile != PROFILE or type(nonce) is not str or not re.fullmatch(r"[0-9a-f]{32}", nonce):
            raise ValueError("profile")
        if type(media) is not dict or set(media) != {"filename", "size_bytes", "sha256", "label", "initializer_sha256"}:
            raise ValueError("media")
        if type(media["filename"]) is not str or not _FILENAME.fullmatch(media["filename"]):
            raise ValueError("filename")
        if type(media["size_bytes"]) is not int or not 0 < media["size_bytes"] <= 16 * 1024**2:
            raise ValueError("size")
        if any(type(media[k]) is not str or not _HEX.fullmatch(media[k]) for k in ("sha256", "initializer_sha256")):
            raise ValueError("digest")
        # Pure public-byte rendering anchors the initializer to reviewed code.
        # This does not create an ISO or execute the guest password initializer.
        expected = build_unattended_setup(
            nonce=nonce,
            locale="en-US",
            input_locale="0409:00000409",
            terms_accepted=True,
            allow_guest_password_initialization=True,
        )
        if (
            media["label"] != expected.volume_label
            or media["initializer_sha256"] != hashlib.sha256(expected.initializer).hexdigest()
        ):
            raise ValueError("initializer")
        return WindowsPreparedBundle(
            profile,
            nonce,
            media["filename"],
            media["size_bytes"],
            media["sha256"],
            media["label"],
            media["initializer_sha256"],
        )
    except (KeyError, TypeError, ValueError):
        raise ValueError("Windows prepared bundle is invalid or uses an unreviewed policy") from None


def _token(s: os.stat_result) -> tuple[int, ...]:
    return (s.st_dev, s.st_ino, s.st_mode, s.st_nlink, s.st_uid, s.st_gid, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


@contextmanager
def _directory(path: Path):
    path = path.absolute()
    if path.resolve(strict=True) != path:
        raise ValueError("directory symlink")
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        s = os.fstat(fd)
        if s.st_uid not in {0, os.geteuid()} or s.st_mode & 0o022 or _token(path.lstat()) != _token(s):
            raise ValueError("directory ownership")
        yield fd
        if (path.lstat().st_dev, path.lstat().st_ino) != (s.st_dev, s.st_ino):
            raise ValueError("directory replaced")
    finally:
        os.close(fd)


def _safe_file(fd: int, size: int, *, allow_qemu_owner: bool = False) -> os.stat_result:
    s = os.fstat(fd)
    owners = {0, os.geteuid()}
    if allow_qemu_owner:
        # libvirt's DAC driver can retain this owner on a shared backing image
        # after a runtime stops. Resolve the host account, never accept an
        # arbitrary numeric UID or extend this exception to setup media.
        try:
            owners.add(pwd.getpwnam("libvirt-qemu").pw_uid)
        except KeyError:
            pass
    if (
        not stat.S_ISREG(s.st_mode)
        or s.st_nlink != 1
        or s.st_size != size
        or s.st_uid not in owners
        or s.st_mode & 0o022
    ):
        raise ValueError("file metadata")
    return s


@dataclass(frozen=True)
class StagedWindowsMedia:
    path: Path
    token: tuple[int, ...]
    size: int
    sha256: str


def stage_windows_media(bundle: WindowsPreparedBundle, *, source_dir: Path, runtime_dir: Path) -> StagedWindowsMedia:
    """Exclusive copy and readback; retain partial copies on any failure."""
    try:
        with _directory(source_dir) as src_dir, _directory(runtime_dir) as dst_dir:
            if os.path.samestat(os.fstat(src_dir), os.fstat(dst_dir)):
                raise ValueError("shared directory")
            src = os.open(bundle.media_filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=src_dir)
            try:
                before = _safe_file(src, bundle.media_size)
                dst = os.open(
                    "windows-setup.iso", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dst_dir
                )
                try:
                    digest = hashlib.sha256()
                    remaining = bundle.media_size
                    while remaining:
                        chunk = os.read(src, min(65536, remaining))
                        if not chunk:
                            raise ValueError("short copy")
                        digest.update(chunk)
                        view = memoryview(chunk)
                        while view:
                            written = os.write(dst, view)
                            if written <= 0:
                                raise ValueError("short write")
                            view = view[written:]
                        remaining -= len(chunk)
                    if (
                        os.read(src, 1)
                        or digest.hexdigest() != bundle.media_sha256
                        or _token(os.fstat(src)) != _token(before)
                    ):
                        raise ValueError("source changed or digest")
                    if _token(os.stat(bundle.media_filename, dir_fd=src_dir, follow_symlinks=False)) != _token(before):
                        raise ValueError("source replaced")
                    os.fchmod(dst, 0o444)
                    os.fsync(dst)
                    record = StagedWindowsMedia(
                        runtime_dir.absolute() / "windows-setup.iso",
                        _token(_safe_file(dst, bundle.media_size)),
                        bundle.media_size,
                        bundle.media_sha256,
                    )
                    os.fsync(dst_dir)
                finally:
                    os.close(dst)
            finally:
                os.close(src)
        verify_staged_media(record, runtime_dir=runtime_dir)
        return record
    except (OSError, ValueError):
        raise ValueError("Windows setup media staging failed; artifacts retained") from None


def verify_staged_media(media: StagedWindowsMedia, *, runtime_dir: Path) -> None:
    try:
        if media.path != runtime_dir.absolute() / "windows-setup.iso":
            raise ValueError("ownership")
        with _directory(runtime_dir) as directory:
            fd = os.open(media.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                before = _safe_file(fd, media.size)
                if _token(before) != media.token or stat.S_IMODE(before.st_mode) != 0o444:
                    raise ValueError("identity")
                digest = hashlib.sha256()
                remaining = media.size
                while remaining:
                    chunk = os.read(fd, min(65536, remaining))
                    if not chunk:
                        raise ValueError("short read")
                    digest.update(chunk)
                    remaining -= len(chunk)
                if os.read(fd, 1) or digest.hexdigest() != media.sha256 or _token(os.fstat(fd)) != media.token:
                    raise ValueError("changed or digest")
                if _token(os.stat(media.path.name, dir_fd=directory, follow_symlinks=False)) != media.token:
                    raise ValueError("replaced")
            finally:
                os.close(fd)
    except (OSError, ValueError):
        raise ValueError("Windows setup media verification failed") from None


def remove_staged_media(media: StagedWindowsMedia, *, runtime_dir: Path) -> None:
    """Caller must first prove the job directory is no longer domain-owned."""
    verify_staged_media(media, runtime_dir=runtime_dir)
    with _directory(runtime_dir) as directory:
        if _token(os.stat(media.path.name, dir_fd=directory, follow_symlinks=False)) != media.token:
            raise ValueError("Windows setup media cleanup refused: identity changed")
        os.unlink(media.path.name, dir_fd=directory)
        os.fsync(directory)


def verify_windows_disk(path: Path, expected_sha256: str) -> None:
    """Bind declared provenance to the installed read-only backing bytes."""
    try:
        if type(expected_sha256) is not str or not _HEX.fullmatch(expected_sha256):
            raise ValueError("digest")
        with _directory(path.parent) as directory:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                before = _safe_file(fd, os.fstat(fd).st_size, allow_qemu_owner=True)
                if not 0 < before.st_size <= 50 * 1024**3:
                    raise ValueError("disk bound")
                digest = hashlib.sha256()
                remaining = before.st_size
                while remaining:
                    chunk = os.read(fd, min(1024**2, remaining))
                    if not chunk:
                        raise ValueError("short disk")
                    digest.update(chunk)
                    remaining -= len(chunk)
                if os.read(fd, 1) or digest.hexdigest() != expected_sha256 or _token(os.fstat(fd)) != _token(before):
                    raise ValueError("disk changed or digest")
                if _token(os.stat(path.name, dir_fd=directory, follow_symlinks=False)) != _token(before):
                    raise ValueError("disk replaced")
            finally:
                os.close(fd)
    except (OSError, ValueError):
        raise ValueError("Windows disk provenance digest verification failed") from None
