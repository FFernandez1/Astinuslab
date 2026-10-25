"""Tests for M3: network isolation — iptables FORWARD DROP for lab workload VMs."""

from __future__ import annotations

WORDPRESS_ID = "wordpress-6.4"

import subprocess

from tests.python._support.helpers import autospec_setattr

# ── Catalog ────────────────────────────────────────────────────────────────────


class TestWorkloadCatalogIsolation:
    def test_wordpress_has_network_isolation_flag(self):
        from astinuslab.catalog import load_workloads

        workloads = {w["id"]: w for w in load_workloads()}
        assert "network_isolation" in workloads[WORDPRESS_ID]

    def test_wordpress_network_isolation_is_true(self):
        from astinuslab.catalog import load_workloads

        workloads = {w["id"]: w for w in load_workloads()}
        assert workloads[WORDPRESS_ID]["network_isolation"] is True

    def test_workload_without_flag_defaults_false(self, monkeypatch):
        from astinuslab import catalog

        raw = {"workloads": [{"id": "test", "display_name": "Test", "enabled": True}]}
        autospec_setattr(monkeypatch, catalog, "_load_yaml", lambda _: raw)
        workloads = catalog.load_workloads()
        assert workloads[0]["network_isolation"] is False


# ── Model / Schema ─────────────────────────────────────────────────────────────


class TestNetworkIsolatedField:
    def test_job_out_has_network_isolated_field(self, client):
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
        assert "network_isolated" in r.json()

    def test_new_job_network_isolated_is_none(self, client):
        r = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "lab-session",
                "execution_mode": "manual_trigger",
                "workload_id": WORDPRESS_ID,
            },
        )
        assert r.json()["network_isolated"] is None


# ── Orchestrator helpers ───────────────────────────────────────────────────────


class TestApplyNetworkIsolation:
    def test_apply_sets_db_flag_on_success(self, client, monkeypatch):
        from astinuslab.orchestration.deps import JobRunDeps

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return ""

        deps = JobRunDeps.default()
        deps.run_command = fake_run

        job_id = _make_job(client)
        result = deps.apply_network_isolation(job_id, "192.168.122.10")

        assert result is True
        # Per F01: isolation now goes through the privileged helper, not raw iptables.
        assert any("network_isolation_admin.sh" in str(c) for c in calls), calls
        assert any(c[:2] == ["sudo", "-n"] for c in calls), calls

        from astinuslab.db import session_scope
        from astinuslab.db.repositories import JobRepository

        with session_scope() as s:
            job = JobRepository(s).get_job(job_id)
            assert job is not None
            assert job.network_isolated is True

    def test_apply_returns_false_on_permission_error(self, client, monkeypatch):
        from astinuslab.orchestration.deps import JobRunDeps

        def fail_run(cmd, **kwargs):
            raise subprocess.CalledProcessError(1, cmd, stderr="Operation not permitted")

        deps = JobRunDeps.default()
        deps.run_command = fail_run

        job_id = _make_job(client)
        result = deps.apply_network_isolation(job_id, "192.168.122.10")
        assert result is False

        from astinuslab.db import session_scope
        from astinuslab.db.models import JobEvent

        with session_scope() as s:
            events = s.query(JobEvent).filter(JobEvent.job_id == job_id, JobEvent.state == "isolation_failed").all()
            assert events, "expected an isolation_failed job event"
            assert "could NOT be applied" in events[0].message

    def test_remove_clears_db_flag(self, client, monkeypatch):
        from astinuslab.orchestration.deps import JobRunDeps

        deps = JobRunDeps.default()
        deps.run_command = lambda cmd, **kw: ""

        job_id = _make_job(client)
        deps.apply_network_isolation(job_id, "192.168.122.10")
        deps.remove_network_isolation(job_id, "192.168.122.10")

        from astinuslab.db import session_scope
        from astinuslab.db.repositories import JobRepository

        with session_scope() as s:
            job = JobRepository(s).get_job(job_id)
            assert job is not None
            assert job.network_isolated is False

    def test_remove_is_nonfatal_on_error(self, client, monkeypatch):
        from astinuslab.orchestration.deps import JobRunDeps

        def fail_run(cmd, **kwargs):
            raise subprocess.CalledProcessError(1, cmd)

        deps = JobRunDeps.default()
        deps.run_command = fail_run

        job_id = _make_job(client)
        # should not raise
        deps.remove_network_isolation(job_id, "192.168.122.10")

    def test_apply_invokes_helper_with_validated_args(self, client, monkeypatch):
        """Per F01: workload_support no longer touches iptables directly.
        It must call network_isolation_admin.sh via sudo -n with the apply
        subcommand, the job_id, and the guest IP (in that order)."""
        from astinuslab.orchestration.deps import JobRunDeps

        captured = []

        def capture_run(cmd, **kw):
            captured.append(cmd)
            return ""

        deps = JobRunDeps.default()
        deps.run_command = capture_run

        job_id = _make_job(client)
        deps.apply_network_isolation(job_id, "192.168.122.99")

        helper_calls = [c for c in captured if "network_isolation_admin.sh" in str(c)]
        assert helper_calls, captured
        cmd = helper_calls[0]
        assert cmd[0:2] == ["sudo", "-n"]
        assert cmd[3:] == ["apply", job_id, "192.168.122.99"]


class TestIsolationFailClosed:
    def _cleanup(self):
        from types import SimpleNamespace

        return SimpleNamespace(attacker_ip_for_isolation=None, isolated_guest_ip=None)

    def test_required_isolation_failure_raises(self):
        import pytest

        from astinuslab.live_session.support import apply_post_provisioning_network_state

        cleanup = self._cleanup()
        with pytest.raises(RuntimeError, match="fail closed"):
            apply_post_provisioning_network_state(
                lambda job_id, guest_ip, attacker_ip=None: False,
                lambda *a, **k: pytest.fail("snapshot must not be created for workload sessions"),
                cleanup,
                "job-x",
                "wordpress-6.4",
                {"network_isolation": True},
                "192.168.122.10",
            )
        assert cleanup.isolated_guest_ip is None

    def test_required_isolation_success_records_ip(self):
        from astinuslab.live_session.support import apply_post_provisioning_network_state

        cleanup = self._cleanup()
        apply_post_provisioning_network_state(
            lambda job_id, guest_ip, attacker_ip=None: True,
            lambda *a, **k: None,
            cleanup,
            "job-x",
            "wordpress-6.4",
            {"network_isolation": True},
            "192.168.122.10",
        )
        assert cleanup.isolated_guest_ip == "192.168.122.10"

    def test_workload_without_isolation_does_not_call_helper(self):
        import pytest

        from astinuslab.live_session.support import apply_post_provisioning_network_state

        cleanup = self._cleanup()
        apply_post_provisioning_network_state(
            lambda *a, **k: pytest.fail("isolation must not be attempted"),
            lambda *a, **k: None,
            cleanup,
            "job-x",
            "static-site",
            {"network_isolation": False},
            "192.168.122.10",
        )
        assert cleanup.isolated_guest_ip is None


# ── UI — badge shown in live section ──────────────────────────────────────────


class TestIsolationBadgeUI:
    def test_isolation_badge_shown_when_isolated(self, client):
        from astinuslab.db import session_scope
        from astinuslab.db.repositories import JobRepository

        job_id = _make_job(client)

        with session_scope() as s:
            JobRepository(s).update_job(
                job_id,
                status="waiting_for_capture",
                network_isolated=True,
                guest_ip="192.168.122.10",
            )

        r = client.get(f"/jobs/{job_id}")
        assert r.status_code == 200
        assert "Network isolated" in r.text or "Red aislada" in r.text or "network_isolated" in r.text.lower()

    def test_isolation_badge_absent_when_not_isolated(self, client):
        job_id = _make_job(client)
        r = client.get(f"/jobs/{job_id}")
        assert r.status_code == 200
        assert "🔒" not in r.text


# ── helpers ────────────────────────────────────────────────────────────────────


def _make_job(client) -> str:
    """Create a queued job via the API and return its id."""
    r = client.post(
        "/api/v1/jobs",
        json={
            "target_id": "ubuntu-22-04-default",
            "scenario_id": "lab-session",
            "execution_mode": "manual_trigger",
            "workload_id": WORDPRESS_ID,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]
