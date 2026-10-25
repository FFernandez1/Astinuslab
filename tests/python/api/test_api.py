"""Integration tests for the REST API endpoints."""

from __future__ import annotations

import shutil
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tests.python._support.helpers import autospec_setattr


@pytest.fixture(autouse=True)
def _pinned_host_capacity(available_host_capacity):
    """Job admission in this module must not depend on the host's free RAM or disk."""


class TestHealth:
    def test_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_ready_returns_200_for_ready_report(self, client, monkeypatch):
        from astinuslab import main as main_mod
        from astinuslab.core.operational_health import ReadinessResult

        monkeypatch.setattr(
            main_mod,
            "collect_readiness",
            lambda: ReadinessResult(
                payload={"status": "ready", "checks": {}},
                http_status=200,
            ),
        )

        response = client.get("/ready")

        assert response.status_code == 200
        assert response.json() == {"status": "ready", "checks": {}}

    def test_ready_returns_503_for_not_ready_report(self, client, monkeypatch):
        from astinuslab import main as main_mod
        from astinuslab.core.operational_health import ReadinessResult

        monkeypatch.setattr(
            main_mod,
            "collect_readiness",
            lambda: ReadinessResult(
                payload={
                    "status": "not_ready",
                    "checks": {
                        "storage": {"status": "unavailable", "critical": True},
                    },
                },
                http_status=503,
            ),
        )

        response = client.get("/ready")

        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"


class TestTargetsEndpoint:
    def test_returns_user_selectable_targets(self, client):
        r = client.get("/api/v1/targets")

        assert r.status_code == 200
        targets = r.json()
        assert isinstance(targets, list)
        assert any(target["id"] == "ubuntu-22-04-default" for target in targets)
        required_fields = {"id", "distro", "version", "enabled", "image_available"}
        assert all(required_fields.issubset(target) for target in targets)


class TestScenariosEndpoint:
    def test_returns_modes_needed_by_job_creation(self, client):
        r = client.get("/api/v1/scenarios")

        assert r.status_code == 200
        scenarios = r.json()
        assert isinstance(scenarios, list)
        modes = {scenario["mode"] for scenario in scenarios}
        required_modes = {"immediate", "post_script", "manual_trigger"}
        assert required_modes.issubset(modes)
        assert modes <= required_modes


class TestWorkloadsEndpoint:
    def test_target_filter_marks_workload_as_compatible(self, client):
        r = client.get("/api/v1/workloads?target_id=ubuntu-22-04-default")
        assert r.status_code == 200
        workloads = {item["id"]: item for item in r.json()}
        assert workloads["wordpress-6.4"]["compatibility_status"] == "compatible"

    def test_compatible_only_filters_out_incompatible_workloads(self, client):
        r = client.get("/api/v1/workloads?target_id=fedora-40&compatible_only=true")
        assert r.status_code == 200
        assert r.json() == []

    def test_target_filter_includes_incompatibility_reason(self, client):
        r = client.get("/api/v1/workloads?target_id=fedora-40")
        assert r.status_code == 200
        workloads = {item["id"]: item for item in r.json()}
        assert workloads["wordpress-6.4"]["compatibility_status"] == "incompatible"
        assert workloads["wordpress-6.4"]["compatibility_reason"]


class TestCreateJob:
    def test_returns_201(self, client):
        r = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "immediate-capture",
                "execution_mode": "immediate",
            },
        )
        assert r.status_code == 201

    def test_returns_409_when_playground_session_is_active(self, client, in_memory_session_scope):
        from astinuslab.playground import repositories as pr

        with in_memory_session_scope() as session:
            pr.create_playground_session(session, job_id="pg-active")

        r = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "immediate-capture",
                "execution_mode": "immediate",
            },
        )

        assert r.status_code == 409
        assert "playground" in r.json()["detail"].lower()

    def test_response_has_expected_fields(self, created_job):
        assert "id" in created_job
        assert created_job["status"] == "queued"
        assert created_job["target_id"] == "ubuntu-22-04-default"
        assert created_job["scenario_id"] == "immediate-capture"

    def test_invalid_target_returns_400(self, client):
        r = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "nonexistent-target",
                "scenario_id": "immediate-capture",
                "execution_mode": "immediate",
            },
        )
        assert r.status_code == 400

    def test_invalid_scenario_returns_400(self, client):
        r = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "nonexistent-scenario",
                "execution_mode": "immediate",
            },
        )
        assert r.status_code == 400

    def test_invalid_execution_mode_returns_400(self, client):
        r = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "immediate-capture",
                "execution_mode": "bogus-mode",
            },
        )
        assert r.status_code == 400

    def test_post_script_mode_accepted(self, client):
        r = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "workload-sample-post-script",
                "execution_mode": "post_script",
            },
        )
        assert r.status_code == 201

    def test_manual_trigger_mode_accepted(self, client):
        r = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "lab-session",
                "execution_mode": "manual_trigger",
            },
        )
        assert r.status_code == 201

    def test_incompatible_workload_returns_400(self, client):
        r = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "fedora-40",
                "scenario_id": "lab-session",
                "execution_mode": "manual_trigger",
                "workload_id": "wordpress-6.4",
            },
        )
        assert r.status_code == 400

    def test_incompatible_workload_detail_names_target_and_workload(self, client):
        r = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "fedora-40",
                "scenario_id": "lab-session",
                "execution_mode": "manual_trigger",
                "workload_id": "wordpress-6.4",
            },
        )
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert "wordpress-6.4" in detail
        assert "fedora-40" in detail

    def test_build_vol3_isf_defaults_false(self, created_job):
        assert created_job["build_vol3_isf"] is False

    def test_build_vol3_isf_can_be_set_true_for_immediate(self, client):
        r = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "immediate-capture",
                "execution_mode": "immediate",
                "build_vol3_isf": True,
            },
        )
        assert r.status_code == 201
        assert r.json()["build_vol3_isf"] is True

    def test_build_vol3_isf_rejected_for_manual_trigger(self, client):
        r = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "lab-session",
                "execution_mode": "manual_trigger",
                "build_vol3_isf": True,
            },
        )
        assert r.status_code == 400
        assert "build_vol3_isf" in r.json()["detail"]

    def test_build_vol3_isf_rejected_for_post_script(self, client):
        r = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "workload-sample-post-script",
                "execution_mode": "post_script",
                "build_vol3_isf": True,
            },
        )

        assert r.status_code == 400
        assert "post_script" in r.json()["detail"]
        assert "build_vol3_isf" in r.json()["detail"]

    def test_build_vol3_isf_stored_in_action_payload(self, client):
        import json

        r = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "immediate-capture",
                "execution_mode": "immediate",
                "build_vol3_isf": True,
            },
        )
        assert r.status_code == 201
        job_id = r.json()["id"]
        actions = client.get("/api/v1/host-actions").json()
        job_action = next(a for a in actions if a["job_id"] == job_id)
        payload = json.loads(job_action["payload"] or "{}")
        assert payload.get("isf_only") is True

    def test_correct_action_enqueued_for_immediate(self, client):
        client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "immediate-capture",
                "execution_mode": "immediate",
            },
        )
        actions = client.get("/api/v1/host-actions").json()
        assert any(a["action_type"] == "run_immediate_job" for a in actions)

    def test_correct_action_enqueued_for_post_script(self, client):
        client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "workload-sample-post-script",
                "execution_mode": "post_script",
            },
        )
        actions = client.get("/api/v1/host-actions").json()
        assert any(a["action_type"] == "run_post_script_job" for a in actions)

    def test_correct_action_enqueued_for_manual_trigger(self, client):
        client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "lab-session",
                "execution_mode": "manual_trigger",
            },
        )
        actions = client.get("/api/v1/host-actions").json()
        assert any(a["action_type"] == "run_manual_trigger_job" for a in actions)


class TestListJobs:
    def test_empty_initially(self, client):
        r = client.get("/api/v1/jobs")
        assert r.status_code == 200
        assert r.json() == []

    def test_includes_created_job(self, client, created_job):
        r = client.get("/api/v1/jobs")
        ids = [j["id"] for j in r.json()]
        assert created_job["id"] in ids

    def test_multiple_jobs(self, client):
        for _ in range(3):
            client.post(
                "/api/v1/jobs",
                json={
                    "target_id": "ubuntu-22-04-default",
                    "scenario_id": "immediate-capture",
                    "execution_mode": "immediate",
                },
            )
        r = client.get("/api/v1/jobs")
        assert len(r.json()) == 3

    def test_respects_limit(self, client):
        for _ in range(3):
            client.post(
                "/api/v1/jobs",
                json={
                    "target_id": "ubuntu-22-04-default",
                    "scenario_id": "immediate-capture",
                    "execution_mode": "immediate",
                },
            )

        r = client.get("/api/v1/jobs?limit=2")

        assert r.status_code == 200
        assert len(r.json()) == 2

    def test_default_unbounded(self, client):
        for _ in range(3):
            client.post(
                "/api/v1/jobs",
                json={
                    "target_id": "ubuntu-22-04-default",
                    "scenario_id": "immediate-capture",
                    "execution_mode": "immediate",
                },
            )

        r = client.get("/api/v1/jobs")

        assert r.status_code == 200
        assert len(r.json()) >= 3


class TestGetJob:
    def test_returns_job(self, client, created_job):
        r = client.get(f"/api/v1/jobs/{created_job['id']}")
        assert r.status_code == 200
        assert r.json()["id"] == created_job["id"]

    def test_404_for_unknown_job(self, client):
        r = client.get("/api/v1/jobs/does-not-exist")
        assert r.status_code == 404


class TestJobLogs:
    def test_returns_event_list(self, client, created_job):
        r = client.get(f"/api/v1/jobs/{created_job['id']}/logs")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_initial_queued_event_present(self, client, created_job):
        r = client.get(f"/api/v1/jobs/{created_job['id']}/logs")
        states = [e["state"] for e in r.json()]
        assert "queued" in states

    def test_404_for_unknown_job(self, client):
        r = client.get("/api/v1/jobs/ghost/logs")
        assert r.status_code == 404


class TestCancelJob:
    def test_404_for_unknown_job(self, client):
        r = client.post("/api/v1/jobs/ghost/cancel")
        assert r.status_code == 404

    def test_cancel_queued_job(self, client, created_job, monkeypatch):
        import astinuslab.jobs.job_services as svc_mod

        autospec_setattr(monkeypatch, svc_mod, "request_job_cancel", lambda job_id: True)
        r = client.post(f"/api/v1/jobs/{created_job['id']}/cancel")
        assert r.status_code == 200

    def test_cancel_returns_409_when_not_cancelable(self, client, created_job, monkeypatch):
        import astinuslab.jobs.job_services as svc_mod

        autospec_setattr(monkeypatch, svc_mod, "request_job_cancel", lambda job_id: False)
        r = client.post(f"/api/v1/jobs/{created_job['id']}/cancel")
        assert r.status_code == 409


class TestTriggerCapture:
    def test_404_for_unknown_job(self, client):
        r = client.post("/api/v1/jobs/ghost/acquire-memory-dump")
        assert r.status_code == 404

    def test_409_when_not_waiting_for_capture(self, client, created_job):
        # Job is in 'queued' state, not 'waiting_for_capture'
        r = client.post(f"/api/v1/jobs/{created_job['id']}/acquire-memory-dump")
        assert r.status_code == 409

    def test_transitions_to_capture_requested(self, client, created_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(created_job["id"], status="waiting_for_capture")

        r = client.post(f"/api/v1/jobs/{created_job['id']}/acquire-memory-dump")
        assert r.status_code == 200
        assert r.json()["status"] == "capture_requested"
        actions = client.get("/api/v1/host-actions").json()
        assert any(
            action["job_id"] == created_job["id"] and action["action_type"] == "acquire_memory_dump"
            for action in actions
        )


class TestRetryJob:
    def test_404_for_unknown_job(self, client):
        r = client.post("/api/v1/jobs/ghost/retry")
        assert r.status_code == 404

    def test_409_when_job_not_failed(self, client, created_job):
        r = client.post(f"/api/v1/jobs/{created_job['id']}/retry")
        assert r.status_code == 409

    def test_creates_new_job_from_failed(self, client, created_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(created_job["id"], status="failed", error_summary="test failure")

        r = client.post(f"/api/v1/jobs/{created_job['id']}/retry")
        assert r.status_code == 201
        new_job = r.json()
        assert new_job["id"] != created_job["id"]
        assert new_job["target_id"] == created_job["target_id"]
        assert new_job["scenario_id"] == created_job["scenario_id"]
        assert new_job["status"] == "queued"

    def test_retry_increases_job_count(self, client, created_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(created_job["id"], status="failed", error_summary="test failure")

        client.post(f"/api/v1/jobs/{created_job['id']}/retry")
        jobs = client.get("/api/v1/jobs").json()
        assert len(jobs) == 2


class TestHostActions:
    def test_returns_list(self, client):
        r = client.get("/api/v1/host-actions")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_immediate_job_enqueues_action(self, client, created_job):
        r = client.get("/api/v1/host-actions")
        action_job_ids = [a["job_id"] for a in r.json()]
        assert created_job["id"] in action_job_ids


class TestArtifactDownload:
    def test_404_when_dump_path_not_set(self, client, created_job):
        r = client.get(f"/api/v1/jobs/{created_job['id']}/artifact")
        assert r.status_code == 404

    def test_403_when_dump_path_outside_dumps_dir(self, client, created_job, in_memory_session_scope):
        from astinuslab.db.repositories import ArtifactRepository

        with in_memory_session_scope() as session:
            ArtifactRepository(session).create_artifact(
                kind="memory",
                producer="job_capture",
                path="/etc/passwd",
                filename="passwd",
                job_id=created_job["id"],
            )

        r = client.get(f"/api/v1/jobs/{created_job['id']}/artifact")
        assert r.status_code == 403

    def test_404_when_disk_dump_path_not_set(self, client, created_job):
        r = client.get(f"/api/v1/jobs/{created_job['id']}/disk-artifact")
        assert r.status_code == 404

    def test_403_when_disk_dump_path_outside_dumps_dir(self, client, created_job, in_memory_session_scope):
        from astinuslab.db.repositories import ArtifactRepository

        with in_memory_session_scope() as session:
            ArtifactRepository(session).create_artifact(
                kind="disk",
                producer="job_capture",
                path="/etc/shadow",
                filename="shadow",
                job_id=created_job["id"],
            )

        r = client.get(f"/api/v1/jobs/{created_job['id']}/disk-artifact")
        assert r.status_code == 403


class TestSafeNext:
    """Unit tests for the _safe_next() open-redirect guard in ui.py."""

    def _fn(self):
        from astinuslab.ui.jobs_helpers import safe_next

        return safe_next

    def test_absolute_url_falls_back_to_root(self):
        assert self._fn()("https://evil.com") == "/"

    def test_protocol_relative_falls_back_to_root(self):
        assert self._fn()("//evil.com") == "/"

    def test_valid_relative_path_is_preserved(self):
        assert self._fn()("/jobs") == "/jobs"

    def test_root_is_preserved(self):
        assert self._fn()("/") == "/"

    def test_nested_path_is_preserved(self):
        assert self._fn()("/jobs/job-abc/detail") == "/jobs/job-abc/detail"

    def test_empty_string_falls_back_to_root(self):
        assert self._fn()("") == "/"

    def test_lang_absolute_url_falls_back_to_root(self):
        assert self._fn()("http://evil.com/steal") == "/"

    def test_backslash_normalised_path_falls_back_to_root(self):
        assert self._fn()("/\\evil.com") == "/"


def test_create_job_dispatches_celery_task(client, monkeypatch, in_memory_session_scope):
    import astinuslab.jobs.job_services as svc_mod

    dispatched: list[tuple] = []

    class _FakeResult:
        id = "test-celery-id-001"

    class _FakeTask:
        def delay(self, job_id, action_id):
            dispatched.append((job_id, action_id))
            return _FakeResult()

    fake_task_map = {
        "run_immediate_job": _FakeTask(),
        "run_post_script_job": _FakeTask(),
        "run_manual_trigger_job": _FakeTask(),
        "acquire_memory_dump": _FakeTask(),
        "control_runtime": _FakeTask(),
    }
    monkeypatch.setattr(svc_mod, "TASK_MAP", fake_task_map)

    resp = client.post(
        "/api/v1/jobs",
        json={
            "target_id": "ubuntu-22-04-default",
            "scenario_id": "immediate-capture",
            "execution_mode": "immediate",
        },
    )
    assert resp.status_code == 201
    assert len(dispatched) == 1
    job_id = resp.json()["id"]
    assert dispatched[0][0] == job_id

    with in_memory_session_scope() as session:
        from astinuslab.db.repositories import JobRepository

        repo = JobRepository(session)
        stored_job = repo.get_job(job_id)
        assert stored_job is not None
        assert stored_job.celery_task_id == "test-celery-id-001"


def test_create_job_commits_rows_before_dispatch(monkeypatch, tmp_path):
    import astinuslab.db.models  # noqa: F401
    import astinuslab.jobs.job_services as svc_mod
    from astinuslab.api.schemas import JobCreate
    from astinuslab.db import Base
    from astinuslab.db.repositories import JobRepository

    engine = create_engine(f"sqlite:///{tmp_path / 'dispatch.db'}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    @contextmanager
    def scope():
        session = Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    seen: dict[str, bool] = {}

    class _FakeResult:
        id = "committed-before-dispatch"

    class _FakeTask:
        def delay(self, job_id, action_id):
            with Session() as observer:
                repo = JobRepository(observer)
                seen["job"] = repo.get_job(job_id) is not None
                seen["action"] = repo.get_action(action_id) is not None
            return _FakeResult()

    monkeypatch.setattr(svc_mod, "TASK_MAP", {"run_immediate_job": _FakeTask()})
    autospec_setattr(monkeypatch, svc_mod, "get_active_key_paths", lambda: (tmp_path / "id", tmp_path / "id.pub"))

    with scope() as session:
        job = svc_mod.create_job_service(
            session,
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
            ),
        )
        job_id = job.id

    assert seen == {"job": True, "action": True}
    with scope() as session:
        stored = JobRepository(session).get_job(job_id)
        assert stored is not None
        assert stored.celery_task_id == "committed-before-dispatch"


def test_create_job_returns_503_when_celery_unavailable(client, monkeypatch):
    import astinuslab.jobs.job_services as svc_mod

    class _BrokenTask:
        def delay(self, *args, **kwargs):
            raise ConnectionError("broker unavailable")

    monkeypatch.setattr(svc_mod, "TASK_MAP", {"run_immediate_job": _BrokenTask()})

    resp = client.post(
        "/api/v1/jobs",
        json={
            "target_id": "ubuntu-22-04-default",
            "scenario_id": "immediate-capture",
            "execution_mode": "immediate",
        },
    )
    assert resp.status_code == 503


def test_trigger_capture_returns_503_when_celery_unavailable(client, monkeypatch, in_memory_session_scope):
    import astinuslab.jobs.job_services as svc_mod

    # Create a job first via the normal path
    resp = client.post(
        "/api/v1/jobs",
        json={
            "target_id": "ubuntu-22-04-default",
            "scenario_id": "immediate-capture",
            "execution_mode": "immediate",
        },
    )
    assert resp.status_code == 201
    job_id = resp.json()["id"]

    # Put the job into 'waiting_for_capture' state so trigger-capture is allowed
    with in_memory_session_scope() as session:
        from astinuslab.db.repositories import JobRepository

        repo = JobRepository(session)
        repo.update_job(job_id, status="waiting_for_capture")

    # Stub TASK_MAP so acquire_memory_dump raises ConnectionError
    class _BrokenTask:
        def delay(self, *args, **kwargs):
            raise ConnectionError("broker unavailable")

    monkeypatch.setattr(svc_mod, "TASK_MAP", {"acquire_memory_dump": _BrokenTask()})

    resp = client.post(f"/api/v1/jobs/{job_id}/acquire-memory-dump")
    assert resp.status_code == 503


class TestArtifactPathResolution:
    def test_safe_artifact_path_uses_settings_storage_dir(self, tmp_path, monkeypatch):
        """Artifact validation must use settings.storage_dir, not __file__-relative paths.

        If these diverge (non-default install location), every artifact download
        returns 403 because the resolved dump path falls outside the allowed root.
        """
        import astinuslab.jobs.job_services as svc_mod

        storage_dir = tmp_path / "custom-storage"
        artifact = storage_dir / "dumps" / "job-abc" / "memory.dump"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"data")
        monkeypatch.setattr(
            svc_mod, "settings", SimpleNamespace(storage_dir=storage_dir, acl_allowed_storage_roots=(storage_dir,))
        )

        assert svc_mod.safe_artifact_path(str(artifact)) == artifact.resolve()

    def test_safe_artifact_path_rejects_sibling_directory(self, tmp_path, monkeypatch):
        """A path in a sibling directory (e.g. /storage/dumps_evil/) must be rejected."""
        from fastapi import HTTPException

        import astinuslab.jobs.job_services as svc_mod

        storage_dir = Path.cwd() / "storage" / f"pytest-path-root-{tmp_path.name}"
        sibling = Path(f"{storage_dir}_evil")
        try:
            (storage_dir / "dumps").mkdir(parents=True)
            sibling.mkdir(parents=True)
            evil_file = sibling / "stolen.dump"
            evil_file.write_bytes(b"data")
            monkeypatch.setattr(
                svc_mod, "settings", SimpleNamespace(storage_dir=storage_dir, acl_allowed_storage_roots=(storage_dir,))
            )

            with pytest.raises(HTTPException) as exc_info:
                svc_mod.safe_artifact_path(str(evil_file))
            assert exc_info.value.status_code == 403
        finally:
            shutil.rmtree(storage_dir, ignore_errors=True)
            shutil.rmtree(sibling, ignore_errors=True)

    def test_safe_artifact_path_accepts_file_inside_dumps_dir(self, tmp_path, monkeypatch):
        """A legitimate dump file inside _DUMPS_DIR must be accepted."""
        import astinuslab.jobs.job_services as svc_mod

        dumps_root = tmp_path / "storage" / "dumps"
        dumps_root.mkdir(parents=True)
        legit_file = dumps_root / "job-abc123" / "memory.dump"
        legit_file.parent.mkdir()
        legit_file.write_bytes(b"\x7fELF" + b"\x00" * 64)

        monkeypatch.setattr(
            svc_mod,
            "settings",
            SimpleNamespace(storage_dir=tmp_path / "storage", acl_allowed_storage_roots=(tmp_path / "storage",)),
        )

        result = svc_mod.safe_artifact_path(str(legit_file))
        assert result == legit_file.resolve()

    def test_safe_artifact_path_rejects_path_traversal(self, tmp_path, monkeypatch):
        """Path traversal via ../ must be rejected even if the resolved target exists."""
        from fastapi import HTTPException

        import astinuslab.jobs.job_services as svc_mod

        storage_dir = Path.cwd() / "storage" / f"pytest-traversal-root-{tmp_path.name}"
        dumps_root = storage_dir / "dumps"
        try:
            dumps_root.mkdir(parents=True)
            # Create a file outside dumps_root that a traversal could reach
            outside_dir = storage_dir / "secrets"
            outside_dir.mkdir()
            outside_file = outside_dir / "sensitive.dump"
            outside_file.write_bytes(b"secret data")

            monkeypatch.setattr(
                svc_mod, "settings", SimpleNamespace(storage_dir=storage_dir, acl_allowed_storage_roots=(storage_dir,))
            )

            # Construct a traversal path that resolves outside dumps_root
            traversal_path = dumps_root / ".." / "secrets" / "sensitive.dump"

            with pytest.raises(HTTPException) as exc_info:
                svc_mod.safe_artifact_path(str(traversal_path))
            assert exc_info.value.status_code == 403
        finally:
            shutil.rmtree(storage_dir, ignore_errors=True)

    def test_safe_artifact_path_rejects_symlink_escape(self, tmp_path, monkeypatch):
        """A symlink inside dumps must not expose a target outside dumps."""
        from fastapi import HTTPException

        import astinuslab.jobs.job_services as svc_mod

        storage_dir = tmp_path / "storage"
        dumps_root = storage_dir / "dumps"
        outside_dir = tmp_path / "outside"
        dumps_root.mkdir(parents=True)
        outside_dir.mkdir()
        outside_file = outside_dir / "secret.dump"
        outside_file.write_bytes(b"secret data")
        link = dumps_root / "job-abc" / "memory.dump"
        link.parent.mkdir()
        link.symlink_to(outside_file)

        monkeypatch.setattr(
            svc_mod,
            "settings",
            SimpleNamespace(storage_dir=storage_dir, acl_allowed_storage_roots=(storage_dir,)),
        )

        with pytest.raises(HTTPException) as exc_info:
            svc_mod.safe_artifact_path(str(link))
        assert exc_info.value.status_code == 403


class TestDeleteJob:
    def test_delete_job_succeeds(self, client, in_memory_session_scope):
        response = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "immediate-capture",
                "execution_mode": "immediate",
            },
        )
        assert response.status_code == 201
        job_id = response.json()["id"]

        with in_memory_session_scope() as session:
            from astinuslab.db.repositories import JobRepository

            repo = JobRepository(session)
            repo.force_transition(
                job_id,
                "completed",
                started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
                finished_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            )
            from astinuslab.jobs.capacity import confirm_cleanup
            from astinuslab.jobs.capacity_host import CleanupProof

            repo.upsert_runtime(job_id, "target", f"astinuslab-{job_id}", state="destroyed")
            assert confirm_cleanup(session, job_id, proof=CleanupProof(job_id, True, True))

        r = client.delete(f"/api/v1/jobs/{job_id}")
        assert r.status_code == 204

    def test_delete_job_preserves_evidence_files(self, client, in_memory_session_scope, tmp_path, monkeypatch):
        import astinuslab.jobs.job_services as svc

        monkeypatch.setattr(svc, "settings", svc.settings.model_copy(update={"storage_dir": tmp_path / "storage"}))

        # 1. Create a job
        response = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "immediate-capture",
                "execution_mode": "immediate",
            },
        )
        assert response.status_code == 201
        job_id = response.json()["id"]

        # 2. Create fake files
        dump_dir = tmp_path / "storage" / "dumps"
        dump_dir.mkdir(parents=True)
        mem_dump = dump_dir / "mem.dump"
        mem_dump.write_text("fake memory")

        # 3. Mark job as completed and record its dump artifact
        with in_memory_session_scope() as session:
            from astinuslab.db.repositories import ArtifactRepository, JobRepository

            repo = JobRepository(session)
            repo.force_transition(
                job_id,
                "completed",
                started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
                finished_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            )
            ArtifactRepository(session).create_artifact(
                kind="memory",
                producer="job_capture",
                path=str(mem_dump),
                filename="mem.dump",
                job_id=job_id,
            )

        # 4. Delete with delete_files=true
        r = client.delete(f"/api/v1/jobs/{job_id}?delete_files=true")
        assert r.status_code == 409
        assert "custody history" in r.json()["detail"]

        # Job deletion cannot bypass Storage's evidence deletion boundary.
        assert mem_dump.read_text() == "fake memory"
        with in_memory_session_scope() as session:
            assert JobRepository(session).job_has_evidence(job_id)

    def test_delete_non_terminal_job_with_delete_files_returns_409(self, client, created_job):
        """Terminal-state check must run before path validation; non-terminal job + delete_files=true → 409, not 404."""
        # created_job is in 'queued' (non-terminal) state and has no dump_path set
        r = client.delete(f"/api/v1/jobs/{created_job['id']}?delete_files=true")
        assert r.status_code == 409

    def test_delete_job_preserves_evidence_outside_storage_before_deleting_record(
        self, client, in_memory_session_scope, tmp_path, monkeypatch
    ):
        import astinuslab.jobs.job_services as svc

        storage_dir = tmp_path / "storage"
        dumps_dir = storage_dir / "dumps"
        dumps_dir.mkdir(parents=True)
        outside_file = tmp_path / "outside.dump"
        outside_file.write_text("do not delete")
        monkeypatch.setattr(svc, "settings", svc.settings.model_copy(update={"storage_dir": storage_dir}))

        response = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "immediate-capture",
                "execution_mode": "immediate",
            },
        )
        assert response.status_code == 201
        job_id = response.json()["id"]

        with in_memory_session_scope() as session:
            from astinuslab.db.repositories import ArtifactRepository, JobRepository

            repo = JobRepository(session)
            repo.force_transition(job_id, "completed")
            ArtifactRepository(session).create_artifact(
                kind="memory",
                producer="job_capture",
                path=str(outside_file),
                filename="outside.dump",
                job_id=job_id,
            )

        r = client.delete(f"/api/v1/jobs/{job_id}?delete_files=true")
        assert r.status_code == 409
        assert "custody history" in r.json()["detail"]
        try:
            assert outside_file.exists()
            with in_memory_session_scope() as session:
                from astinuslab.db.repositories import JobRepository

                assert JobRepository(session).get_job(job_id) is not None
        finally:
            outside_file.unlink(missing_ok=True)


class TestCreateJobUIForm:
    def test_form_post_with_build_vol3_isf(self, client):
        r = client.post(
            "/jobs/new",
            data={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "immediate-capture",
                "capture_type": "memory",
                "build_vol3_isf": "1",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
