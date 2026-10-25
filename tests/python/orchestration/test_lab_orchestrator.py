"""Tests for the pentesting lab feature: workloads, provisioning, snapshots, reset, timer."""

from __future__ import annotations

import hashlib
import json
from typing import cast
from unittest.mock import MagicMock

import pytest

from tests.python._support.helpers import autospec_setattr

WORDPRESS_ID = "wordpress-6.4"
WORDPRESS_NAME = "WordPress 6.4"


def _repo_factory(repo) -> type:
    return cast(type, lambda _session: repo)


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _available_host_capacity(monkeypatch):
    """Orchestration tests must not depend on free space on the developer host."""
    from astinuslab.jobs import capacity_host
    from astinuslab.jobs.capacity import GIB, HostCapacity

    monkeypatch.setattr(
        capacity_host,
        "read_host_capacity",
        lambda *a, **k: HostCapacity(65536, 60000, 1000 * GIB, 900 * GIB, {}, synthetic=True),
    )


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

        autospec_setattr(monkeypatch, svc_mod, "revert_lab_snapshot_task", lambda job_id: True)

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

    def test_content_type_is_html(self, client):
        r = client.get("/workloads")
        assert "text/html" in r.headers["content-type"]

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


@pytest.mark.parametrize("ssh_failure", [False, True])
def test_boot_and_prepare_uses_vm_lifecycle_and_qga(in_memory_session_scope, monkeypatch, tmp_path, ssh_failure):
    import astinuslab.db as db_mod
    from astinuslab.api.schemas import JobCreate
    from astinuslab.db.repositories import JobRepository
    from astinuslab.jobs.job_services import create_job_service
    from astinuslab.orchestration.deps import JobRunDeps

    deps = JobRunDeps.default()

    calls: list[tuple[str, object]] = []

    class _FakeLifecycle:
        def boot(self, conn, **kwargs):
            calls.append(("boot", kwargs["vm_name"]))
            return type(
                "BootResult",
                (),
                {
                    "vm_name": kwargs["vm_name"],
                    "guest_ip": "192.168.122.77",
                },
            )()

    class _FakeDomain:
        pass

    class _FakeConn:
        def lookupByName(self, vm_name):
            calls.append(("lookup_domain", vm_name))
            return _FakeDomain()

        def close(self):
            calls.append(("close_conn", None))

    def _fake_run_command(cmd, **kwargs):
        script_name = next((str(part).split("/")[-1] for part in cmd if str(part).endswith(".sh")), "")
        if script_name == "fix_libvirt_path_acl.sh":
            return ""
        if script_name == "mount_output_share_remote.sh":
            assert calls[-1] == ("wait_for_guest_ssh", "192.168.122.77")
            calls.append(("mount_output_share", cmd[0]))
            return ""
        if cmd[:3] == ["sudo", "-n", "/usr/local/libexec/astinuslab/libvirt_storage_admin.sh"]:
            return ""
        raise AssertionError(f"unexpected command in _boot_and_prepare: {cmd}")

    monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)
    deps.session_scope = in_memory_session_scope
    deps.require_noninteractive_sudo = lambda: None
    deps.is_cancel_requested = lambda job_id: False
    deps.get_active_key_paths = lambda: (tmp_path / "id", tmp_path / "id.pub")
    deps.vm_lifecycle = lambda: _FakeLifecycle()
    deps.open_libvirt_connection = lambda: _FakeConn()
    deps.wait_for_guest_agent = lambda *a, **k: calls.append(("wait_for_guest_agent", a[0]))
    deps.write_guest_metadata_json = lambda *a, **k: None

    def fake_collect_guest_metadata(dom, uri):
        calls.append(("collect_guest_metadata_via_qga", None))
        return {
            "distro_id": "ubuntu",
            "distro_version": "22.04",
            "kernel_release": "5.15.0-test",
            "arch": "x86_64",
        }

    deps.collect_guest_metadata_via_qga = fake_collect_guest_metadata
    deps.parse_guest_ip = lambda *a, **k: (_ for _ in ()).throw(AssertionError("legacy parse_guest_ip used"))

    def wait_for_ssh(job_id, ip, key, user):
        calls.append(("wait_for_guest_ssh", ip))
        if ssh_failure:
            raise RuntimeError("SSH did not become ready")

    deps.wait_for_guest_ssh = wait_for_ssh
    deps.run_command = _fake_run_command
    (tmp_path / "id.pub").write_text("ssh-ed25519 AAAATESTKEY astinuslab@test\n", encoding="utf-8")

    with in_memory_session_scope() as session:
        job = create_job_service(
            session,
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
            ),
        )
        job_id = job.id

    if ssh_failure:
        with pytest.raises(RuntimeError, match="SSH did not become ready"):
            deps.boot_and_prepare(
                job_id=job_id,
                image_path=tmp_path / "target.qcow2",
                os_variant="generic",
                job_target_id="ubuntu-22-04-default",
            )
        assert ("collect_guest_metadata_via_qga", None) in calls
        assert calls[-1] == ("wait_for_guest_ssh", "192.168.122.77")
        assert not any(name == "mount_output_share" for name, _ in calls)
        return

    guest_ip, metadata = deps.boot_and_prepare(
        job_id=job_id,
        image_path=tmp_path / "target.qcow2",
        os_variant="generic",
        job_target_id="ubuntu-22-04-default",
    )

    assert guest_ip == "192.168.122.77"
    assert metadata == {
        "distro_id": "ubuntu",
        "distro_version": "22.04",
        "kernel_release": "5.15.0-test",
        "arch": "x86_64",
        "source": "qga",
    }
    assert calls == [
        ("boot", f"astinuslab-{job_id}"),
        ("wait_for_guest_agent", job_id),
        ("collect_guest_metadata_via_qga", None),
        ("close_conn", None),
        ("wait_for_guest_ssh", "192.168.122.77"),
        ("mount_output_share", "bash"),
    ]
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        updated_job = repo.get_job(job_id)
        runtime = repo.get_runtime(job_id, "target")
        assert updated_job is not None
        assert updated_job.status == "mounting_output_share"
        assert updated_job.current_stage == "mounting_output_share"
        assert updated_job.guest_ip == "192.168.122.77"
        assert updated_job.detected_distro == "ubuntu"
        assert updated_job.detected_version == "22.04"
        assert updated_job.detected_kernel_release == "5.15.0-test"
        assert updated_job.detected_architecture == "x86_64"
        assert runtime is not None
        assert runtime.ip_address == "192.168.122.77"
        assert runtime.state == "running"
        assert runtime.power_state == "on"


def test_boot_and_prepare_records_undersized_guest_disk_warning(
    in_memory_session_scope,
    monkeypatch,
    tmp_path,
):
    import astinuslab.db as db_mod
    import astinuslab.virtualization.guest_wait as guest_wait_mod
    from astinuslab.api.schemas import JobCreate
    from astinuslab.db.repositories import JobRepository
    from astinuslab.jobs.job_services import create_job_service
    from astinuslab.orchestration.deps import JobRunDeps

    deps = JobRunDeps.default()

    class _FakeLifecycle:
        def boot(self, conn, **kwargs):
            return type(
                "BootResult",
                (),
                {"vm_name": kwargs["vm_name"], "guest_ip": "192.168.122.77"},
            )()

    class _FakeConn:
        def close(self):
            pass

    def _fake_run_command(cmd, **kwargs):
        script_name = next((str(part).split("/")[-1] for part in cmd if str(part).endswith(".sh")), "")
        if script_name in {"fix_libvirt_path_acl.sh", "mount_output_share_remote.sh"}:
            return ""
        if cmd[:3] == ["sudo", "-n", "/usr/local/libexec/astinuslab/libvirt_storage_admin.sh"]:
            return ""
        raise AssertionError(f"unexpected command in _boot_and_prepare: {cmd}")

    def _fake_qga_command(libvirt_uri, vm_name, payload):
        assert payload == {"execute": "guest-get-fsinfo"}
        return json.dumps(
            {
                "return": [
                    {
                        "mountpoint": "/",
                        "total-bytes": 10 * 1024**3,
                    }
                ]
            }
        )

    monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)
    deps.session_scope = in_memory_session_scope
    deps.require_noninteractive_sudo = lambda: None
    deps.is_cancel_requested = lambda job_id: False
    deps.vm_lifecycle = lambda: _FakeLifecycle()
    deps.open_libvirt_connection = lambda: _FakeConn()
    deps.wait_for_guest_agent = lambda *a, **k: None
    monkeypatch.setattr(guest_wait_mod, "_run_qga_command", _fake_qga_command)
    deps.wait_for_guest_ssh = lambda *a, **k: None
    deps.write_guest_metadata_json = lambda *a, **k: None
    deps.collect_guest_metadata_via_qga = lambda dom, uri: {
        "distro_id": "ubuntu",
        "distro_version": "22.04",
        "kernel_release": "5.15.0-test",
        "arch": "x86_64",
    }
    deps.run_command = _fake_run_command

    with in_memory_session_scope() as session:
        job = create_job_service(
            session,
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
            ),
        )
        job_id = job.id

    deps.boot_and_prepare(
        job_id=job_id,
        image_path=tmp_path / "target.qcow2",
        os_variant="generic",
        job_target_id="ubuntu-22-04-default",
        vm_disk_gb=50,
    )

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        events = [(event.state, event.message) for event in repo.list_events(job_id)]

    assert any(
        state == "provisioning_workload" and "root partition is only 10.0GB" in message for state, message in events
    )


def test_load_job_params_returns_named_dataclass(in_memory_session_scope, monkeypatch):
    from astinuslab.api.schemas import JobCreate
    from astinuslab.core.config import settings
    from astinuslab.db.repositories import JobRepository
    from astinuslab.jobs import job_state_support

    image_path = (settings.images_dir / "custom.qcow2").resolve()

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="workload-sample-post-script",
                execution_mode="post_script",
                image_path=str(image_path),
                custom_script="id",
                vm_vcpus=4,
                vm_memory_mb=8192,
                vm_disk_gb=80,
                attacker_vm_vcpus=2,
                attacker_vm_memory_mb=2048,
                attacker_vm_disk_gb=20,
                capture_type="both",
            )
        )
        job_id = job.id

    params = job_state_support.load_job_params(in_memory_session_scope, JobRepository, job_id)

    assert isinstance(params, job_state_support.JobParams)
    assert params.target_id == "ubuntu-22-04-default"
    assert params.scenario_id == "workload-sample-post-script"
    assert params.image_path == str(image_path)
    assert params.custom_script == "id"
    assert params.vm_vcpus == 4
    assert params.vm_memory_mb == 8192
    assert params.vm_disk_gb == 80
    assert params.attacker_vm_vcpus == 2
    assert params.attacker_vm_memory_mb == 2048
    assert params.attacker_vm_disk_gb == 20
    assert params.capture_type == "both"


class TestManualTriggerCancellation:
    def test_request_job_cancel_marks_waiting_job_and_running_action_cancel_requested(
        self,
        in_memory_session_scope,
        monkeypatch,
    ):
        import astinuslab.db as db_mod
        import astinuslab.jobs.job_control as job_control_mod
        from astinuslab.api.schemas import JobCreate
        from astinuslab.db.repositories import JobRepository
        from astinuslab.jobs.job_services import create_job_service

        monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)
        monkeypatch.setattr(job_control_mod, "os", type("O", (), {"killpg": staticmethod(lambda *a, **k: None)})())

        with in_memory_session_scope() as session:
            job = create_job_service(
                session,
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="lab-session",
                    execution_mode="manual_trigger",
                    workload_id=WORDPRESS_ID,
                ),
            )
            repo = JobRepository(session)
            repo.update_job(job.id, status="waiting_for_capture", current_stage="waiting_for_capture")
            action = repo.enqueue_action(job.id, "run_manual_trigger_job")
            repo.update_action(action.id, status="running", active_pid=4321)
            job_id = job.id
            action_id = action.id

        assert job_control_mod.request_job_cancel(job_id) is True

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            updated_job = repo.get_job(job_id)
            updated_action = repo.get_action(action_id)
            assert updated_job is not None
            assert updated_job.status == "cancel_requested"
            assert updated_job.current_stage == "cancel_requested"
            assert updated_action is not None
            assert updated_action.status == "cancel_requested"

    def test_request_job_cancel_is_safe_when_called_twice_for_waiting_job(
        self,
        in_memory_session_scope,
        monkeypatch,
    ):
        import astinuslab.db as db_mod
        import astinuslab.jobs.job_control as job_control_mod
        from astinuslab.api.schemas import JobCreate
        from astinuslab.db.repositories import JobRepository
        from astinuslab.jobs.job_services import create_job_service

        monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)
        monkeypatch.setattr(job_control_mod, "os", type("O", (), {"killpg": staticmethod(lambda *a, **k: None)})())

        with in_memory_session_scope() as session:
            job = create_job_service(
                session,
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="lab-session",
                    execution_mode="manual_trigger",
                    workload_id=WORDPRESS_ID,
                ),
            )
            repo = JobRepository(session)
            repo.update_job(job.id, status="waiting_for_capture", current_stage="waiting_for_capture")
            action = repo.enqueue_action(job.id, "run_manual_trigger_job")
            repo.update_action(action.id, status="running", active_pid=4321)
            job_id = job.id
            action_id = action.id

        assert job_control_mod.request_job_cancel(job_id) is True
        assert job_control_mod.request_job_cancel(job_id) is True

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            updated_job = repo.get_job(job_id)
            updated_action = repo.get_action(action_id)

            assert updated_job is not None
            assert updated_job.status == "cancel_requested"
            assert updated_job.current_stage == "cancel_requested"

            assert updated_action is not None
            assert updated_action.status == "cancel_requested"

    def test_run_manual_trigger_job_returns_while_waiting_without_teardown(
        self,
        in_memory_session_scope,
        monkeypatch,
        tmp_path,
    ):
        import astinuslab.db as db_mod
        from astinuslab.api.schemas import JobCreate
        from astinuslab.core.config import settings
        from astinuslab.db.repositories import JobRepository
        from astinuslab.jobs.job_services import create_job_service
        from astinuslab.live_session.runner import run_live_session_job
        from astinuslab.orchestration.deps import JobRunDeps

        deps = JobRunDeps.default()
        monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)
        deps.session_scope = in_memory_session_scope
        deps.check_host_disk_space = lambda job_id: None
        deps.create_lab_snapshot = lambda *a, **k: "lab-clean"
        deps.load_workload_id = lambda job_id: None
        torn_down: list[str] = []
        deps.teardown_vm = lambda job_id: torn_down.append(job_id)

        with in_memory_session_scope() as session:
            job = create_job_service(
                session,
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="lab-session",
                    execution_mode="manual_trigger",
                ),
            )
            repo = JobRepository(session)
            repo.update_job(
                job.id,
                image_path=str((settings.images_dir / f"{job.id}-target.qcow2").resolve()),
            )
            job_id = job.id

        def _fake_boot_and_prepare(*a, **k):
            # Simulate state boot_and_prepare leaves: preparing_image → mounting_output_share
            with in_memory_session_scope() as s:
                r = JobRepository(s)
                j = r.get_job(job_id)
                assert j is not None
                j.status = "mounting_output_share"
                j.current_stage = "mounting_output_share"
                s.flush()
            return ("192.168.122.10", {"kernel_release": "5.15.0-test"})

        deps.boot_and_prepare = _fake_boot_and_prepare
        run_live_session_job(job_id, deps=deps)

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            updated_job = repo.get_job(job_id)
            assert updated_job is not None
            assert updated_job.status == "waiting_for_capture"
            assert updated_job.current_stage == "waiting_for_capture"
            assert updated_job.capture_wait_deadline is not None
            assert any(event.state == "waiting_for_capture" for event in repo.list_events(job_id))

        assert torn_down == []


class TestPostScriptCancellation:
    def test_run_post_script_job_cancels_during_script_and_removes_temp_script(
        self,
        in_memory_session_scope,
        monkeypatch,
        tmp_path,
    ):
        import astinuslab.db as db_mod
        from astinuslab.api.schemas import JobCreate
        from astinuslab.core.config import settings
        from astinuslab.db.repositories import JobRepository
        from astinuslab.jobs.job_services import create_job_service
        from astinuslab.orchestration.deps import JobRunDeps
        from astinuslab.orchestration.runners.boot_capture_runner import run_post_script_job

        deps = JobRunDeps.default()
        monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)
        deps.session_scope = in_memory_session_scope
        deps.check_host_disk_space = lambda job_id: None

        def fake_run(cmd, **kwargs):
            if "run_guest_script.sh" in " ".join(cmd):
                raise RuntimeError("cancel_requested")
            return ""

        deps.run_command = fake_run

        torn_down: list[str] = []
        deps.teardown_vm = lambda job_id: torn_down.append(job_id)

        custom_script = "#!/usr/bin/env bash\necho test\n"

        with in_memory_session_scope() as session:
            job = create_job_service(
                session,
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="workload-sample-post-script",
                    execution_mode="post_script",
                    custom_script=custom_script,
                ),
            )
            repo = JobRepository(session)
            repo.update_job(
                job.id,
                image_path=str((settings.images_dir / f"{job.id}-target.qcow2").resolve()),
            )
            job_id = job.id

        def _fake_boot_and_prepare(*a, **k):
            with in_memory_session_scope() as s:
                r = JobRepository(s)
                j = r.get_job(job_id)
                assert j is not None
                j.status = "mounting_output_share"
                j.current_stage = "mounting_output_share"
                s.flush()
            return ("192.168.122.10", {"kernel_release": "5.15.0-test"})

        deps.boot_and_prepare = _fake_boot_and_prepare
        run_post_script_job(job_id, deps=deps)

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            updated_job = repo.get_job(job_id)
            assert updated_job is not None
            assert updated_job.status == "cancelled"
            assert updated_job.current_stage == "cancelled"
            assert any(event.state == "cancelled" for event in repo.list_events(job_id))

        assert torn_down == [job_id]
        assert not (settings.base_dir / "storage" / "tmp" / f"{job_id}-custom.sh").exists()

    def test_run_post_script_job_cancelled_cleanup_survives_teardown_failure(
        self,
        in_memory_session_scope,
        monkeypatch,
        tmp_path,
    ):
        import astinuslab.db as db_mod
        from astinuslab.core.config import settings  # noqa: F401
        from astinuslab.orchestration.deps import JobRunDeps
        from astinuslab.orchestration.runners.boot_capture_runner import (  # noqa: F401
            acquire_memory_dump,
            run_post_script_job,
        )

        deps = JobRunDeps.default()
        from astinuslab.api.schemas import JobCreate
        from astinuslab.db.repositories import JobRepository
        from astinuslab.jobs.job_services import create_job_service

        monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)
        deps.session_scope = in_memory_session_scope
        deps.check_host_disk_space = lambda job_id: None

        def fake_run(cmd, **kwargs):
            if "run_guest_script.sh" in " ".join(cmd):
                raise RuntimeError("cancel_requested")
            return ""

        deps.run_command = fake_run
        deps.teardown_vm = lambda job_id: (_ for _ in ()).throw(RuntimeError("teardown failed"))

        with in_memory_session_scope() as session:
            job = create_job_service(
                session,
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="workload-sample-post-script",
                    execution_mode="post_script",
                    custom_script="#!/usr/bin/env bash\necho test\n",
                ),
            )
            repo = JobRepository(session)
            repo.update_job(
                job.id,
                image_path=str((settings.images_dir / f"{job.id}-target.qcow2").resolve()),
            )
            job_id = job.id

        def _fake_boot_and_prepare(*a, **k):
            with in_memory_session_scope() as s:
                r = JobRepository(s)
                j = r.get_job(job_id)
                assert j is not None
                j.status = "mounting_output_share"
                j.current_stage = "mounting_output_share"
                s.flush()
            return ("192.168.122.10", {"kernel_release": "5.15.0-test"})

        deps.boot_and_prepare = _fake_boot_and_prepare
        run_post_script_job(job_id, deps=deps)

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            updated_job = repo.get_job(job_id)
            assert updated_job is not None
            assert updated_job.status == "cancelled"
            assert updated_job.current_stage == "cancelled"


class TestArtifactPublicationFailures:
    def test_acquire_memory_dump_fails_when_hashing_fails_after_publish_stage(
        self,
        in_memory_session_scope,
        monkeypatch,
        tmp_path,
    ):
        import astinuslab.db as db_mod
        from astinuslab.core.config import settings  # noqa: F401
        from astinuslab.orchestration.deps import JobRunDeps
        from astinuslab.orchestration.runners.boot_capture_runner import (  # noqa: F401
            acquire_memory_dump,
            run_post_script_job,
        )

        deps = JobRunDeps.default()
        from astinuslab.api.schemas import JobCreate
        from astinuslab.db.repositories import JobRepository
        from astinuslab.jobs.job_services import create_job_service

        monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)
        deps.session_scope = in_memory_session_scope
        deps.get_active_key_paths = lambda: (tmp_path / "id", tmp_path / "id.pub")
        conn = MagicMock()
        conn.lookupByName.return_value.isActive.return_value = 1
        deps.open_libvirt_connection = lambda: conn
        deps.teardown_vm = MagicMock()
        deps.check_host_disk_space = lambda *a, **k: None

        import astinuslab.forensics.memory_dump as md_mod

        class _FakeDumper:
            def __init__(self, *a, **k):
                pass

            def dump(self, conn, vm_name, output_path, **kwargs):
                output_path.write_bytes(b"\x7fELF" + b"A" * 100)

            def validate(self, *a, **k):
                pass

        monkeypatch.setattr(md_mod, "MemoryDumper", _FakeDumper)

        dump_file = tmp_path / "memory.raw.gz"
        dump_file.write_bytes(b"memory-bytes")

        deps.run_command = lambda cmd, **kwargs: ""

        # Task 20 moved hashing into the publication service, so the failure this
        # test guards is no longer a `sha256sum` subprocess: it is the service
        # failing to hash the staged payload. The property under test is
        # unchanged - a failure after the publish-stage transition must fail the
        # job with a diagnosable summary.
        from astinuslab.storage import publication as publication_mod

        def refuse_to_hash(fd, path):
            raise OSError("payload read failed")

        monkeypatch.setattr(publication_mod, "_hash_payload", refuse_to_hash)

        with in_memory_session_scope() as session:
            job = create_job_service(
                session,
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                ),
            )
            repo = JobRepository(session)
            repo.update_job(
                job.id,
                guest_ip="192.168.122.10",
                detected_kernel_release="5.15.0-test",
                image_path=str((settings.images_dir / f"{job.id}-target.qcow2").resolve()),
            )
            # Seed to valid pre-acquisition state (state machine now validates transitions)
            job.status = "waiting_for_capture"
            job.current_stage = "waiting_for_capture"
            session.flush()
            action = repo.enqueue_action(job.id, "acquire_memory_dump")
            job_id = job.id
            action_id = action.id

        acquire_memory_dump(job_id, action_id=action_id, deps=deps)

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            updated_job = repo.get_job(job_id)
            updated_action = repo.get_action(action_id)
            events = repo.list_events(job_id)

            assert updated_job is not None
            assert updated_job.status == "failed"
            assert updated_job.current_stage == "failed"
            assert updated_job.error_summary is not None
            assert "payload read failed" in updated_job.error_summary

            assert updated_action is not None
            assert updated_action.status == "failed"
            assert updated_action.error_summary is not None
            assert "payload read failed" in updated_action.error_summary

            assert any(event.state == "publishing_artifact" for event in events)
            assert any(event.state == "failed" for event in events)

    def test_acquire_memory_dump_rejects_zero_length_published_artifact(
        self,
        in_memory_session_scope,
        monkeypatch,
        tmp_path,
    ):
        import astinuslab.db as db_mod
        from astinuslab.core.config import settings  # noqa: F401
        from astinuslab.orchestration.deps import JobRunDeps
        from astinuslab.orchestration.runners.boot_capture_runner import (  # noqa: F401
            acquire_memory_dump,
            run_post_script_job,
        )

        deps = JobRunDeps.default()
        from astinuslab.api.schemas import JobCreate
        from astinuslab.db.repositories import JobRepository
        from astinuslab.jobs.job_services import create_job_service

        monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)
        deps.session_scope = in_memory_session_scope
        deps.get_active_key_paths = lambda: (tmp_path / "id", tmp_path / "id.pub")
        conn = MagicMock()
        conn.lookupByName.return_value.isActive.return_value = 1
        deps.open_libvirt_connection = lambda: conn
        deps.teardown_vm = MagicMock()
        deps.check_host_disk_space = lambda *a, **k: None

        import astinuslab.forensics.memory_dump as md_mod

        class _FakeDumper:
            def __init__(self, *a, **k):
                pass

            def dump(self, conn, vm_name, output_path, **kwargs):
                output_path.write_bytes(b"")  # Create empty file

            def validate(self, output_path):
                if output_path.stat().st_size == 0:
                    raise RuntimeError(f"Artifact rejected: zero-length or missing dump: {output_path}")

        monkeypatch.setattr(md_mod, "MemoryDumper", _FakeDumper)

        def fake_run(cmd, **kwargs):
            return ""

        deps.run_command = fake_run

        with in_memory_session_scope() as session:
            job = create_job_service(
                session,
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                ),
            )
            repo = JobRepository(session)
            repo.update_job(
                job.id,
                guest_ip="192.168.122.10",
                detected_kernel_release="5.15.0-test",
                image_path=str((settings.images_dir / f"{job.id}-target.qcow2").resolve()),
            )
            # Seed to valid pre-acquisition state (state machine now validates transitions)
            job.status = "waiting_for_capture"
            job.current_stage = "waiting_for_capture"
            session.flush()
            action = repo.enqueue_action(job.id, "acquire_memory_dump")
            job_id = job.id
            action_id = action.id

        acquire_memory_dump(job_id, action_id=action_id, deps=deps)

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            updated_job = repo.get_job(job_id)
            updated_action = repo.get_action(action_id)

            assert updated_job is not None
            assert updated_job.status == "failed"
            assert updated_job.current_stage == "failed"
            assert updated_job.error_summary is not None
            assert "Artifact rejected: zero-length or missing dump" in updated_job.error_summary

            assert updated_action is not None
            assert updated_action.status == "failed"
            assert updated_action.error_summary is not None
            assert "Artifact rejected: zero-length or missing dump" in updated_action.error_summary

    def test_acquire_memory_dump_honors_cancellation_after_publish_stage_before_completion(
        self,
        in_memory_session_scope,
        monkeypatch,
        tmp_path,
    ):
        import astinuslab.db as db_mod
        from astinuslab.core.config import settings  # noqa: F401
        from astinuslab.orchestration.deps import JobRunDeps
        from astinuslab.orchestration.runners.boot_capture_runner import (  # noqa: F401
            acquire_memory_dump,
            run_post_script_job,
        )

        deps = JobRunDeps.default()
        from astinuslab.api.schemas import JobCreate
        from astinuslab.db.repositories import JobRepository
        from astinuslab.jobs.job_services import create_job_service

        monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)
        deps.session_scope = in_memory_session_scope
        deps.get_active_key_paths = lambda: (tmp_path / "id", tmp_path / "id.pub")
        conn = MagicMock()
        conn.lookupByName.return_value.isActive.return_value = 1
        deps.open_libvirt_connection = lambda: conn
        deps.teardown_vm = MagicMock()
        deps.check_host_disk_space = lambda *a, **k: None

        import astinuslab.forensics.memory_dump as md_mod

        class _FakeDumper:
            def __init__(self, *a, **k):
                pass

            def dump(self, conn, vm_name, output_path, **kwargs):
                output_path.write_bytes(b"\x7fELF" + b"A" * 100)

            def validate(self, *a, **k):
                pass

        monkeypatch.setattr(md_mod, "MemoryDumper", _FakeDumper)

        dump_file = tmp_path / "late-cancel.raw.gz"
        dump_file.write_bytes(b"memory-bytes")

        cancel_checks: list[bool] = []

        def fake_is_cancel_requested(job_id: str) -> bool:
            cancel_checks.append(True)
            return len(cancel_checks) >= 2

        def fake_run(cmd, **kwargs):
            if "sha256sum" in " ".join(cmd):
                return "deadbeef  memory.dump"
            return ""

        deps.is_cancel_requested = fake_is_cancel_requested
        deps.run_command = fake_run

        with in_memory_session_scope() as session:
            job = create_job_service(
                session,
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                ),
            )
            repo = JobRepository(session)
            repo.update_job(
                job.id,
                guest_ip="192.168.122.10",
                detected_kernel_release="5.15.0-test",
                image_path=str((settings.images_dir / f"{job.id}-target.qcow2").resolve()),
            )
            # Seed to valid pre-acquisition state (state machine now validates transitions)
            job.status = "waiting_for_capture"
            job.current_stage = "waiting_for_capture"
            session.flush()
            action = repo.enqueue_action(job.id, "acquire_memory_dump")
            job_id = job.id
            action_id = action.id

        acquire_memory_dump(job_id, action_id=action_id, deps=deps)

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            updated_job = repo.get_job(job_id)
            updated_action = repo.get_action(action_id)
            events = repo.list_events(job_id)

            assert updated_job is not None
            assert updated_job.status == "cancelled"
            assert updated_job.current_stage == "cancelled"
            # Artifact metadata is canonical at capture time: the memory dump
            # completed and was recorded before the cancellation landed, so it
            # remains visible on the cancelled job.
            assert updated_job.dump_path is not None
            assert updated_job.dump_checksum == hashlib.sha256(b"\x7fELF" + b"A" * 100).hexdigest()

            assert updated_action is not None
            assert updated_action.status == "cancelled"

            assert any(event.state == "publishing_artifact" for event in events)
            assert any(event.state == "cancelled" for event in events)

    def test_acquire_disk_dump_fails_when_disk_hashing_fails_after_publish_stage(
        self,
        in_memory_session_scope,
        monkeypatch,
        tmp_path,
    ):
        import astinuslab.db as db_mod
        from astinuslab.core.config import settings  # noqa: F401
        from astinuslab.orchestration.deps import JobRunDeps
        from astinuslab.orchestration.runners.boot_capture_runner import (  # noqa: F401
            acquire_memory_dump,
            run_post_script_job,
        )

        deps = JobRunDeps.default()
        from astinuslab.api.schemas import JobCreate
        from astinuslab.db.repositories import JobRepository
        from astinuslab.jobs.job_services import create_job_service

        monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)
        deps.session_scope = in_memory_session_scope
        deps.get_active_key_paths = lambda: (tmp_path / "id", tmp_path / "id.pub")
        conn = MagicMock()
        conn.lookupByName.return_value.isActive.return_value = 1
        deps.open_libvirt_connection = lambda: conn
        deps.teardown_vm = MagicMock()
        deps.check_host_disk_space = lambda *a, **k: None

        import astinuslab.forensics.memory_dump as md_mod

        class _FakeDumper:
            def __init__(self, *a, **k):
                pass

            def dump(self, conn, vm_name, output_path, **kwargs):
                output_path.write_bytes(b"\x7fELF" + b"A" * 100)

            def validate(self, *a, **k):
                pass

        monkeypatch.setattr(md_mod, "MemoryDumper", _FakeDumper)

        disk_file = tmp_path / "disk.img.gz"
        disk_file.write_bytes(b"disk-bytes")

        def fake_run(cmd, **kwargs):
            joined = " ".join(cmd)
            if "acquire_disk_remote.sh" in joined:
                return f"disk_publish_stage=completed\ndisk_final_path={disk_file}\ndisk_filename=disk.img.gz\n"
            return ""

        deps.run_command = fake_run

        # Task 20 moved disk hashing into the publication service, which adopts
        # the file the acquisition script wrote. The guarded property is
        # unchanged: a failure after the publish-stage transition fails the job
        # with a diagnosable summary.
        from astinuslab.storage import publication as publication_mod

        def refuse_to_hash(fd, path):
            raise OSError("disk payload read failed")

        monkeypatch.setattr(publication_mod, "_hash_payload", refuse_to_hash)

        with in_memory_session_scope() as session:
            job = create_job_service(
                session,
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="lab-session",
                    execution_mode="manual_trigger",
                    capture_type="disk",
                ),
            )
            repo = JobRepository(session)
            repo.update_job(
                job.id,
                guest_ip="192.168.122.10",
                detected_kernel_release="5.15.0-test",
                image_path=str((settings.images_dir / f"{job.id}-target.qcow2").resolve()),
            )
            # Seed to valid pre-acquisition state (state machine now validates transitions)
            job.status = "waiting_for_capture"
            job.current_stage = "waiting_for_capture"
            session.flush()
            action = repo.enqueue_action(job.id, "acquire_memory_dump")
            job_id = job.id
            action_id = action.id

        acquire_memory_dump(job_id, action_id=action_id, deps=deps)

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            updated_job = repo.get_job(job_id)
            updated_action = repo.get_action(action_id)
            events = repo.list_events(job_id)

            assert updated_job is not None
            assert updated_job.status == "failed"
            assert updated_job.current_stage == "failed"
            assert updated_job.error_summary is not None
            assert "disk payload read failed" in updated_job.error_summary

            assert updated_action is not None
            assert updated_action.status == "failed"
            assert updated_action.error_summary is not None
            assert "disk payload read failed" in updated_action.error_summary

            assert any(event.state == "publishing_artifact" for event in events)
            assert any(event.state == "failed" for event in events)

    def test_acquire_disk_dump_rejects_zero_length_published_artifact(
        self,
        in_memory_session_scope,
        monkeypatch,
        tmp_path,
    ):
        import astinuslab.db as db_mod
        from astinuslab.core.config import settings  # noqa: F401
        from astinuslab.orchestration.deps import JobRunDeps
        from astinuslab.orchestration.runners.boot_capture_runner import (  # noqa: F401
            acquire_memory_dump,
            run_post_script_job,
        )

        deps = JobRunDeps.default()
        from astinuslab.api.schemas import JobCreate
        from astinuslab.db.repositories import JobRepository
        from astinuslab.jobs.job_services import create_job_service

        monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)
        deps.session_scope = in_memory_session_scope
        deps.get_active_key_paths = lambda: (tmp_path / "id", tmp_path / "id.pub")
        conn = MagicMock()
        conn.lookupByName.return_value.isActive.return_value = 1
        deps.open_libvirt_connection = lambda: conn
        deps.teardown_vm = MagicMock()
        deps.check_host_disk_space = lambda *a, **k: None

        import astinuslab.forensics.memory_dump as md_mod

        class _FakeDumper:
            def __init__(self, *a, **k):
                pass

            def dump(self, conn, vm_name, output_path, **kwargs):
                output_path.write_bytes(b"\x7fELF" + b"A" * 100)

            def validate(self, *a, **k):
                pass

        monkeypatch.setattr(md_mod, "MemoryDumper", _FakeDumper)

        disk_file = tmp_path / "zero-disk.img.gz"
        disk_file.write_bytes(b"")

        def fake_run(cmd, **kwargs):
            if "acquire_disk_remote.sh" in " ".join(cmd):
                return f"disk_publish_stage=completed\ndisk_final_path={disk_file}\ndisk_filename=zero-disk.img.gz\n"
            return ""

        deps.run_command = fake_run

        with in_memory_session_scope() as session:
            job = create_job_service(
                session,
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="lab-session",
                    execution_mode="manual_trigger",
                    capture_type="disk",
                ),
            )
            repo = JobRepository(session)
            repo.update_job(
                job.id,
                guest_ip="192.168.122.10",
                detected_kernel_release="5.15.0-test",
                image_path=str((settings.images_dir / f"{job.id}-target.qcow2").resolve()),
            )
            # Seed to valid pre-acquisition state (state machine now validates transitions)
            job.status = "waiting_for_capture"
            job.current_stage = "waiting_for_capture"
            session.flush()
            action = repo.enqueue_action(job.id, "acquire_memory_dump")
            job_id = job.id
            action_id = action.id

        acquire_memory_dump(job_id, action_id=action_id, deps=deps)

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            updated_job = repo.get_job(job_id)
            updated_action = repo.get_action(action_id)

            assert updated_job is not None
            assert updated_job.status == "failed"
            assert updated_job.current_stage == "failed"
            assert updated_job.error_summary is not None
            assert "Disk artifact rejected: zero-length dump" in updated_job.error_summary

            assert updated_action is not None
            assert updated_action.status == "failed"
            assert updated_action.error_summary is not None
            assert "Disk artifact rejected: zero-length dump" in updated_action.error_summary

    def test_acquire_memory_dump_with_both_capture_fails_on_disk_stage_after_memory_publish(
        self,
        in_memory_session_scope,
        monkeypatch,
        tmp_path,
    ):
        import astinuslab.db as db_mod
        from astinuslab.core.config import settings  # noqa: F401
        from astinuslab.orchestration.deps import JobRunDeps
        from astinuslab.orchestration.runners.boot_capture_runner import (  # noqa: F401
            acquire_memory_dump,
            run_post_script_job,
        )

        deps = JobRunDeps.default()
        from astinuslab.api.schemas import JobCreate
        from astinuslab.db.repositories import JobRepository
        from astinuslab.jobs.job_services import create_job_service

        monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)
        deps.session_scope = in_memory_session_scope
        deps.get_active_key_paths = lambda: (tmp_path / "id", tmp_path / "id.pub")
        conn = MagicMock()
        conn.lookupByName.return_value.isActive.return_value = 1
        deps.open_libvirt_connection = lambda: conn
        deps.teardown_vm = MagicMock()
        deps.check_host_disk_space = lambda *a, **k: None

        import astinuslab.forensics.memory_dump as md_mod

        class _FakeDumper:
            def __init__(self, *a, **k):
                pass

            def dump(self, conn, vm_name, output_path, **kwargs):
                output_path.write_bytes(b"\x7fELF" + b"A" * 100)

            def validate(self, *a, **k):
                pass

        monkeypatch.setattr(md_mod, "MemoryDumper", _FakeDumper)

        memory_file = tmp_path / "memory-both.raw.gz"
        memory_file.write_bytes(b"memory-bytes")
        disk_file = tmp_path / "disk-both.img.gz"
        disk_file.write_bytes(b"disk-bytes")

        def fake_run(cmd, **kwargs):
            joined = " ".join(str(c) for c in cmd)
            if "acquire_disk_remote.sh" in joined:
                return f"disk_publish_stage=completed\ndisk_final_path={disk_file}\ndisk_filename=disk-both.img.gz\n"
            return ""

        deps.run_command = fake_run

        # The disk stage now fails inside the publication service: the attempt
        # adopts the script's file, and adoption of the disk artifact is refused.
        from astinuslab.storage import publication as publication_mod

        real_adopt = publication_mod.PublicationAttempt.adopt

        def adopt_refusing_disk(self, existing_path, manifest_builder):
            if str(existing_path).endswith(".img.gz"):
                raise OSError("disk stage failed")
            return real_adopt(self, existing_path, manifest_builder)

        monkeypatch.setattr(publication_mod.PublicationAttempt, "adopt", adopt_refusing_disk)

        with in_memory_session_scope() as session:
            job = create_job_service(
                session,
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="lab-session",
                    execution_mode="manual_trigger",
                    capture_type="both",
                ),
            )
            repo = JobRepository(session)
            repo.update_job(
                job.id,
                guest_ip="192.168.122.10",
                detected_kernel_release="5.15.0-test",
                image_path=str((settings.images_dir / f"{job.id}-target.qcow2").resolve()),
            )
            # Seed to valid pre-acquisition state (state machine now validates transitions)
            job.status = "waiting_for_capture"
            job.current_stage = "waiting_for_capture"
            session.flush()
            action = repo.enqueue_action(job.id, "acquire_memory_dump")
            job_id = job.id
            action_id = action.id

        acquire_memory_dump(job_id, action_id=action_id, deps=deps)

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            updated_job = repo.get_job(job_id)
            updated_action = repo.get_action(action_id)
            events = repo.list_events(job_id)

            assert updated_job is not None
            assert updated_job.status == "failed"
            assert updated_job.current_stage == "failed"
            assert updated_job.error_summary is not None
            assert "disk stage failed" in updated_job.error_summary
            assert "disk stage failed" in updated_job.error_summary
            # Artifact metadata is canonical at capture time: the memory stage
            # succeeded and was recorded before the disk stage failed, so the
            # memory dump remains visible on the failed job.
            assert updated_job.dump_path is not None
            assert updated_job.dump_checksum is not None

            assert updated_action is not None
            assert updated_action.status == "failed"
            assert updated_action.error_summary is not None
            assert "disk stage failed" in updated_action.error_summary

            # memory stage emits acquiring_memory event; disk stage emits publishing_artifact
            assert any(e.state == "acquiring_memory" and "written to host storage" in e.message for e in events)
            assert any(event.state == "publishing_artifact" for event in events)
            assert any(event.state == "failed" for event in events)


# ── Workload health check ──────────────────────────────────────────────────────


class TestWorkloadHealthCheck:
    def test_health_check_passes_on_200(self, monkeypatch):
        from contextlib import contextmanager

        from astinuslab.core.config import settings  # noqa: F401
        from astinuslab.orchestration.deps import JobRunDeps
        from astinuslab.orchestration.runners.boot_capture_runner import (  # noqa: F401
            acquire_memory_dump,
            run_post_script_job,
        )

        deps = JobRunDeps.default()

        events = []

        class _FakeRepo:
            def add_event(self, job_id, state, msg):
                events.append(msg)

        @contextmanager
        def _fake_scope():
            yield None

        deps.session_scope = _fake_scope
        deps.job_repository_cls = _repo_factory(_FakeRepo())
        deps.is_cancel_requested = lambda job_id: False

        class _FakeResp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        import urllib.request as _req

        autospec_setattr(monkeypatch, _req, "urlopen", lambda url, timeout=None: _FakeResp())

        workload = {
            "display_name": WORDPRESS_NAME,
            "health_check": {"port": 80, "url_path": "/wp-login.php", "expected_status": 200, "timeout_seconds": 10},
        }
        # Should not raise
        deps.check_workload_health("job-test", workload, "192.168.122.10")
        assert any("Health check passed" in e for e in events)

    def test_health_check_raises_on_timeout(self, monkeypatch):
        from contextlib import contextmanager

        import pytest

        from astinuslab.core.config import settings  # noqa: F401
        from astinuslab.orchestration.deps import JobRunDeps
        from astinuslab.orchestration.runners.boot_capture_runner import (  # noqa: F401
            acquire_memory_dump,
            run_post_script_job,
        )

        deps = JobRunDeps.default()

        @contextmanager
        def _fake_scope():
            yield None

        deps.session_scope = _fake_scope
        deps.job_repository_cls = _repo_factory(type("R", (), {"add_event": lambda *a, **k: None})())
        deps.is_cancel_requested = lambda job_id: False

        import urllib.request as _req

        autospec_setattr(
            monkeypatch,
            _req,
            "urlopen",
            lambda url, timeout=None: (_ for _ in ()).throw(ConnectionRefusedError("refused")),
        )

        workload = {
            "display_name": "Test",
            "health_check": {"port": 80, "url_path": "/", "expected_status": 200, "timeout_seconds": 1},
        }
        with pytest.raises(RuntimeError, match="health check timed out"):
            deps.check_workload_health("job-test", workload, "192.168.122.10")

    def test_health_check_skipped_when_no_config(self, monkeypatch):
        from astinuslab.core.config import settings  # noqa: F401
        from astinuslab.orchestration.deps import JobRunDeps
        from astinuslab.orchestration.runners.boot_capture_runner import (  # noqa: F401
            acquire_memory_dump,
            run_post_script_job,
        )

        deps = JobRunDeps.default()

        # Should complete without calling urlopen at all
        called = []
        import urllib.request as _req

        autospec_setattr(monkeypatch, _req, "urlopen", lambda *a, **kw: called.append(True))

        workload = {"display_name": "NoCheck", "health_check": {}}
        deps.check_workload_health("job-test", workload, "192.168.122.10")
        assert not called


# ── Captures table and workload badge ─────────────────────────────────────────


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

    def test_jobs_surfaces_hide_playground_sessions(self, client, created_job, in_memory_session_scope):
        from astinuslab.db.repositories import JobRepository
        from astinuslab.jobs.capacity import confirm_cleanup
        from astinuslab.playground import repositories as pr

        with in_memory_session_scope() as session:
            JobRepository(session).update_job(created_job["id"], status="completed")
            assert confirm_cleanup(session, created_job["id"], never_started=True)
            pr.create_playground_session(session, job_id="pg-active")

        api = client.get("/api/v1/jobs")
        index = client.get("/jobs")
        fragment = client.get("/jobs-table-fragment")

        assert api.status_code == 200
        assert [job["id"] for job in api.json()] == [created_job["id"]]
        assert created_job["id"][:8].encode() in index.content
        assert b"pg-active" not in index.content
        assert created_job["id"][:8].encode() in fragment.content
        assert b"pg-active" not in fragment.content

    def test_workload_badge_has_green_style_on_job_detail(self, client, lab_job):
        r = client.get(f"/jobs/{lab_job['id']}")
        assert b"wiz-badge" in r.content
