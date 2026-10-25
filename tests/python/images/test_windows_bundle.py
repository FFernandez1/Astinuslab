import copy
import hashlib
import os
import pwd
from types import SimpleNamespace

import pytest

from tests.python._support.windows_images import create_windows_bundle_fixture, windows_bundle_declaration


def test_bundle_validates_reviewed_initializer_and_rejects_arbitrary_policy():
    from astinuslab.images.windows_bundle import parse_windows_bundle

    parsed = parse_windows_bundle(windows_bundle_declaration())
    assert parsed.media_filename == "setup.iso"
    assert parsed.nonce == "db3396d32fa84103b4f7b29c27179dbb"


@pytest.mark.parametrize(
    "field,value",
    [
        ("profile", "arbitrary"),
        ("nonce", "';exit 0"),
        ("password", "synthetic"),
        ("parent_acls", []),
        ("media.filename", "../setup.iso"),
        ("media.filename", "/tmp/setup.iso"),
        ("media.filename", "setup.iso:stream"),
        ("media.size_bytes", True),
        ("media.size_bytes", 0),
        ("media.size_bytes", 16777217),
        ("media.sha256", "A" * 64),
        ("media.initializer_sha256", "0" * 64),
        ("media.label", "OTHER"),
        ("media.password", "synthetic"),
    ],
)
def test_bundle_rejects_unknown_keys_bad_types_and_unreviewed_initializer(field, value):
    from astinuslab.images.windows_bundle import parse_windows_bundle

    data = copy.deepcopy(windows_bundle_declaration())
    target, name = (data["media"], field[6:]) if field.startswith("media.") else (data, field)
    target[name] = value
    with pytest.raises(ValueError, match="Windows prepared bundle"):
        parse_windows_bundle(data)


def test_staged_media_is_verified_exclusive_readonly_and_cleanup_is_scoped(tmp_path):
    from astinuslab.images.windows_bundle import remove_staged_media, stage_windows_media, verify_staged_media

    source, job, media, bundle = create_windows_bundle_fixture(tmp_path)
    staged = stage_windows_media(bundle, source_dir=source, runtime_dir=job)
    assert staged.path.read_bytes() == b"synthetic public media"
    assert staged.path.stat().st_mode & 0o777 == 0o444
    assert staged.path.stat().st_ino != media.stat().st_ino
    verify_staged_media(staged, runtime_dir=job)
    with pytest.raises(ValueError):
        stage_windows_media(bundle, source_dir=source, runtime_dir=job)
    unrelated = job / "retained"
    unrelated.write_text("retained")
    remove_staged_media(staged, runtime_dir=job)
    assert not staged.path.exists()
    assert media.read_bytes() == b"synthetic public media"
    assert unrelated.read_text() == "retained"


@pytest.mark.parametrize("bad", ["symlink", "hardlink", "writable", "digest", "size", "directory", "fifo"])
def test_unsafe_source_never_becomes_attached_media(tmp_path, bad):
    from astinuslab.images.windows_bundle import stage_windows_media

    source, job, media, bundle = create_windows_bundle_fixture(tmp_path)
    if bad == "symlink":
        media.rename(source / "other.iso")
        media.symlink_to(source / "other.iso")
    elif bad == "hardlink":
        os.link(media, source / "alias.iso")
    elif bad == "writable":
        media.chmod(0o666)
    elif bad == "digest":
        media.write_bytes(b"X" * media.stat().st_size)
    elif bad == "size":
        media.write_bytes(b"X")
    else:
        media.unlink()
        if bad == "directory":
            media.mkdir()
        else:
            os.mkfifo(media)
    with pytest.raises(ValueError, match="Windows setup media"):
        stage_windows_media(bundle, source_dir=source, runtime_dir=job)


def test_replaced_job_media_is_not_verified_or_deleted(tmp_path):
    from astinuslab.images.windows_bundle import remove_staged_media, stage_windows_media, verify_staged_media

    source, job, media, bundle = create_windows_bundle_fixture(tmp_path)
    staged = stage_windows_media(bundle, source_dir=source, runtime_dir=job)
    staged.path.rename(job / "retained-original")
    staged.path.write_text("unrelated replacement")
    for action in (verify_staged_media, remove_staged_media):
        with pytest.raises(ValueError):
            action(staged, runtime_dir=job)
    assert staged.path.read_text() == "unrelated replacement"
    assert media.exists()


def test_disk_provenance_digest_is_checked_on_actual_bytes(tmp_path):
    from astinuslab.images.windows_bundle import verify_windows_disk

    root = tmp_path / "base"
    root.mkdir(mode=0o700)
    disk = root / "disk.qcow2"
    disk.write_bytes(b"synthetic disk")
    disk.chmod(0o644)
    digest = hashlib.sha256(b"synthetic disk").hexdigest()
    verify_windows_disk(disk, digest)
    disk.write_bytes(b"wrong contents")
    with pytest.raises(ValueError, match="disk provenance"):
        verify_windows_disk(disk, digest)


@pytest.mark.parametrize(
    "owner,mode,contents,accepted",
    [
        (64055, 0o644, b"synthetic disk", True),
        (64056, 0o644, b"synthetic disk", False),
        (64055, 0o664, b"synthetic disk", False),
        (64055, 0o644, b"changed disk", False),
    ],
)
def test_installed_backing_accepts_only_known_qemu_owner_with_verified_bytes(
    tmp_path, monkeypatch, owner, mode, contents, accepted
):
    from astinuslab.images import windows_bundle as module

    disk = tmp_path / "base.qcow2"
    disk.write_bytes(contents)
    disk.chmod(mode)
    inode = disk.stat().st_ino
    original_stat, original_fstat = os.stat, os.fstat
    original_lookup = pwd.getpwnam

    def relabeled(value):
        if value.st_ino != inode:
            return value
        fields = {key: getattr(value, key) for key in dir(value) if key.startswith("st_")}
        return SimpleNamespace(**{**fields, "st_uid": owner})

    monkeypatch.setattr(os, "stat", lambda *a, **k: relabeled(original_stat(*a, **k)))
    monkeypatch.setattr(os, "fstat", lambda fd: relabeled(original_fstat(fd)))
    monkeypatch.setattr(
        pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=64055) if name == "libvirt-qemu" else original_lookup(name)
    )
    digest = hashlib.sha256(b"synthetic disk").hexdigest()
    if accepted:
        module.verify_windows_disk(disk, digest)
        # This exception applies only to the installed disk, never setup media.
        with disk.open("rb") as stream, pytest.raises(ValueError, match="file metadata"):
            module._safe_file(stream.fileno(), len(contents))
    else:
        with pytest.raises(ValueError, match="disk provenance"):
            module.verify_windows_disk(disk, digest)
