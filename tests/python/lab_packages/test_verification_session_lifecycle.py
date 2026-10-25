from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from astinuslab.db import Base
from astinuslab.db.models import LabOperationOutbox, LabVerificationAttempt, PlaygroundSession
from astinuslab.lab_packages.models import (
    HostResources,
    LabDefinition,
    LabExperience,
    LabNetwork,
    LabVM,
    ResourceRecommendations,
)
from astinuslab.lab_packages.verification_service import (
    VerificationError,
    reconcile_authored_sessions,
    release_authored_session,
    save_draft,
    start_verification,
    verify_clean_run_task,
)
from astinuslab.playground import repositories as pr


def _definition() -> LabDefinition:
    return LabDefinition(
        package_id="frozen-lab",
        version="1.0.0",
        title="Frozen lab",
        creator="Researcher",
        license="CC-BY-4.0",
        reproducibility="unlocked",
        resources=ResourceRecommendations(
            minimum=HostResources(cpu_threads=2, memory_mb=4096, free_disk_bytes=40 * 1024**3),
            recommended=HostResources(cpu_threads=4, memory_mb=8192, free_disk_bytes=40 * 1024**3),
        ),
        networks=(LabNetwork(id="isolated", subnet_cidr="192.0.2.0/29"),),
        vms=(
            LabVM(
                id="target",
                role="target",
                image_id="ubuntu-24.04",
                vcpus=2,
                memory_mb=2048,
                disk_gb=10,
                network_ids=("isolated",),
            ),
        ),
        experience=LabExperience(),
    )


def _authored_session(session, tmp_path, *, session_id: str = "pg-authored") -> PlaygroundSession:
    authored = pr.create_playground_session(session, job_id=session_id)
    pr.register_network(
        session,
        network_id=f"net-{session_id}",
        job_id=session_id,
        name="isolated",
        subnet_cidr="192.0.2.0/29",
        isolated=True,
        libvirt_net_name=f"astinuslab-{session_id}",
    )
    disk = tmp_path / f"{session_id}.qcow2"
    disk.write_bytes(b"authored overlay")
    pr.register_vm(
        session,
        vm_id=f"vm-{session_id}",
        job_id=session_id,
        label="target",
        image_id="ubuntu-24.04",
        domain_name=f"astinuslab-{session_id}-target",
        disk_path=str(disk),
    )
    pr.update_network_state(session, f"net-{session_id}", "active")
    pr.update_vm_state(session, f"vm-{session_id}", "running")
    return authored


def test_close_authored_session_is_idempotent_and_preserves_resources(in_memory_session_scope, tmp_path):
    with in_memory_session_scope() as session:
        authored = _authored_session(session, tmp_path)
        vm = pr.list_session_vms(session, authored.id)[0]
        disk_path = vm.disk_path

        first = pr.close_authored_session(session, authored.id)
        second = pr.close_authored_session(session, authored.id)

        assert first.id == second.id == authored.id
        assert second.status == pr.AUTHORING_CLOSED
        assert pr.list_session_vms(session, authored.id)[0].id == vm.id
        assert pr.list_session_networks(session, authored.id)[0].id == "net-pg-authored"
        assert disk_path is not None and tmp_path.joinpath("pg-authored.qcow2").exists()


@pytest.mark.parametrize("terminal_state", ["completed", "cancelled", "failed"])
def test_close_authored_session_rejects_terminal_sessions(in_memory_session_scope, terminal_state):
    with in_memory_session_scope() as session:
        authored = pr.create_playground_session(session, job_id=f"pg-{terminal_state}")
        authored.status = terminal_state
        session.flush()

        with pytest.raises(pr.AuthoredSessionUnavailable, match="can no longer be verified"):
            pr.close_authored_session(session, authored.id)


@pytest.mark.parametrize(("resource_kind", "busy_state"), [("vm", "starting"), ("network", "creating")])
def test_close_authored_session_rejects_claimed_resource_mutations(
    in_memory_session_scope, tmp_path, resource_kind, busy_state
):
    with in_memory_session_scope() as session:
        authored = _authored_session(session, tmp_path, session_id=f"pg-busy-{resource_kind}")
        if resource_kind == "vm":
            pr.update_vm_state(session, f"vm-{authored.id}", busy_state)
        else:
            pr.update_network_state(session, f"net-{authored.id}", busy_state)

        with pytest.raises(pr.AuthoredSessionUnavailable, match="resource operation is still in progress"):
            pr.close_authored_session(session, authored.id)

        assert authored.status == "running"


def test_start_verification_freezes_and_binds_the_exact_authored_session(in_memory_session_scope, tmp_path):
    with in_memory_session_scope() as session:
        authored = _authored_session(session, tmp_path)
        draft = save_draft(session, _definition(), authored_session_id=authored.id)

        handle = start_verification(session, draft_id=draft.id)
        duplicate = start_verification(session, draft_id=draft.id)
        attempt = session.get(LabVerificationAttempt, handle.id)

        assert duplicate.id == handle.id
        assert attempt is not None and attempt.authored_session_id == authored.id
        assert session.get(PlaygroundSession, authored.id).status == pr.AUTHORING_CLOSED


def test_identical_draft_from_new_session_does_not_reuse_previous_verified_attempt(in_memory_session_scope, tmp_path):
    with in_memory_session_scope() as session:
        first_session = _authored_session(session, tmp_path, session_id="pg-first")
        first_draft = save_draft(session, _definition(), authored_session_id=first_session.id)
        first = start_verification(session, draft_id=first_draft.id)
        first_attempt = session.get(LabVerificationAttempt, first.id)
        first_attempt.state = "verified"
        # Finish the fixture's resource cleanup before opening another session;
        # a completed session alone cannot release its retained VM ownership.
        (tmp_path / "pg-first.qcow2").unlink()
        pr.update_vm_state(session, "vm-pg-first", "destroyed")
        pr.update_network_state(session, "net-pg-first", "destroyed")
        first_session.status = "completed"
        session.flush()

        second_session = _authored_session(session, tmp_path, session_id="pg-second")
        second_draft = save_draft(session, _definition(), authored_session_id=second_session.id)
        second = start_verification(session, draft_id=second_draft.id)

        assert second_draft.id == first_draft.id
        assert second.id != first.id
        assert second.authored_session_id == second_session.id
        assert session.get(PlaygroundSession, second_session.id).status == pr.AUTHORING_CLOSED


def test_verification_claim_heartbeat_keeps_live_work_out_of_startup_recovery(
    in_memory_session_scope, monkeypatch, tmp_path
):
    from astinuslab.lab_packages import verification_service as service
    from astinuslab.playground import cleanup as playground_cleanup

    with in_memory_session_scope() as session:
        authored = _authored_session(session, tmp_path)
        authored_id = authored.id
        draft = save_draft(session, _definition(), authored_session_id=authored.id)
        handle = start_verification(session, draft_id=draft.id)
        attempt = session.get(LabVerificationAttempt, handle.id)
        attempt.state = "running"
        event = session.query(LabOperationOutbox).filter_by(operation_id=handle.id).one()
        event.state = "claimed"
        event.claimed_at = service._now() - timedelta(seconds=service.OUTBOX_CLAIM_SECONDS + 1)
        stale_token = event.claimed_at

    heartbeat = service._OutboxClaimHeartbeat(
        attempt_id=handle.id,
        token=stale_token,
        session_scope_factory=in_memory_session_scope,
        interval_seconds=60,
    )
    heartbeat.start()
    heartbeat.stop()
    calls: list[str] = []
    monkeypatch.setattr(service, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(
        playground_cleanup,
        "purge_session_resources",
        lambda *, job_id, final_status, error_summary=None: calls.append(job_id),
    )

    assert reconcile_authored_sessions() == 0
    with in_memory_session_scope() as session:
        event = session.query(LabOperationOutbox).filter_by(operation_id=handle.id).one()
        assert event.claimed_at > stale_token
        assert session.get(PlaygroundSession, authored_id).status == pr.AUTHORING_CLOSED
    assert calls == []


def test_cleanup_claim_heartbeat_keeps_live_cleanup_out_of_startup_recovery(
    in_memory_session_scope, monkeypatch, tmp_path
):
    from astinuslab.lab_packages import verification_service as service
    from astinuslab.playground import cleanup as playground_cleanup

    with in_memory_session_scope() as session:
        authored = _authored_session(session, tmp_path)
        authored_id = authored.id
        draft = save_draft(session, _definition(), authored_session_id=authored.id)
        handle = start_verification(session, draft_id=draft.id)
        attempt = session.get(LabVerificationAttempt, handle.id)
        attempt.state = "verified"
        event = session.query(LabOperationOutbox).filter_by(operation_id=handle.id).one()
        event.state = "completed"
        event.claimed_at = service._now() - timedelta(seconds=service.CLEANUP_CLAIM_SECONDS + 1)
        stale_token = event.claimed_at
        assert pr.claim_authored_session_cleanup(session, authored_id) is True

    heartbeat = service._OutboxClaimHeartbeat(
        attempt_id=handle.id,
        token=stale_token,
        session_scope_factory=in_memory_session_scope,
        interval_seconds=60,
    )
    heartbeat.start()
    heartbeat.stop()
    calls: list[str] = []
    monkeypatch.setattr(service, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(
        playground_cleanup,
        "purge_session_resources",
        lambda *, job_id, final_status, error_summary=None: calls.append(job_id),
    )

    assert reconcile_authored_sessions() == 0
    assert calls == []


def test_concurrent_start_verification_calls_return_one_durable_attempt(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'verification-race.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
        future=True,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    with session_factory.begin() as session:
        authored = _authored_session(session, tmp_path, session_id="pg-race")
        draft_id = save_draft(session, _definition(), authored_session_id=authored.id).id

    def start() -> str:
        with session_factory.begin() as session:
            return start_verification(session, draft_id=draft_id).id

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempt_ids = list(pool.map(lambda _index: start(), range(2)))

    assert len(set(attempt_ids)) == 1
    with session_factory() as session:
        assert session.query(LabVerificationAttempt).filter_by(draft_id=draft_id).count() == 1


def test_verification_task_finishes_capture_before_claiming_cleanup(in_memory_session_scope, monkeypatch, tmp_path):
    from astinuslab.lab_packages import verification_service as service
    from astinuslab.playground import cleanup as playground_cleanup

    with in_memory_session_scope() as session:
        authored = _authored_session(session, tmp_path)
        authored_id = authored.id
        draft = save_draft(session, _definition(), authored_session_id=authored.id)
        handle = start_verification(session, draft_id=draft.id)
    events: list[str] = []

    def fake_run(session, attempt_id, **kwargs):
        attempt = session.get(LabVerificationAttempt, attempt_id)
        frozen = session.get(PlaygroundSession, attempt.authored_session_id)
        assert frozen.status == pr.AUTHORING_CLOSED
        assert tmp_path.joinpath("pg-authored.qcow2").exists()
        events.append("capture")
        attempt.state = "verified"
        attempt.progress_percent = 100
        session.flush()
        return SimpleNamespace(attempt_id=attempt_id)

    def fake_cleanup(*, job_id, final_status, error_summary=None):
        events.append("cleanup")

    monkeypatch.setattr(service, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(service, "run_verification", fake_run)
    monkeypatch.setattr(playground_cleanup, "purge_session_resources", fake_cleanup)

    assert verify_clean_run_task.run(handle.id) == handle.id
    assert events == ["capture", "cleanup"]
    with in_memory_session_scope() as session:
        assert session.get(PlaygroundSession, authored_id).status == "destroying"


def test_terminal_cleanup_is_claimed_exactly_once(in_memory_session_scope, monkeypatch, tmp_path):
    from astinuslab.lab_packages import verification_service as service
    from astinuslab.playground import cleanup as playground_cleanup

    with in_memory_session_scope() as session:
        authored = _authored_session(session, tmp_path)
        authored_id = authored.id
        draft = save_draft(session, _definition(), authored_session_id=authored.id)
        handle = start_verification(session, draft_id=draft.id)
        attempt = session.get(LabVerificationAttempt, handle.id)
        attempt.state = "failed"
        attempt.error_summary = "expected failure"
    calls: list[str] = []
    monkeypatch.setattr(service, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(
        playground_cleanup,
        "purge_session_resources",
        lambda *, job_id, final_status, error_summary=None: calls.append(job_id),
    )

    assert release_authored_session(handle.id) is True
    assert release_authored_session(handle.id) is False
    assert calls == [authored_id]


def test_reconcile_stale_cleanup_claim_is_compare_and_set(in_memory_session_scope, monkeypatch, tmp_path):
    from astinuslab.lab_packages import verification_service as service
    from astinuslab.playground import cleanup as playground_cleanup

    with in_memory_session_scope() as session:
        authored = _authored_session(session, tmp_path, session_id="pg-stale-cleanup")
        draft = save_draft(session, _definition(), authored_session_id=authored.id)
        handle = start_verification(session, draft_id=draft.id)
        attempt = session.get(LabVerificationAttempt, handle.id)
        attempt.state = "failed"
        event = session.query(LabOperationOutbox).filter_by(operation_id=handle.id).one()
        event.state = "failed"
        event.claimed_at = service._now() - timedelta(seconds=service.CLEANUP_CLAIM_SECONDS + 1)
        assert pr.claim_authored_session_cleanup(session, authored.id)

    calls: list[str] = []
    monkeypatch.setattr(service, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(
        playground_cleanup,
        "purge_session_resources",
        lambda *, job_id, final_status, error_summary=None: calls.append(job_id),
    )

    assert reconcile_authored_sessions() == 1
    assert reconcile_authored_sessions() == 0
    assert calls == ["pg-stale-cleanup"]


def test_interrupted_attempt_failure_rejects_stale_event_token(in_memory_session_scope, tmp_path):
    from astinuslab.lab_packages import verification_service as service

    with in_memory_session_scope() as session:
        authored = _authored_session(session, tmp_path, session_id="pg-stale-attempt")
        draft = save_draft(session, _definition(), authored_session_id=authored.id)
        handle = start_verification(session, draft_id=draft.id)
        attempt = session.get(LabVerificationAttempt, handle.id)
        event = session.query(LabOperationOutbox).filter_by(operation_id=handle.id).one()
        event.state = "claimed"
        event.claimed_at = service._now()
        session.flush()

        assert (
            service._fail_interrupted_attempt(
                session,
                attempt,
                expected_event_state="claimed",
                expected_claimed_at=event.claimed_at - timedelta(seconds=1),
            )
            is False
        )
        assert attempt.state == "reserved"


@pytest.mark.parametrize(
    ("terminal_state", "final_status"),
    [("failed", "failed"), ("cancelled", "cancelled")],
)
def test_verification_task_cleans_after_terminal_failure_or_cancellation(
    in_memory_session_scope, monkeypatch, tmp_path, terminal_state, final_status
):
    from astinuslab.lab_packages import verification_service as service
    from astinuslab.playground import cleanup as playground_cleanup

    with in_memory_session_scope() as session:
        authored = _authored_session(session, tmp_path)
        authored_id = authored.id
        draft = save_draft(session, _definition(), authored_session_id=authored.id)
        handle = start_verification(session, draft_id=draft.id)
    calls: list[tuple[str, str]] = []

    def fake_run(session, attempt_id, **kwargs):
        attempt = session.get(LabVerificationAttempt, attempt_id)
        attempt.state = terminal_state
        attempt.progress_percent = 100
        attempt.error_summary = f"expected {terminal_state}"
        session.commit()
        raise VerificationError("expected_terminal", terminal_state)

    monkeypatch.setattr(service, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(service, "run_verification", fake_run)
    monkeypatch.setattr(
        playground_cleanup,
        "purge_session_resources",
        lambda *, job_id, final_status, error_summary=None: calls.append((job_id, final_status)),
    )

    with pytest.raises(VerificationError, match=terminal_state):
        verify_clean_run_task.run(handle.id)

    assert calls == [(authored_id, final_status)]
    with in_memory_session_scope() as session:
        assert session.get(PlaygroundSession, authored_id).status == "destroying"


def test_verification_task_lost_claim_fences_worker_without_mutating_state_or_releasing(
    in_memory_session_scope, monkeypatch, tmp_path
):
    from astinuslab.lab_packages import verification_service as service
    from astinuslab.playground import cleanup as playground_cleanup

    with in_memory_session_scope() as session:
        authored = _authored_session(session, tmp_path)
        authored_id = authored.id
        draft = save_draft(session, _definition(), authored_session_id=authored.id)
        handle = start_verification(session, draft_id=draft.id)
        attempt = session.get(LabVerificationAttempt, handle.id)
        attempt.state = "running"
        attempt.progress_percent = 42
        session.commit()

    release_calls: list[str] = []
    purge_calls: list[str] = []
    heartbeat_stops: list[str] = []

    def fake_run(session, attempt_id, **kwargs):
        claim_heartbeat = kwargs.get("claim_heartbeat")
        assert claim_heartbeat is not None
        raise service._LostOutboxClaim(f"verification {attempt_id} lost its worker claim")

    original_release = service.release_authored_session
    original_stop = service._OutboxClaimHeartbeat.stop

    def spy_release(attempt_id: str) -> bool:
        release_calls.append(attempt_id)
        return original_release(attempt_id)

    def spy_stop(self):
        heartbeat_stops.append(self.attempt_id)
        return original_stop(self)

    monkeypatch.setattr(service, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(service, "run_verification", fake_run)
    monkeypatch.setattr(service, "release_authored_session", spy_release)
    monkeypatch.setattr(
        playground_cleanup,
        "purge_session_resources",
        lambda *, job_id, final_status, error_summary=None: purge_calls.append(job_id),
    )
    monkeypatch.setattr(service._OutboxClaimHeartbeat, "stop", spy_stop)

    with pytest.raises(service._LostOutboxClaim, match="lost its worker claim"):
        verify_clean_run_task.run(handle.id)

    assert release_calls == []
    assert purge_calls == []
    assert handle.id in heartbeat_stops

    with in_memory_session_scope() as session:
        attempt = session.get(LabVerificationAttempt, handle.id)
        assert attempt.state == "running"
        assert attempt.progress_percent == 42
        assert attempt.error_summary is None

        session_row = session.get(PlaygroundSession, authored_id)
        assert session_row.status == pr.AUTHORING_CLOSED

        event = session.query(LabOperationOutbox).filter_by(operation_id=handle.id).one()
        assert event.state == "claimed"
        assert event.completed_at is None


def test_startup_reconciliation_fails_interrupted_attempt_and_cleans_frozen_session(
    in_memory_session_scope, monkeypatch, tmp_path
):
    from astinuslab.lab_packages import verification_service as service
    from astinuslab.playground import cleanup as playground_cleanup

    with in_memory_session_scope() as session:
        authored = _authored_session(session, tmp_path)
        authored_id = authored.id
        draft = save_draft(session, _definition(), authored_session_id=authored.id)
        handle = start_verification(session, draft_id=draft.id)
        event = session.query(LabOperationOutbox).filter_by(operation_id=handle.id).one()
        event.state = "claimed"
        event.claimed_at = service._now() - timedelta(seconds=service.OUTBOX_CLAIM_SECONDS + 1)
    calls: list[str] = []
    monkeypatch.setattr(service, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(
        playground_cleanup,
        "purge_session_resources",
        lambda *, job_id, final_status, error_summary=None: calls.append(job_id),
    )

    assert reconcile_authored_sessions() == 1

    with in_memory_session_scope() as session:
        attempt = session.get(LabVerificationAttempt, handle.id)
        authored_row = session.get(PlaygroundSession, authored_id)
        assert attempt.state == "failed"
        assert attempt.error_summary.startswith("authored_session_lost:")
        assert authored_row.status == "destroying"
    assert calls == [authored_id]


@pytest.mark.parametrize("event_state", ["pending", "claimed"])
def test_startup_reconciliation_preserves_fresh_verification_work(
    in_memory_session_scope, monkeypatch, tmp_path, event_state
):
    from astinuslab.lab_packages import verification_service as service
    from astinuslab.playground import cleanup as playground_cleanup

    with in_memory_session_scope() as session:
        authored = _authored_session(session, tmp_path)
        authored_id = authored.id
        draft = save_draft(session, _definition(), authored_session_id=authored.id)
        handle = start_verification(session, draft_id=draft.id)
        attempt = session.get(LabVerificationAttempt, handle.id)
        event = session.query(LabOperationOutbox).filter_by(operation_id=handle.id).one()
        event.state = event_state
        if event_state == "claimed":
            attempt.state = "running"
            event.claimed_at = service._now()
    calls: list[str] = []
    monkeypatch.setattr(service, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(
        playground_cleanup,
        "purge_session_resources",
        lambda *, job_id, final_status, error_summary=None: calls.append(job_id),
    )

    assert reconcile_authored_sessions() == 0

    with in_memory_session_scope() as session:
        attempt = session.get(LabVerificationAttempt, handle.id)
        assert attempt.state == ("running" if event_state == "claimed" else "reserved")
        assert session.get(PlaygroundSession, authored_id).status == pr.AUTHORING_CLOSED
    assert calls == []


def test_startup_reconciliation_cleans_orphan_frozen_session(in_memory_session_scope, monkeypatch, tmp_path):
    from astinuslab.lab_packages import verification_service as service
    from astinuslab.playground import cleanup as playground_cleanup

    with in_memory_session_scope() as session:
        authored = _authored_session(session, tmp_path)
        authored_id = authored.id
        pr.close_authored_session(session, authored.id)
    calls: list[str] = []
    monkeypatch.setattr(service, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(
        playground_cleanup,
        "purge_session_resources",
        lambda *, job_id, final_status, error_summary=None: calls.append(job_id),
    )

    assert reconcile_authored_sessions() == 1

    with in_memory_session_scope() as session:
        assert session.get(PlaygroundSession, authored_id).status == "destroying"
    assert calls == [authored_id]


def test_startup_reconciliation_resumes_cleanup_after_the_durable_claim(in_memory_session_scope, monkeypatch, tmp_path):
    from astinuslab.lab_packages import verification_service as service
    from astinuslab.playground import cleanup as playground_cleanup

    with in_memory_session_scope() as session:
        authored = _authored_session(session, tmp_path)
        authored_id = authored.id
        draft = save_draft(session, _definition(), authored_session_id=authored.id)
        handle = start_verification(session, draft_id=draft.id)
        attempt = session.get(LabVerificationAttempt, handle.id)
        attempt.state = "verified"
        event = session.query(LabOperationOutbox).filter_by(operation_id=handle.id).one()
        event.state = "completed"
        event.completed_at = service._now() - timedelta(seconds=service.OUTBOX_CLAIM_SECONDS + 1)
        event.claimed_at = service._now() - timedelta(seconds=service.CLEANUP_CLAIM_SECONDS + 1)
        assert pr.claim_authored_session_cleanup(session, authored_id) is True
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(service, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(
        playground_cleanup,
        "purge_session_resources",
        lambda *, job_id, final_status, error_summary=None: calls.append((job_id, final_status)),
    )

    assert reconcile_authored_sessions() == 1
    assert calls == [(authored_id, "completed")]


def test_startup_reconciliation_does_not_resume_a_fresh_cleanup_claim(in_memory_session_scope, monkeypatch, tmp_path):
    from astinuslab.lab_packages import verification_service as service
    from astinuslab.playground import cleanup as playground_cleanup

    with in_memory_session_scope() as session:
        authored = _authored_session(session, tmp_path)
        authored_id = authored.id
        draft = save_draft(session, _definition(), authored_session_id=authored.id)
        handle = start_verification(session, draft_id=draft.id)
        attempt = session.get(LabVerificationAttempt, handle.id)
        attempt.state = "verified"
        event = session.query(LabOperationOutbox).filter_by(operation_id=handle.id).one()
        event.state = "completed"
        event.completed_at = service._now()
        event.claimed_at = service._now()
        assert pr.claim_authored_session_cleanup(session, authored_id) is True
    calls: list[str] = []
    monkeypatch.setattr(service, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(
        playground_cleanup,
        "purge_session_resources",
        lambda *, job_id, final_status, error_summary=None: calls.append(job_id),
    )

    assert reconcile_authored_sessions() == 0
    assert calls == []


@pytest.mark.parametrize("terminal_state", ["failed", "cancelled"])
def test_terminal_attempt_keeps_ownership_until_frozen_session_cleanup(
    in_memory_session_scope, tmp_path, terminal_state
):
    with in_memory_session_scope() as session:
        authored = _authored_session(session, tmp_path)
        draft = save_draft(session, _definition(), authored_session_id=authored.id)
        original = start_verification(session, draft_id=draft.id)
        attempt = session.get(LabVerificationAttempt, original.id)
        attempt.state = terminal_state
        session.flush()

        duplicate = start_verification(session, draft_id=draft.id)

        assert duplicate.id == original.id
        assert duplicate.state == terminal_state
        assert session.query(LabVerificationAttempt).filter_by(draft_id=draft.id).count() == 1


def test_legacy_unbound_attempt_cannot_capture_from_the_draft_session(in_memory_session_scope, tmp_path, monkeypatch):
    from astinuslab.core import fake_mode

    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    monkeypatch.setattr(fake_mode, "FAKE_PLAYGROUND", False)
    from astinuslab.lab_packages.verification_service import run_verification

    with in_memory_session_scope() as session:
        authored = _authored_session(session, tmp_path)
        draft = save_draft(session, _definition(), authored_session_id=authored.id)
        handle = start_verification(session, draft_id=draft.id)
        attempt = session.get(LabVerificationAttempt, handle.id)
        attempt.authored_session_id = None
        session.flush()

        with pytest.raises(VerificationError) as exc_info:
            run_verification(
                session,
                handle.id,
                captured_layers={"target": b"must not be accepted"},
                readiness_checker=lambda _vm_id, _probes: True,
            )

        assert exc_info.value.code == "authored_session_unbound"


def test_start_verification_rejects_an_authored_session_that_is_already_terminal(in_memory_session_scope, tmp_path):
    with in_memory_session_scope() as session:
        authored = _authored_session(session, tmp_path)
        draft = save_draft(session, _definition(), authored_session_id=authored.id)
        authored.status = "completed"
        session.flush()

        with pytest.raises(VerificationError) as exc_info:
            start_verification(session, draft_id=draft.id)

        assert exc_info.value.code == "authored_session_unavailable"
