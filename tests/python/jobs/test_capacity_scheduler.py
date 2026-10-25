"""Capacity fixtures exercise SQLite transactions; no actual VM or host evidence."""

import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from astinuslab.api.schemas import JobCreate
from astinuslab.db.repositories import JobRepository
from astinuslab.jobs.capacity_host import CleanupProof


def job_intent(session, *, attacker=False):
    repo = JobRepository(session)
    job = repo.create_job(
        JobCreate(
            target_id="fixture",
            scenario_id="manual",
            execution_mode="manual_trigger",
            attacker_vm_id="fixture-attacker" if attacker else None,
        )
    )
    repo.enqueue_action(job.id, "run_manual_trigger_job")
    return job


@pytest.fixture
def budget():
    return SimpleNamespace(
        max_concurrent_vms=2, capacity_ram_headroom_mb=1024, capacity_storage_headroom_gb=5, capacity_vm_overhead_mb=256
    )


@pytest.fixture
def host():
    from astinuslab.jobs.capacity import HostCapacity

    return HostCapacity(
        memory_total_mb=16384,
        memory_available_mb=14000,
        storage_total_bytes=500 * 1024**3,
        storage_free_bytes=400 * 1024**3,
        domains={},
    )


def submit(session, job, budget, host, *, count=1, memory=2048, disk=10):
    from astinuslab.jobs.capacity import Requirements, reserve_job

    return reserve_job(session, job, Requirements(count, memory, disk * 1024**3), host, budget)


def test_two_live_third_waits_then_cleanup_autoadmits(in_memory_session_scope, budget, host):
    from astinuslab.jobs.capacity import admit_waiting, confirm_cleanup

    with in_memory_session_scope() as session:
        first, second, third = [job_intent(session) for _ in range(3)]
        assert submit(session, first, budget, host)
        assert submit(session, second, budget, host)
        JobRepository(session).update_job(first.id, status="waiting_for_capture")
        JobRepository(session).update_job(second.id, status="waiting_for_capture")
        assert not submit(session, third, budget, host)
        assert third.status == "waiting_for_capacity"
        assert "VM" in third.capacity_reason
        assert not confirm_cleanup(session, first.id)  # no cleanup proof
        assert admit_waiting(session, host, budget) == []
        repo = JobRepository(session)
        repo.upsert_runtime(first.id, "target", f"astinuslab-{first.id}", state="destroyed")
        assert confirm_cleanup(session, first.id, proof=CleanupProof(first.id, True, True))
        admitted = admit_waiting(session, host, budget)
        assert [job_id for job_id, _ in admitted] == [third.id]
        assert third.status == "queued"
        assert third.capacity_reason is None


def test_pair_atomic_fifo_and_impossible_request(in_memory_session_scope, budget, host):
    from astinuslab.jobs.capacity import admit_waiting, confirm_cleanup

    with in_memory_session_scope() as session:
        first = job_intent(session)
        pair = job_intent(session, attacker=True)
        later = job_intent(session)
        assert submit(session, first, budget, host)
        assert not submit(session, pair, budget, host, count=2, memory=4096, disk=20)
        assert not submit(session, later, budget, host)
        assert "earlier" in later.capacity_reason
        JobRepository(session).upsert_runtime(first.id, "target", f"astinuslab-{first.id}", state="destroyed")
        assert confirm_cleanup(session, first.id, proof=CleanupProof(first.id, True, True))
        assert [j for j, _ in admit_waiting(session, host, budget)] == [pair.id]
        assert not confirm_cleanup(session, pair.id)
        too_big = job_intent(session)
        with pytest.raises(ValueError, match="requires 3 VM"):
            submit(session, too_big, budget, host, count=3)


@pytest.mark.parametrize("limited,reason", [("memory", "RAM"), ("storage", "storage")])
def test_temporary_resources_wait_total_impossible_rejected(in_memory_session_scope, budget, host, limited, reason):
    from astinuslab.jobs.capacity import admit_waiting

    scarce = (
        replace(host, memory_available_mb=2000)
        if limited == "memory"
        else replace(host, storage_free_bytes=12 * 1024**3)
    )
    with in_memory_session_scope() as session:
        job = job_intent(session)
        assert not submit(session, job, budget, scarce)
        assert reason in job.capacity_reason
        assert [j for j, _ in admit_waiting(session, host, budget)] == [job.id]
        impossible = job_intent(session)
        with pytest.raises(ValueError, match=reason):
            submit(
                session,
                impossible,
                budget,
                host,
                memory=20000 if limited == "memory" else 2048,
                disk=600 if limited == "storage" else 10,
            )


def test_unknown_actual_domain_and_failed_runtime_remain_counted(in_memory_session_scope, budget, host):
    with in_memory_session_scope() as session:
        orphan = job_intent(session)
        repo = JobRepository(session)
        repo.update_job(orphan.id, status="failed")
        repo.upsert_runtime(orphan.id, "target", f"astinuslab-{orphan.id}", state="teardown_failed")
        new = job_intent(session)
        actual = replace(host, domains={"untracked-host-domain": 1024})
        assert not submit(session, new, budget, actual)
        assert "VM" in new.capacity_reason


def test_actual_reserved_domain_ram_growth_blocks_overcommit(in_memory_session_scope, budget, host):
    from astinuslab.jobs.capacity import Requirements, shortage

    budget.capacity_ram_headroom_mb = 2048
    with in_memory_session_scope() as session:
        existing = job_intent(session)
        assert submit(session, existing, budget, host, memory=2048)
        actual = replace(
            host,
            memory_total_mb=8192,
            memory_available_mb=7168,
            domains={f"astinuslab-{existing.id}": 4096},
        )
        reason = shortage(session, Requirements(1, 4096, 10 * 1024**3), actual, budget)
        assert reason is not None and "RAM" in reason


def test_live_pair_handoff_preserves_both_vms_and_network():
    from astinuslab.jobs.job_state_support import JobCleanupManager

    calls: list[str] = []
    with JobCleanupManager(
        "fixture",
        logger=SimpleNamespace(warning=lambda *a: None),
        remove_network_isolation=lambda *a, **kw: calls.append("network"),
        teardown_attacker_vm=lambda *a: calls.append("attacker"),
        teardown_vm=lambda *a: calls.append("target"),
        action_by_job={},
    ) as cleanup:
        cleanup.attacker_booted = True
        cleanup.isolated_guest_ip = "192.0.2.1"
        cleanup.teardown = False
    assert calls == []


def test_destroyed_db_row_without_host_absence_proof_cannot_release(in_memory_session_scope, budget, host):
    from astinuslab.jobs.capacity import confirm_cleanup

    with in_memory_session_scope() as session:
        job = job_intent(session)
        assert submit(session, job, budget, host)
        JobRepository(session).upsert_runtime(job.id, "target", f"astinuslab-{job.id}", state="destroyed")
        assert not confirm_cleanup(session, job.id)


def test_service_returns_waiting_without_sending_boot_task(in_memory_session_scope, budget, host, monkeypatch):
    from astinuslab.jobs import capacity_host, job_services

    monkeypatch.setattr(capacity_host, "read_host_capacity", lambda *a, **kw: host)
    config = job_services.settings.model_copy(update=vars(budget))
    monkeypatch.setattr(job_services, "settings", config)
    calls: list[tuple[str, int]] = []
    monkeypatch.setitem(job_services.TASK_MAP, "run_manual_trigger_job", _recording_task(calls, "fixture-task"))
    with in_memory_session_scope() as session:
        jobs = [
            job_services.create_job_service(
                session,
                JobCreate(target_id="ubuntu-22-04-default", scenario_id="lab-session", execution_mode="manual_trigger"),
            )
            for _ in range(3)
        ]
        assert [j.status for j in jobs] == ["queued", "queued", "waiting_for_capacity"]
        assert len(calls) == 2
        assert jobs[-1].capacity_reason


def test_pending_capacity_cancel_finishes_without_teardown(in_memory_session_scope, budget, host, monkeypatch):
    from astinuslab import db
    from astinuslab.db.models import CapacityReservation
    from astinuslab.jobs import job_control

    monkeypatch.setattr(db, "session_scope", in_memory_session_scope)

    def forbidden(*args):
        pytest.fail("a waiting capacity job must not require a VM teardown task")

    monkeypatch.setattr(job_control._teardown_job_task, "delay", forbidden)
    with in_memory_session_scope() as session:
        first, queued = job_intent(session, attacker=True), job_intent(session)
        assert submit(session, first, budget, host, count=2)
        assert not submit(session, queued, budget, host)
        job_id = queued.id
    assert job_control.request_job_cancel(job_id)
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        assert _required(repo.get_job(job_id)).status == "cancelled"
        assert session.get(CapacityReservation, job_id).state == "released"
        assert {a.status for a in repo.list_actions(job_id)} == {"cancelled"}


def test_playground_repository_refuses_jobs_even_if_outer_guard_races(in_memory_session_scope, budget, host):
    from astinuslab.api.errors import ApiError as HTTPException
    from astinuslab.playground.repositories import create_playground_session

    with in_memory_session_scope() as session:
        job = job_intent(session)
        submit(session, job, budget, host)
        with pytest.raises(HTTPException) as error:
            create_playground_session(session, job_id="pg-fixture")
        assert error.value.status_code == 409


def test_repeated_scheduler_ticks_do_not_flood_successfully_accepted_actions(
    in_memory_session_scope, budget, host, monkeypatch
):
    from astinuslab.jobs import capacity_runtime, job_services

    calls: list[tuple[str, int]] = []
    monkeypatch.setitem(job_services.TASK_MAP, "run_manual_trigger_job", _recording_task(calls, "accepted"))
    with in_memory_session_scope() as session:
        job = job_intent(session)
        submit(session, job, budget, host)
    for _ in range(4):
        capacity_runtime.schedule_capacity(
            in_memory_session_scope,
            budget,
            host=host,
            cleanup_probe=lambda *a: pytest.fail("unstarted reservation must not be swept"),
        )
    assert len(calls) == 1


def test_restart_preserves_manual_capture_jobs(in_memory_session_scope):
    import logging
    from datetime import UTC, datetime

    from astinuslab.jobs.startup_recovery import reconcile_orphaned_jobs

    with in_memory_session_scope() as session:
        job = job_intent(session)
        JobRepository(session).update_job(job.id, status="waiting_for_capture", started_at=datetime.now(UTC))
        job_id = job.id

    def forbidden(*args):
        pytest.fail("normal restart must not tear down a live manual-capture job")

    assert (
        reconcile_orphaned_jobs(
            in_memory_session_scope,
            JobRepository,
            forbidden,
            forbidden,
            logging.getLogger("fixture"),
            SimpleNamespace(),
        )
        == 0
    )
    with in_memory_session_scope() as session:
        assert _required(JobRepository(session).get_job(job_id)).status == "waiting_for_capture"


@pytest.fixture
def file_scope(tmp_path):
    from contextlib import contextmanager

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from astinuslab.db import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'capacity.sqlite'}", connect_args={"timeout": 20})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, autoflush=False)

    @contextmanager
    def scope():
        with factory() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    yield scope
    engine.dispose()


def test_simultaneous_submissions_share_atomic_pair_budget(file_scope, host, budget, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import select

    from astinuslab.db.models import CapacityReservation
    from astinuslab.jobs import capacity_host, job_services

    monkeypatch.setattr(job_services, "settings", job_services.settings.model_copy(update=vars(budget)))
    monkeypatch.setattr(capacity_host, "read_host_capacity", lambda *a, **kw: host)
    barrier = Barrier(8)

    def submit_one(index):
        barrier.wait(timeout=10)
        with file_scope() as session:
            job = job_services.create_job_service(
                session,
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="lab-session",
                    execution_mode="manual_trigger",
                    attacker_vm_id="kali" if index % 2 else None,
                ),
            )
            return job.id

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert len(set(pool.map(submit_one, range(8)))) == 8
    with file_scope() as session:
        rows = list(session.scalars(select(CapacityReservation).where(CapacityReservation.state == "reserved")))
        assert 1 <= sum(row.vm_count for row in rows) <= 2
        jobs = JobRepository(session).list_jobs()
        assert all(j.status in {"queued", "waiting_for_capacity"} for j in jobs)
        assert sum(j.status == "waiting_for_capacity" for j in jobs) >= 6


def test_preboot_resource_loss_defers_already_dispatched_launch(in_memory_session_scope, host, budget, monkeypatch):
    from astinuslab import db
    from astinuslab.core import config
    from astinuslab.db.models import CapacityReservation
    from astinuslab.jobs import capacity_host, celery_app
    from astinuslab.live_session import runner

    monkeypatch.setattr(db, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(config, "settings", config.settings.model_copy(update=vars(budget)))
    monkeypatch.setattr(capacity_host, "read_host_capacity", lambda *a, **kw: replace(host, memory_available_mb=1))
    monkeypatch.setattr(runner, "run_live_session_job", lambda *a, **kw: pytest.fail("boot before capacity proof"))
    with in_memory_session_scope() as session:
        first, second = job_intent(session), job_intent(session)
        assert submit(session, first, budget, host)
        assert submit(session, second, budget, host)
        job_id, second_id = first.id, second.id
        action_id = JobRepository(session).list_actions(job_id)[0].id
    celery_app.run_live_session_job_task(job_id, action_id)
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        assert _required(repo.get_job(job_id)).status == "waiting_for_capacity"
        assert "RAM" in (_required(repo.get_job(job_id)).capacity_reason or "")
        assert _required(repo.get_job(second_id)).status == "waiting_for_capacity"
        assert _required(repo.get_action(action_id)).status == "waiting_for_capacity"
        assert not session.get(CapacityReservation, job_id).boot_started


def test_restart_adopts_legacy_live_pair_and_retains_failed_cleanup(in_memory_session_scope, host, budget):
    from datetime import UTC, datetime

    from astinuslab.db.models import CapacityReservation
    from astinuslab.jobs.capacity_runtime import adopt_legacy_reservations, reconcile_capacity

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        live = job_intent(session, attacker=True)
        repo.update_job(live.id, status="waiting_for_capture", started_at=datetime.now(UTC))
        repo.upsert_runtime(live.id, "target", f"astinuslab-{live.id}", state="running")
        repo.upsert_runtime(live.id, "attacker", f"astinuslab-{live.id}-attacker", state="running")
        assert adopt_legacy_reservations(session, budget, targets={"fixture": {}}, workloads={}) == 1
        row = session.get(CapacityReservation, live.id)
        assert row.vm_count == 2 and row.boot_started and row.state == "reserved"
        assert adopt_legacy_reservations(session, budget, targets={}, workloads={}) == 0
        waiting = job_intent(session)
        assert not submit(session, waiting, budget, host)
        repo.update_job(live.id, status="failed")
        assert (
            reconcile_capacity(session, budget, cleanup_probe=lambda *a: CleanupProof(live.id, True, False), host=host)
            == 0
        )
        assert row.state == "reserved"
        assert (
            reconcile_capacity(session, budget, cleanup_probe=lambda *a: CleanupProof(live.id, True, True), host=host)
            == 1
        )
        assert row.state == "released"


def test_scheduler_admits_waiting_job_only_after_host_cleanup_proof(in_memory_session_scope, budget, host, monkeypatch):
    from astinuslab.db.models import CapacityReservation
    from astinuslab.jobs import capacity_runtime, job_services

    with in_memory_session_scope() as session:
        live, waiting = job_intent(session, attacker=True), job_intent(session)
        submit(session, live, budget, host, count=2)
        submit(session, waiting, budget, host)
        session.get(CapacityReservation, live.id).boot_started = True
        JobRepository(session).update_job(live.id, status="completed")
        live_id, waiting_id = live.id, waiting.id
    calls: list[tuple[str, int]] = []
    monkeypatch.setitem(job_services.TASK_MAP, "run_manual_trigger_job", _recording_task(calls, "auto-admitted"))
    result = capacity_runtime.schedule_capacity(
        in_memory_session_scope, budget, host=host, cleanup_probe=lambda *a: CleanupProof(live_id, True, True)
    )
    assert result["released"] == 1 and result["admitted"] == 1
    assert [j for j, _ in calls] == [waiting_id]


def test_failed_cleanup_retries_once_then_auto_admits(in_memory_session_scope, budget, host, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from astinuslab import db
    from astinuslab.db.models import CapacityReservation
    from astinuslab.jobs import capacity_host, capacity_runtime, celery_app, job_services
    from astinuslab.orchestration import lab_lifecycle

    monkeypatch.setattr(db, "session_scope", in_memory_session_scope)
    with in_memory_session_scope() as session:
        live, waiting = job_intent(session, attacker=True), job_intent(session)
        submit(session, live, budget, host, count=2)
        submit(session, waiting, budget, host)
        session.get(CapacityReservation, live.id).boot_started = True
        repo = JobRepository(session)
        repo.update_job(live.id, status="cancel_requested")
        for action in repo.list_actions(live.id):
            repo.update_action(action.id, status="cancelled")
        live_id, waiting_id = live.id, waiting.id
    gone = False

    def proof(*args):
        return CleanupProof(live_id, gone, gone)

    monkeypatch.setattr(capacity_host, "probe_cleanup", proof)
    sends: list[tuple[str, int]] = []
    for kind in ("cleanup_job_resources", "run_manual_trigger_job"):
        monkeypatch.setitem(job_services.TASK_MAP, kind, _recording_task(sends, "fixture-dispatch"))
    capacity_runtime.schedule_capacity(in_memory_session_scope, budget, host=host, cleanup_probe=proof)
    capacity_runtime.schedule_capacity(in_memory_session_scope, budget, host=host, cleanup_probe=proof)
    assert len(sends) == 1
    cleanup_id = sends[0][1]
    monkeypatch.setattr(lab_lifecycle, "teardown_vm", lambda *a: False)
    celery_app.cleanup_job_resources_task(live_id, cleanup_id)
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        assert _required(repo.get_action(cleanup_id)).status == "failed"
        assert session.get(CapacityReservation, live_id).state == "reserved"
        repo.update_action(cleanup_id, finished_at=datetime.now(UTC) - timedelta(minutes=2))
    capacity_runtime.schedule_capacity(in_memory_session_scope, budget, host=host, cleanup_probe=proof)
    assert len(sends) == 2

    def cleaned(*args):
        nonlocal gone
        gone = True
        return True

    monkeypatch.setattr(lab_lifecycle, "teardown_vm", cleaned)
    celery_app.cleanup_job_resources_task(live_id, sends[-1][1])
    capacity_runtime.schedule_capacity(in_memory_session_scope, budget, host=host, cleanup_probe=proof)
    assert sends[-1][0] == waiting_id
    with in_memory_session_scope() as session:
        assert session.get(CapacityReservation, live_id).state == "released"
        assert _required(JobRepository(session).get_job(waiting_id)).status == "queued"


def test_implicit_attacker_and_capture_staging_are_reserved_together(budget):
    from astinuslab.jobs.capacity import GIB
    from astinuslab.jobs.capacity_host import freeze_requirements

    payload = JobCreate(
        target_id="fixture",
        scenario_id="fixture",
        execution_mode="manual_trigger",
        capture_type="both",
        attacker_vm_memory_mb=1024,
        attacker_vm_disk_gb=7,
    )
    frozen, request = freeze_requirements(
        payload, {"memory_mb": 2048, "disk_gb": 10}, {"attacker_vm": {"image_ref": "fixture"}}, budget
    )
    assert request.vm_count == 2
    assert request.memory_mb == 3584
    assert request.storage_bytes == 41 * GIB
    assert frozen["vm_memory_mb"] == 2048 and frozen["vm_disk_gb"] == 10


def test_migration_adopts_existing_rows_without_dropping_job_history(tmp_path):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text

    engine = create_engine(f"sqlite:///{tmp_path / 'migration.sqlite'}")
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[3] / "src/astinuslab/migrations"))
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "a024_job_network_name_excludes_destroyed")
        assert "capacity_reservations" not in inspect(connection).get_table_names()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO jobs(id,target_id,scenario_id,execution_mode,status,capture_type,created_at) VALUES ('legacy','fixture','fixture','manual_trigger','waiting_for_capture','memory',CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO host_actions(job_id,action_type,status,created_at) VALUES ('legacy','run_vol3_plugin','running',CURRENT_TIMESTAMP)"
            )
        )
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
        assert connection.scalar(text("SELECT status FROM jobs WHERE id='legacy'")) == "waiting_for_capture"
        assert connection.execute(text("SELECT owner_token,dispatch_attempts FROM host_actions")).one() == (None, 0)
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
        assert {"state", "boot_started", "preserve_failed_vm", "storage_bytes"} <= {
            c["name"] for c in inspect(connection).get_columns("capacity_reservations")
        }
    engine.dispose()


def test_target_failure_joins_attacker_before_cleanup(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from astinuslab.jobs.job_state_support import JobCleanupManager
    from astinuslab.live_session import runner

    events = []

    class AttackerThread:
        def join(self):
            events.append("attacker_finished")

    deps = MagicMock()
    deps.load_job_params.return_value = SimpleNamespace(
        target_id="fixture",
        scenario_id="fixture",
        image_path=None,
        vm_vcpus=1,
        vm_memory_mb=2048,
        vm_disk_gb=10,
        attacker_vm_vcpus=1,
        attacker_vm_memory_mb=1024,
        attacker_vm_disk_gb=10,
    )
    deps.resolve_target_execution_context.return_value = ({"image_ref": "fixture"}, {}, "fixture", False)
    deps.settings.images_dir = tmp_path
    deps.load_workload_id.return_value = None
    deps.load_attacker_vm_id.return_value = "fixture"
    deps.boot_and_prepare.side_effect = RuntimeError("fixture target boot failed")
    monkeypatch.setattr(runner, "attacker_by_id", lambda *a: {"image_ref": "fixture"})
    monkeypatch.setattr(runner, "_start_attacker_boot_impl", lambda *a: (AttackerThread(), {"ip": None, "error": None}))
    deps.job_cleanup_manager.side_effect = lambda job: JobCleanupManager(
        job,
        logger=MagicMock(),
        remove_network_isolation=MagicMock(),
        teardown_vm=lambda *a: events.append("target_cleanup"),
        teardown_attacker_vm=lambda *a: events.append("attacker_cleanup"),
        action_by_job={},
    )
    runner.run_live_session_job("fixture", deps=deps)
    assert events == ["attacker_finished", "attacker_cleanup", "target_cleanup"]


def _required[T](value: T | None) -> T:
    assert value is not None
    return value


def _recording_task(calls, identifier):
    def delay(*args):
        calls.append(args)
        return SimpleNamespace(id=identifier)

    return SimpleNamespace(delay=delay)


def test_readmission_dispatches_after_consumed_delivery_deferred(in_memory_session_scope, host, budget, monkeypatch):
    from astinuslab.jobs import capacity_runtime, job_services

    calls: list[tuple[str, int]] = []
    monkeypatch.setitem(job_services.TASK_MAP, "run_manual_trigger_job", _recording_task(calls, "first-delivery"))
    with in_memory_session_scope() as session:
        job = job_intent(session)
        submit(session, job, budget, host)
        repo = JobRepository(session)
        action = repo.list_actions(job.id)[0]
        job_id, action_id = job.id, action.id
        job_services._dispatch_action_after_commit(
            session, repo, job_id=job_id, action_id=action_id, action_type=action.action_type
        )
    with in_memory_session_scope() as session:
        assert not capacity_runtime.prepare_launch(
            session, job_id, action_id, budget, replace(host, memory_available_mb=1)
        )
    result = capacity_runtime.schedule_capacity(
        in_memory_session_scope, budget, host=host, cleanup_probe=lambda *a: CleanupProof(job_id, False, False)
    )
    assert result["admitted"] == result["dispatched"] == 1
    assert len(calls) == 2


def test_completed_capture_cleanup_retried_without_rewriting_capture(
    in_memory_session_scope, host, budget, monkeypatch
):
    from astinuslab import db
    from astinuslab.db.models import CapacityReservation
    from astinuslab.jobs import capacity_host, capacity_runtime, celery_app, job_services
    from astinuslab.orchestration import lab_lifecycle

    monkeypatch.setattr(db, "session_scope", in_memory_session_scope)
    sends: list[tuple[str, int]] = []
    monkeypatch.setitem(job_services.TASK_MAP, "cleanup_job_resources", _recording_task(sends, "cleanup-delivery"))
    with in_memory_session_scope() as session:
        job = job_intent(session)
        submit(session, job, budget, host)
        session.get(CapacityReservation, job.id).boot_started = True
        repo = JobRepository(session)
        repo.update_job(job.id, status="completed")
        repo.upsert_runtime(job.id, "target", f"astinuslab-{job.id}", state="teardown_failed")
        job_id = job.id
    capacity_runtime.schedule_capacity(
        in_memory_session_scope, budget, host=host, cleanup_probe=lambda *a: CleanupProof(job_id, False, False)
    )
    assert len(sends) == 1
    cleaned: list[str] = []
    monkeypatch.setattr(lab_lifecycle, "teardown_vm", lambda identifier: cleaned.append(identifier))
    monkeypatch.setattr(capacity_host, "probe_cleanup", lambda *a: CleanupProof(job_id, True, True))
    celery_app.cleanup_job_resources_task(job_id, sends[0][1])
    assert cleaned == [job_id]
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        assert _required(repo.get_job(job_id)).status == "completed"
        assert _required(repo.get_action(sends[0][1])).status == "completed"


def test_powered_off_untracked_domain_does_not_hold_host_ram(in_memory_session_scope, budget, host):
    """A foreign shut-off definition holds disk, not RAM, so it cannot deadlock admission."""
    from astinuslab.jobs.capacity import Requirements, shortage

    budget.max_concurrent_vms = None
    with in_memory_session_scope() as session:
        request = Requirements(1, 2304, 10 * 1024**3)
        running = replace(host, domains={"unrelated-host-vm": 14336})
        reason = shortage(session, request, running, budget)
        assert reason is not None and "RAM" in reason
        powered_off = replace(running, inactive_domains=frozenset({"unrelated-host-vm"}))
        assert shortage(session, request, powered_off, budget) is None


def test_powered_off_domain_with_runtime_row_stays_charged(in_memory_session_scope, budget, host):
    """A shut-off domain an AstinusLab record still claims keeps its full RAM commitment."""
    from astinuslab.jobs.capacity import Requirements, shortage

    budget.max_concurrent_vms = None
    with in_memory_session_scope() as session:
        orphan = job_intent(session)
        repo = JobRepository(session)
        repo.update_job(orphan.id, status="failed")
        repo.upsert_runtime(orphan.id, "target", f"astinuslab-{orphan.id}", state="teardown_failed")
        name = f"astinuslab-{orphan.id}"
        powered_off = replace(host, domains={name: 14336}, inactive_domains=frozenset({name}))
        reason = shortage(session, Requirements(1, 2304, 10 * 1024**3), powered_off, budget)
        assert reason is not None and "RAM" in reason


def test_read_host_capacity_separates_running_from_powered_off_domains(monkeypatch, tmp_path):
    from astinuslab.jobs import capacity_host

    class FakeDomain:
        def __init__(self, name, memory_kib, active):
            self.domain_name, self.memory_kib, self.active = name, memory_kib, active

        def name(self):
            return self.domain_name

        def maxMemory(self):
            return self.memory_kib

        def isActive(self):
            return 1 if self.active else 0

    conn = SimpleNamespace(
        listAllDomains=lambda: [FakeDomain("live", 2048 * 1024, True), FakeDomain("stopped", 8192 * 1024, False)],
        close=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "libvirt", SimpleNamespace(openReadOnly=lambda uri: conn))
    monkeypatch.setattr(capacity_host.fake_mode, "FAKE_LIBVIRT", False)
    actual = capacity_host.read_host_capacity(
        SimpleNamespace(libvirt_uri="test:///default", storage_dir=str(tmp_path), libvirt_image_base_dir=str(tmp_path))
    )
    assert actual.error is None
    assert actual.domains == {"live": 2048, "stopped": 8192}
    assert actual.inactive_domains == frozenset({"stopped"})
