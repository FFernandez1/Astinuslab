"""Prepare execution lifetime must outlive clocks, delivery and cancellation."""

from contextlib import contextmanager
from datetime import timedelta

import pytest

from astinuslab.db.models import LabOperationOutbox, LabPackage, LabPrepareAttempt, LabStorageLease
from astinuslab.lab_packages import prepare_service
from astinuslab.lab_packages.package_service import PackageDeletionConflictError
from astinuslab.lab_packages.resolver import AssetResolver


@pytest.mark.parametrize("seconds", [299, 301, 3601])
def test_live_prepare_excludes_duplicate_delivery_after_clock_expiry(prepare_gc_fixture, monkeypatch, seconds):
    svc, sessions, attempt_id, _digest, _payload, clock = prepare_gc_fixture
    resolve = AssetResolver.resolve
    calls = []
    content_lock = svc.content_lock
    lock_entries = []

    @contextmanager
    def bounded_content_lock(digest):
        # Fail before the old duplicate delivery can self-deadlock on its
        # parent's digest lock. The first acquisition remains the real flock.
        lock_entries.append(digest)
        assert len(lock_entries) == 1, "duplicate delivery reached content lock"
        with content_lock(digest):
            yield

    monkeypatch.setattr(svc, "content_lock", bounded_content_lock)

    def during_resolve(resolver, reference, *args, **kwargs):
        calls.append(reference.sha256)
        if len(calls) == 1:
            clock[0] += timedelta(seconds=seconds)
            with sessions() as other:
                duplicate = prepare_service.run_prepare(other, attempt_id, service=svc)
            assert duplicate.state == "running"
            assert len(calls) == 1, "duplicate delivery must not enter the resolver"
        return resolve(resolver, reference, *args, **kwargs)

    monkeypatch.setattr(AssetResolver, "resolve", during_resolve)
    with sessions() as session:
        result = prepare_service.run_prepare(session, attempt_id, service=svc)
    assert result.state == "completed"
    assert len(calls) == 1


def test_live_prepare_retains_package_after_expiry_and_lease_removal(prepare_gc_fixture, monkeypatch):
    svc, sessions, attempt_id, _digest, _payload, clock = prepare_gc_fixture
    resolve = AssetResolver.resolve

    def during_resolve(resolver, reference, *args, **kwargs):
        clock[0] += timedelta(seconds=3601)
        with sessions() as other:
            package_id = other.get(LabPrepareAttempt, attempt_id).package_record_id
            other.query(LabStorageLease).filter_by(owner_id=attempt_id).delete()
            other.commit()
            with pytest.raises(PackageDeletionConflictError):
                svc.delete_package(package_id, other)
            other.rollback()
            assert other.get(LabPackage, package_id) is not None
        return resolve(resolver, reference, *args, **kwargs)

    monkeypatch.setattr(AssetResolver, "resolve", during_resolve)
    with sessions() as session:
        assert prepare_service.run_prepare(session, attempt_id, service=svc).state == "completed"


def test_cancellation_during_final_asset_retains_bytes_but_never_completes(prepare_gc_fixture, monkeypatch):
    svc, sessions, attempt_id, digest, payload, _clock = prepare_gc_fixture
    resolve = AssetResolver.resolve

    def during_resolve(resolver, reference, *args, **kwargs):
        with sessions() as other:
            handle = prepare_service.cancel_prepare(other, attempt_id, service=svc)
            assert handle.state == "running"
            other.commit()
        return resolve(resolver, reference, *args, **kwargs)

    monkeypatch.setattr(AssetResolver, "resolve", during_resolve)
    with sessions() as session:
        result = prepare_service.run_prepare(session, attempt_id, service=svc)
    assert result.state == "cancelled"
    assert svc.content_store.has(digest, size_bytes=len(payload))
    with sessions() as session:
        assert session.query(LabStorageLease).filter_by(owner_id=attempt_id).count() == 0
        assert session.query(LabOperationOutbox).filter_by(operation_id=attempt_id).one().state == "completed"


def test_launch_refuses_committed_deletion_before_preflight(prepare_gc_fixture, monkeypatch):
    import json

    from astinuslab.db.models import LabPackageTombstone
    from astinuslab.lab_packages import launch_service
    from astinuslab.lab_packages.models import LabDefinition, LabManifest

    svc, sessions, attempt_id, _digest, _payload, _clock = prepare_gc_fixture
    with sessions() as session:
        record = session.get(LabPackage, session.get(LabPrepareAttempt, attempt_id).package_record_id)
        session.add(
            LabPackageTombstone(
                id="lpt-launch-gate",
                package_record_id=record.id,
                archive_path=record.archive_path,
                archive_sha256=record.archive_sha256,
                reason="synthetic launch gate",
            )
        )
        session.commit()
        definition = LabDefinition.model_validate_json(record.definition_json)
        manifest = LabManifest.model_validate(json.loads(record.manifest_json))

        def must_not_preflight(*args, **kwargs):
            pytest.fail("launch admitted a package with committed deletion intent")

        monkeypatch.setattr(launch_service, "_preflight", must_not_preflight)
        with pytest.raises(launch_service.PackageLaunchError, match="delet"):
            launch_service.launch_package(
                session,
                record=record,
                definition=definition,
                manifest=manifest,
                service=svc,
                idempotency_key="launch-gate",
                acknowledged_warning_token=None,
            )


def _claim(svc, session, attempt_id, lock):
    return prepare_service._claim_attempt(session, attempt_id, service=svc, package_lock=lock)


def _rows(session):
    from sqlalchemy import select

    from astinuslab.db.models import LabContentObject, LabPackageAsset

    return {
        model.__tablename__: [
            tuple(getattr(row, column.name) for column in model.__table__.columns)
            for row in session.scalars(select(model))
        ]
        for model in (LabPrepareAttempt, LabOperationOutbox, LabStorageLease, LabPackageAsset, LabContentObject)
    }


def test_reclaim_uses_new_token_with_frozen_clock_and_old_claim_changes_nothing(prepare_gc_fixture):
    from astinuslab.lab_packages.prepare_authority import PrepareClaimLost, owned_prepare_write

    svc, sessions, attempt_id, _digest, _payload, _clock = prepare_gc_fixture
    with sessions() as session:
        package_id = session.get(LabPrepareAttempt, attempt_id).package_record_id
        with svc.try_prepare_package_lock(package_id) as first_lock:
            first = _claim(svc, session, attempt_id, first_lock)
        assert not first_lock.active
        with pytest.raises(PrepareClaimLost):
            with owned_prepare_write(session, first, package_lock=first_lock):
                pytest.fail("released lock accepted")
        with svc.try_prepare_package_lock(package_id) as lock:
            second = _claim(svc, session, attempt_id, lock)
            assert first.execution_token != second.execution_token
            before = _rows(session)
            with pytest.raises(PrepareClaimLost):
                with owned_prepare_write(session, first, package_lock=lock):
                    pytest.fail("stale generation accepted")
            assert _rows(session) == before
            with owned_prepare_write(session, second, package_lock=lock) as attempt:
                attempt.progress_percent = 23
            assert session.get(LabPrepareAttempt, attempt_id).progress_percent == 23


@pytest.mark.parametrize("flushed", [False, True])
def test_guard_refuses_caller_writes_without_committing_or_rolling_them_back(prepare_gc_fixture, flushed):
    from astinuslab.lab_packages.prepare_authority import PrepareTransactionError, owned_prepare_write

    svc, sessions, attempt_id, _digest, _payload, _clock = prepare_gc_fixture
    with sessions() as session:
        package_id = session.get(LabPrepareAttempt, attempt_id).package_record_id
        with svc.try_prepare_package_lock(package_id) as lock:
            claim = _claim(svc, session, attempt_id, lock)
            package = session.get(LabPackage, package_id)
            original = package.last_used_at
            package.last_used_at = _clock[0]
            if flushed:
                session.flush()
                assert not session.dirty
            with pytest.raises(PrepareTransactionError):
                with owned_prepare_write(session, claim, package_lock=lock):
                    pytest.fail("caller transaction accepted")
            assert session.in_transaction()
            assert package.last_used_at == _clock[0]
            with sessions() as observer:
                assert observer.get(LabPackage, package_id).last_used_at == original
            session.rollback()


def test_guard_refreshes_cached_cancel_and_terminal_commit_is_atomic(prepare_gc_fixture, monkeypatch):
    from astinuslab.lab_packages.prepare_authority import owned_prepare_write

    svc, sessions, attempt_id, _digest, _payload, _clock = prepare_gc_fixture
    with sessions() as worker:
        package_id = worker.get(LabPrepareAttempt, attempt_id).package_record_id
        with svc.try_prepare_package_lock(package_id) as lock:
            claim = _claim(svc, worker, attempt_id, lock)
            cached = worker.get(LabPrepareAttempt, attempt_id)
            assert not cached.cancel_requested
            with sessions() as caller:
                assert prepare_service.cancel_prepare(caller, attempt_id, service=svc).state == "running"
                caller.commit()
            with owned_prepare_write(worker, claim, package_lock=lock) as attempt:
                assert attempt is cached and attempt.cancel_requested
            before = _rows(worker)
            commit = worker.commit

            def refuse_commit():
                worker.flush()
                raise RuntimeError("synthetic terminal commit failure")

            monkeypatch.setattr(worker, "commit", refuse_commit)
            with pytest.raises(RuntimeError, match="terminal commit failure"):
                with owned_prepare_write(worker, claim, package_lock=lock) as attempt:
                    prepare_service._terminalize_locked(worker, attempt, package_lock=lock, state="completed")
            assert _rows(worker) == before
            monkeypatch.setattr(worker, "commit", commit)
            with owned_prepare_write(worker, claim, package_lock=lock) as attempt:
                prepare_service._terminalize_locked(worker, attempt, package_lock=lock, state="completed")
            assert worker.get(LabPrepareAttempt, attempt_id).state == "cancelled"


def test_busy_create_is_read_only_and_different_key_conflicts_even_without_leases(prepare_gc_fixture):
    svc, sessions, attempt_id, _digest, _payload, _clock = prepare_gc_fixture
    with sessions() as worker:
        package_id = worker.get(LabPrepareAttempt, attempt_id).package_record_id
        with svc.try_prepare_package_lock(package_id) as lock:
            _claim(svc, worker, attempt_id, lock)
            with sessions() as caller:
                caller.query(LabStorageLease).filter_by(owner_id=attempt_id).delete()
                caller.commit()
                before = _rows(caller)
                record = caller.get(LabPackage, package_id)
                same = prepare_service.create_prepare(caller, record=record, service=svc, idempotency_key="gc-boundary")
                assert same.state == "running" and not same.created
                assert _rows(caller) == before
                with pytest.raises(prepare_service.PrepareError, match="network policy"):
                    prepare_service.create_prepare(
                        caller, record=record, service=svc, idempotency_key="gc-boundary", network_opt_in=True
                    )
                with pytest.raises(prepare_service.PrepareError, match="active"):
                    prepare_service.create_prepare(caller, record=record, service=svc, idempotency_key="other")
        with sessions() as caller:
            with pytest.raises(prepare_service.PrepareError, match="nonterminal"):
                prepare_service.create_prepare(
                    caller, record=caller.get(LabPackage, package_id), service=svc, idempotency_key="other"
                )


@pytest.mark.parametrize("claim_first", [False, True])
def test_expired_reservation_claim_delete_admits_only_one_winner(prepare_gc_fixture, claim_first):
    from pathlib import Path

    from astinuslab.db.models import LabPackageTombstone

    svc, sessions, attempt_id, _digest, _payload, clock = prepare_gc_fixture
    clock[0] += timedelta(seconds=3601)
    with sessions() as session:
        attempt = session.get(LabPrepareAttempt, attempt_id)
        package_id = attempt.package_record_id
        archive = Path(session.get(LabPackage, package_id).archive_path)
        if claim_first:
            with svc.try_prepare_package_lock(package_id) as lock:
                assert _claim(svc, session, attempt_id, lock)
                with sessions() as deleter:
                    with pytest.raises(PackageDeletionConflictError):
                        svc.delete_package(package_id, deleter)
                    assert deleter.query(LabPackageTombstone).count() == 0
            assert archive.exists()
        else:
            svc.delete_package(package_id, session)
            assert not archive.exists()
            assert session.get(LabPackage, package_id) is None
            assert session.query(LabOperationOutbox).filter_by(operation_id=attempt_id).one().state == "completed"
            assert session.query(LabStorageLease).filter_by(owner_id=attempt_id).count() == 0
            with pytest.raises(prepare_service.PrepareError, match="not found"):
                prepare_service.run_prepare(session, attempt_id, service=svc)


def test_tombstone_replay_uses_package_lock_and_skips_live_execution(prepare_gc_fixture):
    from pathlib import Path

    from astinuslab.db.models import LabPackageTombstone

    svc, sessions, attempt_id, _digest, _payload, clock = prepare_gc_fixture
    with sessions() as worker:
        package_id = worker.get(LabPrepareAttempt, attempt_id).package_record_id
        archive = Path(worker.get(LabPackage, package_id).archive_path)
        with svc.try_prepare_package_lock(package_id) as lock:
            _claim(svc, worker, attempt_id, lock)
            with sessions() as recovery:
                record = recovery.get(LabPackage, package_id)
                tombstone = LabPackageTombstone(
                    id="lpt-replay",
                    package_record_id=package_id,
                    archive_path=str(archive),
                    archive_sha256=record.archive_sha256,
                    reason="synthetic replay",
                )
                recovery.add(tombstone)
                recovery.commit()
                clock[0] += timedelta(seconds=3601)
                assert not svc._replay_tombstone(tombstone, recovery)
                assert recovery.get(LabPackageTombstone, "lpt-replay").state == "pending"
                assert archive.exists()
        with sessions() as recovery:
            assert svc._replay_tombstone(recovery.get(LabPackageTombstone, "lpt-replay"), recovery)
            assert recovery.get(LabPackage, package_id) is None
            assert not archive.exists()


def test_cancel_during_digest_wait_prevents_resolver(prepare_gc_fixture, monkeypatch):
    svc, sessions, attempt_id, _digest, _payload, _clock = prepare_gc_fixture
    original = svc.content_lock

    @contextmanager
    def cancel_before_acquire(digest):
        with sessions() as caller:
            prepare_service.cancel_prepare(caller, attempt_id, service=svc)
            caller.commit()
        with original(digest):
            yield

    monkeypatch.setattr(svc, "content_lock", cancel_before_acquire)
    monkeypatch.setattr(AssetResolver, "resolve", lambda *a, **k: pytest.fail("cancelled worker resolved bytes"))
    with sessions() as worker:
        assert prepare_service.run_prepare(worker, attempt_id, service=svc).state == "cancelled"


def test_process_death_releases_package_custody_without_clock_delay(prepare_gc_fixture):
    import multiprocessing

    from astinuslab.lab_packages.prepare_authority import PreparePackageBusy

    svc, sessions, attempt_id, _digest, _payload, _clock = prepare_gc_fixture
    with sessions() as session:
        package_id = session.get(LabPrepareAttempt, attempt_id).package_record_id
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()

    def claim_and_wait():
        try:
            # Never reuse an inherited SQLite connection in the child.
            sessions.kw["bind"].dispose(close=False)
            with sessions() as session, svc.try_prepare_package_lock(package_id) as lock:
                claim = _claim(svc, session, attempt_id, lock)
                child.send(claim.execution_token)
                child.recv()
        except BaseException as exc:
            child.send(repr(exc))
            raise

    process = context.Process(target=claim_and_wait)
    process.start()
    child.close()
    try:
        assert parent.poll(5), "child did not claim within fixture deadline"
        old_token = parent.recv()
        assert len(old_token) == 32
        with pytest.raises(PreparePackageBusy):
            with svc.try_prepare_package_lock(package_id):
                pytest.fail("live child lost custody")
        with sessions() as duplicate:
            assert prepare_service.run_prepare(duplicate, attempt_id, service=svc).state == "running"
        process.terminate()
        process.join(5)
        assert not process.is_alive()
        with sessions() as recovery:
            assert prepare_service.run_prepare(recovery, attempt_id, service=svc).state == "completed"
            assert recovery.get(LabPrepareAttempt, attempt_id).execution_token != old_token
    finally:
        if process.is_alive():
            process.terminate()
        process.join(5)
        parent.close()
        process.close()


def test_recovery_preserves_ready_asset_origin_without_resolving_again(prepare_gc_fixture, monkeypatch):
    from astinuslab.db.models import LabPackageAsset

    svc, sessions, attempt_id, digest, payload, _clock = prepare_gc_fixture
    with sessions() as session:
        assert prepare_service.run_prepare(session, attempt_id, service=svc).state == "completed"
        attempt = session.get(LabPrepareAttempt, attempt_id)
        asset = (
            session.query(LabPackageAsset)
            .filter_by(package_record_id=attempt.package_record_id, expected_sha256=digest)
            .one()
        )
        original_origin, original_prepared_at = asset.prepared_origin, asset.prepared_at
        # Simulate process death after committed ownership but before terminal state.
        attempt.state = "running"
        session.query(LabOperationOutbox).filter_by(operation_id=attempt_id).one().state = "claimed"
        session.commit()
        monkeypatch.setattr(AssetResolver, "resolve", lambda *a, **k: pytest.fail("ready asset resolved again"))
        assert prepare_service.run_prepare(session, attempt_id, service=svc).state == "completed"
        session.refresh(asset)
        assert (asset.prepared_origin, asset.prepared_at) == (original_origin, original_prepared_at)
        assert svc.content_store.has(digest, size_bytes=len(payload))


@pytest.mark.parametrize("stage", ["claim", "resolving", "ownership", "terminal"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_commit_failures_release_custody_and_preserve_original_error(
    prepare_gc_fixture, monkeypatch, stage, cleanup_fails
):
    from astinuslab.db.models import LabPackageAsset

    svc, sessions, attempt_id, _digest, _payload, _clock = prepare_gc_fixture
    with sessions() as session:
        package_id = session.get(LabPrepareAttempt, attempt_id).package_record_id
        commit = session.commit
        failures: list[str] = []
        resolve = AssetResolver.resolve
        resolved = []

        def observed_resolve(*args, **kwargs):
            resolved.append(True)
            return resolve(*args, **kwargs)

        def injected_commit():
            attempt = session.get(LabPrepareAttempt, attempt_id)
            assets = prepare_service._package_assets(session, package_id)
            matches = {
                "claim": attempt.state == "running"
                and not resolved
                and all(a.prepare_state == "missing" for a in assets),
                "resolving": any(a.prepare_state == "resolving" for a in assets) and not resolved,
                "ownership": any(a.prepare_state == "ready" for a in assets),
                "terminal": attempt.state == "completed",
            }
            if not failures and matches[stage]:
                failures.append(stage)
                session.flush()
                raise RuntimeError(f"original {stage} commit error")
            if failures and cleanup_fails:
                session.flush()
                raise ValueError("secondary finalization error")
            commit()

        monkeypatch.setattr(AssetResolver, "resolve", observed_resolve)
        monkeypatch.setattr(session, "commit", injected_commit)
        with pytest.raises(RuntimeError, match=f"original {stage} commit error"):
            prepare_service.run_prepare(session, attempt_id, service=svc)
        assert failures == [stage]
        if stage in {"claim", "resolving"}:
            assert not resolved
        with svc.try_prepare_package_lock(package_id):
            pass
        with sessions() as observer:
            attempt = observer.get(LabPrepareAttempt, attempt_id)
            assert attempt.state != "completed"
            if stage == "ownership":
                assert not any(
                    asset.content_sha256
                    for asset in observer.query(LabPackageAsset).filter_by(package_record_id=package_id)
                )
        # A claim which was committed but whose error cleanup failed remains
        # recoverable with a fresh generation, without waiting for lease expiry.
        if cleanup_fails and stage != "claim":
            with sessions() as recovery:
                assert prepare_service.run_prepare(recovery, attempt_id, service=svc).state == "completed"


def test_legacy_duplicate_nonterminal_attempts_are_refused_without_mutation(prepare_gc_fixture):
    svc, sessions, attempt_id, _digest, _payload, _clock = prepare_gc_fixture
    with sessions() as session:
        first = session.get(LabPrepareAttempt, attempt_id)
        session.add(
            LabPrepareAttempt(
                id="lpa-legacy-duplicate",
                package_record_id=first.package_record_id,
                definition_sha256=first.definition_sha256,
                idempotency_key="legacy-duplicate",
                state="running",
            )
        )
        session.commit()
        before = _rows(session)
        with pytest.raises(prepare_service.PrepareError, match="nonterminal"):
            prepare_service.run_prepare(session, attempt_id, service=svc)
        assert _rows(session) == before


@pytest.mark.parametrize("commit_request", [False, True])
def test_reservation_keeps_writer_until_request_completion(prepare_gc_fixture, commit_request):
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    svc, sessions, attempt_id, _digest, _payload, _clock = prepare_gc_fixture
    with sessions() as caller:
        prepare_service.cancel_prepare(caller, attempt_id, service=svc)
        caller.commit()
        package_id = caller.get(LabPrepareAttempt, attempt_id).package_record_id
        created = prepare_service.create_prepare(
            caller, record=caller.get(LabPackage, package_id), service=svc, idempotency_key="new-reservation"
        )
        # create_prepare has returned (file lock released), but another writer
        # cannot slip between admission and the caller's commit/rollback.
        with sessions() as contender:
            contender.execute(text("PRAGMA busy_timeout=0"))
            with pytest.raises(OperationalError, match="locked"):
                svc.delete_package(package_id, contender)
            contender.rollback()
        if commit_request:
            caller.commit()
        else:
            caller.rollback()
        with sessions() as contender:
            if commit_request:
                assert contender.get(LabPrepareAttempt, created.id) is not None
                with pytest.raises(PackageDeletionConflictError, match="leased"):
                    svc.delete_package(package_id, contender)
            else:
                assert contender.get(LabPrepareAttempt, created.id) is None
                svc.delete_package(package_id, contender)
                assert contender.get(LabPackage, package_id) is None


def test_reconcile_never_dispatches_failed_intent(prepare_gc_fixture, monkeypatch):
    svc, sessions, attempt_id, _digest, _payload, _clock = prepare_gc_fixture
    with sessions() as session:
        session.get(LabPrepareAttempt, attempt_id).state = "failed"
        session.commit()
    monkeypatch.setattr(prepare_service, "session_scope", sessions)
    assert prepare_service.reconcile_prepares() == 0  # fixture rejects all task dispatch


@pytest.mark.parametrize("other_operation", [False, True])
def test_abandoned_running_delete_preserves_unrelated_live_leases(prepare_gc_fixture, other_operation):
    from uuid import uuid4

    svc, sessions, attempt_id, _digest, _payload, clock = prepare_gc_fixture
    with sessions() as session:
        package_id = session.get(LabPrepareAttempt, attempt_id).package_record_id
        with svc.try_prepare_package_lock(package_id) as lock:
            _claim(svc, session, attempt_id, lock)
        if other_operation:
            session.add(
                LabStorageLease(
                    id=uuid4().hex,
                    resource_kind="package",
                    resource_id=package_id,
                    owner_id="other-operation",
                    operation_kind="package_export",
                    expires_at=clock[0] + timedelta(hours=1),
                )
            )
            session.commit()
            with pytest.raises(PackageDeletionConflictError, match="leased"):
                svc.delete_package(package_id, session)
            session.rollback()
            assert session.get(LabPrepareAttempt, attempt_id).state == "running"
            assert session.query(LabStorageLease).filter_by(owner_id="other-operation").count() == 1
        else:
            # Fresh running leases cannot outlive their departed process owner.
            svc.delete_package(package_id, session)
            assert session.get(LabPackage, package_id) is None
            assert session.query(LabOperationOutbox).filter_by(operation_id=attempt_id).one().state == "completed"


def test_two_keys_race_to_admit_only_one_nonterminal_attempt(prepare_gc_fixture):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    svc, sessions, attempt_id, _digest, _payload, _clock = prepare_gc_fixture
    with sessions() as session:
        package_id = session.get(LabPrepareAttempt, attempt_id).package_record_id
        prepare_service.cancel_prepare(session, attempt_id, service=svc)
        session.commit()
    ready = Barrier(2, timeout=5)

    def reserve(key):
        with sessions() as session:
            record = session.get(LabPackage, package_id)
            ready.wait()
            try:
                handle = prepare_service.create_prepare(session, record=record, service=svc, idempotency_key=key)
                session.commit()
                return handle.id
            except prepare_service.PrepareError as exc:
                session.rollback()
                assert exc.code == "prepare_conflict"
                return None

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(reserve, ("concurrent-a", "concurrent-b")))
    assert sum(result is not None for result in results) == 1
    with sessions() as session:
        assert (
            session.query(LabPrepareAttempt).filter(LabPrepareAttempt.state.in_(("reserved", "running"))).count() == 1
        )
