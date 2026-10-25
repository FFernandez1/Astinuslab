"""Tests for M7: optional attacker VM in lab sessions."""

from __future__ import annotations

from typing import cast

WORDPRESS_ID = "wordpress-6.4"


def _repo_factory(repo) -> type:
    return cast(type, lambda _session: repo)


# ── Catalog ────────────────────────────────────────────────────────────────────


class TestAttackerVMCatalog:
    def test_single_workload_attacker_vm_none_by_default(self):
        from astinuslab.catalog import workload_by_id

        wp = workload_by_id(WORDPRESS_ID)
        assert wp is not None
        assert wp["attacker_vm"] is None

    def test_stack_attacker_vm_none_when_not_configured(self):
        from astinuslab.catalog import workload_by_id

        stack = workload_by_id("full-php-stack")
        assert stack is not None
        # The example YAML has attacker_vm commented out, so it should be None
        assert stack["attacker_vm"] is None

    def test_attacker_vm_key_present_on_all_workloads(self):
        from astinuslab.catalog import load_workloads

        for w in load_workloads():
            assert "attacker_vm" in w, f"'attacker_vm' key missing from workload {w['id']}"

    def test_attacker_vm_parsed_when_set(self):
        """If attacker_vm is set in YAML, it is surfaced in the workload dict."""
        from astinuslab.catalog import _parse_single_workload

        fake_item = {
            "id": "test-wl",
            "attacker_vm": {
                "image_ref": "kali.qcow2",
                "username": "kali",
                "os_variant": "generic",
            },
        }
        result = _parse_single_workload(fake_item)
        assert result["attacker_vm"] is not None
        assert result["attacker_vm"]["image_ref"] == "kali.qcow2"
        assert result["attacker_vm"]["username"] == "kali"


# ── API ────────────────────────────────────────────────────────────────────────


class TestAttackerVMAPI:
    def test_job_out_has_attacker_fields(self, client):
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
        body = r.json()
        assert "attacker_vm_name" in body
        assert "attacker_ip" in body
        assert body["attacker_vm_name"] is None
        assert body["attacker_ip"] is None


# ── Orchestrator ──────────────────────────────────────────────────────────────


class TestAttackerVMOrchestrator:
    def test_boot_simple_vm_runs_correct_script(self, client, monkeypatch):
        """boot_simple_vm calls the lightweight (no virtiofs) boot script."""
        from pathlib import Path

        from astinuslab.orchestration.deps import JobRunDeps

        cmds: list[list[str]] = []

        def fake_run_command(cmd, **k):
            cmds.append(cmd)
            return ""

        deps = JobRunDeps.default()
        deps.run_command = fake_run_command
        deps.parse_guest_ip = lambda vid, **k: "10.0.0.9"
        deps.wait_for_guest_ssh = lambda *a, **k: None
        deps.get_active_key_paths = lambda: (Path("/fake/key"), Path("/fake/key.pub"))

        import contextlib
        from unittest.mock import MagicMock

        fake_repo = MagicMock()

        @contextlib.contextmanager
        def fake_scope():
            yield MagicMock()

        deps.session_scope = fake_scope
        deps.job_repository_cls = _repo_factory(fake_repo)

        job_id = _make_job(client, WORDPRESS_ID)
        ip = deps.boot_simple_vm(job_id, "attacker", Path("/img/kali.qcow2"), "generic", "kali")

        assert ip == "10.0.0.9"
        script_cmds = [c for c in cmds if "bash" in c]
        assert any("rebuild_cloud_image_libvirt_storage.sh" in " ".join(c) for c in script_cmds), (
            "Expected lightweight (no virtiofs) boot script"
        )
        assert not any("virtiofs" in " ".join(c) for c in script_cmds), "Attacker VM should NOT use virtiofs script"

    def test_teardown_attacker_vm_delegates_to_teardown_vm(self):
        from astinuslab.orchestration.deps import JobRunDeps

        deps = JobRunDeps.default()
        torn_down: list[str] = []
        deps.teardown_vm = lambda vid: torn_down.append(vid)

        deps.teardown_attacker_vm("job-abc")
        assert torn_down == ["job-abc-attacker"]

    def test_boot_failure_tears_down_target_without_attacker_teardown(self, client):
        from astinuslab.live_session.runner import run_live_session_job
        from astinuslab.orchestration.deps import JobRunDeps

        deps = JobRunDeps.default()
        torn_down: list[str] = []
        deps.teardown_vm = lambda vid: torn_down.append(vid)
        deps.teardown_attacker_vm = lambda jid: torn_down.append(f"{jid}-attacker")
        deps.boot_and_prepare = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        deps.load_workload_id = lambda jid: None

        job_id = _make_job(client, WORDPRESS_ID)
        # _attacker_booted starts False, so teardown_attacker_vm should NOT be called
        # (boot never happened), but teardown_vm should be called.
        run_live_session_job(job_id, deps=deps)

        assert torn_down == [job_id]
        r = client.get(f"/api/v1/jobs/{job_id}")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "failed"

    def test_no_attacker_teardown_when_not_booted(self, client):
        """teardown_attacker_vm is NOT called when no attacker VM was booted."""
        from astinuslab.live_session.runner import run_live_session_job
        from astinuslab.orchestration.deps import JobRunDeps

        deps = JobRunDeps.default()
        attacker_teardown_calls: list = []
        deps.teardown_attacker_vm = lambda jid: attacker_teardown_calls.append(jid)
        deps.teardown_vm = lambda vid: None
        deps.boot_and_prepare = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("cancel_requested"))
        deps.load_workload_id = lambda jid: None

        job_id = _make_job(client, WORDPRESS_ID)
        run_live_session_job(job_id, deps=deps)

        assert attacker_teardown_calls == []

    def test_apply_isolation_with_attacker_invokes_helper_with_attacker_ip(self):
        """Per F01: workload_support delegates to network_isolation_admin.sh.
        With an attacker_ip the apply subcommand receives 3 positional args."""
        from astinuslab.orchestration.deps import JobRunDeps

        deps = JobRunDeps.default()
        cmds: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            cmds.append(cmd)
            return ""

        deps.run_command = fake_run

        # Patch session_scope to avoid DB
        import contextlib
        from unittest.mock import MagicMock

        fake_session = MagicMock()
        fake_repo = MagicMock()
        fake_repo.update_job = MagicMock()
        fake_repo.add_event = MagicMock()
        fake_session.__enter__ = lambda s: s
        fake_session.__exit__ = MagicMock(return_value=False)

        @contextlib.contextmanager
        def fake_scope():
            yield fake_session

        deps.session_scope = fake_scope
        deps.job_repository_cls = _repo_factory(fake_repo)

        result = deps.apply_network_isolation("job123", "10.0.0.5", attacker_ip="10.0.0.6")

        assert result is True
        helper_calls = [c for c in cmds if "network_isolation_admin.sh" in str(c)]
        assert helper_calls, cmds
        cmd = helper_calls[0]
        assert cmd[0:2] == ["sudo", "-n"]
        assert cmd[3:] == ["apply", "job123", "10.0.0.5", "10.0.0.6"]

    def test_remove_isolation_with_attacker_invokes_helper_with_attacker_ip(self):
        """Per F01: removal also delegates to the helper, with the same arg shape."""
        from astinuslab.orchestration.deps import JobRunDeps

        deps = JobRunDeps.default()
        deleted: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            deleted.append(cmd)
            return ""

        deps.run_command = fake_run

        import contextlib
        from unittest.mock import MagicMock

        fake_session = MagicMock()
        fake_repo = MagicMock()

        @contextlib.contextmanager
        def fake_scope():
            yield fake_session

        deps.session_scope = fake_scope
        deps.job_repository_cls = _repo_factory(fake_repo)

        deps.remove_network_isolation("job123", "10.0.0.5", attacker_ip="10.0.0.6")

        helper_calls = [c for c in deleted if "network_isolation_admin.sh" in str(c)]
        assert helper_calls, deleted
        cmd = helper_calls[0]
        assert cmd[0:2] == ["sudo", "-n"]
        assert cmd[3:] == ["remove", "job123", "10.0.0.5", "10.0.0.6"]


# ── helpers ────────────────────────────────────────────────────────────────────


def _make_job(client, workload_id: str) -> str:
    r = client.post(
        "/api/v1/jobs",
        json={
            "target_id": "ubuntu-22-04-default",
            "scenario_id": "lab-session",
            "execution_mode": "manual_trigger",
            "workload_id": workload_id,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]
