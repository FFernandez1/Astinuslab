"""Terminal cleanup retains firewall ownership until helper success; no host I/O."""

import logging
import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from astinuslab.api.schemas import JobCreate
from astinuslab.db.models import CapacityReservation
from astinuslab.db.repositories import JobRepository
from astinuslab.jobs.capacity_host import CleanupProof
from astinuslab.workloads import workload_support


def _isolated_job(scope, *, status="cancel_requested"):
    with scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(JobCreate(target_id="fixture", scenario_id="manual", execution_mode="manual_trigger"))
        repo.update_job(job.id, status=status, network_isolated=True, guest_ip="192.0.2.10", attacker_ip="192.0.2.11")
        repo.upsert_runtime(job.id, "target", f"astinuslab-{job.id}", state="destroyed")
        session.add(
            CapacityReservation(
                job_id=job.id,
                vm_count=1,
                memory_mb=2048,
                storage_bytes=1024**3,
                preserve_failed_vm=False,
                state="reserved",
                boot_started=True,
            )
        )
        return job.id


@pytest.mark.parametrize("failure", [subprocess.CalledProcessError(1, "helper"), FileNotFoundError("helper")])
def test_failed_rule_removal_retains_ownership_and_does_not_follow_cancel(in_memory_session_scope, failure):
    job_id = _isolated_job(in_memory_session_scope)
    command = MagicMock(side_effect=failure)
    result = workload_support.remove_network_isolation(
        in_memory_session_scope,
        JobRepository,
        command,
        logging.getLogger(__name__),
        job_id,
        "192.0.2.10",
        "192.0.2.11",
    )
    assert result is False
    assert "job_id" not in command.call_args.kwargs  # cancellation must not terminate cleanup
    with in_memory_session_scope() as session:
        job = JobRepository(session).get_job(job_id)
        assert job is not None
        assert job.network_isolated is True


def test_partial_isolation_apply_records_cleanup_intent_before_host_call(in_memory_session_scope):
    job_id = _isolated_job(in_memory_session_scope, status="provisioning_workload")
    with in_memory_session_scope() as session:
        JobRepository(session).update_job(job_id, network_isolated=False)

    def partial_apply(command, **kwargs):
        with in_memory_session_scope() as session:
            job = JobRepository(session).get_job(job_id)
            assert job is not None
            assert job.network_isolated is True
        raise subprocess.CalledProcessError(1, command)

    assert (
        workload_support.apply_network_isolation(
            in_memory_session_scope,
            JobRepository,
            partial_apply,
            logging.getLogger(__name__),
            job_id,
            "192.0.2.10",
            "192.0.2.11",
        )
        is False
    )


@pytest.mark.parametrize("final_status", ["cancelled", "failed"])
def test_terminal_task_removes_recorded_rules_and_keeps_vm_cleanup_nonfatal(
    in_memory_session_scope,
    monkeypatch,
    final_status,
):
    from astinuslab import db
    from astinuslab.core import fake_mode
    from astinuslab.jobs import celery_app
    from astinuslab.orchestration import lab_lifecycle

    job_id = _isolated_job(
        in_memory_session_scope, status="cancel_requested" if final_status == "cancelled" else "failed"
    )
    monkeypatch.setattr(db, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(fake_mode, "FAKE_ORCHESTRATION", False)
    monkeypatch.setattr(lab_lifecycle, "session_scope", in_memory_session_scope)
    command = MagicMock(side_effect=subprocess.CalledProcessError(1, "helper"))
    teardown = MagicMock(return_value=True)
    monkeypatch.setattr(lab_lifecycle, "_run_command", command)
    monkeypatch.setattr(lab_lifecycle, "_teardown_vm_impl", teardown)

    celery_app.teardown_job_task.run(job_id, final_status)
    assert teardown.call_count == 2
    assert [call.args[4] for call in teardown.call_args_list] == [job_id, f"{job_id}-attacker"]
    assert command.call_count >= 1
    assert command.call_args.args[0][3:] == ["remove", job_id, "192.0.2.10", "192.0.2.11"]
    with in_memory_session_scope() as session:
        job = JobRepository(session).get_job(job_id)
        assert job is not None
        assert job.status == final_status and job.network_isolated is True

    command.side_effect = None
    command.return_value = ""
    celery_app.teardown_job_task.run(job_id, final_status)
    with in_memory_session_scope() as session:
        job = JobRepository(session).get_job(job_id)
        assert job is not None
        assert job.network_isolated is False


@pytest.mark.parametrize("final_status", ["failed", "completed"])
@pytest.mark.parametrize("previously_released", [False, True])
def test_firewall_cleanup_failure_keeps_capacity_and_failed_action_until_retry(
    in_memory_session_scope, monkeypatch, final_status, previously_released
):
    from astinuslab import db
    from astinuslab.core import fake_mode
    from astinuslab.jobs import capacity_host, capacity_runtime, celery_app
    from astinuslab.jobs.capacity import confirm_cleanup
    from astinuslab.orchestration import lab_lifecycle

    job_id = _isolated_job(in_memory_session_scope, status=final_status)
    monkeypatch.setattr(db, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(fake_mode, "FAKE_ORCHESTRATION", False)
    monkeypatch.setattr(lab_lifecycle, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(lab_lifecycle, "_teardown_vm_impl", lambda *a: True)
    command = MagicMock(side_effect=subprocess.CalledProcessError(1, "helper"))
    monkeypatch.setattr(lab_lifecycle, "_run_command", command)
    proof = CleanupProof(job_id, True, True)
    monkeypatch.setattr(capacity_host, "probe_cleanup", lambda *a: proof)
    with in_memory_session_scope() as session:
        if previously_released:
            row = session.get(CapacityReservation, job_id)
            assert row is not None
            row.state = "released"
            row.released_at = datetime.now(UTC)
            row.preserve_failed_vm = True
            session.flush()
            assert capacity_runtime.adopt_legacy_reservations(session, SimpleNamespace(), targets={}, workloads={}) == 1
            assert row.state == "reserved" and row.released_at is None and row.boot_started is True
            # Released custody already certified the old VM was gone; its old
            # failure-preservation policy must not disable firewall recovery.
            assert row.preserve_failed_vm is False
        assert not confirm_cleanup(session, job_id, proof=proof)
        capacity_runtime.queue_cleanup_intents(session, SimpleNamespace())
        action_id = JobRepository(session).list_actions(job_id)[-1].id
    celery_app.cleanup_job_resources_task(job_id, action_id)
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        action = repo.get_action(action_id)
        job = repo.get_job(job_id)
        row = session.get(CapacityReservation, job_id)
        assert action is not None and job is not None and row is not None
        assert action.status == "failed"
        assert job.status == final_status
        assert row.state == "reserved"
        action_id = repo.enqueue_action(job_id, "cleanup_job_resources", {"final_status": final_status}).id
    command.side_effect = None
    command.return_value = ""
    celery_app.cleanup_job_resources_task(job_id, action_id)
    with in_memory_session_scope() as session:
        action = JobRepository(session).get_action(action_id)
        assert action is not None
        assert action.status == "completed"
        assert confirm_cleanup(session, job_id, proof=proof)


@pytest.mark.parametrize(("status", "isolated"), [("completed", False), ("waiting_for_capture", True)])
def test_released_reservation_adoption_leaves_clean_terminal_and_active_jobs_unchanged(
    in_memory_session_scope, status, isolated
):
    from astinuslab.jobs.capacity_runtime import adopt_legacy_reservations

    job_id = _isolated_job(in_memory_session_scope, status=status)
    with in_memory_session_scope() as session:
        row = session.get(CapacityReservation, job_id)
        assert row is not None
        row.state = "released"
        row.released_at = datetime.now(UTC)
        released_at = row.released_at
        row.preserve_failed_vm = True
        JobRepository(session).update_job(job_id, network_isolated=isolated)
        assert adopt_legacy_reservations(session, SimpleNamespace(), targets={}, workloads={}) == 0
        assert row.state == "released" and row.released_at == released_at
        assert row.preserve_failed_vm is True


@pytest.mark.parametrize(("firewall_ok", "fake_run"), [(True, False), (False, False), (False, True)])
def test_startup_teardown_has_bound_commands_and_recovers_recorded_isolation(
    in_memory_session_scope, monkeypatch, firewall_ok, fake_run
):
    from astinuslab import db
    from astinuslab.core import logging_config
    from astinuslab.jobs import capacity_runtime, runtime_support, startup_recovery
    from astinuslab.jobs import celery_worker_signals as signals
    from astinuslab.lab_packages import runner_preflight
    from astinuslab.orchestration.fake_runtime import PrivilegedCommandRefused
    from astinuslab.virtualization import host_script_manifest, process_support, vm_support

    job_id = _isolated_job(in_memory_session_scope, status="preparing_image")
    monkeypatch.setattr(signals.fake_mode, "FAKE_ORCHESTRATION", fake_run)
    if fake_run:
        monkeypatch.setattr(signals.fake_mode, "FAKE_LIBVIRT", False)
    monkeypatch.setattr(runner_preflight, "initialize_runner_storage", lambda: None)
    monkeypatch.setattr(db, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(logging_config, "configure_logging", lambda *a, **kw: None)
    monkeypatch.setattr(host_script_manifest, "preflight_problems", lambda **kw: [])
    monkeypatch.setattr(capacity_runtime, "adopt_legacy_reservations", lambda *a: 0)
    monkeypatch.setattr(runtime_support, "mark_runtime_destroyed", lambda *a: None)
    monkeypatch.setattr(runtime_support, "mark_runtime_teardown_failed", lambda *a: None)
    commands = []

    # Enforce the production low-level dependency signature, while prohibiting host I/O.
    def command(logger, session_scope, repo_cls, action_by_job, job_processes, process_lock, cmd, **kwargs):
        commands.append(cmd)
        if "remove" in cmd and not firewall_ok:
            raise subprocess.CalledProcessError(1, cmd)
        return ""

    monkeypatch.setattr(process_support, "run_command", command)

    def teardown(run_command, logger, settings, mark_destroyed, identifier, mark_failed):
        run_command(["cleanup-probe", identifier])
        return True

    teardown_spy = MagicMock(side_effect=teardown)
    monkeypatch.setattr(vm_support, "teardown_vm", teardown_spy)

    class StopAfterRecovery(Exception):
        pass

    def reconcile(*args, **kwargs):
        if fake_run:
            with pytest.raises(PrivilegedCommandRefused):
                kwargs["teardown_fn"](job_id)
        else:
            assert kwargs["teardown_fn"](job_id) is firewall_ok
        raise StopAfterRecovery()

    monkeypatch.setattr(startup_recovery, "reconcile_orphaned_jobs", reconcile)
    with pytest.raises(StopAfterRecovery):
        signals._on_worker_ready(SimpleNamespace(hostname="runner@fixture"))
    if fake_run:
        teardown_spy.assert_not_called()
        assert commands == []
    else:
        assert commands[-1] == ["cleanup-probe", job_id]
        assert any(cmd[3:] == ["remove", job_id, "192.0.2.10", "192.0.2.11"] for cmd in commands)
    with in_memory_session_scope() as session:
        job = JobRepository(session).get_job(job_id)
        assert job is not None
        assert job.network_isolated is not firewall_ok


def test_fake_orchestration_cleanup_refuses_before_any_host_subprocess(monkeypatch):
    from astinuslab.core import fake_mode
    from astinuslab.orchestration import lab_lifecycle
    from astinuslab.orchestration.fake_runtime import PrivilegedCommandRefused

    monkeypatch.setattr(fake_mode, "FAKE_ORCHESTRATION", True)
    process = MagicMock(side_effect=AssertionError("host command crossed fake boundary"))
    monkeypatch.setattr(lab_lifecycle, "_run_command_impl", process)
    with pytest.raises(PrivilegedCommandRefused):
        lab_lifecycle._run_command(["sudo", "-n", "cleanup-helper"])
    process.assert_not_called()


def test_fake_orchestration_teardown_refuses_before_libvirt_and_retains_ownership(in_memory_session_scope, monkeypatch):
    from astinuslab.core import fake_mode
    from astinuslab.orchestration import lab_lifecycle
    from astinuslab.orchestration.fake_runtime import PrivilegedCommandRefused

    job_id = _isolated_job(in_memory_session_scope)
    monkeypatch.setattr(fake_mode, "FAKE_ORCHESTRATION", True)
    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    monkeypatch.setattr(lab_lifecycle, "session_scope", in_memory_session_scope)
    teardown = MagicMock(return_value=True)
    monkeypatch.setattr(lab_lifecycle, "_teardown_vm_impl", teardown)
    with pytest.raises(PrivilegedCommandRefused):
        lab_lifecycle.teardown_vm(job_id)
    teardown.assert_not_called()
    with in_memory_session_scope() as session:
        job = JobRepository(session).get_job(job_id)
        row = session.get(CapacityReservation, job_id)
        assert job is not None and row is not None
        assert job.network_isolated is True
        assert row.state == "reserved"
