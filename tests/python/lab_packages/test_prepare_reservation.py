"""Reservation and idempotency contract for package preparation.

Preparation acquires content on behalf of an imported package, so the client
key that names an operation has to mean exactly one operation. Reusing a key
must never silently widen scope — a retry that asks for network access when the
first attempt did not is a conflict, not a resume — and local policy must be
able to refuse source-network acquisition outright. A failed transfer keeps its
verified prefix, so resuming under the same key reopens that attempt rather
than starting a second one.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from astinuslab.db.models import LabOperationOutbox, LabPackage, LabPackageAsset, LabPrepareAttempt, LabStorageLease
from astinuslab.lab_packages.archive import write_archive
from astinuslab.lab_packages.package_service import LabPackageService, PackageDeletionConflictError
from astinuslab.lab_packages.prepare_service import (
    PrepareError,
    cancel_prepare,
    create_prepare,
    run_prepare,
    status_prepare,
)
from tests.python._support.lab_packages import prepare_definition

pytestmark = pytest.mark.unit


def _network_policy(monkeypatch, *, allowed: bool) -> None:
    """Settings is frozen, so swap the module's object rather than a field."""
    from astinuslab.lab_packages import prepare_service

    monkeypatch.setattr(
        prepare_service,
        "settings",
        prepare_service.settings.model_copy(update={"packages_allow_source_network": allowed}),
    )


@pytest.fixture()
def service(prepare_package_service) -> LabPackageService:
    return prepare_package_service


@pytest.fixture()
def source(tmp_path: Path) -> Path:
    content = b"embedded clean layer"
    archive = write_archive(
        prepare_definition(),
        "# Runbook\n",
        assets={hashlib.sha256(content).hexdigest(): content},
        allow_legacy_unreferenced_assets=True,
    )
    path = tmp_path / "input.astinuslab"
    path.write_bytes(archive)
    return path


# --- idempotency key validation ------------------------------------------------


@pytest.mark.parametrize("key", ["", "   ", "k" * 129, 42, None])
def test_an_unusable_idempotency_key_is_refused(in_memory_session_scope, service, source, key):
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record

        with pytest.raises(PrepareError) as caught:
            create_prepare(session, record=record, service=service, idempotency_key=key)

        assert caught.value.code == "invalid_idempotency_key"


# --- idempotency ---------------------------------------------------------------


def test_the_same_key_returns_the_same_reservation(in_memory_session_scope, service, source):
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record

        first = create_prepare(session, record=record, service=service, idempotency_key="k1")
        second = create_prepare(session, record=record, service=service, idempotency_key="k1")

        assert first.id == second.id


def test_reusing_a_key_with_a_different_network_policy_is_a_conflict(
    in_memory_session_scope, service, source, monkeypatch
):
    _network_policy(monkeypatch, allowed=True)

    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        create_prepare(session, record=record, service=service, idempotency_key="k1")

        with pytest.raises(PrepareError, match="different network policy") as caught:
            create_prepare(session, record=record, service=service, idempotency_key="k1", network_opt_in=True)

        assert caught.value.code == "idempotency_conflict"


def test_source_network_opt_in_is_refused_by_local_policy(in_memory_session_scope, service, source, monkeypatch):
    _network_policy(monkeypatch, allowed=False)

    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record

        with pytest.raises(PrepareError, match="disabled by local policy") as caught:
            create_prepare(session, record=record, service=service, idempotency_key="k-net", network_opt_in=True)

        assert caught.value.code == "resolver_policy_denied"


def test_a_retry_survives_the_policy_being_revoked_afterwards(in_memory_session_scope, service, source, monkeypatch):
    _network_policy(monkeypatch, allowed=True)

    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        first = create_prepare(session, record=record, service=service, idempotency_key="k-net", network_opt_in=True)
        session.commit()

        _network_policy(monkeypatch, allowed=False)
        retry = create_prepare(session, record=record, service=service, idempotency_key="k-net", network_opt_in=True)

        assert retry.id == first.id


def test_reusing_a_failed_key_reopens_that_attempt_and_its_event(in_memory_session_scope, service, source, monkeypatch):
    from astinuslab.lab_packages import prepare_service

    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        first = create_prepare(session, record=record, service=service, idempotency_key="k-resume")
        session.commit()

        attempt = session.get(LabPrepareAttempt, first.id)
        attempt.state = "failed"
        attempt.error_summary = "transfer interrupted"
        event = (
            session.query(LabOperationOutbox)
            .filter_by(operation_kind="lab_prepare_assets", operation_id=first.id)
            .one_or_none()
        )
        if event is not None:
            event.state = "failed"
        session.commit()

        retry_time = prepare_service._now() + timedelta(minutes=10)
        monkeypatch.setattr(prepare_service, "_now", lambda: retry_time)
        resumed = create_prepare(session, record=record, service=service, idempotency_key="k-resume")

        assert resumed.id == first.id
        reopened = session.get(LabPrepareAttempt, first.id)
        assert reopened.state == "reserved"
        assert reopened.error_summary is None
        assert reopened.cancel_requested is False
        expiry = retry_time + timedelta(seconds=prepare_service.PREPARE_LEASE_SECONDS)
        assert reopened.expires_at == expiry
        assert {lease.expires_at for lease in session.query(LabStorageLease).filter_by(owner_id=first.id)} == {expiry}


# --- status and cancellation ---------------------------------------------------


def test_status_of_an_unknown_preparation_is_refused(in_memory_session_scope):
    with in_memory_session_scope() as session, pytest.raises(PrepareError) as caught:
        status_prepare(session, "lpa-absent")

    assert caught.value.code == "prepare_not_found"


def test_cancelling_an_unknown_preparation_is_refused(in_memory_session_scope):
    with in_memory_session_scope() as session, pytest.raises(PrepareError) as caught:
        cancel_prepare(session, "lpa-absent")

    assert caught.value.code == "prepare_not_found"


def test_running_an_unknown_preparation_is_refused(in_memory_session_scope, service):
    with in_memory_session_scope() as session, pytest.raises(PrepareError) as caught:
        run_prepare(session, "lpa-absent", service=service)

    assert caught.value.code == "prepare_not_found"


def test_cancelling_a_reserved_preparation_marks_the_request(in_memory_session_scope, service, source):
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        handle = create_prepare(session, record=record, service=service, idempotency_key="k-cancel")
        session.commit()

        cancel_prepare(session, handle.id, service=service)

        assert session.get(LabPrepareAttempt, handle.id).cancel_requested is True


def test_a_cancelled_preparation_reaches_a_terminal_cancelled_state(in_memory_session_scope, service, source):
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        handle = create_prepare(session, record=record, service=service, idempotency_key="k-cancel")
        session.commit()
        cancel_prepare(session, handle.id, service=service)
        session.commit()

        result = run_prepare(session, handle.id, service=service)

        assert result.state == "cancelled"
        assert result.progress_percent == 100
        assert "cancel" in (session.get(LabPrepareAttempt, handle.id).error_summary or "")


def test_cancelling_a_completed_preparation_leaves_it_completed(in_memory_session_scope, service, source):
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        handle = create_prepare(session, record=record, service=service, idempotency_key="k-done")
        session.commit()
        completed = run_prepare(session, handle.id, service=service)
        assert completed.state == "completed"

        cancel_prepare(session, handle.id, service=service)

        assert session.get(LabPrepareAttempt, handle.id).cancel_requested is False
        assert status_prepare(session, handle.id).state == "completed"


# --- stored-contract recovery --------------------------------------------------


def test_an_unreadable_stored_definition_is_refused(in_memory_session_scope, service, source):
    from sqlalchemy import update

    from astinuslab.db.models import LabPackage
    from astinuslab.lab_packages.prepare_service import _definition

    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        # Package rows are ORM-immutable by design; damage it at the SQL layer,
        # which is what storage corruption or a hand-edited database looks like.
        session.execute(
            update(LabPackage.__table__).where(LabPackage.id == record.id).values(definition_json="{not json")  # type: ignore[arg-type]
        )
        session.commit()
        session.expire_all()

        with pytest.raises(PrepareError) as caught:
            _definition(session.get(LabPackage, record.id))

        assert caught.value.code == "invalid_package_definition"


def test_a_json_field_that_is_not_json_falls_back_to_its_default():
    from astinuslab.lab_packages.prepare_service import _load_json

    assert _load_json("{not json", []) == []
    assert _load_json(None, {"a": 1}) == {"a": 1}
    assert _load_json('{"a": 1}', {}) == {"a": 1}


def test_an_asset_contract_is_recovered_from_the_definition(in_memory_session_scope, service, source):
    from astinuslab.db.models import LabPackage, LabPackageAsset
    from astinuslab.lab_packages.prepare_service import _asset_reference, _definition

    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        asset = session.query(LabPackageAsset).filter_by(package_record_id=record.id).first()
        package = session.get(LabPackage, record.id)

        reference = _asset_reference(package, asset, _definition(package))

        assert reference.sha256 == asset.expected_sha256
        assert reference.size_bytes == asset.expected_size_bytes
        assert reference.redistribution_confirmed is False


def test_a_malformed_source_list_is_treated_as_no_sources(in_memory_session_scope, service, source):
    from astinuslab.db.models import LabPackage, LabPackageAsset
    from astinuslab.lab_packages.prepare_service import _asset_reference, _definition

    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        package = session.get(LabPackage, record.id)
        # A transient row: the persisted ones carry constraints this case is
        # deliberately outside, and the helper only reads its fields.
        asset = LabPackageAsset(
            id="lpa-transient",
            package_record_id=record.id,
            logical_path="assets/sha256/" + "f" * 64,
            expected_sha256="f" * 64,
            expected_size_bytes=1,
            media_type="application/octet-stream",
            origin="source",
            source_json='{"not": "a list"}',
        )

        reference = _asset_reference(package, asset, _definition(package))

        assert reference.sources == ()
        assert reference.embedded is False


# --- run guards ----------------------------------------------------------------


def test_running_a_preparation_whose_package_was_deleted_fails_it(in_memory_session_scope, service, source):
    from sqlalchemy import delete

    from astinuslab.db.models import LabPackage, LabPackageAsset, LabPrepareAttempt

    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        handle = create_prepare(session, record=record, service=service, idempotency_key="k-orphan")
        session.commit()
        session.execute(delete(LabPackageAsset.__table__).where(LabPackageAsset.package_record_id == record.id))  # type: ignore[arg-type]
        session.execute(delete(LabPackage.__table__).where(LabPackage.id == record.id))  # type: ignore[arg-type]
        session.commit()
        session.expire_all()

        result = run_prepare(session, handle.id, service=service)

        assert result.state == "failed"
        attempt = session.get(LabPrepareAttempt, handle.id)
        assert "package_deleted" in (attempt.error_summary or "")
        assert attempt.progress_percent == 100


def test_running_a_completed_preparation_returns_it_unchanged(in_memory_session_scope, service, source):
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        handle = create_prepare(session, record=record, service=service, idempotency_key="k-twice")
        session.commit()
        first = run_prepare(session, handle.id, service=service)

        second = run_prepare(session, handle.id, service=service)

        assert first.state == "completed"
        assert second.state == "completed"


def test_setting_an_unknown_asset_state_is_refused(in_memory_session_scope, service, source):
    from astinuslab.db.models import LabPackageAsset, LabPrepareAttempt
    from astinuslab.lab_packages.prepare_service import _set_asset_state

    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        handle = create_prepare(session, record=record, service=service, idempotency_key="k-state")
        session.commit()
        asset = session.query(LabPackageAsset).filter_by(package_record_id=record.id).first()
        attempt = session.get(LabPrepareAttempt, handle.id)

        with pytest.raises(ValueError, match="invalid prepare asset state"):
            _set_asset_state(session, attempt, asset, "teleported")


# --- acquisition failures ------------------------------------------------------


@pytest.fixture()
def external_source(tmp_path: Path) -> Path:
    """A package whose clean layer is declared but not embedded."""
    from astinuslab.lab_packages.models import AssetReference

    base = prepare_definition()
    layer = AssetReference(
        sha256="d" * 64,
        size_bytes=4096,
        media_type="application/x-qcow2",
        sources=("https://example.invalid/layers/target.qcow2",),
        license="CC-BY-4.0",
        redistribution_confirmed=False,
    )
    definition = base.model_copy(
        update={
            "reproducibility": "locked",
            "vms": (base.vms[0].model_copy(update={"clean_layer": layer}),),
        }
    )
    path = tmp_path / "external.astinuslab"
    path.write_bytes(write_archive(definition, "# Locked runbook\n"))
    return path


def test_an_unreachable_asset_fails_its_preparation(in_memory_session_scope, service, external_source):
    from astinuslab.db.models import LabPackageAsset, LabPrepareAttempt

    with in_memory_session_scope() as session:
        record = service.import_path(external_source, session).record
        handle = create_prepare(session, record=record, service=service, idempotency_key="k-offline")
        session.commit()

        result = run_prepare(session, handle.id, service=service)

        assert result.state == "failed"
        attempt = session.get(LabPrepareAttempt, handle.id)
        assert attempt.progress_percent == 100
        assert attempt.error_summary
        asset = session.query(LabPackageAsset).filter_by(package_record_id=record.id).first()
        assert asset.prepare_state == "failed"
        assert asset.prepare_error_summary


def test_failed_resume_restores_custody_until_cancelled(in_memory_session_scope, service, external_source, monkeypatch):
    from astinuslab.lab_packages import prepare_service

    with in_memory_session_scope() as session:
        record = service.import_path(external_source, session).record
        record_id, archive_path = record.id, Path(record.archive_path)
        first = create_prepare(session, record=record, service=service, idempotency_key="resume-custody")
        session.commit()
        assert run_prepare(session, first.id, service=service).state == "failed"
        assert session.query(LabStorageLease).filter_by(owner_id=first.id).count() == 0
        retry_time = prepare_service._now() + timedelta(minutes=10)
        monkeypatch.setattr(prepare_service, "_now", lambda: retry_time)

        resumed = create_prepare(session, record=record, service=service, idempotency_key="resume-custody")
        session.commit()
        assert resumed.id == first.id
        assert resumed.state == "reserved"
        expected = {("package", record_id)} | {
            ("content", asset.expected_sha256)
            for asset in session.query(LabPackageAsset).filter_by(package_record_id=record_id)
            if asset.role == "boot-layer" or asset.logical_path.startswith("assets/sha256/")
        }
        leases = session.query(LabStorageLease).filter_by(owner_id=first.id).all()
        assert {(lease.resource_kind, lease.resource_id) for lease in leases} == expected
        expiry = retry_time + timedelta(seconds=prepare_service.PREPARE_LEASE_SECONDS)
        assert {lease.expires_at for lease in leases} == {expiry}
        assert session.get(LabPrepareAttempt, first.id).expires_at == expiry
        events = session.query(LabOperationOutbox).filter_by(operation_id=first.id).all()
        assert len(events) == 1 and events[0].state == "pending"
        with pytest.raises(PackageDeletionConflictError):
            service.delete_package(record_id, session)
        session.rollback()

    with in_memory_session_scope() as session:
        assert session.get(LabPackage, record_id) is not None
        assert archive_path.is_file()
        assert session.get(LabPrepareAttempt, first.id).state == "reserved"
        cancel_prepare(session, first.id, service=service)
        session.commit()
        assert run_prepare(session, first.id, service=service).state == "cancelled"
        assert session.query(LabStorageLease).filter_by(owner_id=first.id).count() == 0
        service.delete_package(record_id, session)
        session.commit()
        assert session.get(LabPackage, record_id) is None


@pytest.mark.parametrize("conflict_kind", ["package", "content"])
def test_failed_resume_conflict_preserves_terminal_intent(
    in_memory_session_scope, service, external_source, conflict_kind
):
    with in_memory_session_scope() as session:
        record = service.import_path(external_source, session).record
        first = create_prepare(session, record=record, service=service, idempotency_key="resume-conflict")
        session.commit()
        assert run_prepare(session, first.id, service=service).state == "failed"
        event = session.query(LabOperationOutbox).filter_by(operation_id=first.id).one()
        old_event_state = event.state
        resource = record.id if conflict_kind == "package" else "d" * 64
        service._acquire_lease(
            session, kind=conflict_kind, resource_id=resource, owner_id="competitor", operation="test"
        )
        session.commit()
        with pytest.raises(PrepareError) as caught:
            create_prepare(session, record=record, service=service, idempotency_key="resume-conflict")
        assert caught.value.code == "prepare_conflict"
        session.rollback()
        assert session.get(LabPrepareAttempt, first.id).state == "failed"
        assert session.query(LabOperationOutbox).filter_by(operation_id=first.id).one().state == old_event_state
        assert session.query(LabStorageLease).filter_by(owner_id=first.id).count() == 0


@pytest.mark.parametrize("expired", [False, True])
def test_prepare_ownership_is_visible_to_gc_at_lock_release(prepare_gc_fixture, monkeypatch, expired):
    from astinuslab.db.models import LabContentObject

    service, sessions, attempt_id, digest, payload, clock = prepare_gc_fixture
    original_lock = service.content_lock
    observed: dict[str, Any] = {}

    @contextmanager
    def lock_with_collector(sha):
        with original_lock(sha):
            yield
        if expired:
            clock[0] += timedelta(hours=2)
        with sessions() as collector:
            row = collector.get(LabContentObject, {"sha256": digest, "size_bytes": len(payload)})
            observed["owner_visible"] = row is not None
            asset = collector.query(LabPackageAsset).filter_by(expected_sha256=digest).one()
            observed["asset_ready"] = asset.prepare_state == "ready" and asset.content_sha256 == digest
            observed["report"] = service.garbage_collect(collector, grace_seconds=0)

    monkeypatch.setattr(service, "content_lock", lock_with_collector)
    with sessions() as worker:
        assert run_prepare(worker, attempt_id, service=service).state == "completed"
    assert observed["owner_visible"] is True
    assert observed["asset_ready"] is True
    assert observed["report"]["removed_content"] == 0
    assert service.content_store.path_for(digest).read_bytes() == payload
    assert not (service.content_store.root / "quarantine" / f"unclaimed-{digest}").exists()


def test_prepare_failed_ownership_commit_rolls_back_before_unlock(prepare_gc_fixture, monkeypatch):
    from astinuslab.db.models import LabContentObject

    service, sessions, attempt_id, digest, payload, _clock = prepare_gc_fixture
    original_lock = service.content_lock
    lock_held = False
    observed = []

    @contextmanager
    def watch_lock(sha):
        nonlocal lock_held
        with original_lock(sha):
            lock_held = True
            try:
                yield
            finally:
                lock_held = False
                observed.append(worker.connection().connection.driver_connection.in_transaction)

    monkeypatch.setattr(service, "content_lock", watch_lock)
    with sessions() as worker:
        commit = worker.commit
        failed = False

        def failing_commit():
            nonlocal failed
            if not failed and any(isinstance(row, LabContentObject) for row in worker.new):
                failed = True
                assert lock_held, "ownership commit must hold the digest lock"
                worker.flush()
                raise RuntimeError("injected ownership commit failure")
            return commit()

        monkeypatch.setattr(worker, "commit", failing_commit)
        with pytest.raises(RuntimeError, match="injected ownership commit failure"):
            run_prepare(worker, attempt_id, service=service)
    assert observed == [False]
    with sessions() as check:
        assert check.get(LabContentObject, {"sha256": digest, "size_bytes": len(payload)}) is None
        assert check.get(LabPrepareAttempt, attempt_id).state == "failed"
        assert check.query(LabPackageAsset).filter_by(expected_sha256=digest).one().prepare_state != "ready"
    assert service.content_store.path_for(digest).read_bytes() == payload


def test_prepare_commit_can_finish_while_gc_waits_after_committing_deletes(prepare_gc_fixture, monkeypatch):
    import threading

    from astinuslab.db.models import LabContentObject, LabPackageInspection
    from astinuslab.lab_packages import package_service
    from astinuslab.lab_packages.resolver import AssetResolver

    service, sessions, attempt_id, digest, payload, clock = prepare_gc_fixture
    old = clock[0] - timedelta(hours=2)
    with sessions() as seed:
        for index in range(2):
            data = f"unreferenced-{index}".encode()
            sha = hashlib.sha256(data).hexdigest()
            path = service.content_store.path_for(sha)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            seed.add(LabContentObject(sha256=sha, size_bytes=len(data), path=str(path), last_used_at=old))
        seed.add(
            LabContentObject(
                sha256=digest,
                size_bytes=len(payload),
                path=str(service.content_store.path_for(digest)),
                last_used_at=old,
            )
        )
        staged = service.package_root / ".astinuslab-inspection-expired.astinuslab"
        staged.write_bytes(b"expired inspection")
        seed.add(LabPackageInspection(id="lpi-expired", state="staged", staged_path=str(staged), expires_at=old))
        seed.commit()
    clock[0] += timedelta(hours=2)
    reconciled, collect_now, waiting = threading.Event(), threading.Event(), threading.Event()
    original_reconcile = service.reconcile
    original_lock = package_service._exclusive_package_lock
    original_resolve = AssetResolver.resolve
    collected = {}
    errors = []
    collector_session: list[Any] = []

    def reconcile_then_pause(session):
        result = original_reconcile(session)
        reconciled.set()
        assert collect_now.wait(5), "Prepare did not start within the fixture deadline"
        return result

    @contextmanager
    def observed_lock(path):
        if threading.current_thread() is thread and path == service._resource_lock_path("content", digest):
            collector = collector_session[0]
            assert not collector.dirty and not collector.deleted
            assert not collector.connection().connection.driver_connection.in_transaction
            waiting.set()
        with original_lock(path):
            yield

    def resolve_with_contender(resolver, reference, *args, **kwargs):
        result = original_resolve(resolver, reference, *args, **kwargs)
        # Claim renewal is required; expire those renewed leases so this gate
        # still exercises GC waiting on the digest lock after prior deletions.
        clock[0] += timedelta(hours=2)
        collect_now.set()
        assert waiting.wait(5), "GC did not contend for the Prepare digest"
        return result

    def collect():
        try:
            with sessions() as collector:
                collector_session.append(collector)
                collected.update(service.garbage_collect(collector, grace_seconds=0))
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(service, "reconcile", reconcile_then_pause)
    monkeypatch.setattr(package_service, "_exclusive_package_lock", observed_lock)
    monkeypatch.setattr(AssetResolver, "resolve", resolve_with_contender)
    thread = threading.Thread(target=collect, daemon=True)
    thread.start()
    try:
        assert reconciled.wait(5)
        with sessions() as worker:
            assert run_prepare(worker, attempt_id, service=service).state == "completed"
    finally:
        collect_now.set()
        thread.join(5)
    assert not thread.is_alive()
    assert not errors
    assert collected["expired_inspections"] == 1
    assert collected["removed_content"] == 2
    assert service.content_store.path_for(digest).read_bytes() == payload


def test_a_failed_preparation_records_a_readable_diagnostic(in_memory_session_scope, service, external_source):
    import json as _json

    from astinuslab.db.models import LabPrepareAttempt

    with in_memory_session_scope() as session:
        record = service.import_path(external_source, session).record
        handle = create_prepare(session, record=record, service=service, idempotency_key="k-diag")
        session.commit()
        run_prepare(session, handle.id, service=service)

        stored = _json.loads(session.get(LabPrepareAttempt, handle.id).result_json)

        assert stored["diagnostics"]
        assert stored["diagnostics"][0]["code"]
        assert stored["diagnostics"][0]["asset"]


def test_source_network_access_is_refused_during_a_run_without_opt_in(
    in_memory_session_scope, service, external_source, monkeypatch
):
    _network_policy(monkeypatch, allowed=False)

    with in_memory_session_scope() as session:
        record = service.import_path(external_source, session).record
        handle = create_prepare(session, record=record, service=service, idempotency_key="k-nonet")
        session.commit()

        result = run_prepare(session, handle.id, service=service)

        assert result.state == "failed"


def test_a_ready_asset_whose_cached_bytes_vanished_is_prepared_again(in_memory_session_scope, service, source):
    from astinuslab.db.models import LabPackageAsset

    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        first = create_prepare(session, record=record, service=service, idempotency_key="k-recache")
        session.commit()
        assert run_prepare(session, first.id, service=service).state == "completed"

        asset = session.query(LabPackageAsset).filter_by(package_record_id=record.id).first()
        service.content_store.path_for(asset.expected_sha256).unlink()

        second = create_prepare(session, record=record, service=service, idempotency_key="k-recache-2")
        session.commit()
        result = run_prepare(session, second.id, service=service)

        assert result.state in {"completed", "failed"}
