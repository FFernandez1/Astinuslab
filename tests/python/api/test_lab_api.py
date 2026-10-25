"""Tests for the pentesting lab feature: workloads, provisioning, snapshots, reset, timer."""

from __future__ import annotations

import pytest

from tests.python._support.helpers import autospec_setattr


@pytest.fixture(autouse=True)
def _pinned_host_capacity(available_host_capacity):
    """Job admission in this module must not depend on the host's free RAM or disk."""


WORDPRESS_ID = "wordpress-6.4"
WORDPRESS_NAME = "WordPress 6.4"


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture()
def lab_job(client):
    """Creates a manual_trigger job with the current WordPress workload."""
    r = client.post(
        "/api/v1/jobs",
        json={
            "target_id": "ubuntu-22-04-default",
            "scenario_id": "lab-session",
            "execution_mode": "manual_trigger",
            "workload_id": WORDPRESS_ID,
        },
    )
    assert r.status_code == 201
    return r.json()


# ── Workloads endpoint ─────────────────────────────────────────────────────────


class TestWorkloadsEndpoint:
    def test_returns_200(self, client):
        r = client.get("/api/v1/workloads")
        assert r.status_code == 200

    def test_returns_list(self, client):
        r = client.get("/api/v1/workloads")
        assert isinstance(r.json(), list)

    def test_includes_wordpress(self, client):
        ids = [w["id"] for w in client.get("/api/v1/workloads").json()]
        assert WORDPRESS_ID in ids

    def test_includes_dvwa(self, client):
        ids = [w["id"] for w in client.get("/api/v1/workloads").json()]
        assert "dvwa" in ids

    def test_items_have_expected_fields(self, client):
        for w in client.get("/api/v1/workloads").json():
            assert "id" in w
            assert "display_name" in w
            assert "tags" in w
            assert "services" in w

    def test_wordpress_has_services(self, client):
        workloads = {w["id"]: w for w in client.get("/api/v1/workloads").json()}
        wp = workloads[WORDPRESS_ID]
        assert len(wp["services"]) > 0
        assert any(s["port"] == 80 for s in wp["services"])

    def test_wordpress_services_have_credentials(self, client):
        workloads = {w["id"]: w for w in client.get("/api/v1/workloads").json()}
        wp = workloads[WORDPRESS_ID]
        web_svc = next(s for s in wp["services"] if s["port"] == 80)
        assert web_svc["credentials"] is not None
        assert "user" in web_svc["credentials"]
        assert "password" in web_svc["credentials"]


# ── Job creation with workload ─────────────────────────────────────────────────


class TestCreateLabJob:
    def test_lab_job_created_successfully(self, lab_job):
        assert lab_job["status"] == "queued"
        assert lab_job["workload_id"] == WORDPRESS_ID

    def test_lab_job_has_null_lab_url_initially(self, lab_job):
        assert lab_job["lab_url"] is None

    def test_lab_job_has_null_snapshot_initially(self, lab_job):
        assert lab_job["lab_snapshot_name"] is None

    def test_lab_job_has_null_provisioned_at_initially(self, lab_job):
        assert lab_job["workload_provisioned_at"] is None

    def test_workload_id_rejected_for_immediate_mode(self, client):
        r = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "immediate-capture",
                "execution_mode": "immediate",
                "workload_id": WORDPRESS_ID,
            },
        )
        assert r.status_code == 400

    def test_workload_id_rejected_for_post_script_mode(self, client):
        r = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "workload-sample-post-script",
                "execution_mode": "post_script",
                "workload_id": WORDPRESS_ID,
            },
        )
        assert r.status_code == 400

    def test_unknown_workload_id_rejected(self, client):
        r = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "lab-session",
                "execution_mode": "manual_trigger",
                "workload_id": "nonexistent-workload",
            },
        )
        assert r.status_code == 400

    def test_lab_session_without_workload_is_valid(self, client):
        r = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "lab-session",
                "execution_mode": "manual_trigger",
            },
        )
        assert r.status_code == 201
        assert r.json()["workload_id"] is None

    def test_workload_id_persisted_in_job(self, client, lab_job):
        r = client.get(f"/api/v1/jobs/{lab_job['id']}")
        assert r.status_code == 200
        assert r.json()["workload_id"] == WORDPRESS_ID

    def test_enqueues_run_manual_trigger_action(self, client, lab_job):
        actions = client.get("/api/v1/host-actions").json()
        job_actions = [a for a in actions if a["job_id"] == lab_job["id"]]
        assert any(a["action_type"] == "run_manual_trigger_job" for a in job_actions)


# ── Lab info endpoint ──────────────────────────────────────────────────────────


class TestLabInfoEndpoint:
    def test_404_for_unknown_job(self, client):
        r = client.get("/api/v1/jobs/ghost/lab-info")
        assert r.status_code == 404

    def test_404_for_job_without_workload(self, client, created_job):
        r = client.get(f"/api/v1/jobs/{created_job['id']}/lab-info")
        assert r.status_code == 404

    def test_returns_workload_info(self, client, lab_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(
                lab_job["id"],
                status="waiting_for_capture",
                guest_ip="192.168.122.10",
                lab_url="http://192.168.122.10:80/wp-login.php",
            )

        r = client.get(f"/api/v1/jobs/{lab_job['id']}/lab-info")
        assert r.status_code == 200
        data = r.json()
        assert data["workload_id"] == WORDPRESS_ID
        assert data["guest_ip"] == "192.168.122.10"

    def test_lab_info_includes_services(self, client, lab_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(lab_job["id"], guest_ip="192.168.122.10")

        r = client.get(f"/api/v1/jobs/{lab_job['id']}/lab-info")
        assert r.status_code == 200
        services = r.json()["services"]
        assert len(services) > 0
        assert any(s["port"] == 80 for s in services)

    def test_lab_info_service_has_url(self, client, lab_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(lab_job["id"], guest_ip="192.168.122.10")

        r = client.get(f"/api/v1/jobs/{lab_job['id']}/lab-info")
        services = r.json()["services"]
        web_svc = next(s for s in services if s["port"] == 80)
        assert "192.168.122.10" in web_svc["url"]

    def test_snapshot_available_false_initially(self, client, lab_job):
        r = client.get(f"/api/v1/jobs/{lab_job['id']}/lab-info")
        assert r.status_code == 200
        assert r.json()["snapshot_available"] is False

    def test_snapshot_available_true_when_set(self, client, lab_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(lab_job["id"], lab_snapshot_name="lab-clean")

        r = client.get(f"/api/v1/jobs/{lab_job['id']}/lab-info")
        assert r.json()["snapshot_available"] is True


# ── Reset lab endpoint ─────────────────────────────────────────────────────────


class TestResetLabEndpoint:
    def test_404_for_unknown_job(self, client):
        r = client.post("/api/v1/jobs/ghost/reset-lab")
        assert r.status_code == 404

    def test_409_when_not_waiting_for_capture(self, client, lab_job):
        # Job is queued, not waiting_for_capture
        r = client.post(f"/api/v1/jobs/{lab_job['id']}/reset-lab")
        assert r.status_code == 409

    def test_409_when_no_snapshot(self, client, lab_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(lab_job["id"], status="waiting_for_capture")

        r = client.post(f"/api/v1/jobs/{lab_job['id']}/reset-lab")
        assert r.status_code == 409

    def test_202_when_valid(self, client, lab_job, in_memory_session_scope, monkeypatch):
        import astinuslab.jobs.job_services as svc_mod

        autospec_setattr(monkeypatch, svc_mod.revert_lab_snapshot_task, "delay", lambda job_id: True)

        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(
                lab_job["id"],
                status="waiting_for_capture",
                lab_snapshot_name="lab-clean",
            )

        r = client.post(f"/api/v1/jobs/{lab_job['id']}/reset-lab")
        assert r.status_code == 202


# ── Retry carries workload_id ──────────────────────────────────────────────────


class TestRetryLabJob:
    def test_retry_preserves_workload_id(self, client, lab_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(lab_job["id"], status="failed", error_summary="test")

        r = client.post(f"/api/v1/jobs/{lab_job['id']}/retry")
        assert r.status_code == 201
        new_job = r.json()
        assert new_job["workload_id"] == WORDPRESS_ID

    def test_retry_creates_new_job_id(self, client, lab_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(lab_job["id"], status="failed", error_summary="test")

        r = client.post(f"/api/v1/jobs/{lab_job['id']}/retry")
        assert r.json()["id"] != lab_job["id"]


# ── UI: index page workload dropdown ──────────────────────────────────────────
