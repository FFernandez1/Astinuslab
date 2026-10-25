"""Tests for the pentesting lab feature: workloads, provisioning, snapshots, reset, timer."""

from __future__ import annotations

import pytest

from tests.python._support.helpers import autospec_setattr

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


class TestIndexWorkloadUI:
    def test_index_contains_workload_select(self, client):
        r = client.get("/jobs/new")
        assert r.status_code == 200
        assert b"workload-panel-container" in r.content

    def test_index_contains_wordpress_option(self, client):
        r = client.get("/fragments/workload-panel?scenario_id=lab-session&target_id=ubuntu-22-04-default")
        assert WORDPRESS_ID.encode() in r.content

    def test_index_contains_dvwa_option(self, client):
        r = client.get("/fragments/workload-panel?scenario_id=lab-session&target_id=ubuntu-22-04-default")
        assert b"dvwa" in r.content


# ── UI: job create with workload ──────────────────────────────────────────────


class TestCreateLabJobUI:
    def test_lab_session_form_creates_job(self, client):
        r = client.post(
            "/jobs/new",
            data={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "lab-session",
                "workload_id": WORDPRESS_ID,
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"].startswith("/jobs/job-")

    def test_lab_session_without_workload_form_creates_job(self, client):
        r = client.post(
            "/jobs/new",
            data={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "lab-session",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303


# ── UI: live fragment lab panel ────────────────────────────────────────────────


class TestLabPanelUI:
    def test_lab_panel_shown_when_waiting_with_workload(self, client, lab_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(
                lab_job["id"],
                status="waiting_for_capture",
                guest_ip="192.168.122.10",
                lab_url="http://192.168.122.10/wp-login.php",
                lab_snapshot_name="lab-clean",
                workload_provisioned_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            )

        r = client.get(f"/jobs/{lab_job['id']}/live-fragment")
        assert r.status_code == 200
        assert b"Active Lab" in r.content or b"Lab Activo" in r.content or b"Lab actif" in r.content

    def test_lab_panel_shows_reset_button_when_snapshot_exists(self, client, lab_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(
                lab_job["id"],
                status="waiting_for_capture",
                guest_ip="192.168.122.10",
                lab_snapshot_name="lab-clean",
            )

        r = client.get(f"/jobs/{lab_job['id']}/live-fragment")
        assert b"reset-lab" in r.content

    def test_lab_panel_no_reset_button_without_snapshot(self, client, lab_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(
                lab_job["id"],
                status="waiting_for_capture",
                guest_ip="192.168.122.10",
            )

        r = client.get(f"/jobs/{lab_job['id']}/live-fragment")
        # Check for the reset button HTML element, not the string "reset-lab"
        # which also appears in the always-rendered JS event-handler block.
        assert b'id="btn-reset-lab-' not in r.content

    def test_provisioning_status_shows_active_spinner(self, client, lab_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(lab_job["id"], status="provisioning_workload")

        r = client.get(f"/jobs/{lab_job['id']}/live-fragment")
        assert r.status_code == 200
        assert b"provisioning" in r.content
        assert b'class="spinner"' in r.content

    def test_regular_waiting_card_shown_without_workload(self, client, created_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(created_job["id"], status="waiting_for_capture", current_stage="waiting_for_capture")

        r = client.get(f"/jobs/{created_job['id']}/live-fragment")
        assert r.status_code == 200
        assert b"live-section" in r.content


# ── UI: reset lab route ────────────────────────────────────────────────────────


class TestResetLabUI:
    def test_redirects_after_reset(self, client, lab_job, in_memory_session_scope, monkeypatch):
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

        r = client.post(f"/jobs/{lab_job['id']}/reset-lab", follow_redirects=False)
        assert r.status_code == 303
        assert f"/jobs/{lab_job['id']}" in r.headers["location"]

    def test_409_when_not_waiting(self, client, lab_job):
        r = client.post(f"/jobs/{lab_job['id']}/reset-lab")
        assert r.status_code == 409


# ── Admin workloads page ───────────────────────────────────────────────────────


class TestAdminWorkloadsPage:
    def test_returns_200(self, client):
        r = client.get("/workloads")
        assert r.status_code == 200

    def test_shows_wordpress_workload(self, client):
        r = client.get("/workloads")
        assert b"WordPress" in r.content

    def test_shows_dvwa_workload(self, client):
        r = client.get("/workloads")
        assert b"DVWA" in r.content

    def test_shows_service_ports(self, client):
        r = client.get("/workloads")
        assert b"port" in r.content
        assert b"80" in r.content

    def test_shows_install_script_path(self, client):
        r = client.get("/workloads")
        assert b"install.sh" in r.content

    def test_shows_cve_context(self, client):
        r = client.get("/workloads")
        assert b"CVE" in r.content or b"vulnerable" in r.content

    def test_nav_link_present_on_index(self, client):
        r = client.get("/jobs")
        assert b"/workloads" in r.content


# ── Auto-capture on timer expiry ───────────────────────────────────────────────


class TestCapturesTableWorkloadBehavior:
    def test_manual_trigger_lab_job_shown_in_captures_table(self, client, lab_job):
        r = client.get("/jobs-table-fragment")
        assert r.status_code == 200
        assert lab_job["id"][:8].encode() in r.content
        assert WORDPRESS_ID.encode() in r.content

    def test_regular_job_has_no_workload_badge(self, client, created_job):
        r = client.get("/jobs-table-fragment")
        assert r.status_code == 200
        # The immediate-capture job has no workload_id — no green badge
        # We just confirm the page renders and the job appears
        assert created_job["id"][:8].encode() in r.content


@pytest.mark.parametrize(
    "selected_id,workload_enabled,catalog_user,expected_user",
    [
        ("acceptance-ubuntu", False, "ubuntu", "ubuntu"),
        ("acceptance-ubuntu", True, "ubuntu", "ubuntu"),
        (None, True, None, "legacy-pentester"),
        (None, False, None, "kali"),
    ],
)
def test_attacker_ssh_uses_selected_catalog_then_legacy_workload(
    client, in_memory_session_scope, monkeypatch, selected_id, workload_enabled, catalog_user, expected_user
):
    from astinuslab.api.schemas import JobCreate
    from astinuslab.db.repositories import JobRepository
    from astinuslab.ui import job_context

    catalog_entry = {"id": "acceptance-ubuntu", "image_ref": "ubuntu.img", "username": catalog_user}
    monkeypatch.setattr(job_context, "attacker_by_id", lambda _: catalog_entry, raising=False)
    monkeypatch.setattr(
        job_context,
        "load_workloads",
        lambda: [{"id": "legacy-lab", "display_name": "Legacy lab", "attacker_vm": {"username": "legacy-pentester"}}],
    )
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="lab-session",
                execution_mode="manual_trigger",
                attacker_vm_id=selected_id,
                workload_id="legacy-lab" if workload_enabled else None,
            )
        )
        job_id = job.id
        repo.update_job(job_id, status="waiting_for_capture")
        repo.upsert_runtime(
            job_id, "attacker", "astinuslab-test-attacker", ip_address="192.0.2.81", state="running", power_state="on"
        )
    for url in (f"/jobs/{job_id}", f"/jobs/{job_id}/live-fragment"):
        response = client.get(url)
        assert response.status_code == 200
        assert f"{expected_user}@192.0.2.81" in response.text
        if expected_user != "kali":
            assert "kali@192.0.2.81" not in response.text
