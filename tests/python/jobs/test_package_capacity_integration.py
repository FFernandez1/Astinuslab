"""Merge regressions use isolated SQLite and injected observations only."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from astinuslab.api.schemas import JobCreate
from astinuslab.db.models import JobVM, PlaygroundSession
from astinuslab.db.repositories import JobRepository
from astinuslab.jobs.capacity import GIB, HostCapacity, Requirements, shortage


@pytest.mark.parametrize("resource", ["total_ram", "available_ram", "storage"])
def test_pending_package_vm_dimensions_are_charged(in_memory_session_scope, resource):
    config = SimpleNamespace(
        max_concurrent_vms=8,
        capacity_ram_headroom_mb=1024,
        capacity_storage_headroom_gb=5,
        capacity_vm_overhead_mb=256,
    )
    host = HostCapacity(16384, 14000, 500 * GIB, 400 * GIB, {})
    if resource == "total_ram":
        host = replace(host, memory_total_mb=10000)
    elif resource == "available_ram":
        host = replace(host, memory_available_mb=10000)
    else:
        host = replace(host, storage_free_bytes=35 * GIB)
    with in_memory_session_scope() as session:
        session.add(PlaygroundSession(id="pg-package", status="completed"))
        session.flush()
        session.add(
            JobVM(
                id="pending-package-vm",
                session_id="pg-package",
                label="large",
                image_id="fixture",
                state="pending",
                domain_name="astinuslab-pg-package-large",
                disk_path="/fixture/never-read",
                memory_mb=8192,
                disk_gb=30,
            )
        )
        session.flush()
        reason = shortage(session, Requirements(1, 2048, 10 * GIB), host, config)
        assert reason is not None
        assert ("storage" if resource == "storage" else "RAM") in reason
        # Releasing an unrelated session status cannot erase pending VM intent.
        session.get(JobVM, "pending-package-vm").state = "destroyed"
        session.flush()
        assert shortage(session, Requirements(1, 2048, 10 * GIB), host, config) is None


@pytest.mark.parametrize("failure", [None, "packages", "instances"])
def test_startup_orders_package_custody_before_generic_cleanup_and_admission(
    in_memory_session_scope,
    monkeypatch,
    tmp_path,
    failure,
):
    from astinuslab import db
    from astinuslab.jobs import action_lifecycle, capacity_runtime, package_instance_coordinator, startup_recovery
    from astinuslab.jobs import celery_worker_signals as signals
    from astinuslab.lab_packages import export_service, prepare_service, verification_service
    from astinuslab.virtualization import host_script_manifest

    events = []

    def record(name):
        def call(*args, **kwargs):
            events.append(name)
            if name == failure:
                raise RuntimeError("injected reconciliation failure")

        return call

    monkeypatch.setattr(db, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(signals, "settings", signals.settings.model_copy(update={"storage_dir": tmp_path}))
    monkeypatch.setattr(host_script_manifest, "preflight_problems", lambda **kw: [])
    for module, name, event in [
        (capacity_runtime, "adopt_legacy_reservations", "adopt"),
        (startup_recovery, "reconcile_orphaned_jobs", "jobs"),
        (action_lifecycle, "reconcile_interrupted_actions", "claims"),
        (action_lifecycle, "recover_queued_dispatches", "dispatch"),
        (startup_recovery, "reconcile_publication_attempts", "publications"),
        (startup_recovery, "reconcile_lab_package_storage", "packages"),
        (package_instance_coordinator, "reconcile_package_instances", "instances"),
        (verification_service, "reconcile_authored_sessions", "authored"),
        (verification_service, "reconcile_verifications", "verification"),
        (prepare_service, "reconcile_prepares", "prepares"),
        (export_service, "reconcile_exports", "exports"),
        (startup_recovery, "recover_playground_sessions", "playground"),
        (capacity_runtime, "schedule_capacity", "capacity"),
    ]:
        monkeypatch.setattr(module, name, record(event))
    signals._on_worker_ready(SimpleNamespace(hostname="runner@fixture"))
    expected = [
        "adopt",
        "jobs",
        "claims",
        "dispatch",
        "publications",
        "packages",
        "instances",
        "authored",
        "verification",
        "prepares",
        "exports",
        "playground",
        "capacity",
    ]
    assert events == (expected[: expected.index(failure) + 1] if failure else expected)


def test_merged_beat_keeps_package_verification_and_capacity():
    from astinuslab.jobs.celery_app import celery_app, settings

    schedule = celery_app.conf.beat_schedule
    assert schedule["admit-waiting-jobs"] == {"task": "admit_waiting_jobs", "schedule": 15}
    if settings.celery_reconciliation_enabled:
        assert schedule["reconcile-verifications"]["task"] == "reconcile_verifications"
    assert "admit_waiting_jobs" in celery_app.tasks
    assert "reconcile_verifications" in celery_app.tasks


def test_batch_analysis_claim_executes_once(in_memory_session_scope, monkeypatch):
    from astinuslab import db
    from astinuslab.forensics import vol3_analysis
    from astinuslab.jobs import celery_app

    monkeypatch.setattr(db, "session_scope", in_memory_session_scope)
    calls = []
    monkeypatch.setattr(vol3_analysis, "run_vol3_analysis", lambda job_id, action_id: calls.append((job_id, action_id)))
    monkeypatch.setattr(celery_app, "_dispatch_pending_actions", lambda job_id: None)
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(JobCreate(target_id="fixture", scenario_id="fixture", execution_mode="manual_trigger"))
        job.status = "completed"
        action = repo.enqueue_action(job.id, "run_vol3_analysis")
        job_id, action_id = job.id, action.id
    celery_app.run_vol3_analysis_task(job_id, action_id)
    celery_app.run_vol3_analysis_task(job_id, action_id)
    assert calls == [(job_id, action_id)]
    with in_memory_session_scope() as session:
        assert session.get(type(job), job_id).status == "completed"


@pytest.mark.parametrize("occupant", ["job", "playground", None])
def test_reset_registration_rechecks_shared_admission(in_memory_session_scope, monkeypatch, occupant):
    import json
    from datetime import UTC, datetime

    from astinuslab.db.models import LabInstance, LabInstanceAction, LabOperationOutbox, LabPackage
    from astinuslab.jobs import package_instance_coordinator as coordinator

    now = datetime.now(UTC)
    payload = {
        "_coordinator": {
            "action_id": "reset-action",
            "generation": 1,
            "session_id": "pg-old",
            "phase": "reset_cleanup",
            "reset_snapshot": {"package_record_id": "package", "networks": [], "vms": [], "topology": []},
        }
    }
    claim = coordinator._OperationClaim(
        "instance",
        "reset",
        "reset-action",
        1,
        "pg-old",
        now,
        payload,
        "reset_cleanup",
        False,
    )
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)
    with in_memory_session_scope() as session:
        session.add(
            LabPackage(
                id="package",
                package_id="fixture",
                version="1",
                title="fixture",
                creator="fixture",
                license="CC0",
                reproducibility="unlocked",
                archive_path="/fixture/never-read",
                archive_size_bytes=1,
                archive_sha256="a" * 64,
                manifest_sha256="b" * 64,
                manifest_json="{}",
                definition_json="{}",
            )
        )
        session.flush()
        session.add(
            LabInstance(
                id="instance",
                package_record_id="package",
                idempotency_key="fixture",
                state="resetting",
            )
        )
        session.flush()
        session.add(
            LabInstanceAction(
                id="reset-action",
                instance_id="instance",
                action_type="reset",
                state="running",
                payload_json=json.dumps(payload),
            )
        )
        session.add(
            LabOperationOutbox(
                id="outbox",
                operation_kind="package_instance_reset",
                operation_id="instance",
                state="claimed",
                claimed_at=now,
                payload_json=json.dumps(payload),
            )
        )
        if occupant == "job":
            JobRepository(session).create_job(
                JobCreate(
                    target_id="fixture",
                    scenario_id="fixture",
                    execution_mode="manual_trigger",
                )
            )
        elif occupant == "playground":
            session.add(PlaygroundSession(id="pg-other", status="running"))
    if occupant:
        with pytest.raises(RuntimeError, match="active"):
            coordinator._create_reset_session(claim)
        with in_memory_session_scope() as session:
            assert session.get(LabInstance, "instance").playground_session_id is None
            assert session.query(PlaygroundSession).count() == (1 if occupant == "playground" else 0)
            assert session.get(LabOperationOutbox, "outbox").payload_json == json.dumps(payload)
    else:
        from astinuslab.api.errors import ApiError
        from astinuslab.playground.repositories import create_playground_session

        # The reset lease owns admission even between its old session cleanup
        # and registration of the replacement session.
        with in_memory_session_scope() as session:
            with pytest.raises(ApiError, match="reset"):
                create_playground_session(session, job_id="pg-racing-author")
        result = coordinator._create_reset_session(claim)
        assert result is not None and result.session_id != "pg-old"
        with in_memory_session_scope() as session:
            assert session.get(LabInstance, "instance").playground_session_id == result.session_id
            assert session.get(PlaygroundSession, result.session_id).status == "queued"


@pytest.mark.parametrize("retained", ["actual_domain", "vm_row"])
def test_retained_playground_resources_block_launch_until_cleanup(
    in_memory_session_scope,
    retained,
):
    from astinuslab.jobs.capacity import admit_waiting, reserve_job
    from astinuslab.jobs.capacity_runtime import prepare_launch

    config = SimpleNamespace(
        max_concurrent_vms=8,
        capacity_ram_headroom_mb=1024,
        capacity_storage_headroom_gb=5,
        capacity_vm_overhead_mb=256,
    )
    host = HostCapacity(16384, 14000, 500 * GIB, 400 * GIB, {})
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(
            JobCreate(
                target_id="fixture",
                scenario_id="fixture",
                execution_mode="manual_trigger",
            )
        )
        action = repo.enqueue_action(job.id, "run_manual_trigger_job")
        # First admission succeeds. Host/DB observations change before boot.
        assert reserve_job(session, job, Requirements(1, 2048, 10 * GIB), host, config)
        if retained == "actual_domain":
            retained_host = replace(host, domains={"astinuslab-pg-orphan-target": 1024})
        else:
            retained_host = host
            session.add(PlaygroundSession(id="pg-ended", status="completed"))
            session.flush()
            session.add(
                JobVM(
                    id="retained",
                    session_id="pg-ended",
                    label="target",
                    image_id="fixture",
                    state="failed",
                    domain_name="astinuslab-pg-ended-target",
                    disk_path="/fixture/never-read",
                    memory_mb=1024,
                    disk_gb=1,
                )
            )
            session.flush()
        # Generous remaining capacity must not permit the two product modes
        # to overlap, including a delivery already published before discovery.
        assert not prepare_launch(session, job.id, action.id, config, retained_host)
        assert job.status == "waiting_for_capacity"
        assert job.capacity_reason is not None and "Playground" in job.capacity_reason
        assert admit_waiting(session, retained_host, config) == []
        if retained == "vm_row":
            session.get(JobVM, "retained").state = "destroyed"
            session.flush()
        assert admit_waiting(session, host, config) == [(job.id, action.id)]
        assert prepare_launch(session, job.id, action.id, config, host)
