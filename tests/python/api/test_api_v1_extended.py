"""Extended integration tests for the REST API v1 endpoints."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.mark.parametrize(
    ("readiness", "expected"),
    [
        (
            {
                "enabled": False,
                "analysis": (False, "feature_disabled", "Volatility integration is disabled.", None),
                "isf_build": (False, "feature_disabled", "Volatility integration is disabled.", None),
            },
            {
                "enabled": False,
                "analysis": {
                    "ready": False,
                    "code": "feature_disabled",
                    "detail": "Volatility integration is disabled.",
                    "version": None,
                    "untested": False,
                },
                "isf_build": {
                    "ready": False,
                    "code": "feature_disabled",
                    "detail": "Volatility integration is disabled.",
                    "version": None,
                    "untested": False,
                },
            },
        ),
        (
            {
                "enabled": True,
                "analysis": (True, "ready", "Volatility 3 is ready.", "2.28.0"),
                "isf_build": (True, "ready", "dwarf2json is ready.", None),
            },
            {
                "enabled": True,
                "analysis": {
                    "ready": True,
                    "code": "ready",
                    "detail": "Volatility 3 is ready.",
                    "version": "2.28.0",
                    "untested": False,
                },
                "isf_build": {
                    "ready": True,
                    "code": "ready",
                    "detail": "dwarf2json is ready.",
                    "version": None,
                    "untested": False,
                },
            },
        ),
        (
            {
                "enabled": True,
                "analysis": (
                    False,
                    "volatility_not_configured",
                    "Configure the operator-supplied Volatility tool.",
                    None,
                ),
                "isf_build": (
                    False,
                    "dwarf2json_not_configured",
                    "Configure the operator-supplied dwarf2json tool.",
                    None,
                ),
            },
            {
                "enabled": True,
                "analysis": {
                    "ready": False,
                    "code": "volatility_not_configured",
                    "detail": "Configure the operator-supplied Volatility tool.",
                    "version": None,
                    "untested": False,
                },
                "isf_build": {
                    "ready": False,
                    "code": "dwarf2json_not_configured",
                    "detail": "Configure the operator-supplied dwarf2json tool.",
                    "version": None,
                    "untested": False,
                },
            },
        ),
        (
            {
                "enabled": True,
                "analysis": (True, "ready", "Volatility 3 is ready.", "2.28.0"),
                "isf_build": (
                    False,
                    "dwarf2json_not_configured",
                    "Configure the operator-supplied dwarf2json tool.",
                    None,
                ),
            },
            {
                "enabled": True,
                "analysis": {
                    "ready": True,
                    "code": "ready",
                    "detail": "Volatility 3 is ready.",
                    "version": "2.28.0",
                    "untested": False,
                },
                "isf_build": {
                    "ready": False,
                    "code": "dwarf2json_not_configured",
                    "detail": "Configure the operator-supplied dwarf2json tool.",
                    "version": None,
                    "untested": False,
                },
            },
        ),
        (
            {
                "enabled": True,
                "analysis": (
                    False,
                    "volatility_not_configured",
                    "Configure the operator-supplied Volatility tool.",
                    None,
                ),
                "isf_build": (True, "ready", "dwarf2json is ready.", None),
            },
            {
                "enabled": True,
                "analysis": {
                    "ready": False,
                    "code": "volatility_not_configured",
                    "detail": "Configure the operator-supplied Volatility tool.",
                    "version": None,
                    "untested": False,
                },
                "isf_build": {
                    "ready": True,
                    "code": "ready",
                    "detail": "dwarf2json is ready.",
                    "version": None,
                    "untested": False,
                },
            },
        ),
    ],
    ids=("disabled", "fully-ready", "enabled-tools-absent", "volatility-only", "dwarf2json-only"),
)
def test_volatility_readiness_endpoint_returns_exact_safe_public_payload(client, monkeypatch, readiness, expected):
    import astinuslab.forensics.api as volatility_api
    from astinuslab.forensics.vol3_tools import ToolStatus, Vol3Readiness

    snapshot = Vol3Readiness(
        enabled=readiness["enabled"],
        analysis=ToolStatus(*readiness["analysis"]),
        isf_build=ToolStatus(*readiness["isf_build"]),
    )
    monkeypatch.setattr(volatility_api, "get_vol3_readiness", lambda: snapshot, raising=False)

    response = client.get("/api/v1/volatility/readiness")

    assert response.status_code == 200
    assert response.json() == expected
    assert not ({"path", "resolved_path", "sha256", "hash", "raw"} & set(str(response.json()).lower().split()))


def test_volatility_readiness_endpoint_requires_admin_auth(monkeypatch):
    from starlette.testclient import TestClient

    import astinuslab.core.security as security_mod
    import astinuslab.forensics.api as volatility_api
    from astinuslab.forensics.vol3_tools import ToolStatus, Vol3Readiness
    from astinuslab.main import app

    token = "readiness-admin-token"
    monkeypatch.setattr(
        security_mod,
        "settings",
        SimpleNamespace(
            auth_enabled=True,
            admin_api_token_hash=f"sha256:{hashlib.sha256(token.encode()).hexdigest()}",
        ),
    )
    snapshot = Vol3Readiness(
        enabled=True,
        analysis=ToolStatus(True, "ready", "Volatility 3 is ready.", "2.28.0"),
        isf_build=ToolStatus(True, "ready", "dwarf2json is ready."),
    )
    monkeypatch.setattr(volatility_api, "get_vol3_readiness", lambda: snapshot, raising=False)

    with TestClient(app) as auth_client:
        denied = auth_client.get("/api/v1/volatility/readiness")
        allowed = auth_client.get(
            "/api/v1/volatility/readiness",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert denied.status_code == 401
    assert denied.headers["www-authenticate"] == "Bearer"
    assert allowed.status_code == 200


class TestArtifactDownloadHeaders:
    def test_artifact_download_exposes_sha256_header(
        self, client, created_job, in_memory_session_scope, tmp_path, monkeypatch
    ):
        """Forensic memory download must surface SHA256 + size as response headers."""
        import astinuslab.jobs.job_services as svc_mod

        storage_dir = tmp_path / "storage"
        dumps_dir = storage_dir / "dumps"
        dumps_dir.mkdir(parents=True)
        dump_path = dumps_dir / "mem.dump"
        dump_path.write_bytes(b"fake-memory")
        monkeypatch.setattr(
            svc_mod, "settings", SimpleNamespace(storage_dir=storage_dir, acl_allowed_storage_roots=(storage_dir,))
        )

        from astinuslab.db.repositories import ArtifactRepository, JobRepository

        with in_memory_session_scope() as session:
            JobRepository(session).update_job(created_job["id"], status="completed")
            ArtifactRepository(session).create_artifact(
                kind="memory",
                producer="job_capture",
                path=str(dump_path),
                filename="mem.dump",
                sha256="a" * 64,
                size_bytes=len(b"fake-memory"),
                job_id=created_job["id"],
            )

        r = client.get(f"/api/v1/jobs/{created_job['id']}/artifact")
        assert r.status_code == 200
        assert r.headers["X-Astinuslab-SHA256"] == "a" * 64
        assert r.headers["X-Astinuslab-Size"] == str(len(b"fake-memory"))


class TestSafeArtifactPath:
    def test_safe_artifact_path_rejects_tmp(self, tmp_path, monkeypatch):
        """A path under /tmp must be rejected; /tmp is no longer an allowed root."""
        from fastapi import HTTPException

        import astinuslab.jobs.job_services as svc_mod

        storage_dir = tmp_path / "storage"
        (storage_dir / "dumps").mkdir(parents=True)
        monkeypatch.setattr(
            svc_mod, "settings", SimpleNamespace(storage_dir=storage_dir, acl_allowed_storage_roots=(storage_dir,))
        )

        tmp_file = Path("/tmp") / f"pytest-tmp-reject-{tmp_path.name}.dump"
        tmp_file.write_bytes(b"data")
        try:
            with pytest.raises(HTTPException) as exc_info:
                svc_mod.safe_artifact_path(str(tmp_file))
            assert exc_info.value.status_code == 403
        finally:
            tmp_file.unlink(missing_ok=True)


class TestCvesEndpoint:
    def test_returns_list_of_cves(self, client):
        r = client.get("/api/v1/cves")
        assert r.status_code == 200
        assert isinstance(r.json(), list)
        if r.json():
            item = r.json()[0]
            assert "cve_id" in item
            assert "workload_ids" in item


class TestCaptureAlias:
    def test_capture_alias_works(self, client, created_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(created_job["id"], status="waiting_for_capture")

        r = client.post(f"/api/v1/jobs/{created_job['id']}/capture")
        assert r.status_code == 200
        assert r.json()["status"] == "capture_requested"


class TestDeleteJob:
    def test_deletes_terminal_job(self, client, created_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository
        from astinuslab.jobs.capacity import confirm_cleanup

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(created_job["id"], status="completed")
            # The stub dispatch never booted a VM; release its unused claim.
            assert confirm_cleanup(session, created_job["id"], never_started=True)

        r = client.delete(f"/api/v1/jobs/{created_job['id']}")
        assert r.status_code == 204

        # Verify it's gone
        r_get = client.get(f"/api/v1/jobs/{created_job['id']}")
        assert r_get.status_code == 404

    def test_409_for_active_job(self, client, created_job):
        # Job is 'queued'
        r = client.delete(f"/api/v1/jobs/{created_job['id']}")
        assert r.status_code == 409


class TestForceFailJob:
    def test_transitions_to_failed(self, client, created_job):
        r = client.post(f"/api/v1/jobs/{created_job['id']}/force-fail")
        assert r.status_code == 200
        assert r.json()["status"] == "failed"

    def test_idempotent_for_already_terminal_job(self, client, created_job, in_memory_session_scope):
        """Calling force-fail on a terminal job must return the existing job, not 409."""
        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(created_job["id"], status="completed")

        r = client.post(f"/api/v1/jobs/{created_job['id']}/force-fail")
        assert r.status_code == 200
        assert r.json()["status"] == "completed"


class TestLabInfo:
    def test_returns_info_for_job_with_workload(self, client, in_memory_session_scope):
        # Create a job with a workload
        resp = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "lab-session",
                "execution_mode": "manual_trigger",
                "workload_id": "wordpress-6.4",
            },
        )
        assert resp.status_code == 201
        job_id = resp.json()["id"]

        with in_memory_session_scope() as session:
            from astinuslab.db.repositories import JobRepository

            repo = JobRepository(session)
            repo.update_job(job_id, guest_ip="1.2.3.4", lab_snapshot_name="clean")

        r = client.get(f"/api/v1/jobs/{job_id}/lab-info")
        assert r.status_code == 200
        data = r.json()
        assert data["workload_id"] == "wordpress-6.4"
        assert data["guest_ip"] == "1.2.3.4"
        assert data["snapshot_available"] is True
        assert len(data["services"]) > 0

    def test_404_for_job_without_workload(self, client, created_job):
        # created_job has no workload
        r = client.get(f"/api/v1/jobs/{created_job['id']}/lab-info")
        assert r.status_code == 404


class TestResetLab:
    def test_reset_requested_when_valid(self, client, created_job, in_memory_session_scope):
        with in_memory_session_scope() as session:
            from astinuslab.db.repositories import JobRepository

            repo = JobRepository(session)
            repo.update_job(created_job["id"], status="waiting_for_capture", lab_snapshot_name="clean")

        with patch("astinuslab.jobs.job_services.revert_lab_snapshot_task.delay") as mock_delay:
            r = client.post(f"/api/v1/jobs/{created_job['id']}/reset-lab")
            assert r.status_code == 202
            assert r.json()["status"] == "reset_requested"
            mock_delay.assert_called_once_with(created_job["id"])

    def test_409_when_no_snapshot(self, client, created_job, in_memory_session_scope):
        with in_memory_session_scope() as session:
            from astinuslab.db.repositories import JobRepository

            repo = JobRepository(session)
            repo.update_job(created_job["id"], status="waiting_for_capture")  # no snapshot name

        r = client.post(f"/api/v1/jobs/{created_job['id']}/reset-lab")
        assert r.status_code == 409


class TestJobRuntimes:
    def test_lists_runtimes(self, client, created_job, in_memory_session_scope):
        with in_memory_session_scope() as session:
            from astinuslab.db.repositories import JobRepository

            repo = JobRepository(session)
            repo.upsert_runtime(created_job["id"], "target", vm_name="vm1", state="running", power_state="on")

        r = client.get(f"/api/v1/jobs/{created_job['id']}/runtimes")
        assert r.status_code == 200
        assert len(r.json()) == 1
        assert r.json()[0]["role"] == "target"


class TestControlRuntime:
    def test_accepted_when_valid(self, client, created_job, in_memory_session_scope):
        with in_memory_session_scope() as session:
            from astinuslab.db.repositories import JobRepository

            repo = JobRepository(session)
            repo.update_job(created_job["id"], status="waiting_for_capture")
            repo.upsert_runtime(created_job["id"], "target", vm_name="vm1", state="running", power_state="on")

        with patch("astinuslab.jobs.runtime_control.control_runtime") as mock_control:
            r = client.post(f"/api/v1/jobs/{created_job['id']}/runtimes/target/reboot")
            assert r.status_code == 202
            assert r.json()["status"] == "accepted"
            # Accepted means "recorded for the lifecycle worker", not "executed":
            # the API process must never reach the hypervisor itself.
            mock_control.assert_not_called()

        with in_memory_session_scope() as session:
            from astinuslab.db.repositories import JobRepository

            actions = JobRepository(session).list_actions(created_job["id"])
            queued = [a for a in actions if a.action_type == "control_runtime"]
            assert len(queued) == 1
            assert queued[0].status == "queued"
            assert queued[0].payload is not None
            assert json.loads(queued[0].payload) == {"role": "target", "action": "reboot"}

    def test_400_for_invalid_action(self, client, created_job):
        r = client.post(f"/api/v1/jobs/{created_job['id']}/runtimes/target/pause")
        assert r.status_code == 400


class TestRunRetention:
    def test_triggers_retention(self, client):
        with patch("astinuslab.storage.retention.run_retention") as mock_run:
            mock_run.return_value = {"dumps_deleted": 0, "logs_deleted": 0}
            r = client.post("/api/v1/run-retention")
            assert r.status_code == 200
            assert "dumps_deleted" in r.json()
