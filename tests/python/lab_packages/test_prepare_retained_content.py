"""Prepare reads retained immutable content without acquiring mutation rights."""

from __future__ import annotations

import fcntl
import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from astinuslab.db.models import (
    LabContentObject,
    LabOperationOutbox,
    LabPackage,
    LabPackageAsset,
    LabPrepareAttempt,
    LabStorageLease,
)
from astinuslab.lab_packages.archive import write_archive
from astinuslab.lab_packages.content_store import ContentStore, StoredAsset
from astinuslab.lab_packages.models import AssetReference
from astinuslab.lab_packages.package_service import LabPackageService
from astinuslab.lab_packages.prepare_service import PrepareError, cancel_prepare, create_prepare, run_prepare
from astinuslab.lab_packages.resolver import AssetResolver
from astinuslab.lab_packages.verification_service import _record_content_asset
from tests.python._support.lab_packages import service_definition, service_package


@dataclass
class Cache:
    service: LabPackageService
    session: Session
    record: LabPackage
    item: StoredAsset
    payload: bytes

    def retain(self) -> LabStorageLease:
        _record_content_asset(
            self.session,
            self.item,
            owner_id="lva-retained",
            expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1),
        )
        self.session.commit()
        lease = self.session.scalar(select(LabStorageLease).where(LabStorageLease.owner_id == "lva-retained"))
        assert lease is not None
        return lease


@pytest.fixture
def cache(tmp_path: Path, in_memory_session_scope: Any) -> Iterator[Cache]:
    source, payload = service_package(tmp_path)
    store = ContentStore(tmp_path / "content")
    service = LabPackageService(package_root=tmp_path / "packages", content_store=store)
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        sha = hashlib.sha256(payload).hexdigest()
        yield Cache(service, session, record, StoredAsset(sha, len(payload), store.path_for(sha)), payload)


def test_prepare_retained_cache_commits_under_both_locks_without_publication(
    cache: Cache, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = cache.retain()
    expiry, inode = lease.expires_at, cache.item.path.stat().st_ino
    store, session = cache.service.content_store, cache.session

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("retained cache reuse must not mutate or resolve content")

    monkeypatch.setattr(AssetResolver, "resolve", refuse)
    monkeypatch.setattr(store, "put", refuse)
    monkeypatch.setattr(store, "has", refuse)
    original_hash = store._stream_digest
    hash_calls = []

    def checked_hash(*args: Any, **kwargs: Any) -> Any:
        connection = session.connection().connection.driver_connection
        assert connection is not None and not connection.in_transaction
        hash_calls.append(True)
        return original_hash(*args, **kwargs)

    monkeypatch.setattr(store, "_stream_digest", checked_hash)
    prepare = create_prepare(session, record=cache.record, service=cache.service, idempotency_key="retained")
    session.commit()
    assert not hash_calls, "admission must not hash while holding its writer"
    pin = session.scalar(
        select(LabStorageLease).where(
            LabStorageLease.owner_id == prepare.id, LabStorageLease.resource_kind == "content"
        )
    )
    assert pin is not None
    session.rollback()
    outer = cache.service.content_lock
    held = False

    @contextmanager
    def watched_lock(digest: str) -> Iterator[None]:
        nonlocal held
        connection = session.connection().connection.driver_connection
        assert connection is not None and not connection.in_transaction
        with outer(digest):
            held = True
            yield
            held = False

    monkeypatch.setattr(cache.service, "content_lock", watched_lock)
    commit = session.commit
    ready_commits = []

    def checked_commit() -> None:
        ready = [row for row in session.dirty if isinstance(row, LabPackageAsset) and row.prepare_state == "ready"]
        if ready:
            assert held
            lock_path = cache.item.path.parent / ("." + cache.item.sha256 + ".lock")
            with lock_path.open("a") as descriptor:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            ready_commits.append(True)
        commit()

    monkeypatch.setattr(session, "commit", checked_commit)
    result = run_prepare(session, prepare.id, service=cache.service)
    assert result.state == "completed" and result.assets[0].origin == "cache"
    assert hash_calls and ready_commits
    assert (
        create_prepare(session, record=cache.record, service=cache.service, idempotency_key="retained").id == prepare.id
    )
    assert cache.item.path.read_bytes() == cache.payload and cache.item.path.stat().st_ino == inode
    session.refresh(lease)
    assert lease.expires_at == expiry
    assert list(session.scalars(select(LabStorageLease.id))) == [lease.id]


@pytest.mark.parametrize("damage", ["missing", "corrupt", "symlink", "hardlink"])
def test_prepare_preserves_foreign_retained_missing_or_invalid_cache(
    cache: Cache, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    lease = cache.retain()
    path = cache.item.path
    retained = path.with_suffix(".retained")
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_bytes(b"invalid retained bytes")
    elif damage == "symlink":
        path.rename(retained)
        path.symlink_to(retained)
    else:
        os.link(path, retained)

    def custody() -> tuple[int, ...] | None:
        if not path.exists():
            return None
        info = path.lstat()
        return tuple(
            getattr(info, field)
            for field in (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_uid",
                "st_gid",
                "st_nlink",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
        )

    before = custody()
    expected = path.read_bytes() if path.exists() else None

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("foreign retention must exclude destructive resolver fallback")

    monkeypatch.setattr(AssetResolver, "resolve", refuse)
    monkeypatch.setattr(cache.service.content_store, "has", refuse)
    attempt = create_prepare(cache.session, record=cache.record, service=cache.service, idempotency_key="damaged")
    cache.session.commit()
    result = run_prepare(cache.session, attempt.id, service=cache.service)
    assert result.state == "failed"
    assert custody() == before
    assert (path.read_bytes() if path.exists() else None) == expected
    assert cache.session.get(LabStorageLease, lease.id) is not None
    assert list(cache.session.scalars(select(LabStorageLease.id))) == [lease.id]


@pytest.mark.parametrize("damage", ("size", "digest"))
def test_prepare_repairs_unleased_corrupt_embedded_content(cache: Cache, damage: str) -> None:
    cache.item.path.write_bytes(b"corrupt" if damage == "size" else b"x" * len(cache.payload))
    attempt = create_prepare(cache.session, record=cache.record, service=cache.service, idempotency_key="repair")
    cache.session.commit()
    assert run_prepare(cache.session, attempt.id, service=cache.service).state == "completed"
    assert cache.item.path.read_bytes() == cache.payload


def test_retained_ready_asset_recovery_never_deletes_corruption(cache: Cache, monkeypatch: pytest.MonkeyPatch) -> None:
    lease = cache.retain()
    handle = create_prepare(cache.session, record=cache.record, service=cache.service, idempotency_key="ready-recovery")
    cache.session.commit()
    assert run_prepare(cache.session, handle.id, service=cache.service).state == "completed"
    attempt = cache.session.get(LabPrepareAttempt, handle.id)
    event = cache.session.scalar(select(LabOperationOutbox).where(LabOperationOutbox.operation_id == handle.id))
    assert attempt is not None and event is not None
    attempt.state, event.state = "running", "claimed"
    cache.session.commit()
    cache.item.path.write_bytes(b"retained invalid cache")
    inode = cache.item.path.stat().st_ino
    monkeypatch.setattr(cache.service.content_store, "has", lambda *a, **k: pytest.fail("destructive ready probe"))
    monkeypatch.setattr(AssetResolver, "resolve", lambda *a, **k: pytest.fail("retained corruption reached resolver"))
    assert run_prepare(cache.session, handle.id, service=cache.service).state == "failed"
    assert cache.item.path.read_bytes() == b"retained invalid cache" and cache.item.path.stat().st_ino == inode
    assert list(cache.session.scalars(select(LabStorageLease.id))) == [lease.id]


def test_cancellation_during_retained_cache_hash_preserves_verification_pin(
    cache: Cache, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = cache.retain()
    handle = create_prepare(cache.session, record=cache.record, service=cache.service, idempotency_key="cancel-read")
    cache.session.commit()
    original_hash = cache.service.content_store._stream_digest

    def cancel_during_hash(*args: Any, **kwargs: Any) -> Any:
        result = original_hash(*args, **kwargs)
        cancel_prepare(cache.session, handle.id, service=cache.service)
        cache.session.commit()
        return result

    monkeypatch.setattr(cache.service.content_store, "_stream_digest", cancel_during_hash)
    assert run_prepare(cache.session, handle.id, service=cache.service).state == "cancelled"
    assert cache.item.path.read_bytes() == cache.payload
    assert list(cache.session.scalars(select(LabStorageLease.id))) == [lease.id]


def test_prepare_refuses_noncanonical_content_mapping_without_repair(
    cache: Cache, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = cache.session.get(LabContentObject, {"sha256": cache.item.sha256, "size_bytes": len(cache.payload)})
    assert content is not None
    foreign = tmp_path / "foreign-retained"
    foreign.write_bytes(cache.payload)
    content.path = str(foreign)
    cache.session.commit()
    handle = create_prepare(cache.session, record=cache.record, service=cache.service, idempotency_key="mapping")
    cache.session.commit()
    monkeypatch.setattr(AssetResolver, "resolve", lambda *a, **k: pytest.fail("invalid mapping reached resolver"))
    assert run_prepare(cache.session, handle.id, service=cache.service).state == "failed"
    cache.session.refresh(content)
    assert content.path == str(foreign) and foreign.read_bytes() == cache.item.path.read_bytes() == cache.payload


def test_competing_missing_content_prepare_keeps_exclusive_admission(cache: Cache, tmp_path: Path) -> None:
    reference = AssetReference(
        sha256=cache.item.sha256, size_bytes=len(cache.payload), media_type="application/x-qcow2", license="CC-BY-4.0"
    )
    definition = service_definition()
    definition = definition.model_copy(
        update={
            "package_id": "other-package",
            "vms": (definition.vms[0].model_copy(update={"clean_layer": reference}),),
        }
    )
    source = tmp_path / "other.astinuslab"
    source.write_bytes(write_archive(definition, "# Other\n"))
    second = cache.service.import_path(source, cache.session).record
    asset = cache.session.scalar(
        select(LabPackageAsset).where(
            LabPackageAsset.package_record_id == cache.record.id, LabPackageAsset.content_sha256 == cache.item.sha256
        )
    )
    assert asset is not None
    asset.content_sha256 = asset.content_size_bytes = None
    content = cache.session.get(LabContentObject, {"sha256": cache.item.sha256, "size_bytes": len(cache.payload)})
    assert content is not None
    cache.session.delete(content)
    cache.item.path.unlink()
    cache.session.commit()
    first = create_prepare(cache.session, record=cache.record, service=cache.service, idempotency_key="first-missing")
    cache.session.commit()
    with pytest.raises(PrepareError, match="content is leased"):
        create_prepare(cache.session, record=second, service=cache.service, idempotency_key="second-missing")
    cache.session.rollback()
    assert run_prepare(cache.session, first.id, service=cache.service).state == "completed"
