"""Runtime controls cross the API/worker boundary as durable intent.

The API process holds no hypervisor privilege: it validates policy, commits a
``control_runtime`` action and hands its id to the single lifecycle worker,
which re-checks the same policy before touching libvirt. These tests pin the
authorized path plus the unauthorized, conflicting, duplicate and
broker-failure outcomes.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from astinuslab.api.errors import ApiError
from astinuslab.api.schemas import JobCreate
from astinuslab.db.repositories import JobRepository
from astinuslab.jobs import job_services, runtime_control


@pytest.fixture
def live_runtime(in_memory_session_scope, monkeypatch, tmp_path):
    """A manual-trigger job parked in waiting_for_capture with a running target."""
    from astinuslab import db
    from astinuslab.core import config
    from astinuslab.forensics import services as forensic_services

    monkeypatch.setattr(db, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(runtime_control, "session_scope", in_memory_session_scope)
    settings = config.settings.model_copy(update={"storage_dir": tmp_path / "storage"})
    monkeypatch.setattr(config, "settings", settings)
    monkeypatch.setattr(job_services, "settings", settings)
    monkeypatch.setattr(forensic_services, "settings", settings)
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(
            JobCreate(target_id="ubuntu-22-04-default", scenario_id="lab-session", execution_mode="manual_trigger")
        )
        repo.update_job(job.id, status="waiting_for_capture")
        repo.upsert_runtime(job.id, "target", f"astinuslab-{job.id}", state="running", power_state="on")
        repo.upsert_runtime(job.id, "attacker", f"astinuslab-{job.id}-attacker", state="running", power_state="on")
        return job.id


def _control_actions(scope, job_id):
    """Detached snapshots of a Job's control actions, safe to read after commit."""
    with scope() as session:
        return [
            SimpleNamespace(
                id=a.id,
                status=a.status,
                payload=a.payload,
                error_summary=a.error_summary,
                dispatched_at=a.dispatched_at,
                celery_task_id=a.celery_task_id,
            )
            for a in JobRepository(session).list_actions(job_id)
            if a.action_type == "control_runtime"
        ]


def _stub_task(monkeypatch, delay):
    monkeypatch.setitem(job_services.TASK_MAP, "control_runtime", SimpleNamespace(delay=delay))


# --------------------------------------------------------------------------
# Authorized dispatch and worker execution
# --------------------------------------------------------------------------


def test_api_commits_intent_and_never_calls_the_hypervisor(live_runtime, in_memory_session_scope, monkeypatch):
    host_call = MagicMock()
    monkeypatch.setattr(runtime_control, "control_runtime", host_call)
    dispatched: list[tuple[str, int]] = []

    def send(job_id, action_id):
        # The row the worker will read must already be committed and visible.
        with in_memory_session_scope() as worker_view:
            action = JobRepository(worker_view).get_action(action_id)
            assert action is not None
            assert action.action_type == "control_runtime"
            assert action.status == "queued"
            assert action.dispatched_at is not None
            assert action.payload is not None
            assert json.loads(action.payload) == {"role": "target", "action": "shutdown"}
        dispatched.append((job_id, action_id))
        return SimpleNamespace(id="runtime-task")

    _stub_task(monkeypatch, send)

    assert job_services.control_runtime_service(live_runtime, "target", "shutdown") is True

    host_call.assert_not_called()
    assert len(dispatched) == 1
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        action = repo.get_action(dispatched[0][1])
        assert action is not None
        assert action.celery_task_id == "runtime-task"
        assert any(
            e.state == "runtime_control" and "shutdown on target" in e.message for e in repo.list_events(live_runtime)
        )


def test_worker_executes_the_control_against_the_recorded_vm(live_runtime, in_memory_session_scope, monkeypatch):
    from astinuslab.jobs.celery_app import control_runtime_task

    executed: list[tuple] = []

    def record_call(*args, **kwargs):
        executed.append((args, kwargs))
        return True

    monkeypatch.setattr(
        runtime_control,
        "control_runtime",
        record_call,
    )
    _stub_task(monkeypatch, lambda *args: SimpleNamespace(id="runtime-task"))

    assert job_services.control_runtime_service(live_runtime, "target", "shutdown") is True
    action_id = _control_actions(in_memory_session_scope, live_runtime)[0].id
    control_runtime_task(live_runtime, action_id)

    assert executed == [((live_runtime, "target", "shutdown"), {"vm_name": f"astinuslab-{live_runtime}"})]
    with in_memory_session_scope() as session:
        action = JobRepository(session).get_action(action_id)
        assert action is not None
        assert action.status == "completed"


def test_control_runtime_reaches_libvirt_only_from_the_worker_side_command(
    live_runtime, in_memory_session_scope, monkeypatch
):
    """The virsh invocation carries the runtime row's name, not a rebuilt one."""
    import time

    from astinuslab.jobs.runtime_actions import control_runtime as control_runtime_impl

    monkeypatch.setattr(time, "sleep", lambda _: None)
    run_command = MagicMock()
    control_runtime_impl(
        in_memory_session_scope,
        JobRepository,
        SimpleNamespace(libvirt_uri="qemu:///system"),
        run_command,
        MagicMock(return_value={"power_state": "off", "role": "target"}),
        live_runtime,
        "target",
        "shutdown",
        vm_name="astinuslab-recorded-name",
    )
    run_command.assert_called_once_with(
        ["virsh", "-c", "qemu:///system", "shutdown", "astinuslab-recorded-name"], job_id=live_runtime
    )


# --------------------------------------------------------------------------
# Authorization
# --------------------------------------------------------------------------


def test_runtime_control_endpoint_requires_admin_auth(monkeypatch, live_runtime, in_memory_session_scope):
    """Auth is enforced before the service runs; a denied call records nothing."""
    from starlette.testclient import TestClient

    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    token = "runtime-control-admin-token"
    monkeypatch.setattr(
        security_mod,
        "settings",
        SimpleNamespace(
            auth_enabled=True,
            admin_api_token_hash=f"sha256:{hashlib.sha256(token.encode()).hexdigest()}",
        ),
    )
    host_call = MagicMock()
    monkeypatch.setattr(runtime_control, "control_runtime", host_call)

    with TestClient(app) as auth_client:
        denied = auth_client.post(f"/api/v1/jobs/{live_runtime}/runtimes/target/shutdown")

    assert denied.status_code == 401
    host_call.assert_not_called()
    assert _control_actions(in_memory_session_scope, live_runtime) == []


def test_runtime_control_rejects_unknown_job_and_missing_runtime(live_runtime, in_memory_session_scope, monkeypatch):
    host_call = MagicMock()
    monkeypatch.setattr(runtime_control, "control_runtime", host_call)

    with pytest.raises(HTTPException) as missing_job:
        job_services.control_runtime_service("job-does-not-exist", "target", "shutdown")
    assert missing_job.value.status_code == 404

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        other = repo.create_job(
            JobCreate(target_id="ubuntu-22-04-default", scenario_id="lab-session", execution_mode="manual_trigger")
        )
        repo.update_job(other.id, status="waiting_for_capture")
        other_id = other.id

    # This Job has no attacker runtime row, so it owns no attacker VM to control.
    with pytest.raises(ApiError) as no_runtime:
        job_services.control_runtime_service(other_id, "attacker", "shutdown")
    assert no_runtime.value.status_code == 409
    host_call.assert_not_called()
    assert _control_actions(in_memory_session_scope, other_id) == []


@pytest.mark.parametrize("role,action", [("target", "pause"), ("target", "resume"), ("hypervisor", "shutdown")])
def test_runtime_control_rejects_unsupported_role_or_action(
    live_runtime, in_memory_session_scope, monkeypatch, role, action
):
    host_call = MagicMock()
    monkeypatch.setattr(runtime_control, "control_runtime", host_call)

    with pytest.raises(HTTPException) as error:
        job_services.control_runtime_service(live_runtime, role, action)

    assert error.value.status_code == 400
    host_call.assert_not_called()
    assert _control_actions(in_memory_session_scope, live_runtime) == []


# --------------------------------------------------------------------------
# Supported phases
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    ["queued", "preparing_image", "capture_requested", "acquiring_memory", "completed", "failed", "cancelled"],
)
def test_runtime_control_is_refused_outside_the_live_lab_phase(
    live_runtime, in_memory_session_scope, monkeypatch, status
):
    host_call = MagicMock()
    monkeypatch.setattr(runtime_control, "control_runtime", host_call)
    with in_memory_session_scope() as session:
        JobRepository(session).update_job(live_runtime, status=status)

    with pytest.raises(ApiError) as error:
        job_services.control_runtime_service(live_runtime, "target", "shutdown")

    assert error.value.status_code == 409
    host_call.assert_not_called()
    assert _control_actions(in_memory_session_scope, live_runtime) == []


def test_worker_cancels_a_control_whose_job_left_the_live_phase(live_runtime, in_memory_session_scope, monkeypatch):
    """A queued control that is overtaken must not power-cycle a capturing guest."""
    from astinuslab.jobs.celery_app import control_runtime_task

    host_call = MagicMock()
    monkeypatch.setattr(runtime_control, "control_runtime", host_call)
    _stub_task(monkeypatch, lambda *args: SimpleNamespace(id="runtime-task"))

    assert job_services.control_runtime_service(live_runtime, "target", "shutdown") is True
    action_id = _control_actions(in_memory_session_scope, live_runtime)[0].id
    with in_memory_session_scope() as session:
        JobRepository(session).update_job(live_runtime, status="capture_requested")

    control_runtime_task(live_runtime, action_id)

    host_call.assert_not_called()
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        action = repo.get_action(action_id)
        assert action is not None
        assert action.status == "cancelled"
        assert action.error_summary is not None
        assert "capture_requested" in action.error_summary
        job = repo.get_job(live_runtime)
        assert job is not None
        assert job.status == "capture_requested"


# --------------------------------------------------------------------------
# Capture/control conflicts, both orderings
# --------------------------------------------------------------------------


def test_pending_capture_blocks_a_new_runtime_control(live_runtime, in_memory_session_scope, monkeypatch):
    """Capture first: the control request is refused while the capture is pending."""
    host_call = MagicMock()
    monkeypatch.setattr(runtime_control, "control_runtime", host_call)
    with in_memory_session_scope() as session:
        JobRepository(session).enqueue_action(live_runtime, "acquire_memory_dump", {"capture_type": "memory"})

    with pytest.raises(ApiError) as error:
        job_services.control_runtime_service(live_runtime, "target", "shutdown")

    assert error.value.status_code == 409
    assert "capture" in error.value.detail
    host_call.assert_not_called()
    assert _control_actions(in_memory_session_scope, live_runtime) == []


def test_capture_after_a_queued_control_supersedes_it(live_runtime, in_memory_session_scope, monkeypatch):
    """Control first: the capture still wins, and the control settles as cancelled."""
    from astinuslab.forensics.services import enqueue_acquire_memory_dump_service
    from astinuslab.jobs.celery_app import control_runtime_task

    host_call = MagicMock()
    monkeypatch.setattr(runtime_control, "control_runtime", host_call)
    _stub_task(monkeypatch, lambda *args: SimpleNamespace(id="runtime-task"))
    monkeypatch.setitem(
        job_services.TASK_MAP, "acquire_memory_dump", SimpleNamespace(delay=lambda *a: SimpleNamespace(id="capture"))
    )

    assert job_services.control_runtime_service(live_runtime, "target", "shutdown") is True
    action_id = _control_actions(in_memory_session_scope, live_runtime)[0].id

    with in_memory_session_scope() as session:
        captured = enqueue_acquire_memory_dump_service(session, live_runtime, capture_type="memory")
        assert captured.status == "capture_requested"

    control_runtime_task(live_runtime, action_id)

    host_call.assert_not_called()
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        action = repo.get_action(action_id)
        assert action is not None
        assert action.status == "cancelled"
        capture = next(a for a in repo.list_actions(live_runtime) if a.action_type == "acquire_memory_dump")
        assert capture.status == "queued"
        job = repo.get_job(live_runtime)
        assert job is not None
        assert job.status == "capture_requested"


def test_worker_refuses_a_control_while_a_capture_action_is_still_pending(
    live_runtime, in_memory_session_scope, monkeypatch
):
    """Stale job status alone must not let a control run alongside a capture."""
    host_call = MagicMock()
    monkeypatch.setattr(runtime_control, "control_runtime", host_call)
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        control = repo.enqueue_action(live_runtime, "control_runtime", {"role": "target", "action": "shutdown"})
        capture = repo.enqueue_action(live_runtime, "acquire_memory_dump", {"capture_type": "memory"})
        repo.update_action(capture.id, status="running")
        action_id = control.id

    runtime_control.run_control_runtime_action(live_runtime, action_id)

    host_call.assert_not_called()
    with in_memory_session_scope() as session:
        action = JobRepository(session).get_action(action_id)
        assert action is not None
        assert action.status == "cancelled"
        assert action.error_summary is not None
        assert "capture" in action.error_summary


def test_running_acquisition_lock_blocks_the_host_call(live_runtime, in_memory_session_scope, monkeypatch):
    """An acquisition holding the per-VM capture lock excludes the control."""
    from astinuslab.core.config import settings
    from astinuslab.core.locks import capture_lock, capture_lock_dir

    host_call = MagicMock()
    monkeypatch.setattr(runtime_control, "control_runtime", host_call)
    with in_memory_session_scope() as session:
        action_id = (
            JobRepository(session)
            .enqueue_action(live_runtime, "control_runtime", {"role": "target", "action": "shutdown"})
            .id
        )

    with capture_lock(f"astinuslab-{live_runtime}", base_dir=capture_lock_dir(settings, "forensic")):
        runtime_control.run_control_runtime_action(live_runtime, action_id)

    host_call.assert_not_called()
    with in_memory_session_scope() as session:
        action = JobRepository(session).get_action(action_id)
        assert action is not None
        assert action.status == "cancelled"
        assert action.error_summary is not None
        assert "capture is already in progress" in action.error_summary


def test_capture_and_control_requests_share_one_job_intent_lock():
    """The two request paths contend for the same lock file, not lookalikes."""
    from astinuslab.forensics.services import _forensic_enqueue_lock_dir
    from astinuslab.jobs.job_services import runtime_intent_lock_dir

    assert _forensic_enqueue_lock_dir() == runtime_intent_lock_dir()


def test_a_running_control_makes_the_api_refuse_a_capture_request(live_runtime, in_memory_session_scope, monkeypatch):
    """While the worker drives libvirt it holds the Job's intent lock."""
    from astinuslab.core.config import settings
    from astinuslab.core.locks import blocking_file_lock, capture_lock_path
    from astinuslab.forensics.services import enqueue_acquire_memory_dump_service
    from astinuslab.jobs.job_services import runtime_intent_lock_dir

    monkeypatch.setitem(
        job_services.TASK_MAP, "acquire_memory_dump", SimpleNamespace(delay=lambda *a: SimpleNamespace(id="capture"))
    )
    assert settings.storage_dir  # the fixture redirected settings to tmp_path

    with blocking_file_lock(capture_lock_path(live_runtime, base_dir=runtime_intent_lock_dir())):
        with in_memory_session_scope() as session:
            with pytest.raises(ApiError) as error:
                enqueue_acquire_memory_dump_service(session, live_runtime, capture_type="memory")

    assert error.value.status_code == 409
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job = repo.get_job(live_runtime)
        assert job is not None
        assert job.status == "waiting_for_capture"
        assert not [a for a in repo.list_actions(live_runtime) if a.action_type == "acquire_memory_dump"]


# --------------------------------------------------------------------------
# Duplicates
# --------------------------------------------------------------------------


def test_second_request_for_the_same_role_is_refused(live_runtime, in_memory_session_scope, monkeypatch):
    host_call = MagicMock()
    monkeypatch.setattr(runtime_control, "control_runtime", host_call)
    _stub_task(monkeypatch, lambda *args: SimpleNamespace(id="runtime-task"))

    assert job_services.control_runtime_service(live_runtime, "target", "shutdown") is True
    with pytest.raises(ApiError) as error:
        job_services.control_runtime_service(live_runtime, "target", "reboot")

    assert error.value.status_code == 409
    assert len(_control_actions(in_memory_session_scope, live_runtime)) == 1
    host_call.assert_not_called()


def test_a_queued_control_for_one_role_does_not_block_the_other_role(
    live_runtime, in_memory_session_scope, monkeypatch
):
    monkeypatch.setattr(runtime_control, "control_runtime", MagicMock())
    _stub_task(monkeypatch, lambda *args: SimpleNamespace(id="runtime-task"))

    assert job_services.control_runtime_service(live_runtime, "target", "shutdown") is True
    assert job_services.control_runtime_service(live_runtime, "attacker", "reboot") is True

    roles = [json.loads(a.payload)["role"] for a in _control_actions(in_memory_session_scope, live_runtime)]
    assert roles == ["target", "attacker"]


def test_duplicate_delivery_executes_the_control_once(live_runtime, in_memory_session_scope, monkeypatch):
    from astinuslab.jobs.celery_app import control_runtime_task

    host_call = MagicMock(return_value=True)
    monkeypatch.setattr(runtime_control, "control_runtime", host_call)
    with in_memory_session_scope() as session:
        action_id = (
            JobRepository(session)
            .enqueue_action(live_runtime, "control_runtime", {"role": "target", "action": "reboot"})
            .id
        )

    control_runtime_task(live_runtime, action_id)
    control_runtime_task(live_runtime, action_id)

    host_call.assert_called_once_with(live_runtime, "target", "reboot", vm_name=f"astinuslab-{live_runtime}")
    with in_memory_session_scope() as session:
        action = JobRepository(session).get_action(action_id)
        assert action is not None
        assert action.status == "completed"


def test_misrouted_delivery_leaves_the_other_action_untouched(live_runtime, in_memory_session_scope, monkeypatch):
    """The claim is fenced on action type, so an unrelated action is not consumed."""
    from astinuslab.jobs.celery_app import control_runtime_task

    host_call = MagicMock()
    monkeypatch.setattr(runtime_control, "control_runtime", host_call)
    with in_memory_session_scope() as session:
        capture_id = (
            JobRepository(session).enqueue_action(live_runtime, "acquire_memory_dump", {"capture_type": "memory"}).id
        )

    assert control_runtime_task(live_runtime, capture_id) is None

    host_call.assert_not_called()
    with in_memory_session_scope() as session:
        capture = JobRepository(session).get_action(capture_id)
        assert capture is not None
        assert capture.action_type == "acquire_memory_dump"
        assert capture.status == "queued"
        assert capture.started_at is None
        assert capture.owner_token is None


def test_control_action_of_another_job_is_refused(live_runtime, in_memory_session_scope, monkeypatch):
    host_call = MagicMock()
    monkeypatch.setattr(runtime_control, "control_runtime", host_call)
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        other = repo.create_job(
            JobCreate(target_id="ubuntu-22-04-default", scenario_id="lab-session", execution_mode="manual_trigger")
        )
        repo.update_job(other.id, status="waiting_for_capture")
        action_id = repo.enqueue_action(other.id, "control_runtime", {"role": "target", "action": "shutdown"}).id
        other_id = other.id

    with pytest.raises(runtime_control.RuntimeControlIdentityError):
        runtime_control.run_control_runtime_action(live_runtime, action_id)

    host_call.assert_not_called()
    with in_memory_session_scope() as session:
        untouched = JobRepository(session).get_action(action_id)
        assert untouched is not None
        assert untouched.job_id == other_id
        assert untouched.status == "queued"


# --------------------------------------------------------------------------
# Broker failure and recovery
# --------------------------------------------------------------------------


def test_broker_failure_compensates_the_intent_and_preserves_the_job(
    live_runtime, in_memory_session_scope, monkeypatch
):
    monkeypatch.setattr(runtime_control, "control_runtime", MagicMock())

    def refused(*args):
        raise ConnectionError("broker unavailable")

    _stub_task(monkeypatch, refused)

    with pytest.raises(HTTPException) as error:
        job_services.control_runtime_service(live_runtime, "target", "shutdown")

    assert error.value.status_code == 503
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job = repo.get_job(live_runtime)
        assert job is not None
        assert job.status == "waiting_for_capture"
        assert _control_actions(in_memory_session_scope, live_runtime)[0].status == "failed"
        runtime = repo.get_runtime(live_runtime, "target")
        assert runtime is not None
        assert (runtime.power_state, runtime.state) == ("on", "running")


def test_undelivered_control_intent_is_redispatched_after_a_restart(live_runtime, in_memory_session_scope, monkeypatch):
    from astinuslab.db.models import CapacityReservation
    from astinuslab.jobs import capacity_runtime
    from astinuslab.jobs.action_lifecycle import recover_queued_dispatches
    from astinuslab.jobs.capacity import HostCapacity

    host = HostCapacity(
        memory_total_mb=16384,
        memory_available_mb=12000,
        storage_total_bytes=100 * 1024**3,
        storage_free_bytes=80 * 1024**3,
        domains={f"astinuslab-{live_runtime}": 2048},
    )
    config = SimpleNamespace(
        max_concurrent_vms=2,
        capacity_ram_headroom_mb=1024,
        capacity_storage_headroom_gb=5,
        capacity_vm_overhead_mb=256,
    )
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        session.add(
            CapacityReservation(
                job_id=live_runtime,
                vm_count=1,
                memory_mb=2048,
                storage_bytes=20 * 1024**3,
                state="reserved",
                boot_started=True,
            )
        )
        action = repo.enqueue_action(live_runtime, "control_runtime", {"role": "target", "action": "shutdown"})
        # Committed intent whose broker send never landed.
        repo.update_action(action.id, dispatched_at=datetime.now(UTC), dispatch_attempts=1)
        action_id = action.id

    sent: list[tuple] = []

    def record_call(*args):
        sent.append(args)
        return SimpleNamespace(id="recovered-control")

    _stub_task(monkeypatch, record_call)

    assert recover_queued_dispatches(in_memory_session_scope, service_identity="runner") == 1
    result = capacity_runtime.schedule_capacity(
        in_memory_session_scope, config, host=host, cleanup_probe=lambda *_: None
    )

    assert result["dispatched"] == 1
    assert sent == [(live_runtime, action_id)]


def test_undelivered_control_intent_is_not_redispatched_once_the_phase_passed(
    live_runtime, in_memory_session_scope, monkeypatch
):
    from astinuslab.jobs import capacity_runtime
    from astinuslab.jobs.capacity import HostCapacity

    host = HostCapacity(
        memory_total_mb=16384,
        memory_available_mb=12000,
        storage_total_bytes=100 * 1024**3,
        storage_free_bytes=80 * 1024**3,
        domains={},
    )
    config = SimpleNamespace(
        max_concurrent_vms=2,
        capacity_ram_headroom_mb=1024,
        capacity_storage_headroom_gb=5,
        capacity_vm_overhead_mb=256,
    )
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        repo.enqueue_action(live_runtime, "control_runtime", {"role": "target", "action": "shutdown"})
        repo.update_job(live_runtime, status="capture_requested")

    sent: list[tuple] = []

    def record_call(*args):
        sent.append(args)
        return SimpleNamespace(id="unexpected")

    _stub_task(monkeypatch, record_call)

    result = capacity_runtime.schedule_capacity(
        in_memory_session_scope, config, host=host, cleanup_probe=lambda *_: None
    )

    assert result["dispatched"] == 0
    assert sent == []


# --------------------------------------------------------------------------
# Unrelated jobs and reservations
# --------------------------------------------------------------------------


def test_runtime_control_leaves_unrelated_jobs_and_reservations_unchanged(
    live_runtime, in_memory_session_scope, monkeypatch
):
    from astinuslab.db.models import CapacityReservation
    from astinuslab.jobs.celery_app import control_runtime_task

    monkeypatch.setattr(runtime_control, "control_runtime", MagicMock(return_value=True))
    _stub_task(monkeypatch, lambda *args: SimpleNamespace(id="runtime-task"))

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        neighbour = repo.create_job(
            JobCreate(target_id="ubuntu-22-04-default", scenario_id="lab-session", execution_mode="manual_trigger")
        )
        repo.update_job(neighbour.id, status="waiting_for_capture")
        repo.upsert_runtime(neighbour.id, "target", f"astinuslab-{neighbour.id}", state="running", power_state="on")
        neighbour_action = repo.enqueue_action(neighbour.id, "acquire_memory_dump", {"capture_type": "memory"})
        for job_id in (live_runtime, neighbour.id):
            session.add(
                CapacityReservation(
                    job_id=job_id,
                    vm_count=1,
                    memory_mb=2048,
                    storage_bytes=20 * 1024**3,
                    state="reserved",
                    boot_started=True,
                )
            )
        neighbour_id, neighbour_action_id = neighbour.id, neighbour_action.id

    assert job_services.control_runtime_service(live_runtime, "target", "shutdown") is True
    control_runtime_task(live_runtime, _control_actions(in_memory_session_scope, live_runtime)[0].id)

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        neighbour_job = repo.get_job(neighbour_id)
        assert neighbour_job is not None
        assert neighbour_job.status == "waiting_for_capture"
        neighbour_runtime = repo.get_runtime(neighbour_id, "target")
        assert neighbour_runtime is not None
        assert neighbour_runtime.vm_name == f"astinuslab-{neighbour_id}"
        assert (neighbour_runtime.power_state, neighbour_runtime.state) == ("on", "running")
        action = repo.get_action(neighbour_action_id)
        assert action is not None
        assert action.status == "queued"
        for job_id in (live_runtime, neighbour_id):
            reservation = session.get(CapacityReservation, job_id)
            assert (reservation.state, reservation.boot_started) == ("reserved", True)


# --------------------------------------------------------------------------
# UI entry point
# --------------------------------------------------------------------------


def test_ui_runtime_control_queues_without_host_access(client, live_runtime, in_memory_session_scope, monkeypatch):
    host_call = MagicMock()
    monkeypatch.setattr(runtime_control, "control_runtime", host_call)

    response = client.post(f"/jobs/{live_runtime}/runtimes/target/shutdown", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/jobs/{live_runtime}"
    host_call.assert_not_called()
    actions = _control_actions(in_memory_session_scope, live_runtime)
    assert len(actions) == 1 and actions[0].status == "queued"
