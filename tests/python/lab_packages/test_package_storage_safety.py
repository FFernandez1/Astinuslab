"""Filesystem safety contract for the package import and retention helpers.

These helpers stand between an operator-supplied archive path and the retained
package store, so every refusal here is a real boundary: a symlinked source, a
source swapped mid-copy, an archive that outgrows the configured ceiling, or a
retained path that no longer holds the bytes its digest claims. The happy path
is already covered through ``LabPackageService``; this module drives the
failure paths directly, and asserts that a failed publication leaves nothing
behind.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from astinuslab.lab_packages import package_service as ps
from astinuslab.lab_packages.package_service import PackageImportError, copy_digest_verified

pytestmark = pytest.mark.unit


def _write(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    return path


def _digest_of(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _leftovers(directory: Path) -> list[str]:
    """Temporary files the helpers must never leave behind."""
    return sorted(p.name for p in directory.iterdir() if p.name.startswith(".astinuslab-import-"))


def _mutated(real: os.stat_result, **changes: int) -> SimpleNamespace:
    """A stat-like view of ``real`` with individual fields overridden."""
    fields = {
        name: getattr(real, name) for name in ("st_mode", "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    }
    fields.update(changes)
    return SimpleNamespace(**fields)


# --- _ensure_private_directory -------------------------------------------------


def test_private_directory_is_created_with_owner_only_permissions(tmp_path: Path):
    target = tmp_path / "store" / "packages"

    ps._ensure_private_directory(target)

    assert target.is_dir()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "store").stat().st_mode) == 0o700


def test_private_directory_tightens_an_existing_target_directory(tmp_path: Path):
    target = tmp_path / "loose"
    target.mkdir(mode=0o755)

    ps._ensure_private_directory(target)

    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_private_directory_refuses_a_symlinked_component(tmp_path: Path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)

    with pytest.raises(PackageImportError, match="only directories"):
        ps._ensure_private_directory(tmp_path / "link" / "packages")


def test_private_directory_refuses_a_file_in_the_path(tmp_path: Path):
    _write(tmp_path / "blocker", b"not a directory")

    with pytest.raises(PackageImportError, match="only directories"):
        ps._ensure_private_directory(tmp_path / "blocker" / "packages")


# --- _digest_file --------------------------------------------------------------


def test_digest_file_reports_digest_and_size(tmp_path: Path):
    payload = b"astinuslab package bytes"
    source = _write(tmp_path / "pkg.astinuslab", payload)

    assert ps._digest_file(source) == (_digest_of(payload), len(payload))


def test_digest_file_refuses_a_missing_path(tmp_path: Path):
    with pytest.raises(PackageImportError, match="regular non-symlink file"):
        ps._digest_file(tmp_path / "absent.astinuslab")


def test_digest_file_refuses_a_symlink(tmp_path: Path):
    _write(tmp_path / "real.astinuslab", b"payload")
    link = tmp_path / "link.astinuslab"
    link.symlink_to(tmp_path / "real.astinuslab")

    with pytest.raises(PackageImportError, match="regular non-symlink file"):
        ps._digest_file(link)


def test_digest_file_refuses_a_directory(tmp_path: Path):
    (tmp_path / "directory").mkdir()

    with pytest.raises(PackageImportError, match="regular non-symlink file"):
        ps._digest_file(tmp_path / "directory")


def test_digest_file_refuses_an_empty_archive(tmp_path: Path):
    source = _write(tmp_path / "empty.astinuslab", b"")

    with pytest.raises(PackageImportError, match="must not be empty"):
        ps._digest_file(source)


def test_digest_file_refuses_an_archive_over_the_ceiling(tmp_path: Path):
    source = _write(tmp_path / "big.astinuslab", b"x" * 64)

    with pytest.raises(PackageImportError, match="too large"):
        ps._digest_file(source, max_bytes=16)


def test_digest_file_detects_a_source_replaced_while_it_was_read(tmp_path: Path, monkeypatch):
    source = _write(tmp_path / "swapped.astinuslab", b"payload")
    real_lstat = Path.lstat
    calls = {"n": 0}

    def counting_lstat(self, **kwargs):
        result = real_lstat(self, **kwargs)
        if self == source:
            calls["n"] += 1
            if calls["n"] > 1:
                return _mutated(result, st_ino=result.st_ino + 1)
        return result

    monkeypatch.setattr(Path, "lstat", counting_lstat)

    with pytest.raises(PackageImportError, match="changed while it was read"):
        ps._digest_file(source)


# --- copy_digest_verified ------------------------------------------------------


def test_copy_digest_verified_refuses_a_symlinked_source(tmp_path: Path):
    _write(tmp_path / "real.astinuslab", b"payload")
    link = tmp_path / "link.astinuslab"
    link.symlink_to(tmp_path / "real.astinuslab")

    with pytest.raises(PackageImportError, match="regular non-symlink file"):
        copy_digest_verified(link, tmp_path / "sink")


def test_copy_digest_verified_refuses_a_missing_source(tmp_path: Path):
    with pytest.raises(PackageImportError, match="regular non-symlink file"):
        copy_digest_verified(tmp_path / "absent", tmp_path / "sink")


def test_copy_digest_verified_refuses_an_empty_source(tmp_path: Path):
    source = _write(tmp_path / "empty.astinuslab", b"")

    with pytest.raises(PackageImportError, match="must not be empty"):
        copy_digest_verified(source, tmp_path / "sink")


def test_copy_digest_verified_refuses_a_source_over_the_ceiling(tmp_path: Path):
    source = _write(tmp_path / "big.astinuslab", b"x" * 64)

    with pytest.raises(PackageImportError, match="too large"):
        copy_digest_verified(source, tmp_path / "sink", max_bytes=16)


def test_copy_digest_verified_refuses_an_existing_sink(tmp_path: Path):
    source = _write(tmp_path / "pkg.astinuslab", b"payload")
    _write(tmp_path / "sink", b"occupied")

    with pytest.raises(PackageImportError, match="could not copy package archive"):
        copy_digest_verified(source, tmp_path / "sink")


def test_copy_digest_verified_detects_a_source_changed_during_the_copy(tmp_path: Path, monkeypatch):
    source = _write(tmp_path / "pkg.astinuslab", b"payload")
    real_lstat = Path.lstat
    calls = {"n": 0}

    def counting_lstat(self, **kwargs):
        result = real_lstat(self, **kwargs)
        if self == source:
            calls["n"] += 1
            if calls["n"] > 1:
                return _mutated(result, st_mtime_ns=result.st_mtime_ns + 1)
        return result

    monkeypatch.setattr(Path, "lstat", counting_lstat)

    with pytest.raises(PackageImportError, match="changed while it was copied"):
        copy_digest_verified(source, tmp_path / "sink")


# --- _copy_archive -------------------------------------------------------------


def test_copy_archive_publishes_a_new_destination(tmp_path: Path):
    payload = b"retained package bytes"
    source = _write(tmp_path / "pkg.astinuslab", payload)
    destination = tmp_path / "store" / "pkg.astinuslab"
    destination.parent.mkdir()

    created = ps._copy_archive(
        source, destination, _digest_of(payload), len(payload), max_bytes=ps.PACKAGE_IMPORT_MAX_BYTES
    )

    assert created is True
    assert destination.read_bytes() == payload
    assert _leftovers(destination.parent) == []


def test_copy_archive_accepts_an_identical_retained_archive(tmp_path: Path):
    payload = b"retained package bytes"
    source = _write(tmp_path / "pkg.astinuslab", payload)
    destination = _write(tmp_path / "retained.astinuslab", payload)

    created = ps._copy_archive(
        source, destination, _digest_of(payload), len(payload), max_bytes=ps.PACKAGE_IMPORT_MAX_BYTES
    )

    assert created is False
    assert destination.read_bytes() == payload


def test_copy_archive_refuses_a_retained_archive_with_another_identity(tmp_path: Path):
    payload = b"retained package bytes"
    source = _write(tmp_path / "pkg.astinuslab", payload)
    destination = _write(tmp_path / "retained.astinuslab", b"different bytes entirely")

    with pytest.raises(PackageImportError, match="unexpected identity"):
        ps._copy_archive(source, destination, _digest_of(payload), len(payload), max_bytes=ps.PACKAGE_IMPORT_MAX_BYTES)


def test_copy_archive_refuses_a_symlinked_retained_path(tmp_path: Path):
    payload = b"retained package bytes"
    source = _write(tmp_path / "pkg.astinuslab", payload)
    _write(tmp_path / "elsewhere", payload)
    destination = tmp_path / "retained.astinuslab"
    destination.symlink_to(tmp_path / "elsewhere")

    with pytest.raises(PackageImportError, match="not a valid regular file"):
        ps._copy_archive(source, destination, _digest_of(payload), len(payload), max_bytes=ps.PACKAGE_IMPORT_MAX_BYTES)


def test_copy_archive_refuses_a_symlinked_source(tmp_path: Path):
    payload = b"retained package bytes"
    _write(tmp_path / "real.astinuslab", payload)
    source = tmp_path / "link.astinuslab"
    source.symlink_to(tmp_path / "real.astinuslab")
    destination = tmp_path / "store" / "pkg.astinuslab"
    destination.parent.mkdir()

    # The open carries O_NOFOLLOW, so the kernel refuses the symlink with ELOOP
    # before any byte is read; the helper reports that as a retention failure.
    with pytest.raises(PackageImportError, match="could not retain package archive"):
        ps._copy_archive(source, destination, _digest_of(payload), len(payload), max_bytes=ps.PACKAGE_IMPORT_MAX_BYTES)

    assert not destination.exists()
    assert _leftovers(destination.parent) == []


def test_copy_archive_refuses_a_source_that_is_not_a_regular_file(tmp_path: Path):
    source = tmp_path / "fifo.astinuslab"
    os.mkfifo(source)
    destination = tmp_path / "store" / "pkg.astinuslab"
    destination.parent.mkdir()

    # A FIFO opens cleanly under O_NONBLOCK, so it reaches the explicit
    # regular-file check rather than failing in the kernel.
    with pytest.raises(PackageImportError, match="regular non-symlink file"):
        ps._copy_archive(source, destination, "a" * 64, 1, max_bytes=ps.PACKAGE_IMPORT_MAX_BYTES)

    assert not destination.exists()
    assert _leftovers(destination.parent) == []


def test_copy_archive_refuses_a_source_over_the_ceiling_and_cleans_up(tmp_path: Path):
    payload = b"x" * 64
    source = _write(tmp_path / "pkg.astinuslab", payload)
    destination = tmp_path / "store" / "pkg.astinuslab"
    destination.parent.mkdir()

    with pytest.raises(PackageImportError, match="too large"):
        ps._copy_archive(source, destination, _digest_of(payload), len(payload), max_bytes=16)

    assert not destination.exists()
    assert _leftovers(destination.parent) == []


def test_copy_archive_refuses_a_source_that_changed_during_the_copy(tmp_path: Path):
    payload = b"retained package bytes"
    source = _write(tmp_path / "pkg.astinuslab", payload)
    destination = tmp_path / "store" / "pkg.astinuslab"
    destination.parent.mkdir()

    # The declared identity disagrees with the bytes actually copied, which is
    # what a source swapped between digesting and retention looks like here.
    with pytest.raises(PackageImportError, match="changed while it was copied"):
        ps._copy_archive(
            source, destination, _digest_of(b"other bytes"), len(payload), max_bytes=ps.PACKAGE_IMPORT_MAX_BYTES
        )

    assert not destination.exists()
    assert _leftovers(destination.parent) == []


def test_copy_archive_removes_a_published_archive_that_changed_after_publication(tmp_path: Path, monkeypatch):
    payload = b"retained package bytes"
    source = _write(tmp_path / "pkg.astinuslab", payload)
    destination = tmp_path / "store" / "pkg.astinuslab"
    destination.parent.mkdir()
    real_replace = os.replace

    def tampering_replace(src, dst, **kwargs):
        real_replace(src, dst, **kwargs)
        Path(dst).write_bytes(b"tampered after publication")

    monkeypatch.setattr(ps.os, "replace", tampering_replace)

    with pytest.raises(PackageImportError, match="changed while it was published"):
        ps._copy_archive(source, destination, _digest_of(payload), len(payload), max_bytes=ps.PACKAGE_IMPORT_MAX_BYTES)

    assert not destination.exists()
    assert _leftovers(destination.parent) == []


def test_copy_archive_reports_an_os_failure_as_a_retention_failure(tmp_path: Path, monkeypatch):
    payload = b"retained package bytes"
    source = _write(tmp_path / "pkg.astinuslab", payload)
    destination = tmp_path / "store" / "pkg.astinuslab"
    destination.parent.mkdir()

    def failing_replace(src, dst, **kwargs):
        raise OSError("replace refused")

    monkeypatch.setattr(ps.os, "replace", failing_replace)

    with pytest.raises(PackageImportError, match="could not retain package archive"):
        ps._copy_archive(source, destination, _digest_of(payload), len(payload), max_bytes=ps.PACKAGE_IMPORT_MAX_BYTES)

    assert not destination.exists()
    assert _leftovers(destination.parent) == []


# --- _remove_if_same -----------------------------------------------------------


def test_remove_if_same_ignores_an_absent_expectation(tmp_path: Path):
    retained = _write(tmp_path / "retained", b"payload")

    ps._remove_if_same(retained, None)

    assert retained.exists()


def test_remove_if_same_removes_the_file_it_published(tmp_path: Path):
    retained = _write(tmp_path / "retained", b"payload")

    ps._remove_if_same(retained, retained.lstat())

    assert not retained.exists()


def test_remove_if_same_leaves_a_file_another_writer_replaced(tmp_path: Path):
    retained = _write(tmp_path / "retained", b"payload")
    published = retained.lstat()
    # Replace rather than unlink-then-write: the original inode stays allocated
    # until the rename lands, so the replacement cannot reuse its number.
    replacement = _write(tmp_path / "replacement", b"someone else's bytes")
    os.replace(replacement, retained)
    assert retained.lstat().st_ino != published.st_ino

    ps._remove_if_same(retained, published)

    assert retained.read_bytes() == b"someone else's bytes"


def test_remove_if_same_tolerates_a_vanished_path(tmp_path: Path):
    retained = _write(tmp_path / "retained", b"payload")
    published = retained.lstat()
    retained.unlink()

    ps._remove_if_same(retained, published)

    assert not retained.exists()


# --- _exclusive_package_lock ---------------------------------------------------


def test_exclusive_package_lock_is_held_for_the_duration_of_the_block(tmp_path: Path):
    import fcntl

    lock_path = tmp_path / "identity.lock"

    def try_lock() -> bool:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return True
        finally:
            os.close(descriptor)

    with ps._exclusive_package_lock(lock_path):
        # flock is held per open file description, so a separate open competes
        # with the block above exactly as another process would.
        assert try_lock() is False

    assert try_lock() is True
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
