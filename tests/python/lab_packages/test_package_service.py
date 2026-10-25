from __future__ import annotations

import hashlib
import os
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from astinuslab.db.models import (
    LabContentObject,
    LabContentPublicationAttempt,
    LabInstance,
    LabPackage,
    LabPackageAsset,
    LabPackageExportAttempt,
    LabPackagePublicationAttempt,
    LabStorageLease,
)
from astinuslab.lab_packages.content_store import ContentStore
from astinuslab.lab_packages.package_service import (
    LabPackageService,
    PackageDeletionConflictError,
    PackageImportError,
    PackageInspectionExpiredError,
    copy_digest_verified,
)
from astinuslab.lab_packages.prepare_service import create_prepare, run_prepare, status_prepare
from tests.python._support.lab_packages import service_package as _package


def test_prepare_resolves_cached_package_assets_and_is_idempotent(in_memory_session_scope, tmp_path: Path):
    source, content = _package(tmp_path)
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))

    with in_memory_session_scope() as session:
        imported = service.import_path(source, session)
        first = create_prepare(session, record=imported.record, service=service, idempotency_key="prepare-cache")
        session.commit()
        completed = run_prepare(session, first.id, service=service)
        retry = create_prepare(session, record=imported.record, service=service, idempotency_key="prepare-cache")

        assert completed.state == "completed"
        assert retry.id == first.id
        assert retry.assets[0].state == "ready"
        assert retry.assets[0].origin in {"embedded", "cache", "cache-fallback"}
        assert retry.assets[0].sha256 == hashlib.sha256(content).hexdigest()
        assert status_prepare(session, first.id).state == "completed"


def test_prepare_recovers_embedded_bytes_when_the_cache_entry_is_removed(in_memory_session_scope, tmp_path: Path):
    source, content = _package(tmp_path)
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))

    with in_memory_session_scope() as session:
        imported = service.import_path(source, session)
        asset = session.scalar(
            select(LabPackageAsset).where(
                LabPackageAsset.package_record_id == imported.record.id,
                LabPackageAsset.logical_path.like("assets/sha256/%"),
            )
        )
        assert asset is not None
        content_row = session.get(
            LabContentObject, {"sha256": asset.expected_sha256, "size_bytes": asset.expected_size_bytes}
        )
        assert content_row is not None
        Path(content_row.path).unlink()
        asset.content_sha256 = None
        asset.content_size_bytes = None
        session.delete(content_row)
        session.commit()

        handle = create_prepare(session, record=imported.record, service=service, idempotency_key="embedded-recovery")
        session.commit()
        completed = run_prepare(session, handle.id, service=service)

        assert completed.state == "completed"
        assert completed.assets[0].origin == "embedded"
        assert service.content_store.get(asset.expected_sha256, size_bytes=len(content)) == content


def test_prepare_offline_failure_keeps_asset_unpublished_and_can_be_cancelled(in_memory_session_scope, tmp_path: Path):
    source, _ = _package(tmp_path, with_asset=False)
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))

    with in_memory_session_scope() as session:
        imported = service.import_path(source, session)
        first = create_prepare(session, record=imported.record, service=service, idempotency_key="prepare-empty")
        session.commit()
        completed = run_prepare(session, first.id, service=service)

        assert completed.state == "completed"
        assert completed.assets == ()


def test_prepare_lease_blocks_package_deletion_until_operation_finishes(in_memory_session_scope, tmp_path: Path):
    source, _ = _package(tmp_path)
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))

    with in_memory_session_scope() as session:
        imported = service.import_path(source, session)
        handle = create_prepare(session, record=imported.record, service=service, idempotency_key="delete-race")
        session.commit()

        with pytest.raises(PackageDeletionConflictError, match="leased"):
            service.delete_package(imported.record.id, session)
        # End the rejected request's writer reservation before a worker starts.
        session.rollback()

        run_prepare(session, handle.id, service=service)
        service.delete_package(imported.record.id, session)


def test_inspect_is_passive_and_import_caches_embedded_assets(in_memory_session_scope, tmp_path: Path):
    source, content = _package(tmp_path)
    package_root = tmp_path / "packages"
    content_store = ContentStore(tmp_path / "content")
    service = LabPackageService(package_root=package_root, content_store=content_store)

    inspection, signature = service.inspect(source)
    assert inspection.manifest.package_id == "service-lab"
    assert signature.status.value == "unsigned"
    assert list(package_root.glob("*.astinuslab")) == []
    digest = hashlib.sha256(content).hexdigest()
    assert content_store.has(digest, size_bytes=len(content)) is False

    with in_memory_session_scope() as session:
        imported = service.import_path(source, session)
        assert imported.record.package_id == "service-lab"
        assert imported.signature.status.value == "unsigned"
        assert imported.embedded_assets[0].size_bytes == len(content)
        assert imported.embedded_assets[0].path.read_bytes() == content
        assert Path(imported.record.archive_path).exists()


def test_import_streams_descriptor_assets_from_retained_archive(
    monkeypatch: pytest.MonkeyPatch, in_memory_session_scope, tmp_path: Path
):
    monkeypatch.setattr("astinuslab.lab_packages.archive.PACKAGE_INLINE_PAYLOAD_BYTES", 8)
    source, content = _package(tmp_path)
    service = LabPackageService(
        package_root=tmp_path / "packages",
        content_store=ContentStore(tmp_path / "content"),
    )

    with in_memory_session_scope() as session:
        imported = service.import_path(source, session)

    digest = hashlib.sha256(content).hexdigest()
    assert imported.inspection.assets == {}
    assert service.content_store.has(digest, size_bytes=len(content))


def test_import_is_idempotent_for_same_archive(in_memory_session_scope, tmp_path: Path):
    source, _ = _package(tmp_path, with_asset=False)
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))

    with in_memory_session_scope() as session:
        first = service.import_path(source, session)
        second = service.import_path(source, session)
        assert second.record.id == first.record.id
        assert len(list((tmp_path / "packages").glob("*.astinuslab"))) == 1


def test_archive_publication_shares_the_reconciliation_sweep_lock(
    monkeypatch: pytest.MonkeyPatch, in_memory_session_scope, tmp_path: Path
):
    from astinuslab.lab_packages import package_service as package_service_module

    source, _ = _package(tmp_path, with_asset=False)
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    original_lock = package_service_module._exclusive_package_lock
    acquired: list[Path] = []

    @contextmanager
    def tracked_lock(path: Path):
        acquired.append(path)
        with original_lock(path):
            yield

    monkeypatch.setattr(package_service_module, "_exclusive_package_lock", tracked_lock)
    with in_memory_session_scope() as session:
        service.import_path(source, session)

    assert service._archive_sweep_lock_path() in acquired


def test_invalid_archive_does_not_publish_or_register(in_memory_session_scope, tmp_path: Path):
    source = tmp_path / "invalid.astinuslab"
    source.write_bytes(b"not a zip")
    package_root = tmp_path / "packages"
    service = LabPackageService(package_root=package_root, content_store=ContentStore(tmp_path / "content"))

    with in_memory_session_scope() as session:
        with pytest.raises(PackageImportError, match="invalid package archive|invalid"):
            service.import_path(source, session)
        assert list(package_root.glob("*.astinuslab")) == []
        assert service.content_store.has(hashlib.sha256(b"missing").hexdigest()) is False


def test_cleanup_staging_removes_only_stale_interrupted_files(tmp_path: Path):
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    stale_upload = service.package_root / ".astinuslab-upload-stale"
    stale_import = service.package_root / ".astinuslab-import-stale"
    fresh_upload = service.package_root / ".astinuslab-upload-fresh"
    stale_upload.write_bytes(b"upload")
    stale_import.write_bytes(b"import")
    fresh_upload.write_bytes(b"fresh")
    old = time.time() - 120
    os.utime(stale_upload, (old, old))
    os.utime(stale_import, (old, old))

    assert service.cleanup_staging(max_age_seconds=60) == 2
    assert not stale_upload.exists()
    assert not stale_import.exists()
    assert fresh_upload.exists()


def test_copy_digest_verified_only_copies_and_fsyncs_caller_temp_file(tmp_path: Path):
    source = tmp_path / "source"
    source.write_bytes(b"immutable bytes")
    temporary = tmp_path / "caller-owned.partial"

    digest, size = copy_digest_verified(source, temporary)

    assert digest == hashlib.sha256(b"immutable bytes").hexdigest()
    assert size == len(b"immutable bytes")
    assert temporary.read_bytes() == b"immutable bytes"


def test_staged_inspection_commits_a_managed_copy_after_the_upload_is_gone(in_memory_session_scope, tmp_path: Path):
    source, content = _package(tmp_path)
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))

    with in_memory_session_scope() as session:
        staged = service.stage_inspection(source, session)
        source.unlink()
        imported = service.commit_inspection(staged.id, session)
        package_id = imported.record.package_id
        archive_path = Path(imported.record.archive_path)

    assert package_id == "service-lab"
    assert archive_path.is_file()
    assert imported.embedded_assets[0].path.read_bytes() == content


def test_expired_staged_inspection_cannot_be_committed(in_memory_session_scope, tmp_path: Path):
    source, _ = _package(tmp_path, with_asset=False)
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))

    with in_memory_session_scope() as session:
        staged = service.stage_inspection(source, session, expires_in_seconds=1)
        inspection = service.get_staged_inspection(staged.id, session)
        inspection.expires_at = inspection.created_at
        session.flush()
        with pytest.raises(PackageInspectionExpiredError):
            service.commit_inspection(staged.id, session)


def test_reconcile_claims_live_inspection_staging_before_archive_sweep(in_memory_session_scope, tmp_path: Path):
    source, _ = _package(tmp_path, with_asset=False)
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))

    with in_memory_session_scope() as session:
        staged = service.stage_inspection(source, session)
        staged_path = Path(service.get_staged_inspection(staged.id, session).staged_path)
        assert (
            session.scalar(
                select(LabStorageLease.id).where(
                    LabStorageLease.resource_kind == "inspection",
                    LabStorageLease.resource_id == staged.id,
                )
            )
            is not None
        )

        report = service.reconcile(session)

        assert staged_path.exists()
        assert report["quarantined_archives"] == 0


def test_abandoned_inspection_releases_its_staging_lease(in_memory_session_scope, tmp_path: Path):
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    with in_memory_session_scope() as session:
        inspection = service.reserve_inspection(session)

        service.abandon_inspection(inspection.id, session, error_summary="upload interrupted")

        assert service.get_staged_inspection(inspection.id, session).state == "failed"
        assert (
            session.scalar(
                select(LabStorageLease.id).where(
                    LabStorageLease.resource_kind == "inspection",
                    LabStorageLease.resource_id == inspection.id,
                )
            )
            is None
        )


def test_reconcile_finishes_a_publication_crash_after_archive_rename(in_memory_session_scope, tmp_path: Path):
    source, _ = _package(tmp_path, with_asset=False)
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))

    with in_memory_session_scope() as session:
        staged = service.stage_inspection(source, session)
        inspection = service.get_staged_inspection(staged.id, session)
        destination = service.package_root / f"service-lab-1.0.0-{inspection.archive_sha256}.astinuslab"
        attempt = LabPackagePublicationAttempt(
            id="lpp-crashed",
            inspection_id=inspection.id,
            archive_sha256=inspection.archive_sha256,
            package_id="service-lab",
            version="1.0.0",
            staging_path=inspection.staged_path,
            final_path=str(destination),
            state="reserved",
        )
        session.add(attempt)
        session.commit()
        Path(inspection.staged_path).replace(destination)
        attempt = session.get(LabPackagePublicationAttempt, attempt.id)
        assert attempt is not None
        attempt.state = "published"
        session.commit()

        report = service.reconcile(session)
        assert report["completed_publications"] == 1
        assert session.get(LabPackagePublicationAttempt, "lpp-crashed").state == "completed"


def test_reconcile_quarantines_failed_archive_and_releases_its_identity_lease(in_memory_session_scope, tmp_path: Path):
    source, _ = _package(tmp_path, with_asset=False)
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))

    with in_memory_session_scope() as session:
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        destination = service.package_root / "service-lab-1.0.0-failed.astinuslab"
        destination.write_bytes(b"wrong archived bytes")
        session.add(
            LabPackagePublicationAttempt(
                id="lpp-failed",
                archive_sha256=digest,
                package_id="service-lab",
                version="1.0.0",
                staging_path=str(source),
                final_path=str(destination),
                state="failed",
            )
        )
        session.add(
            LabStorageLease(
                id="lease-failed",
                resource_kind="package_identity",
                resource_id="service-lab@1.0.0",
                owner_id="lpp-failed",
                operation_kind="package_commit",
                expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
        )
        session.commit()

        report = service.reconcile(session)

        assert session.get(LabPackagePublicationAttempt, "lpp-failed").state == "failed"
        assert session.scalar(select(LabStorageLease.id).where(LabStorageLease.owner_id == "lpp-failed")) is None
        assert report["quarantined_publications"] == 1
        assert report["quarantined_archives"] == 0
        assert not destination.exists()

        second_report = service.reconcile(session)

        assert second_report["quarantined_publications"] == 0
        assert second_report["quarantined_archives"] == 0


def test_completed_export_identity_is_permanent_and_requires_digest(in_memory_session_scope):
    with in_memory_session_scope() as session:
        session.add(
            LabPackageExportAttempt(
                id="export-one",
                package_id="service-lab",
                version="1.0.0",
                definition_sha256="a" * 64,
                archive_sha256="b" * 64,
                state="completed",
            )
        )
        session.flush()
        session.add(
            LabPackageExportAttempt(
                id="export-two",
                package_id="service-lab",
                version="1.0.0",
                definition_sha256="c" * 64,
                archive_sha256="d" * 64,
                state="completed",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
        session.add(
            LabPackageExportAttempt(
                id="export-missing-digest",
                package_id="other-lab",
                version="1.0.0",
                definition_sha256="e" * 64,
                state="completed",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


def test_delete_removes_terminal_instance_rows_before_package(in_memory_session_scope, tmp_path: Path):
    source, _ = _package(tmp_path, with_asset=False)
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    with in_memory_session_scope() as session:
        imported = service.import_path(source, session)
        session.add(
            LabInstance(
                id="instance-terminal",
                package_record_id=imported.record.id,
                idempotency_key="key",
                state="destroyed",
            )
        )
        session.commit()
        service.delete_package(imported.record.id, session)
        assert session.get(LabInstance, "instance-terminal") is None


def test_delete_preserves_cleanup_incomplete_instance_recovery_intent(in_memory_session_scope, tmp_path: Path):
    source, _ = _package(tmp_path, with_asset=False)
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    with in_memory_session_scope() as session:
        imported = service.import_path(source, session)
        session.add(
            LabInstance(
                id="instance-cleanup",
                package_record_id=imported.record.id,
                idempotency_key="cleanup",
                state="cleanup_incomplete",
            )
        )
        session.commit()

        with pytest.raises(PackageDeletionConflictError, match="active instance"):
            service.delete_package(imported.record.id, session)

        assert session.get(LabInstance, "instance-cleanup") is not None


def test_live_package_lease_blocks_deletion(in_memory_session_scope, tmp_path: Path):
    source, _ = _package(tmp_path, with_asset=False)
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    with in_memory_session_scope() as session:
        imported = service.import_path(source, session)
        session.add(
            LabStorageLease(
                id="lease-live",
                resource_kind="package",
                resource_id=imported.record.id,
                owner_id="worker",
                operation_kind="download",
                expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
        )
        session.commit()
        with pytest.raises(PackageDeletionConflictError, match="leased"):
            service.delete_package(imported.record.id, session)


def test_gc_removes_owned_expired_inspection_partial(in_memory_session_scope, tmp_path: Path):
    source, _ = _package(tmp_path, with_asset=False)
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    with in_memory_session_scope() as session:
        staged = service.stage_inspection(source, session)
        row = service.get_staged_inspection(staged.id, session)
        partial = Path(row.staged_path).with_name(f".{Path(row.staged_path).name}.partial")
        partial.write_bytes(b"interrupted")
        row.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
        session.commit()
        service.garbage_collect(session, grace_seconds=0)
        assert not partial.exists()
        assert (
            session.scalar(
                select(LabStorageLease.id).where(
                    LabStorageLease.resource_kind == "inspection",
                    LabStorageLease.resource_id == staged.id,
                )
            )
            is None
        )


def test_reconcile_backfills_missing_legacy_asset_ownership(in_memory_session_scope, tmp_path: Path):
    source, _ = _package(tmp_path)
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    with in_memory_session_scope() as session:
        imported = service.import_path(source, session)
        for asset in session.scalars(
            select(LabPackageAsset).where(LabPackageAsset.package_record_id == imported.record.id)
        ):
            session.delete(asset)
        session.commit()
        assert (
            session.scalar(select(LabPackageAsset.id).where(LabPackageAsset.package_record_id == imported.record.id))
            is None
        )
        report = service.reconcile(session)
        assert report["legacy_backfilled"] == 1
        assert (
            session.scalar(select(LabPackageAsset.id).where(LabPackageAsset.package_record_id == imported.record.id))
            is not None
        )


def test_legacy_backfill_quarantines_mismatched_record_as_unlocked(in_memory_session_scope, tmp_path: Path):
    source, _ = _package(tmp_path)
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    with in_memory_session_scope() as session:
        imported = service.import_path(source, session)
        for asset in session.scalars(
            select(LabPackageAsset).where(LabPackageAsset.package_record_id == imported.record.id)
        ):
            session.delete(asset)
        session.execute(
            update(LabPackage)
            .where(LabPackage.id == imported.record.id)
            .values(
                definition_json="{}",
                reproducibility="locked",
            )
        )
        session.commit()

        report = service.reconcile(session)

        record = session.get(LabPackage, imported.record.id)
        assert record is not None
        assert report["legacy_backfilled"] == 0
        assert record.reproducibility == "unlocked"
        assert session.scalar(select(LabPackageAsset.id).where(LabPackageAsset.package_record_id == record.id)) is None


def test_embedded_content_has_durable_publication_attempt_before_ownership(in_memory_session_scope, tmp_path: Path):
    source, content = _package(tmp_path)
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    with in_memory_session_scope() as session:
        service.import_path(source, session)
        attempt = session.scalar(
            select(LabContentPublicationAttempt).where(
                LabContentPublicationAttempt.sha256 == hashlib.sha256(content).hexdigest()
            )
        )
        assert attempt is not None
        assert attempt.state == "completed"


def test_package_asset_requires_complete_matching_content_identity(in_memory_session_scope, tmp_path: Path):
    source, content = _package(tmp_path)
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    digest = hashlib.sha256(content).hexdigest()
    other_digest = "f" * 64

    with in_memory_session_scope() as session:
        imported = service.import_path(source, session)
        asset = session.scalar(select(LabPackageAsset).where(LabPackageAsset.package_record_id == imported.record.id))
        assert asset is not None
        asset.content_size_bytes = None
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

        asset = session.scalar(select(LabPackageAsset).where(LabPackageAsset.package_record_id == imported.record.id))
        assert asset is not None
        session.add(
            LabContentObject(
                sha256=other_digest,
                size_bytes=len(content),
                path=str(service.content_store.path_for(other_digest)),
            )
        )
        asset.content_sha256 = other_digest
        asset.content_size_bytes = len(content)
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
        assert digest != other_digest


def test_reconcile_adopts_only_a_journaled_content_store_path(in_memory_session_scope, tmp_path: Path):
    content = b"published before database ownership"
    digest = hashlib.sha256(content).hexdigest()
    store = ContentStore(tmp_path / "content")
    service = LabPackageService(package_root=tmp_path / "packages", content_store=store)

    with in_memory_session_scope() as session:
        stored = store.put(content, sha256=digest, size_bytes=len(content))
        session.add(
            LabContentPublicationAttempt(
                id="lcp-published",
                sha256=digest,
                size_bytes=len(content),
                final_path=str(tmp_path / "not-the-content-store-path"),
                state="published",
            )
        )
        session.commit()

        report = service.reconcile(session)

        attempt = session.get(LabContentPublicationAttempt, "lcp-published")
        assert attempt is not None
        assert attempt.state == "quarantined"
        assert session.get(LabContentObject, {"sha256": digest, "size_bytes": len(content)}) is None
        assert report["quarantined_content"] == 1
        assert not stored.path.exists()
        assert (store.root / "quarantine" / f"unclaimed-{digest}").exists()


def test_reconcile_quarantines_verified_content_without_package_owner(in_memory_session_scope, tmp_path: Path):
    content = b"published before database ownership"
    digest = hashlib.sha256(content).hexdigest()
    store = ContentStore(tmp_path / "content")
    service = LabPackageService(package_root=tmp_path / "packages", content_store=store)

    with in_memory_session_scope() as session:
        stored = store.put(content, sha256=digest, size_bytes=len(content))
        session.add(
            LabContentPublicationAttempt(
                id="lcp-published",
                sha256=digest,
                size_bytes=len(content),
                final_path=str(stored.path),
                state="published",
            )
        )
        session.commit()

        report = service.reconcile(session)

        attempt = session.get(LabContentPublicationAttempt, "lcp-published")
        content_row = session.get(LabContentObject, {"sha256": digest, "size_bytes": len(content)})
        assert attempt is not None
        assert attempt.state == "quarantined"
        assert content_row is None
        assert report["completed_content_publications"] == 0
        assert not stored.path.exists()


def test_reconcile_preserves_inflight_content_with_a_live_lease(in_memory_session_scope, tmp_path: Path):
    content = b"content still awaiting package ownership"
    digest = hashlib.sha256(content).hexdigest()
    store = ContentStore(tmp_path / "content")
    service = LabPackageService(package_root=tmp_path / "packages", content_store=store)

    with in_memory_session_scope() as session:
        stored = store.put(content, sha256=digest, size_bytes=len(content))
        session.add(
            LabContentPublicationAttempt(
                id="lcp-inflight",
                sha256=digest,
                size_bytes=len(content),
                final_path=str(stored.path),
                state="published",
            )
        )
        session.add(
            LabStorageLease(
                id="lease-inflight",
                resource_kind="content",
                resource_id=digest,
                owner_id="lcp-inflight",
                operation_kind="content_publish",
                expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
        )
        session.commit()

        report = service.reconcile(session)

        attempt = session.get(LabContentPublicationAttempt, "lcp-inflight")
        assert attempt is not None
        assert attempt.state == "published"
        assert session.get(LabContentObject, {"sha256": digest, "size_bytes": len(content)}) is not None
        assert stored.path.exists()
        assert report["completed_content_publications"] == 0
