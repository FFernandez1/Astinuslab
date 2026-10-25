"""Path and identity safety contract for the content-addressed asset store.

The store is the one place where package bytes land on disk under a name
derived from their digest, so it has to refuse anything that would let a
crafted package escape its tree: a symlinked component on the way to the root,
a cache entry replaced by a symlink or a device node, a digest that is not a
digest, or a size outside the configured ceiling. The publication happy path is
covered in ``test_content_store.py``; this module drives the refusals.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

from astinuslab.lab_packages.content_store import (
    ContentStore,
    ContentStoreError,
    _is_symlink_or_nonregular,
    _validate_digest,
    _validate_size,
)

pytestmark = pytest.mark.unit

PAYLOAD = b"asset bytes"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


@pytest.fixture()
def store(tmp_path: Path) -> ContentStore:
    return ContentStore(tmp_path / "content", max_bytes=4096)


# --- validators ----------------------------------------------------------------


def test_validate_digest_accepts_a_lowercase_sha256():
    assert _validate_digest(DIGEST) == DIGEST


@pytest.mark.parametrize(
    "value",
    ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64, DIGEST + " ", 12345, None],
)
def test_validate_digest_refuses_anything_else(value):
    with pytest.raises(ContentStoreError, match="invalid SHA-256 digest"):
        _validate_digest(value)


def test_validate_size_accepts_a_size_inside_the_ceiling():
    assert _validate_size(16, 32) == 16


@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, None, "16"])
def test_validate_size_refuses_a_non_positive_integer(value):
    with pytest.raises(ContentStoreError, match="invalid asset size"):
        _validate_size(value, 32)


def test_validate_size_refuses_a_size_over_the_ceiling():
    with pytest.raises(ContentStoreError, match="too large"):
        _validate_size(33, 32)


def test_symlink_or_nonregular_accepts_a_regular_file(tmp_path: Path):
    entry = tmp_path / "asset"
    entry.write_bytes(PAYLOAD)

    _is_symlink_or_nonregular(entry, entry.lstat())


def test_symlink_or_nonregular_refuses_a_symlink(tmp_path: Path):
    (tmp_path / "real").write_bytes(PAYLOAD)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real")

    with pytest.raises(ContentStoreError, match="is a symlink"):
        _is_symlink_or_nonregular(link, link.lstat())


def test_symlink_or_nonregular_refuses_a_fifo(tmp_path: Path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)

    with pytest.raises(ContentStoreError, match="not a regular file"):
        _is_symlink_or_nonregular(fifo, fifo.lstat())


# --- construction --------------------------------------------------------------


def test_store_creates_a_private_tree(tmp_path: Path):
    root = tmp_path / "content"

    ContentStore(root)

    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "sha256").stat().st_mode) == 0o700


def test_store_accepts_a_string_root(tmp_path: Path):
    created = ContentStore(str(tmp_path / "content"))  # type: ignore[arg-type]

    assert created.root == tmp_path / "content"


@pytest.mark.parametrize("limit", [0, -1, True, 2.5, None])
def test_store_refuses_an_invalid_size_limit(tmp_path: Path, limit):
    with pytest.raises(ContentStoreError, match="invalid store size limit"):
        ContentStore(tmp_path / "content", max_bytes=limit)


def test_store_refuses_a_symlinked_root(tmp_path: Path):
    (tmp_path / "real").mkdir()
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real", target_is_directory=True)

    with pytest.raises(ContentStoreError, match="symlink"):
        ContentStore(link)


def test_store_refuses_a_symlinked_path_component(tmp_path: Path):
    (tmp_path / "real").mkdir()
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real", target_is_directory=True)

    with pytest.raises(ContentStoreError, match="symlink"):
        ContentStore(link / "content")


def test_store_refuses_a_root_that_is_a_file(tmp_path: Path):
    blocker = tmp_path / "blocker"
    blocker.write_bytes(PAYLOAD)

    with pytest.raises(ContentStoreError, match="could not create content store root"):
        ContentStore(blocker)


# --- deterministic paths -------------------------------------------------------


def test_path_for_buckets_by_digest_prefix(store: ContentStore):
    path = store.path_for(DIGEST)

    assert path.parent.name == DIGEST[:2]
    assert path.name == DIGEST
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_partial_path_is_hidden_beside_the_final_path(store: ContentStore):
    assert store.partial_path_for(DIGEST).name == f".{DIGEST}.partial"
    assert store.partial_path_for(DIGEST).parent == store.path_for(DIGEST).parent


def test_path_for_refuses_a_malformed_digest(store: ContentStore):
    with pytest.raises(ContentStoreError, match="invalid SHA-256 digest"):
        store.path_for("not-a-digest")


# --- partial_size --------------------------------------------------------------


def test_partial_size_is_zero_when_no_transfer_exists(store: ContentStore):
    assert store.partial_size(DIGEST, size_bytes=len(PAYLOAD)) == 0


def test_partial_size_reports_a_resumable_prefix(store: ContentStore):
    partial = store.partial_path_for(DIGEST)
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(PAYLOAD[:4])

    assert store.partial_size(DIGEST, size_bytes=len(PAYLOAD)) == 4


def test_partial_size_discards_a_prefix_longer_than_the_asset(store: ContentStore):
    partial = store.partial_path_for(DIGEST)
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(PAYLOAD + b"overrun")

    assert store.partial_size(DIGEST, size_bytes=len(PAYLOAD)) == 0
    assert not partial.exists()


def test_partial_size_refuses_a_symlinked_transfer(store: ContentStore, tmp_path: Path):
    (tmp_path / "elsewhere").write_bytes(PAYLOAD)
    partial = store.partial_path_for(DIGEST)
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.symlink_to(tmp_path / "elsewhere")

    with pytest.raises(ContentStoreError, match="is a symlink"):
        store.partial_size(DIGEST, size_bytes=len(PAYLOAD))


def test_partial_size_refuses_a_malformed_digest(store: ContentStore):
    with pytest.raises(ContentStoreError, match="invalid SHA-256 digest"):
        store.partial_size("nope", size_bytes=1)


def test_partial_size_refuses_an_invalid_expected_size(store: ContentStore):
    with pytest.raises(ContentStoreError, match="invalid asset size"):
        store.partial_size(DIGEST, size_bytes=0)


# --- _open_regular -------------------------------------------------------------


def test_open_regular_reads_a_regular_file(tmp_path: Path):
    asset = tmp_path / "asset"
    asset.write_bytes(PAYLOAD)

    with ContentStore._open_regular(asset) as handle:
        assert handle.read() == PAYLOAD


def test_open_regular_refuses_a_symlink(tmp_path: Path):
    (tmp_path / "real").write_bytes(PAYLOAD)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real")

    with pytest.raises(ContentStoreError, match="is a symlink"):
        ContentStore._open_regular(link)


def test_open_regular_reports_a_missing_asset(tmp_path: Path):
    with pytest.raises(ContentStoreError, match="could not open asset"):
        ContentStore._open_regular(tmp_path / "absent")


def test_open_regular_refuses_a_fifo(tmp_path: Path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)

    with pytest.raises(ContentStoreError, match="not a regular file"):
        ContentStore._open_regular(fifo)
