"""Boot-layer hashing and publication contract for locked export.

``materialize_boot_layer`` accepts a captured layer as raw bytes, a path, or a
stream, and every one of those has to reach the content store with a digest the
store can re-verify. A seekable stream is rewound for that second pass; a
pipe-like stream cannot be, so it is spooled to a private temporary file first.
These tests drive each source kind and each refusal, and assert that a failed
publication leaves no spool file behind.
"""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path

import pytest

from astinuslab.lab_packages.content_store import ContentStore, ContentStoreError
from astinuslab.lab_packages.export_service import (
    PackageExportError,
    _canonical_json,
    _hash_source,
    materialize_boot_layer,
)

pytestmark = pytest.mark.unit

PAYLOAD = b"qcow2 boot layer bytes"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


@pytest.fixture()
def store(tmp_path: Path) -> ContentStore:
    return ContentStore(tmp_path / "content", max_bytes=4096)


class _UnseekableStream(io.RawIOBase):
    """A pipe-like stream: readable once, never rewindable."""

    def __init__(self, payload: bytes) -> None:
        self._buffer = io.BytesIO(payload)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)


class _NoneReturningStream(io.RawIOBase):
    """A non-blocking stream with nothing ready: ``read`` yields ``None``."""

    def __init__(self, *, seekable: bool) -> None:
        self._seekable = seekable

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return self._seekable

    def seek(self, offset: int, whence: int = 0) -> int:
        return 0

    def read(self, size: int = -1) -> None:
        return None


def _spool_files() -> list[str]:
    import tempfile

    return sorted(p for p in os.listdir(tempfile.gettempdir()) if p.startswith("astinuslab-layer-"))


# --- _canonical_json -----------------------------------------------------------


def test_canonical_json_is_sorted_and_compact():
    assert _canonical_json({"b": 1, "a": [2, 3]}) == '{"a":[2,3],"b":1}'


def test_canonical_json_refuses_a_non_serializable_snapshot():
    with pytest.raises(PackageExportError, match="unsupported JSON") as caught:
        _canonical_json({"handle": object()})
    assert caught.value.code == "invalid_snapshot"


def test_canonical_json_refuses_non_finite_numbers():
    with pytest.raises(PackageExportError, match="unsupported JSON"):
        _canonical_json({"ratio": float("inf")})


# --- _hash_source --------------------------------------------------------------


def test_hash_source_accepts_raw_bytes():
    assert _hash_source(PAYLOAD, maximum=4096) == (DIGEST, len(PAYLOAD))


def test_hash_source_accepts_a_memoryview():
    assert _hash_source(memoryview(PAYLOAD), maximum=4096) == (DIGEST, len(PAYLOAD))


def test_hash_source_accepts_a_regular_path(tmp_path: Path):
    layer = tmp_path / "layer.qcow2"
    layer.write_bytes(PAYLOAD)

    assert _hash_source(layer, maximum=4096) == (DIGEST, len(PAYLOAD))


def test_hash_source_accepts_a_stream():
    assert _hash_source(io.BytesIO(PAYLOAD), maximum=4096) == (DIGEST, len(PAYLOAD))


def test_hash_source_refuses_a_symlinked_layer(tmp_path: Path):
    (tmp_path / "real.qcow2").write_bytes(PAYLOAD)
    link = tmp_path / "link.qcow2"
    link.symlink_to(tmp_path / "real.qcow2")

    with pytest.raises(PackageExportError, match="must be a regular file") as caught:
        _hash_source(link, maximum=4096)
    assert caught.value.code == "unsafe_asset"


def test_hash_source_refuses_a_directory(tmp_path: Path):
    (tmp_path / "layers").mkdir()

    with pytest.raises(PackageExportError, match="must be a regular file"):
        _hash_source(tmp_path / "layers", maximum=4096)


def test_hash_source_reports_a_missing_layer_as_unavailable(tmp_path: Path):
    with pytest.raises(PackageExportError, match="unavailable") as caught:
        _hash_source(tmp_path / "absent.qcow2", maximum=4096)
    assert caught.value.code == "asset_unavailable"


def test_hash_source_refuses_a_layer_over_the_ceiling():
    with pytest.raises(PackageExportError, match="too large") as caught:
        _hash_source(PAYLOAD, maximum=4)
    assert caught.value.code == "asset_too_large"


def test_hash_source_refuses_an_empty_layer():
    with pytest.raises(PackageExportError, match="must not be empty") as caught:
        _hash_source(b"", maximum=4096)
    assert caught.value.code == "empty_asset"


def test_hash_source_refuses_an_unsupported_source():
    with pytest.raises(PackageExportError, match="source is invalid") as caught:
        _hash_source(12345, maximum=4096)  # type: ignore[arg-type]
    assert caught.value.code == "invalid_asset"


def test_hash_source_refuses_a_stream_with_nothing_ready():
    with pytest.raises(PackageExportError, match="returned no bytes") as caught:
        _hash_source(_NoneReturningStream(seekable=True), maximum=4096)  # type: ignore[arg-type]
    assert caught.value.code == "invalid_asset"


def test_hash_source_reports_a_failing_stream_as_unavailable():
    class _FailingStream(io.RawIOBase):
        def readable(self) -> bool:
            return True

        def read(self, size: int = -1) -> bytes:
            raise OSError("device disappeared")

    with pytest.raises(PackageExportError, match="could not be read") as caught:
        _hash_source(_FailingStream(), maximum=4096)  # type: ignore[arg-type]
    assert caught.value.code == "asset_unavailable"


# --- materialize_boot_layer ----------------------------------------------------


def test_materialize_publishes_raw_bytes(store: ContentStore):
    stored, media_type = materialize_boot_layer(store, PAYLOAD)

    assert stored.sha256 == DIGEST
    assert stored.size_bytes == len(PAYLOAD)
    assert media_type == "application/x-qcow2"


def test_materialize_publishes_a_path(store: ContentStore, tmp_path: Path):
    layer = tmp_path / "layer.qcow2"
    layer.write_bytes(PAYLOAD)

    stored, _ = materialize_boot_layer(store, layer)

    assert stored.sha256 == DIGEST


def test_materialize_rewinds_a_seekable_stream(store: ContentStore):
    stream = io.BytesIO(PAYLOAD)

    stored, _ = materialize_boot_layer(store, stream)

    assert stored.sha256 == DIGEST
    assert stored.size_bytes == len(PAYLOAD)


def test_materialize_spools_an_unseekable_stream(store: ContentStore):
    before = _spool_files()

    stored, _ = materialize_boot_layer(store, _UnseekableStream(PAYLOAD))  # type: ignore[arg-type]

    assert stored.sha256 == DIGEST
    assert _spool_files() == before


def test_materialize_honours_an_explicit_media_type(store: ContentStore):
    _, media_type = materialize_boot_layer(store, PAYLOAD, media_type="application/x-raw-disk")

    assert media_type == "application/x-raw-disk"


def test_materialize_refuses_an_unseekable_stream_over_the_ceiling(store: ContentStore):
    before = _spool_files()

    with pytest.raises(PackageExportError, match="too large") as caught:
        materialize_boot_layer(store, _UnseekableStream(PAYLOAD), maximum=4)  # type: ignore[arg-type]

    assert caught.value.code == "asset_too_large"
    assert _spool_files() == before


def test_materialize_refuses_an_empty_unseekable_stream(store: ContentStore):
    before = _spool_files()

    with pytest.raises(PackageExportError, match="must not be empty") as caught:
        materialize_boot_layer(store, _UnseekableStream(b""))  # type: ignore[arg-type]

    assert caught.value.code == "empty_asset"
    assert _spool_files() == before


def test_materialize_refuses_an_unseekable_stream_with_nothing_ready(store: ContentStore):
    before = _spool_files()

    with pytest.raises(PackageExportError, match="returned no bytes"):
        materialize_boot_layer(store, _NoneReturningStream(seekable=False))  # type: ignore[arg-type]

    assert _spool_files() == before


def test_materialize_reports_a_store_failure_and_removes_the_spool(store: ContentStore, monkeypatch):
    before = _spool_files()

    def refusing_put(*args, **kwargs):
        raise ContentStoreError("store is full")

    monkeypatch.setattr(store, "put", refusing_put)

    with pytest.raises(PackageExportError, match="could not publish") as caught:
        materialize_boot_layer(store, _UnseekableStream(PAYLOAD))  # type: ignore[arg-type]

    assert caught.value.code == "asset_publish_failed"
    assert _spool_files() == before
