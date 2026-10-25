"""Resumable-transfer and cache-verification contract for the content store.

A large asset arrives over several attempts, so the store keeps a bounded
prefix under a private digest-derived name and only publishes the final path
once the complete size and digest verify. Nothing partial is ever visible under
the name a package will later read, a prefix that disagrees with the declared
identity is discarded rather than resumed, and a cache entry whose bytes drift
is removed rather than served.
"""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path

import pytest

from astinuslab.lab_packages.content_store import ContentStore, ContentStoreError

pytestmark = pytest.mark.unit

PAYLOAD = b"a boot layer worth resuming"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


@pytest.fixture()
def store(tmp_path: Path) -> ContentStore:
    return ContentStore(tmp_path / "content", max_bytes=4096)


class _Chunked(io.RawIOBase):
    """Delivers the payload, then stops — a transfer that ended early."""

    def __init__(self, payload: bytes) -> None:
        self._buffer = io.BytesIO(payload)

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)


# --- put_resumable -------------------------------------------------------------


def test_a_complete_transfer_publishes_the_final_path(store: ContentStore):
    stored = store.put_resumable(PAYLOAD, sha256=DIGEST, size_bytes=len(PAYLOAD))

    assert stored is not None
    assert stored.sha256 == DIGEST
    assert stored.from_cache is False
    assert store.path_for(DIGEST).read_bytes() == PAYLOAD
    assert not store.partial_path_for(DIGEST).exists()


def test_an_incomplete_transfer_publishes_nothing_and_keeps_its_prefix(store: ContentStore):
    result = store.put_resumable(_Chunked(PAYLOAD[:8]), sha256=DIGEST, size_bytes=len(PAYLOAD))  # type: ignore[arg-type]

    assert result is None
    assert not store.path_for(DIGEST).exists()
    assert store.partial_path_for(DIGEST).read_bytes() == PAYLOAD[:8]


def test_a_second_attempt_resumes_from_the_retained_prefix(store: ContentStore):
    store.put_resumable(_Chunked(PAYLOAD[:8]), sha256=DIGEST, size_bytes=len(PAYLOAD))  # type: ignore[arg-type]

    stored = store.put_resumable(_Chunked(PAYLOAD[8:]), sha256=DIGEST, size_bytes=len(PAYLOAD), offset=8)  # type: ignore[arg-type]

    assert stored is not None
    assert store.path_for(DIGEST).read_bytes() == PAYLOAD


def test_an_already_cached_asset_short_circuits_the_transfer(store: ContentStore):
    store.put(PAYLOAD, sha256=DIGEST, size_bytes=len(PAYLOAD))

    stored = store.put_resumable(b"", sha256=DIGEST, size_bytes=len(PAYLOAD))

    assert stored is not None
    assert stored.from_cache is True


def test_a_reset_discards_the_retained_prefix(store: ContentStore):
    store.put_resumable(_Chunked(PAYLOAD[:8]), sha256=DIGEST, size_bytes=len(PAYLOAD))  # type: ignore[arg-type]

    stored = store.put_resumable(PAYLOAD, sha256=DIGEST, size_bytes=len(PAYLOAD), reset=True)

    assert stored is not None
    assert store.path_for(DIGEST).read_bytes() == PAYLOAD


def test_bytes_that_do_not_match_the_declared_digest_are_refused(store: ContentStore):
    wrong = b"x" * len(PAYLOAD)

    with pytest.raises(ContentStoreError, match="digest mismatch"):
        store.put_resumable(wrong, sha256=DIGEST, size_bytes=len(PAYLOAD))

    assert not store.path_for(DIGEST).exists()
    assert not store.partial_path_for(DIGEST).exists()


def test_a_transfer_longer_than_its_declared_size_is_refused(store: ContentStore):
    with pytest.raises(ContentStoreError, match="too large"):
        store.put_resumable(PAYLOAD, sha256=DIGEST, size_bytes=4)

    assert not store.path_for(DIGEST).exists()


def test_a_prefix_longer_than_the_asset_is_discarded_before_resuming(store: ContentStore):
    partial = store.partial_path_for(DIGEST)
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(PAYLOAD + b"overrun bytes")

    stored = store.put_resumable(PAYLOAD, sha256=DIGEST, size_bytes=len(PAYLOAD))

    assert stored is not None
    assert store.path_for(DIGEST).read_bytes() == PAYLOAD


def test_a_symlinked_prefix_is_refused(store: ContentStore, tmp_path: Path):
    (tmp_path / "elsewhere").write_bytes(PAYLOAD)
    partial = store.partial_path_for(DIGEST)
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.symlink_to(tmp_path / "elsewhere")

    with pytest.raises(ContentStoreError, match="is a symlink"):
        store.put_resumable(PAYLOAD, sha256=DIGEST, size_bytes=len(PAYLOAD))


def test_a_transfer_refuses_a_malformed_digest(store: ContentStore):
    with pytest.raises(ContentStoreError, match="invalid SHA-256 digest"):
        store.put_resumable(PAYLOAD, sha256="nope", size_bytes=len(PAYLOAD))


def test_a_transfer_refuses_a_size_over_the_store_ceiling(store: ContentStore):
    with pytest.raises(ContentStoreError, match="too large"):
        store.put_resumable(PAYLOAD, sha256=DIGEST, size_bytes=8192)


def test_a_source_that_reports_nothing_ready_is_refused(store: ContentStore):
    class _NoneReturning(io.RawIOBase):
        def readable(self) -> bool:
            return True

        def read(self, size: int = -1) -> None:
            return None

    with pytest.raises(ContentStoreError, match="returned no bytes"):
        store.put_resumable(_NoneReturning(), sha256=DIGEST, size_bytes=len(PAYLOAD))  # type: ignore[arg-type]


def test_a_failing_source_leaves_only_the_bounded_prefix(store: ContentStore):
    class _Failing(io.RawIOBase):
        def readable(self) -> bool:
            return True

        def read(self, size: int = -1) -> bytes:
            raise OSError("transfer aborted")

    with pytest.raises(ContentStoreError, match="could not read asset source"):
        store.put_resumable(_Failing(), sha256=DIGEST, size_bytes=len(PAYLOAD))  # type: ignore[arg-type]

    assert not store.path_for(DIGEST).exists()


# --- cache verification --------------------------------------------------------


def test_has_reports_a_verified_entry(store: ContentStore):
    store.put(PAYLOAD, sha256=DIGEST, size_bytes=len(PAYLOAD))

    assert store.has(DIGEST, size_bytes=len(PAYLOAD)) is True
    assert store.has(DIGEST) is True


def test_has_is_false_for_an_absent_entry(store: ContentStore):
    assert store.has(DIGEST, size_bytes=len(PAYLOAD)) is False


def test_a_cache_entry_of_the_wrong_size_is_discarded(store: ContentStore):
    store.put(PAYLOAD, sha256=DIGEST, size_bytes=len(PAYLOAD))
    store.path_for(DIGEST).write_bytes(PAYLOAD + b"appended")

    assert store.has(DIGEST, size_bytes=len(PAYLOAD)) is False
    assert not store.path_for(DIGEST).exists()


def test_a_cache_entry_whose_bytes_drifted_is_discarded(store: ContentStore):
    store.put(PAYLOAD, sha256=DIGEST, size_bytes=len(PAYLOAD))
    replacement = b"y" * len(PAYLOAD)
    store.path_for(DIGEST).write_bytes(replacement)

    assert store.has(DIGEST, size_bytes=len(PAYLOAD)) is False
    assert not store.path_for(DIGEST).exists()


def test_a_symlinked_cache_entry_is_refused(store: ContentStore, tmp_path: Path):
    (tmp_path / "elsewhere").write_bytes(PAYLOAD)
    entry = store.path_for(DIGEST)
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.symlink_to(tmp_path / "elsewhere")

    with pytest.raises(ContentStoreError, match="is a symlink"):
        store.has(DIGEST, size_bytes=len(PAYLOAD))


# --- reading back --------------------------------------------------------------


def test_get_returns_the_stored_bytes(store: ContentStore):
    store.put(PAYLOAD, sha256=DIGEST, size_bytes=len(PAYLOAD))

    assert store.get(DIGEST, size_bytes=len(PAYLOAD)) == PAYLOAD


def test_get_refuses_an_absent_asset(store: ContentStore):
    with pytest.raises(ContentStoreError):
        store.get(DIGEST, size_bytes=len(PAYLOAD))


def test_open_streams_the_stored_bytes(store: ContentStore):
    store.put(PAYLOAD, sha256=DIGEST, size_bytes=len(PAYLOAD))

    with store.open(DIGEST, size_bytes=len(PAYLOAD)) as handle:
        assert handle.read() == PAYLOAD


def test_open_refuses_an_absent_asset(store: ContentStore):
    with pytest.raises(ContentStoreError):
        store.open(DIGEST, size_bytes=len(PAYLOAD))


# --- _remove_regular -----------------------------------------------------------


def test_remove_regular_leaves_a_file_another_writer_replaced(store: ContentStore, tmp_path: Path):
    entry = tmp_path / "entry"
    entry.write_bytes(PAYLOAD)
    recorded = entry.lstat()
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"someone else's bytes")
    os.replace(replacement, entry)

    ContentStore._remove_regular(entry, recorded)

    assert entry.read_bytes() == b"someone else's bytes"


def test_remove_regular_tolerates_a_vanished_path(tmp_path: Path):
    entry = tmp_path / "entry"
    entry.write_bytes(PAYLOAD)
    recorded = entry.lstat()
    entry.unlink()

    ContentStore._remove_regular(entry, recorded)

    assert not entry.exists()
