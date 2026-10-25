"""Shared package bytes and overlapping read custody regressions."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from astinuslab.db.models import LabContentObject, LabPackageAsset, LabStorageLease
from astinuslab.lab_packages import api, package_service
from astinuslab.lab_packages.archive import write_archive
from astinuslab.lab_packages.content_store import ContentStore, ContentStoreError
from astinuslab.lab_packages.models import LabDefinition
from astinuslab.lab_packages.package_service import LabPackageService
from tests.python._support.lab_packages import file_session_scope, service_definition


def test_preflight_size_claim_preserves_another_packages_valid_content(tmp_path, in_memory_session_scope):
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    payload = b"synthetic immutable boot layer"
    digest = hashlib.sha256(payload).hexdigest()
    raw = service_definition().model_dump(mode="json")
    raw["vms"][0]["boot_layer"] = {
        "sha256": digest,
        "size_bytes": len(payload),
        "media_type": "application/x-qcow2",
        "embedded": True,
        "license": "CC-BY-4.0",
        "redistribution_confirmed": True,
    }
    source = tmp_path / "good.astinuslab"
    source.write_bytes(write_archive(LabDefinition.model_validate(raw), "# Good", assets={digest: payload}))
    with in_memory_session_scope() as session:
        good_id = service.import_path(source, session).record.id
    raw["package_id"] = "inconsistent-size-lab"
    raw["vms"][0]["boot_layer"].update(size_bytes=len(payload) + 1, embedded=False)
    source = tmp_path / "bad-metadata.astinuslab"
    source.write_bytes(write_archive(LabDefinition.model_validate(raw), "# Size typo"))
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        definition = LabDefinition.model_validate_json(record.definition_json)
        assert api._available_asset_digests(definition, service) == frozenset()
        assert service.content_store.path_for(digest).read_bytes() == payload
        assert session.scalar(
            select(LabPackageAsset.id).where(
                LabPackageAsset.package_record_id == good_id, LabPackageAsset.content_sha256 == digest
            )
        )
        assert session.get(LabContentObject, {"sha256": digest, "size_bytes": len(payload)}) is not None


@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("operation", ["has", "open", "put", "put_resumable"])
def test_contradictory_expected_size_never_removes_valid_content(tmp_path, warm, operation):
    store = ContentStore(tmp_path / "content")
    payload = b"shared valid bytes"
    digest = hashlib.sha256(payload).hexdigest()
    stored = store.put(payload, sha256=digest, size_bytes=len(payload))
    if not warm:
        store = ContentStore(tmp_path / "content")
    if operation == "has":
        assert not store.has(digest, size_bytes=len(payload) + 1)
    elif operation == "open":
        with pytest.raises(ContentStoreError, match="size mismatch"):
            with store.open(digest, size_bytes=len(payload) + 1):
                pytest.fail("inconsistent expected size was accepted")
    elif operation == "put":
        with pytest.raises(ContentStoreError, match="size mismatch"):
            store.put(payload, sha256=digest, size_bytes=len(payload) + 1)
    else:
        assert store.put_resumable(payload, sha256=digest, size_bytes=len(payload) + 1) is None
    assert stored.path.read_bytes() == payload
    assert store.get(digest, size_bytes=len(payload)) == payload


def test_gc_rechecks_lease_committed_at_content_lock(tmp_path, monkeypatch):
    scope = file_session_scope(tmp_path / "gc.sqlite")
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    payload = b"synthetic export content"
    digest = hashlib.sha256(payload).hexdigest()
    stored = service.content_store.put(payload, sha256=digest, size_bytes=len(payload))
    old = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=2)
    with scope() as session:
        session.add(
            LabContentObject(
                sha256=digest, size_bytes=len(payload), path=str(stored.path), created_at=old, last_used_at=old
            )
        )
    original = package_service._exclusive_package_lock
    content_entries = 0

    @contextmanager
    def interleave(path):
        nonlocal content_entries
        with original(path):
            if path == service._resource_lock_path("content", digest):
                content_entries += 1
                # Reconciliation takes the first lock; GC takes the second.
                if content_entries == 2:
                    with scope() as writer:
                        writer.add(
                            LabStorageLease(
                                id="new-export-lease",
                                resource_kind="content",
                                resource_id=digest,
                                operation_kind="package_export",
                                owner_id="export-fixture",
                                expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1),
                            )
                        )
            yield

    monkeypatch.setattr(package_service, "_exclusive_package_lock", interleave)
    with scope() as session:
        result = service.garbage_collect(session, grace_seconds=3600)
        assert content_entries == 2
        assert result["removed_content"] == 0
        assert stored.path.read_bytes() == payload
        assert session.get(LabStorageLease, "new-export-lease") is not None
        assert session.get(LabContentObject, {"sha256": digest, "size_bytes": len(payload)}) is not None


@pytest.mark.parametrize("endpoint", [api.get_lab_package_experience, api.download_lab_package])
def test_overlapping_package_read_returns_structured_conflict(tmp_path, in_memory_session_scope, monkeypatch, endpoint):
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    source = tmp_path / "source.astinuslab"
    source.write_bytes(write_archive(service_definition(), "# Read fixture"))
    monkeypatch.setattr(api, "default_package_service", lambda: service)
    with in_memory_session_scope() as session:
        record_id = service.import_path(source, session).record.id
        owner = service.acquire_download_lease(record_id, session)
        with pytest.raises(HTTPException) as error:
            endpoint(record_id, session)
        assert error.value.status_code == 409
        assert isinstance(error.value.detail, dict)
        assert error.value.detail["code"] == "package_busy"
        leases = list(session.scalars(select(LabStorageLease).where(LabStorageLease.resource_id == record_id)))
        assert [lease.owner_id for lease in leases] == [owner]
