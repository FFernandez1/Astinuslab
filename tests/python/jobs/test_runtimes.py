from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.python._support.helpers import autospec_setattr

WORKLOAD_ID = "wordpress-6.4"


def _make_lab_job(client, attacker_vm_id: str | None = None) -> dict:
    payload = {
        "target_id": "ubuntu-22-04-default",
        "scenario_id": "lab-session",
        "execution_mode": "manual_trigger",
        "workload_id": WORKLOAD_ID,
    }
    if attacker_vm_id:
        payload["attacker_vm_id"] = attacker_vm_id
    response = client.post("/api/v1/jobs", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


class TestJobRuntimesAPI:
    def test_new_job_exposes_target_runtime(self, client):
        job = _make_lab_job(client)

        response = client.get(f"/api/v1/jobs/{job['id']}/runtimes")

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["role"] == "target"
        assert body[0]["capture_memory_enabled"] is True
        assert body[0]["capture_disk_enabled"] is True

    def test_job_with_attacker_vm_id_creates_attacker_runtime(self, client):
        job = _make_lab_job(client, attacker_vm_id="kali")

        response = client.get(f"/api/v1/jobs/{job['id']}/runtimes")

        assert response.status_code == 200
        roles = {item["role"] for item in response.json()}
        assert roles == {"target", "attacker"}

    def test_control_runtime_rejects_missing_attacker(self, client):
        job = _make_lab_job(client)

        response = client.post(f"/api/v1/jobs/{job['id']}/runtimes/attacker/reboot")

        assert response.status_code == 409

    def test_control_runtime_rejects_removed_start_action(self, client):
        job = _make_lab_job(client)

        response = client.post(f"/api/v1/jobs/{job['id']}/runtimes/target/start")

        assert response.status_code == 400


class TestCaptureGuardrails:
    def test_memory_capture_rejected_when_target_runtime_is_off(self, client, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        job = _make_lab_job(client)
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(job["id"], status="waiting_for_capture", current_stage="waiting_for_capture")
            repo.update_runtime(job["id"], "target", power_state="off", state="stopped")

        response = client.post(f"/api/v1/jobs/{job['id']}/acquire-memory-dump")

        assert response.status_code == 409
        assert "powered off" in response.text

    def test_disk_capture_still_allowed_when_target_runtime_is_off(self, client, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        response = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "lab-session",
                "execution_mode": "manual_trigger",
                "capture_type": "disk",
                "workload_id": WORKLOAD_ID,
            },
        )
        assert response.status_code == 201, response.text
        job = response.json()

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(job["id"], status="waiting_for_capture", current_stage="waiting_for_capture")
            repo.update_runtime(job["id"], "target", power_state="off", state="stopped")

        capture = client.post(f"/api/v1/jobs/{job['id']}/acquire-memory-dump")

        assert capture.status_code == 200
        assert capture.json()["status"] == "capture_requested"


class TestRuntimeServices:
    def test_control_runtime_service_adds_event(self, in_memory_session_scope, monkeypatch):
        import astinuslab.db as db_mod
        import astinuslab.virtualization.process_support as proc_mod
        from astinuslab.api.schemas import JobCreate
        from astinuslab.db.repositories import JobRepository
        from astinuslab.jobs.job_services import control_runtime_service, create_job_service

        monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)
        autospec_setattr(monkeypatch, proc_mod, "run_command", lambda *a, **k: "running")

        with in_memory_session_scope() as session:
            job = create_job_service(
                session,
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="lab-session",
                    execution_mode="manual_trigger",
                    workload_id=WORKLOAD_ID,
                ),
            )
            repo = JobRepository(session)
            repo.update_job(job.id, status="waiting_for_capture", current_stage="waiting_for_capture")
            repo.update_runtime(job.id, "target", power_state="on", state="running")
            job_id = job.id

        assert control_runtime_service(job_id, "target", "reboot") is True

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            events = repo.list_events(job_id)
            assert any(e.state == "runtime_control" and "reboot" in e.message for e in events)

    def test_control_runtime_service_rejects_target_controls_after_capture_requested(
        self, in_memory_session_scope, monkeypatch
    ):
        """Once a capture is requested the guest's state belongs to the acquisition."""
        import astinuslab.db as db_mod
        import astinuslab.virtualization.process_support as proc_mod
        from astinuslab.api.schemas import JobCreate
        from astinuslab.db.repositories import JobRepository
        from astinuslab.jobs.job_services import control_runtime_service, create_job_service

        monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)
        autospec_setattr(monkeypatch, proc_mod, "run_command", lambda *a, **k: "running")

        with in_memory_session_scope() as session:
            job = create_job_service(
                session,
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="lab-session",
                    execution_mode="manual_trigger",
                    workload_id=WORKLOAD_ID,
                ),
            )
            repo = JobRepository(session)
            repo.update_job(job.id, status="capture_requested", current_stage="capture_requested")
            repo.update_runtime(job.id, "target", power_state="on", state="running")
            job_id = job.id

        from astinuslab.api.errors import ApiError

        with pytest.raises(ApiError) as error:
            control_runtime_service(job_id, "target", "reboot")
        assert error.value.status_code == 409

        with in_memory_session_scope() as session:
            actions = JobRepository(session).list_actions(job_id)
            assert not [a for a in actions if a.action_type == "control_runtime"]

    def test_trigger_capture_service_can_override_capture_type(self, in_memory_session_scope):
        from astinuslab.api.schemas import JobCreate
        from astinuslab.db.repositories import JobRepository
        from astinuslab.forensics.services import trigger_capture_service
        from astinuslab.jobs.job_services import create_job_service

        with in_memory_session_scope() as session:
            job = create_job_service(
                session,
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="lab-session",
                    execution_mode="manual_trigger",
                    workload_id=WORKLOAD_ID,
                ),
            )
            repo = JobRepository(session)
            repo.update_job(job.id, status="waiting_for_capture", current_stage="waiting_for_capture")
            updated = trigger_capture_service(session, job.id, capture_type="disk")
            assert updated.capture_type == "disk"
            assert updated.status == "capture_requested"
            assert any(action.action_type == "acquire_memory_dump" for action in repo.list_actions(job.id))


class TestOfflineDiskCapture:
    def test_acquire_and_publish_uses_host_mode_when_target_is_off(
        self, in_memory_session_scope, monkeypatch, tmp_path
    ):
        import astinuslab.db as db_mod
        from astinuslab.api.schemas import JobCreate
        from astinuslab.db.repositories import JobRepository
        from astinuslab.jobs.job_services import create_job_service
        from astinuslab.orchestration.deps import JobRunDeps

        monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)
        deps = JobRunDeps.default()
        deps.session_scope = in_memory_session_scope
        from unittest.mock import MagicMock

        conn = MagicMock()
        conn.lookupByName.return_value.isActive.return_value = 0
        deps.open_libvirt_connection = lambda: conn
        deps.get_active_key_paths = lambda: (tmp_path / "id", tmp_path / "id.pub")

        commands: list[list[str]] = []
        disk_file = tmp_path / "offline-disk.img.gz"
        disk_file.write_bytes(b"disk-bytes")

        def fake_run(cmd, **kwargs):
            commands.append(cmd)
            joined = " ".join(cmd)
            if "acquire_disk_host.sh" in joined:
                return f"disk_publish_stage=completed\ndisk_final_path={disk_file}\ndisk_filename=offline-disk.img.gz\n"
            if cmd[:1] == ["sha256sum"]:
                return "deadbeef  whatever"
            return ""

        deps.run_command = fake_run

        with in_memory_session_scope() as session:
            job = create_job_service(
                session,
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="lab-session",
                    execution_mode="manual_trigger",
                    workload_id=WORKLOAD_ID,
                    capture_type="disk",
                ),
            )
            repo = JobRepository(session)
            # Seed job to waiting_for_capture (valid pre-acquisition state)
            job.status = "waiting_for_capture"
            job.current_stage = "waiting_for_capture"
            session.flush()
            repo.update_runtime(job.id, "target", power_state="off", state="stopped")
            job_id = job.id

        from astinuslab.orchestration.runners.boot_capture_runner import acquire_and_publish

        acquire_and_publish(
            job_id,
            "ubuntu-22-04-default",
            "192.168.122.10",
            "astinuslab-offline-target",
            "ubuntu",
            "22.04",
            None,
            kernel_release="5.15.0-test",
            capture_type="disk",
            deps=deps,
        )

        assert any("acquire_disk_host.sh" in " ".join(cmd) for cmd in commands)
        assert not any("acquire_disk_remote.sh" in " ".join(cmd) for cmd in commands)


class TestResetLab:
    def test_409_when_not_waiting_for_capture(self, in_memory_session_scope, monkeypatch):
        import astinuslab.db as db_mod
        from astinuslab.api.errors import ApiError
        from astinuslab.api.schemas import JobCreate
        from astinuslab.jobs.job_services import create_job_service, reset_lab_service

        monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)

        with in_memory_session_scope() as session:
            job = create_job_service(
                session,
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="lab-session",
                    execution_mode="manual_trigger",
                    workload_id=WORKLOAD_ID,
                ),
            )
            job_id = job.id

        with pytest.raises(ApiError) as exc_info:
            reset_lab_service(job_id)
        assert exc_info.value.status_code == 409

    def test_409_when_no_snapshot(self, in_memory_session_scope, monkeypatch):
        import astinuslab.db as db_mod
        from astinuslab.api.errors import ApiError
        from astinuslab.api.schemas import JobCreate
        from astinuslab.db.repositories import JobRepository
        from astinuslab.jobs.job_services import create_job_service, reset_lab_service

        monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)

        with in_memory_session_scope() as session:
            job = create_job_service(
                session,
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="lab-session",
                    execution_mode="manual_trigger",
                    workload_id=WORKLOAD_ID,
                ),
            )
            repo = JobRepository(session)
            repo.update_job(job.id, status="waiting_for_capture", lab_snapshot_name=None)
            job_id = job.id

        with pytest.raises(ApiError) as exc_info:
            reset_lab_service(job_id)
        assert exc_info.value.status_code == 409

    def test_dispatches_revert_task(self, in_memory_session_scope, monkeypatch):
        import astinuslab.db as db_mod
        import astinuslab.jobs.job_services as svc_mod
        from astinuslab.api.schemas import JobCreate
        from astinuslab.db.repositories import JobRepository
        from astinuslab.jobs.job_services import create_job_service, reset_lab_service

        monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)

        dispatched = []
        monkeypatch.setattr(
            svc_mod,
            "revert_lab_snapshot_task",
            SimpleNamespace(delay=lambda job_id: dispatched.append(job_id)),
        )

        with in_memory_session_scope() as session:
            job = create_job_service(
                session,
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="lab-session",
                    execution_mode="manual_trigger",
                    workload_id=WORKLOAD_ID,
                ),
            )
            repo = JobRepository(session)
            repo.update_job(job.id, status="waiting_for_capture", lab_snapshot_name="snap-clean")
            job_id = job.id

        assert reset_lab_service(job_id) is True
        assert dispatched == [job_id]


def test_delete_job_requires_storage_custody_even_when_file_missing(in_memory_session_scope):
    from astinuslab.api.schemas import JobCreate
    from astinuslab.core.config import settings
    from astinuslab.db.repositories import ArtifactRepository, JobRepository
    from astinuslab.jobs.job_services import create_job_service, delete_job_service

    storage_root = settings.acl_allowed_storage_roots[0]
    missing_dump = storage_root / "dumps" / "job-missing" / "memory.dump"

    with in_memory_session_scope() as session:
        job = create_job_service(
            session,
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="lab-session",
                execution_mode="manual_trigger",
                workload_id=WORKLOAD_ID,
            ),
        )
        repo = JobRepository(session)
        repo.update_job(job.id, status="completed")
        ArtifactRepository(session).create_artifact(
            kind="memory",
            producer="job_capture",
            path=str(missing_dump),
            filename="memory.dump",
            job_id=job.id,
        )
        job_id = job.id

    with in_memory_session_scope() as session:
        from astinuslab.api.errors import ApiError

        with pytest.raises(ApiError, match="Storage"):
            delete_job_service(session, job_id, delete_files=True)

    with in_memory_session_scope() as session:
        assert JobRepository(session).get_job(job_id) is not None
