from __future__ import annotations

import hashlib
import io
import os
import stat
import tracemalloc
from pathlib import Path
from typing import BinaryIO, cast

import pytest

from astinuslab.lab_packages.content_store import ContentStore, ContentStoreError


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def test_put_publishes_exact_bytes_at_content_addressed_path(tmp_path: Path):
    payload = b"forensic evidence\n"
    store = ContentStore(tmp_path / "cache")

    result = store.put(io.BytesIO(payload), sha256=_digest(payload), size_bytes=len(payload))

    assert result.sha256 == _digest(payload)
    assert result.size_bytes == len(payload)
    assert result.path == tmp_path / "cache" / "sha256" / result.sha256[:2] / result.sha256
    assert result.path.read_bytes() == payload
    assert store.get(result.sha256, size_bytes=len(payload)) == payload


@pytest.mark.parametrize("with_size", [False, True])
def test_get_small_entry_does_not_allocate_the_asset_limit(tmp_path: Path, with_size: bool):
    payload = b"forensic evidence\n"
    store = ContentStore(tmp_path / "cache")
    result = store.put(payload, sha256=_digest(payload), size_bytes=len(payload))

    tracemalloc.start()
    try:
        assert store.get(result.sha256, size_bytes=len(payload) if with_size else None) == payload
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024**2, "a tiny asset must not allocate the 16 GiB asset ceiling"


@pytest.mark.parametrize("size", [1024**2, 1024**2 + 7])
def test_get_returns_all_bytes_across_read_boundaries(tmp_path: Path, size: int):
    payload = b"x" * size
    store = ContentStore(tmp_path / "cache", max_bytes=size)
    result = store.put(payload, sha256=_digest(payload), size_bytes=size)
    assert store.get(result.sha256, size_bytes=size) == payload


def test_get_rejects_growth_past_limit_after_open(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    payload = b"data"
    store = ContentStore(tmp_path / "cache", max_bytes=len(payload))
    result = store.put(payload, sha256=_digest(payload), size_bytes=len(payload))
    verified_open = store.open

    def grow_after_open(*args, **kwargs):
        handle = verified_open(*args, **kwargs)
        with result.path.open("ab") as writer:
            writer.write(b"extra")
        return handle

    monkeypatch.setattr(store, "open", grow_after_open)
    with pytest.raises(ContentStoreError, match="too large"):
        store.get(result.sha256, size_bytes=len(payload))


def test_cache_hit_reuses_existing_file_without_replacing_it(tmp_path: Path):
    payload = b"already cached"
    store = ContentStore(tmp_path / "cache")
    first = store.put(payload, sha256=_digest(payload), size_bytes=len(payload))
    before = first.path.stat().st_ino

    second = store.put(b"unused", sha256=first.sha256, size_bytes=first.size_bytes)

    assert second.path == first.path
    assert second.from_cache is True
    assert second.path.stat().st_ino == before
    assert second.path.read_bytes() == payload


def test_put_rejects_short_or_oversized_stream_and_leaves_no_temporary_files(tmp_path: Path):
    payload = b"expected payload"
    store = ContentStore(tmp_path / "cache", max_bytes=len(payload))
    digest = _digest(payload)

    with pytest.raises(ContentStoreError, match="size mismatch"):
        store.put(io.BytesIO(b"short"), sha256=digest, size_bytes=len(payload))
    with pytest.raises(ContentStoreError, match="too large"):
        store.put(io.BytesIO(payload + b"extra"), sha256=digest, size_bytes=len(payload))

    assert not list((tmp_path / "cache").rglob(".astinuslab-*"))
    assert not (tmp_path / "cache" / "sha256" / digest[:2] / digest).exists()


def test_put_rejects_digest_mismatch_without_publishing(tmp_path: Path):
    payload = b"not the expected bytes"
    expected = "0" * 64
    store = ContentStore(tmp_path / "cache")

    with pytest.raises(ContentStoreError, match="digest mismatch"):
        store.put(payload, sha256=expected, size_bytes=len(payload))

    assert not (tmp_path / "cache" / "sha256" / expected[:2] / expected).exists()


def test_wrong_existing_cache_entry_is_removed_and_replaced_only_after_verification(tmp_path: Path):
    payload = b"correct bytes"
    digest = _digest(payload)
    store = ContentStore(tmp_path / "cache")
    path = tmp_path / "cache" / "sha256" / digest[:2] / digest
    path.parent.mkdir(parents=True)
    path.write_bytes(b"wrong bytes")

    result = store.put(payload, sha256=digest, size_bytes=len(payload))

    assert result.from_cache is False
    assert path.read_bytes() == payload


def test_symlink_cache_entry_is_rejected_without_following_target(tmp_path: Path):
    payload = b"secret"
    digest = _digest(payload)
    store = ContentStore(tmp_path / "cache")
    path = tmp_path / "cache" / "sha256" / digest[:2] / digest
    path.parent.mkdir(parents=True)
    target = tmp_path / "outside"
    target.write_bytes(payload)
    path.symlink_to(target)

    with pytest.raises(ContentStoreError, match="symlink"):
        store.has(digest, size_bytes=len(payload))
    assert target.read_bytes() == payload


def test_source_path_must_be_regular_non_symlink_file(tmp_path: Path):
    payload = b"evidence"
    source = tmp_path / "source"
    source.write_bytes(payload)
    store = ContentStore(tmp_path / "cache")
    digest = _digest(payload)

    with pytest.raises(ContentStoreError, match="symlink"):
        link = tmp_path / "link"
        link.symlink_to(source)
        store.put(link, sha256=digest, size_bytes=len(payload))

    with pytest.raises(ContentStoreError, match="regular"):
        store.put(tmp_path, sha256=digest, size_bytes=len(payload))


def test_fifo_source_is_opened_nonblocking_and_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    payload = b"fifo payload"
    fifo = tmp_path / "source.fifo"
    fifo_path = str(fifo)
    os.mkfifo(fifo)
    store = ContentStore(tmp_path / "cache")
    original_open = os.open

    def guarded_open(path: str | bytes | Path, flags: int, *args: int) -> int:
        if str(path) == fifo_path and not flags & getattr(os, "O_NONBLOCK", 0):
            raise AssertionError("FIFO opened without O_NONBLOCK")
        return original_open(path, flags, *args)

    monkeypatch.setattr("astinuslab.lab_packages.content_store.os.open", guarded_open)
    with pytest.raises(ContentStoreError, match="regular"):
        store.put(fifo, sha256=_digest(payload), size_bytes=len(payload))


def test_existing_parent_symlink_is_rejected_before_store_mutation(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    parent_link = tmp_path / "linked-parent"
    parent_link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ContentStoreError, match="symlink"):
        ContentStore(parent_link / "cache")
    assert not (outside / "cache").exists()


def test_directory_fsync_failure_is_reported_after_atomic_publication(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    payload = b"directory durability"
    store = ContentStore(tmp_path / "cache")

    original_fsync = os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("fsync failed")
        original_fsync(descriptor)

    monkeypatch.setattr("astinuslab.lab_packages.content_store.os.fsync", fail_directory_fsync)
    with pytest.raises(ContentStoreError, match="fsync"):
        store.put(payload, sha256=_digest(payload), size_bytes=len(payload))


def test_store_roots_are_private_and_open_verifies_content(tmp_path: Path):
    payload = b"open me"
    store = ContentStore(tmp_path / "cache")
    result = store.put(payload, sha256=_digest(payload), size_bytes=len(payload))

    assert stat.S_IMODE((tmp_path / "cache").stat().st_mode) & 0o077 == 0
    with store.open(result.sha256, size_bytes=len(payload)) as handle:
        assert handle.read() == payload

    result.path.write_bytes(b"tampered")
    with pytest.raises(ContentStoreError, match="digest mismatch"):
        store.open(result.sha256, size_bytes=len(payload))


def test_verified_fingerprint_avoids_rehash_until_file_identity_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    payload = b"fingerprint fast path"
    store = ContentStore(tmp_path / "cache")
    stored = store.put(payload, sha256=_digest(payload), size_bytes=len(payload))
    calls = 0
    original = store._stream_digest

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "_stream_digest", counted)
    assert store.has(stored.sha256, size_bytes=stored.size_bytes)
    assert calls == 0
    os.utime(stored.path, None)
    assert store.has(stored.sha256, size_bytes=stored.size_bytes)
    assert calls == 1


@pytest.mark.parametrize("digest", ["", "x" * 64, "A" * 64, "../" + "0" * 61])
def test_invalid_digest_is_rejected_before_path_use(tmp_path: Path, digest: str):
    store = ContentStore(tmp_path / "cache")
    with pytest.raises(ContentStoreError, match="digest"):
        store.has(digest, size_bytes=1)


def test_interrupted_stream_does_not_publish_or_leave_temp_files(tmp_path: Path):
    class BrokenStream:
        def read(self, _size: int) -> bytes:
            raise OSError("interrupted")

    store = ContentStore(tmp_path / "cache")
    digest = "1" * 64
    with pytest.raises(ContentStoreError, match="read"):
        store.put(cast(BinaryIO, BrokenStream()), sha256=digest, size_bytes=1)

    assert not list((tmp_path / "cache").rglob(".astinuslab-*"))


def test_resumable_put_keeps_only_partial_prefix_until_completion(tmp_path: Path):
    payload = b"resumable forensic payload"
    digest = _digest(payload)

    class Interrupted:
        def __init__(self) -> None:
            self._sent = False

        def read(self, _size: int) -> bytes:
            if not self._sent:
                self._sent = True
                return payload[:8]
            raise OSError("interrupted")

    store = ContentStore(tmp_path / "cache")
    with pytest.raises(ContentStoreError, match="read"):
        store.put_resumable(cast(BinaryIO, Interrupted()), sha256=digest, size_bytes=len(payload), offset=0)
    assert store.partial_size(digest, size_bytes=len(payload)) == 8
    assert not store.path_for(digest).exists()

    completed = store.put_resumable(
        io.BytesIO(payload[8:]),
        sha256=digest,
        size_bytes=len(payload),
        offset=8,
    )

    assert completed is not None
    assert completed.path.read_bytes() == payload
    assert not store.partial_path_for(digest).exists()


def test_oversized_regular_cache_entries_are_removed_before_return_or_raise(tmp_path: Path):
    payload = b"too large"
    digest = _digest(payload)
    store = ContentStore(tmp_path / "cache", max_bytes=4)
    path = tmp_path / "cache" / "sha256" / digest[:2] / digest
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)

    assert store.has(digest) is False
    assert not path.exists()

    path.write_bytes(payload)
    with pytest.raises(ContentStoreError, match="too large"):
        store.open(digest)
    assert not path.exists()
