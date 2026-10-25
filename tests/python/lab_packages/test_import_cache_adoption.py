"""Import may retain verified immutable bytes without stealing a writer lease."""

from __future__ import annotations

import fcntl
import hashlib
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from astinuslab.db.models import (
    LabContentObject,
    LabContentPublicationAttempt,
    LabPackage,
    LabPackageAsset,
    LabPackagePublicationAttempt,
    LabStorageLease,
)
from astinuslab.lab_packages.content_store import ContentStore, ContentStoreError, StoredAsset
from astinuslab.lab_packages.package_service import (
    LabPackageService,
    PackageDeletionConflictError,
    PackageImportError,
)
from astinuslab.lab_packages.verification_service import _record_content_asset
from tests.python._support.lab_packages import service_package


@dataclass
class CacheFixture:
    service: LabPackageService
    session: Session
    source: Path
    payload: bytes
    item: StoredAsset
    inode: tuple[int, int]
    lease_id: str
    expiry: datetime

    def assert_verification_custody(self) -> None:
        current = self.item.path.stat()
        assert (current.st_dev, current.st_ino) == self.inode
        assert self.item.path.read_bytes() == self.payload
        lease = self.session.get(LabStorageLease, self.lease_id)
        assert lease is not None
        assert lease.owner_id == "lva-fixture" and lease.operation_kind == "lab_verification"
        assert lease.expires_at == self.expiry
        assert not list(self.service.content_store.root.glob("quarantine/*"))


@pytest.fixture
def verified_cache(tmp_path: Path, in_memory_session_scope: Any) -> Iterator[CacheFixture]:
    source, payload = service_package(tmp_path)
    store = ContentStore(tmp_path / "content")
    item = store.put(payload, sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=store)
    expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)
    with in_memory_session_scope() as session:
        _record_content_asset(session, item, owner_id="lva-fixture", expires_at=expiry)
        session.commit()
        lease = session.scalar(select(LabStorageLease).where(LabStorageLease.owner_id == "lva-fixture"))
        assert lease is not None
        current = item.path.stat()
        yield CacheFixture(service, session, source, payload, item, (current.st_dev, current.st_ino), lease.id, expiry)


@pytest.mark.parametrize("streamed", [False, True])
def test_import_reuses_verification_leased_bytes_without_republication(
    verified_cache: CacheFixture, monkeypatch: pytest.MonkeyPatch, streamed: bool
) -> None:
    fixture = verified_cache
    if streamed:
        monkeypatch.setattr("astinuslab.lab_packages.archive.PACKAGE_INLINE_PAYLOAD_BYTES", 8)

    def refuse_put(*args: Any, **kwargs: Any) -> StoredAsset:
        raise AssertionError("immutable reuse must not enter a content mutation path")

    monkeypatch.setattr(fixture.service.content_store, "put", refuse_put)
    imported = fixture.service.import_path(fixture.source, fixture.session)

    assert len(imported.embedded_assets) == 1 and imported.embedded_assets[0].from_cache
    assert fixture.session.scalar(select(LabPackageAsset).where(LabPackageAsset.content_sha256 == fixture.item.sha256))
    fixture.assert_verification_custody()
    assert list(fixture.session.scalars(select(LabStorageLease.id))) == [fixture.lease_id]


def test_reconcile_completes_existing_published_archive_with_live_verification_lease(
    verified_cache: CacheFixture,
) -> None:
    fixture = verified_cache
    service, session = fixture.service, fixture.session
    staged = service.stage_inspection(fixture.source, session)
    inspection = service.get_staged_inspection(staged.id, session)
    destination = service.package_root / f"service-lab-1.0.0-{inspection.archive_sha256}.astinuslab"
    attempt = LabPackagePublicationAttempt(
        id="lpp-interrupted",
        inspection_id=inspection.id,
        archive_sha256=inspection.archive_sha256,
        package_id="service-lab",
        version="1.0.0",
        staging_path=inspection.staged_path,
        final_path=str(destination),
        state="published",
        error_summary="content is leased by an active operation",
    )
    session.add(attempt)
    service._acquire_lease(
        session,
        kind="package_identity",
        resource_id="service-lab@1.0.0",
        owner_id=attempt.id,
        operation="package_import",
    )
    session.commit()
    Path(inspection.staged_path).replace(destination)
    archived = destination.read_bytes()

    report = service.reconcile(session)

    assert report["completed_publications"] == 1 and report["quarantined_publications"] == 0
    session.refresh(attempt)
    session.refresh(inspection)
    assert attempt.state == "completed" and inspection.state == "committed"
    assert destination.read_bytes() == archived
    assert session.scalar(select(LabPackage)) is not None
    fixture.assert_verification_custody()
    assert list(session.scalars(select(LabStorageLease.id))) == [fixture.lease_id]
    assert service.reconcile(session)["completed_publications"] == 0


def test_adoption_pin_is_durable_before_asset_ownership_and_survives_interruption(
    verified_cache: CacheFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = verified_cache
    service, session = fixture.service, fixture.session
    original = LabPackageService._record_asset_ownership
    pin_ids: list[str] = []

    def interrupted(self: LabPackageService, current: Session, *args: Any, **kwargs: Any) -> None:
        assert self is service
        current.rollback()  # A durable pin must survive loss of pending record changes.
        pin = current.scalar(select(LabStorageLease).where(LabStorageLease.operation_kind == "content_adopt"))
        assert pin is not None and pin.resource_id == fixture.item.sha256
        publication = current.get(LabPackagePublicationAttempt, pin.owner_id)
        assert publication is not None and publication.state == "published"
        assert current.get(LabStorageLease, fixture.lease_id) is not None
        pin_ids.append(pin.id)
        with pytest.raises(PackageDeletionConflictError, match="leased"):
            service._acquire_lease(
                current,
                kind="content",
                resource_id=fixture.item.sha256,
                owner_id="competing-mutation",
                operation="content_publish",
            )
        raise RuntimeError("interrupted before package asset commit")

    monkeypatch.setattr(LabPackageService, "_record_asset_ownership", interrupted)
    with pytest.raises(PackageImportError, match="could not commit package inspection") as failure:
        service.import_path(fixture.source, session)
    assert str(failure.value.__cause__) == "interrupted before package asset commit"
    assert pin_ids and session.get(LabStorageLease, pin_ids[0]) is not None
    assert session.scalar(select(LabPackageAsset)) is None
    fixture.assert_verification_custody()

    monkeypatch.setattr(LabPackageService, "_record_asset_ownership", original)
    assert service.reconcile(session)["completed_publications"] == 1
    assert session.get(LabStorageLease, pin_ids[0]) is None
    fixture.assert_verification_custody()


def test_adoption_commits_its_pin_before_releasing_either_digest_lock(
    verified_cache: CacheFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = verified_cache
    service, session = fixture.service, fixture.session
    original_commit = session.commit
    observed: list[str] = []
    paths = (
        service._resource_lock_path("content", fixture.item.sha256),
        fixture.item.path.with_name(f".{fixture.item.sha256}.lock"),
    )

    def commit() -> None:
        pins = [
            row for row in session.new if isinstance(row, LabStorageLease) and row.operation_kind == "content_adopt"
        ]
        if pins:
            for path in paths:
                with path.open("rb") as stream:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            observed.append(pins[0].id)
        original_commit()

    monkeypatch.setattr(session, "commit", commit)
    service.import_path(fixture.source, session)
    assert len(observed) == 1
    for path in paths:
        with path.open("rb") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    fixture.assert_verification_custody()


def test_adoption_pin_retains_content_when_original_reader_expires(verified_cache: CacheFixture) -> None:
    fixture = verified_cache
    service, session = fixture.service, fixture.session
    # Exercise the interval before package-asset ownership exists. The new
    # caller must remain protected even if the earlier reader expires then.
    with service.content_lock(fixture.item.sha256):
        assert service._reuse_embedded_asset(session, fixture.item.sha256, len(fixture.payload), owner_id="adopter")
    verification_lease = session.get(LabStorageLease, fixture.lease_id)
    assert verification_lease is not None
    verification_lease.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
    session.commit()

    report = service.garbage_collect(session, grace_seconds=0)

    assert report["removed_content"] == 0
    assert fixture.item.path.read_bytes() == fixture.payload
    assert session.scalar(select(LabStorageLease).where(LabStorageLease.owner_id == "adopter")) is not None


@pytest.mark.parametrize("unsafe", ["missing", "corrupt", "symlink", "hardlink", "record-path"])
def test_import_refuses_unsafe_leased_cache_without_mutation(
    verified_cache: CacheFixture, tmp_path: Path, unsafe: str
) -> None:
    fixture = verified_cache
    path = fixture.item.path
    sentinel = tmp_path / "unrelated"
    sentinel.write_bytes(fixture.payload)
    if unsafe == "missing":
        path.unlink()
    elif unsafe == "corrupt":
        path.write_bytes(b"X" * len(fixture.payload))
    elif unsafe == "symlink":
        path.unlink()
        path.symlink_to(sentinel)
    elif unsafe == "hardlink":
        os.link(path, tmp_path / "second-link")
    else:
        row = fixture.session.get(LabContentObject, {"sha256": fixture.item.sha256, "size_bytes": len(fixture.payload)})
        assert row is not None
        row.path = str(sentinel)
        fixture.session.commit()
    before = None if unsafe == "missing" else (path.lstat(), path.read_bytes())

    with pytest.raises(PackageImportError):
        fixture.service.import_path(fixture.source, fixture.session)

    if before is None:
        assert not path.exists()
    else:
        assert path.lstat() == before[0] and path.read_bytes() == before[1]
    assert sentinel.read_bytes() == fixture.payload
    lease = fixture.session.get(LabStorageLease, fixture.lease_id)
    assert lease is not None and lease.expires_at == fixture.expiry
    assert fixture.session.scalar(select(LabPackage)) is None
    assert (
        fixture.session.scalar(select(LabStorageLease.id).where(LabStorageLease.operation_kind == "content_adopt"))
        is None
    )


def test_missing_cache_with_existing_publication_still_requires_exclusive_mutation_lease(
    verified_cache: CacheFixture,
) -> None:
    fixture = verified_cache
    fixture.item.path.unlink()
    attempt = LabContentPublicationAttempt(
        id="lcp-other-publisher",
        sha256=fixture.item.sha256,
        size_bytes=len(fixture.payload),
        final_path=str(fixture.item.path),
        state="reserved",
    )
    fixture.session.add(attempt)
    fixture.session.commit()

    with pytest.raises(PackageImportError, match="leased"):
        fixture.service.import_path(fixture.source, fixture.session)

    assert not fixture.item.path.exists()
    assert fixture.session.get(LabStorageLease, fixture.lease_id) is not None
    assert fixture.session.scalar(select(LabPackage)) is None


def test_missing_cache_does_not_republish_over_wrong_content_record_path(
    verified_cache: CacheFixture,
    tmp_path: Path,
) -> None:
    fixture = verified_cache
    fixture.item.path.unlink()
    row = fixture.session.get(LabContentObject, {"sha256": fixture.item.sha256, "size_bytes": len(fixture.payload)})
    assert row is not None
    row.path = str(tmp_path / "unrelated")
    fixture.session.commit()
    inspection, _signature = fixture.service.inspect(fixture.source)

    with pytest.raises(PackageImportError, match="different path"):
        fixture.service._cache_embedded_assets(fixture.session, inspection, adoption_owner_id="adopter")

    assert not fixture.item.path.exists()
    assert row.path == str(tmp_path / "unrelated")
    assert fixture.session.get(LabStorageLease, fixture.lease_id) is not None


def test_cached_probe_refuses_reference_size_mismatch_without_removing_valid_bytes(
    verified_cache: CacheFixture,
) -> None:
    fixture = verified_cache
    with pytest.raises(ContentStoreError, match="size mismatch"):
        with fixture.service.content_store.locked_cached_asset(
            fixture.item.sha256, size_bytes=len(fixture.payload) + 1
        ):
            pytest.fail("size mismatch was accepted")
    fixture.assert_verification_custody()


def test_cached_probe_refuses_inode_replacement_during_hash_without_unlinking(
    verified_cache: CacheFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = verified_cache
    store = fixture.service.content_store
    original = store._stream_digest
    replacement = fixture.item.path.with_suffix(".replacement")
    replacement.write_bytes(fixture.payload)

    def swap(handle: BinaryIO, *, limit: int | None = None) -> tuple[int, str]:
        result = original(handle, limit=limit)
        replacement.replace(fixture.item.path)
        return result

    monkeypatch.setattr(store, "_stream_digest", swap)
    with pytest.raises(ContentStoreError, match="changed"):
        with store.locked_cached_asset(fixture.item.sha256, size_bytes=len(fixture.payload)):
            pytest.fail("replacement inode was accepted")
    assert fixture.item.path.read_bytes() == fixture.payload
    assert fixture.session.get(LabStorageLease, fixture.lease_id) is not None
