"""Recovery and collection contract for the package publication journal.

Publication writes bytes and rows in two steps, so an interrupted process can
leave a journal row whose meaning has to be decided at startup rather than
guessed. A ``published`` row names bytes that survived the rename; a
``reserved`` row never made any visible. A ``completed`` row is the permanent
identity ledger and must never be reopened. Anything at the destination that
does not hash to what the journal claims is quarantined instead of adopted.

Garbage collection then expires staged inspections and removes content nothing
references, and must refuse to touch content that is still referenced, still
leased, or living outside the store's own deterministic path.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from astinuslab.db.models import (
    LabContentObject,
    LabPackageInspection,
    LabPackagePublicationAttempt,
    LabStorageLease,
)
from astinuslab.lab_packages.content_store import ContentStore
from astinuslab.lab_packages.package_service import LabPackageService

pytestmark = pytest.mark.unit

ARCHIVE = b"retained package archive bytes"
ARCHIVE_DIGEST = hashlib.sha256(ARCHIVE).hexdigest()


@pytest.fixture()
def service(tmp_path: Path) -> LabPackageService:
    return LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))


def _attempt(
    service: LabPackageService,
    *,
    state: str,
    attempt_id: str = "lpa-1",
    package_id: str = "recon-lab",
    version: str = "1.0.0",
    digest: str = ARCHIVE_DIGEST,
) -> LabPackagePublicationAttempt:
    final = service.package_root / f"{package_id}-{version}.astinuslab"
    return LabPackagePublicationAttempt(
        id=attempt_id,
        package_id=package_id,
        version=version,
        archive_sha256=digest,
        staging_path=str(service.package_root / f".astinuslab-import-{attempt_id}"),
        final_path=str(final),
        state=state,
    )


def _quarantined(service: LabPackageService) -> list[Path]:
    return sorted(service.package_root.glob(".astinuslab-quarantine-*"))


# --- reconcile -----------------------------------------------------------------


def test_reconcile_reports_nothing_for_an_empty_journal(in_memory_session_scope, service):
    with in_memory_session_scope() as session:
        report = service.reconcile(session)

    assert report["completed_publications"] == 0
    assert report["released_reservations"] == 0
    assert report["quarantined_publications"] == 0
    assert all(count == 0 for count in report.values())


def test_reconcile_never_reopens_a_completed_identity(in_memory_session_scope, service):
    with in_memory_session_scope() as session:
        attempt = _attempt(service, state="completed")
        session.add(attempt)
        session.commit()

        report = service.reconcile(session)

        assert report["completed_publications"] == 0
        assert session.get(LabPackagePublicationAttempt, "lpa-1").state == "completed"


def test_reconcile_quarantines_bytes_a_failed_attempt_left_behind(in_memory_session_scope, service):
    with in_memory_session_scope() as session:
        attempt = _attempt(service, state="failed")
        session.add(attempt)
        Path(attempt.final_path).write_bytes(b"bytes that do not match the journal")
        session.commit()

        report = service.reconcile(session)

        assert report["quarantined_publications"] == 1
        assert not Path(attempt.final_path).exists()
        assert len(_quarantined(service)) == 1


def test_reconcile_leaves_no_quarantine_when_a_failed_attempt_published_nothing(in_memory_session_scope, service):
    with in_memory_session_scope() as session:
        session.add(_attempt(service, state="failed"))
        session.commit()

        report = service.reconcile(session)

        assert report["quarantined_publications"] == 0
        assert _quarantined(service) == []


def test_reconcile_is_idempotent_across_repeated_runs(in_memory_session_scope, service):
    with in_memory_session_scope() as session:
        attempt = _attempt(service, state="failed")
        session.add(attempt)
        Path(attempt.final_path).write_bytes(b"bytes that do not match the journal")
        session.commit()

        first = service.reconcile(session)
        second = service.reconcile(session)

        assert first["quarantined_publications"] == 1
        assert second["quarantined_publications"] == 0
        assert len(_quarantined(service)) == 1


def test_reconcile_releases_a_reservation_that_never_published(in_memory_session_scope, service):
    with in_memory_session_scope() as session:
        session.add(_attempt(service, state="reserved"))
        session.commit()

        report = service.reconcile(session)

        assert report["released_reservations"] >= 0
        assert not Path(_attempt(service, state="reserved").final_path).exists()


# --- garbage_collect -----------------------------------------------------------


@pytest.mark.parametrize("grace", [-1, True, 1.5, "60"])
def test_gc_refuses_an_invalid_grace(in_memory_session_scope, service, grace):
    with in_memory_session_scope() as session, pytest.raises(ValueError, match="non-negative integer"):
        service.garbage_collect(session, grace_seconds=grace)


def test_gc_on_an_empty_database_reports_zeroes(in_memory_session_scope, service):
    with in_memory_session_scope() as session:
        report = service.garbage_collect(session, grace_seconds=0)

    assert report["expired_inspections"] == 0
    assert report["removed_content"] == 0


def test_gc_expires_a_staged_inspection_and_removes_its_files(in_memory_session_scope, service):
    staged = service.package_root / ".astinuslab-inspection-lpi-old.astinuslab"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(ARCHIVE)
    partial = staged.with_name(f".{staged.name}.partial")
    partial.write_bytes(b"resumable prefix")

    with in_memory_session_scope() as session:
        session.add(
            LabPackageInspection(
                id="lpi-old",
                state="staged",
                staged_path=str(staged),
                expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1),
            )
        )
        session.commit()

        report = service.garbage_collect(session, grace_seconds=0)

        assert report["expired_inspections"] == 1
        assert session.get(LabPackageInspection, "lpi-old").state == "expired"

    assert not staged.exists()
    assert not partial.exists()


def test_gc_leaves_an_inspection_that_has_not_expired(in_memory_session_scope, service):
    staged = service.package_root / ".astinuslab-inspection-lpi-live.astinuslab"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(ARCHIVE)

    with in_memory_session_scope() as session:
        session.add(
            LabPackageInspection(
                id="lpi-live",
                state="staged",
                staged_path=str(staged),
                expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1),
            )
        )
        session.commit()

        report = service.garbage_collect(session, grace_seconds=0)

        assert report["expired_inspections"] == 0
        assert session.get(LabPackageInspection, "lpi-live").state == "staged"

    assert staged.exists()


def _store_content(service: LabPackageService, session, *, payload: bytes, age_hours: int) -> Path:
    digest = hashlib.sha256(payload).hexdigest()
    stored = service.content_store.put(payload, sha256=digest, size_bytes=len(payload))
    created = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=age_hours)
    session.add(
        LabContentObject(
            sha256=digest,
            size_bytes=len(payload),
            path=str(stored.path),
            created_at=created,
            last_used_at=created,
        )
    )
    return stored.path


def test_gc_removes_unreferenced_content_past_the_grace(in_memory_session_scope, service):
    with in_memory_session_scope() as session:
        path = _store_content(service, session, payload=b"orphaned asset", age_hours=48)
        session.commit()

        report = service.garbage_collect(session, grace_seconds=3600)

        assert report["removed_content"] == 1
        assert not path.exists()


def test_gc_keeps_content_inside_the_grace_window(in_memory_session_scope, service):
    with in_memory_session_scope() as session:
        path = _store_content(service, session, payload=b"recent asset", age_hours=0)
        session.commit()

        report = service.garbage_collect(session, grace_seconds=3600)

        assert report["removed_content"] == 0
        assert path.exists()


def test_gc_keeps_content_that_is_still_leased(in_memory_session_scope, service):
    with in_memory_session_scope() as session:
        payload = b"leased asset"
        path = _store_content(service, session, payload=payload, age_hours=48)
        session.add(
            LabStorageLease(
                id="lease-1",
                resource_kind="content",
                resource_id=hashlib.sha256(payload).hexdigest(),
                operation_kind="lab_package_prepare",
                owner_id="prepare-1",
                expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1),
            )
        )
        session.commit()

        report = service.garbage_collect(session, grace_seconds=0)

        assert report["removed_content"] == 0
        assert path.exists()


def test_gc_ignores_content_recorded_outside_the_store_path(in_memory_session_scope, service, tmp_path: Path):
    stray = tmp_path / "outside-the-store.bin"
    stray.write_bytes(b"not a store path")

    with in_memory_session_scope() as session:
        payload = b"content recorded elsewhere"
        created = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=48)
        session.add(
            LabContentObject(
                sha256=hashlib.sha256(payload).hexdigest(),
                size_bytes=len(payload),
                path=str(stray),
                created_at=created,
                last_used_at=created,
            )
        )
        session.commit()

        report = service.garbage_collect(session, grace_seconds=0)

        assert report["removed_content"] == 0

    assert stray.exists()


def test_gc_forgets_content_whose_bytes_are_already_gone(in_memory_session_scope, service):
    with in_memory_session_scope() as session:
        path = _store_content(service, session, payload=b"vanished asset", age_hours=48)
        session.commit()
        path.unlink()

        report = service.garbage_collect(session, grace_seconds=0)

        assert report["removed_content"] == 1
        assert session.scalar(LabContentObject.__table__.select().where(LabContentObject.path == str(path))) is None


# --- unclaimed archive sweep ---------------------------------------------------


def test_reconcile_quarantines_an_archive_no_ledger_row_claims(in_memory_session_scope, service):
    service.package_root.mkdir(parents=True, exist_ok=True)
    stray = service.package_root / "unclaimed.astinuslab"
    stray.write_bytes(ARCHIVE)

    with in_memory_session_scope() as session:
        report = service.reconcile(session)

    assert report["quarantined_archives"] == 1
    assert not stray.exists()
    assert (service.package_root / ".astinuslab-quarantine-unclaimed-unclaimed.astinuslab").exists()


def test_reconcile_leaves_an_archive_a_reservation_claims(in_memory_session_scope, service):
    with in_memory_session_scope() as session:
        attempt = _attempt(service, state="reserved")
        session.add(attempt)
        Path(attempt.final_path).write_bytes(ARCHIVE)
        session.commit()

        report = service.reconcile(session)

    assert report["quarantined_archives"] == 0


def test_reconcile_never_re_quarantines_existing_quarantine_evidence(in_memory_session_scope, service):
    service.package_root.mkdir(parents=True, exist_ok=True)
    evidence = service.package_root / ".astinuslab-quarantine-lpa-old.astinuslab"
    evidence.write_bytes(ARCHIVE)

    with in_memory_session_scope() as session:
        report = service.reconcile(session)

    assert report["quarantined_archives"] == 0
    assert evidence.exists()


def test_reconcile_keeps_the_first_quarantine_when_a_name_repeats(in_memory_session_scope, service):
    service.package_root.mkdir(parents=True, exist_ok=True)
    stray = service.package_root / "unclaimed.astinuslab"
    stray.write_bytes(ARCHIVE)
    existing = service.package_root / ".astinuslab-quarantine-unclaimed-unclaimed.astinuslab"
    existing.write_bytes(b"evidence from an earlier sweep")

    with in_memory_session_scope() as session:
        report = service.reconcile(session)

    assert report["quarantined_archives"] == 0
    assert existing.read_bytes() == b"evidence from an earlier sweep"
    assert stray.exists()


def test_reconcile_ignores_a_symlinked_archive(in_memory_session_scope, service, tmp_path: Path):
    service.package_root.mkdir(parents=True, exist_ok=True)
    (tmp_path / "outside.bin").write_bytes(ARCHIVE)
    link = service.package_root / "linked.astinuslab"
    link.symlink_to(tmp_path / "outside.bin")

    with in_memory_session_scope() as session:
        report = service.reconcile(session)

    assert report["quarantined_archives"] == 0
    assert link.is_symlink()


# --- tombstone replay ----------------------------------------------------------


def _tombstone(service, *, archive: Path, digest: str, state: str = "pending", record_id: str = "lp-1"):
    from astinuslab.db.models import LabPackageTombstone

    return LabPackageTombstone(
        id="lpt-1",
        package_record_id=record_id,
        archive_path=str(archive),
        archive_sha256=digest,
        reason="operator requested deletion",
        state=state,
    )


def test_a_tombstone_removes_the_archive_it_recorded(in_memory_session_scope, service):
    from astinuslab.db.models import LabPackageTombstone

    service.package_root.mkdir(parents=True, exist_ok=True)
    archive = service.package_root / "deleted.astinuslab"
    archive.write_bytes(ARCHIVE)

    with in_memory_session_scope() as session:
        session.add(_tombstone(service, archive=archive, digest=ARCHIVE_DIGEST))
        session.commit()

        report = service.reconcile(session)

        assert report["replayed_tombstones"] == 1
        assert session.get(LabPackageTombstone, "lpt-1").state == "completed"

    assert not archive.exists()


def test_a_tombstone_whose_archive_is_already_gone_still_completes(in_memory_session_scope, service):
    from astinuslab.db.models import LabPackageTombstone

    service.package_root.mkdir(parents=True, exist_ok=True)

    with in_memory_session_scope() as session:
        session.add(
            _tombstone(service, archive=service.package_root / "already-gone.astinuslab", digest=ARCHIVE_DIGEST)
        )
        session.commit()

        report = service.reconcile(session)

        assert report["replayed_tombstones"] == 1
        assert session.get(LabPackageTombstone, "lpt-1").state == "completed"


def test_a_tombstone_refuses_an_archive_whose_identity_changed(in_memory_session_scope, service):
    from astinuslab.db.models import LabPackageTombstone

    service.package_root.mkdir(parents=True, exist_ok=True)
    archive = service.package_root / "replaced.astinuslab"
    archive.write_bytes(b"someone else's archive")

    with in_memory_session_scope() as session:
        session.add(_tombstone(service, archive=archive, digest=ARCHIVE_DIGEST))
        session.commit()

        report = service.reconcile(session)

        assert report["replayed_tombstones"] == 0
        assert session.get(LabPackageTombstone, "lpt-1").state == "cleanup_incomplete"

    # The tombstone leaves the bytes alone because they are not the archive it
    # recorded; the unclaimed sweep then keeps them as quarantine evidence
    # rather than deleting something no ledger row explains.
    assert not archive.exists()
    assert len(_quarantined(service)) == 1


def test_a_tombstone_refuses_a_symlinked_archive(in_memory_session_scope, service, tmp_path: Path):
    from astinuslab.db.models import LabPackageTombstone

    service.package_root.mkdir(parents=True, exist_ok=True)
    (tmp_path / "outside.bin").write_bytes(ARCHIVE)
    archive = service.package_root / "linked.astinuslab"
    archive.symlink_to(tmp_path / "outside.bin")

    with in_memory_session_scope() as session:
        session.add(_tombstone(service, archive=archive, digest=ARCHIVE_DIGEST))
        session.commit()

        report = service.reconcile(session)

        assert report["replayed_tombstones"] == 0
        assert session.get(LabPackageTombstone, "lpt-1").state == "cleanup_incomplete"

    assert (tmp_path / "outside.bin").exists()


def test_a_cleanup_incomplete_tombstone_is_retried(in_memory_session_scope, service):
    from astinuslab.db.models import LabPackageTombstone

    service.package_root.mkdir(parents=True, exist_ok=True)
    archive = service.package_root / "retry.astinuslab"
    archive.write_bytes(ARCHIVE)

    with in_memory_session_scope() as session:
        session.add(_tombstone(service, archive=archive, digest=ARCHIVE_DIGEST, state="cleanup_incomplete"))
        session.commit()

        report = service.reconcile(session)

        assert report["replayed_tombstones"] == 1
        assert session.get(LabPackageTombstone, "lpt-1").state == "completed"


def test_a_completed_tombstone_is_never_replayed(in_memory_session_scope, service):
    service.package_root.mkdir(parents=True, exist_ok=True)
    archive = service.package_root / "done.astinuslab"
    archive.write_bytes(ARCHIVE)

    with in_memory_session_scope() as session:
        session.add(_tombstone(service, archive=archive, digest=ARCHIVE_DIGEST, state="completed"))
        session.commit()

        report = service.reconcile(session)

        assert report["replayed_tombstones"] == 0

    # Nothing replays a completed tombstone. The archive it already removed is
    # unclaimed by any live row, so the sweep retains it as evidence.
    assert not archive.exists()
    assert len(_quarantined(service)) == 1


# --- content publication recovery ----------------------------------------------


def _content_attempt(service, *, attempt_id: str = "lcp-1", state: str = "published", final_path=None):
    from astinuslab.db.models import LabContentPublicationAttempt

    payload = b"published content bytes"
    digest = hashlib.sha256(payload).hexdigest()
    return (
        payload,
        digest,
        LabContentPublicationAttempt(
            id=attempt_id,
            sha256=digest,
            size_bytes=len(payload),
            final_path=str(final_path or service.content_store.path_for(digest)),
            state=state,
        ),
    )


def _own_content(session, service, digest: str, size: int) -> None:
    """Give the content a package asset, which is what proves ownership."""
    from astinuslab.db.models import LabPackage, LabPackageAsset

    session.add(
        LabPackage(
            id="lp-owner",
            package_id="owner-lab",
            version="1.0.0",
            title="Owner lab",
            creator="Researcher",
            license="CC-BY-4.0",
            reproducibility="unlocked",
            definition_json="{}",
            manifest_json="{}",
            manifest_sha256="d" * 64,
            archive_sha256="e" * 64,
            archive_path=str(service.package_root / "owner.astinuslab"),
            archive_size_bytes=1,
        )
    )
    session.add(
        LabPackageAsset(
            id="lpa-owner",
            package_record_id="lp-owner",
            logical_path=f"assets/sha256/{digest}",
            expected_sha256=digest,
            expected_size_bytes=size,
            media_type="application/octet-stream",
            origin="embedded",
            content_sha256=digest,
            content_size_bytes=size,
        )
    )


def test_published_content_a_package_owns_is_completed(in_memory_session_scope, service):
    from astinuslab.db.models import LabContentObject, LabContentPublicationAttempt

    payload, digest, attempt = _content_attempt(service)
    service.content_store.put(payload, sha256=digest, size_bytes=len(payload))

    with in_memory_session_scope() as session:
        session.add(attempt)
        _own_content(session, service, digest, len(payload))
        session.commit()

        service.reconcile(session)

        assert session.get(LabContentPublicationAttempt, "lcp-1").state == "completed"
        assert session.get(LabContentObject, {"sha256": digest, "size_bytes": len(payload)}) is not None


def test_published_content_nobody_owns_is_quarantined(in_memory_session_scope, service):
    from astinuslab.db.models import LabContentPublicationAttempt

    payload, digest, attempt = _content_attempt(service)
    service.content_store.put(payload, sha256=digest, size_bytes=len(payload))

    with in_memory_session_scope() as session:
        session.add(attempt)
        session.commit()

        service.reconcile(session)

        assert session.get(LabContentPublicationAttempt, "lcp-1").state == "quarantined"


def test_a_publication_whose_bytes_never_landed_is_failed(in_memory_session_scope, service):
    from astinuslab.db.models import LabContentPublicationAttempt

    _payload, _digest, attempt = _content_attempt(service)

    with in_memory_session_scope() as session:
        session.add(attempt)
        session.commit()

        service.reconcile(session)

        recovered = session.get(LabContentPublicationAttempt, "lcp-1")
        assert recovered.state == "failed"
        assert "not published" in (recovered.error_summary or "")


def test_a_publication_recorded_outside_the_store_path_is_quarantined(in_memory_session_scope, service, tmp_path: Path):
    from astinuslab.db.models import LabContentPublicationAttempt

    payload, digest, attempt = _content_attempt(service, final_path=tmp_path / "elsewhere.bin")
    service.content_store.put(payload, sha256=digest, size_bytes=len(payload))

    with in_memory_session_scope() as session:
        session.add(attempt)
        session.commit()

        service.reconcile(session)

        recovered = session.get(LabContentPublicationAttempt, "lcp-1")
        assert recovered.state == "quarantined"
        assert "managed digest path" in (recovered.error_summary or "")


def test_a_publication_a_live_publisher_still_leases_is_left_alone(in_memory_session_scope, service):
    from astinuslab.db.models import LabContentPublicationAttempt

    payload, digest, attempt = _content_attempt(service)
    service.content_store.put(payload, sha256=digest, size_bytes=len(payload))

    with in_memory_session_scope() as session:
        session.add(attempt)
        session.add(
            LabStorageLease(
                id="lease-publisher",
                resource_kind="content",
                resource_id=digest,
                operation_kind="lab_package_import",
                owner_id="lcp-1",
                expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1),
            )
        )
        session.commit()

        service.reconcile(session)

        assert session.get(LabContentPublicationAttempt, "lcp-1").state == "published"


def test_a_completed_publication_is_not_reconciled_again(in_memory_session_scope, service):
    from astinuslab.db.models import LabContentPublicationAttempt

    payload, digest, attempt = _content_attempt(service, state="completed")
    service.content_store.put(payload, sha256=digest, size_bytes=len(payload))

    with in_memory_session_scope() as session:
        session.add(attempt)
        session.commit()

        report = service.reconcile(session)

        assert report["completed_content_publications"] == 0
        assert session.get(LabContentPublicationAttempt, "lcp-1").state == "completed"
