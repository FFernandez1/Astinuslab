"""Tests for M6: multi-workload stacks."""

from __future__ import annotations

import pytest

WORDPRESS_ID = "wordpress-6.4"


# ── Catalog ────────────────────────────────────────────────────────────────────


class TestStackCatalog:
    def test_stack_present_in_workloads(self):
        from astinuslab.catalog import load_workloads

        ids = [w["id"] for w in load_workloads()]
        assert "full-php-stack" in ids

    def test_stack_is_stack_flag(self):
        from astinuslab.catalog import workload_by_id

        stack = workload_by_id("full-php-stack")
        assert stack is not None
        assert stack["is_stack"] is True

    def test_stack_has_components(self):
        from astinuslab.catalog import workload_by_id

        stack = workload_by_id("full-php-stack")
        assert stack is not None
        assert WORDPRESS_ID in stack["components"]
        assert "dvwa" in stack["components"]

    def test_stack_aggregates_services(self):
        from astinuslab.catalog import workload_by_id

        stack = workload_by_id("full-php-stack")
        assert stack is not None
        service_names = [s["name"] for s in stack["services"]]  # noqa: F841
        # Should include services from both WordPress and DVWA
        assert len(stack["services"]) >= 2

    def test_stack_aggregates_cve_ids(self):
        from astinuslab.catalog import workload_by_id

        stack = workload_by_id("full-php-stack")
        assert stack is not None
        # Should contain CVEs from both components
        assert "CVE-2022-21661" in stack["cve_ids"]  # from WordPress 6.4
        assert "CVE-2012-1823" in stack["cve_ids"]  # from DVWA

    def test_stack_inherits_network_isolation(self):
        from astinuslab.catalog import workload_by_id

        stack = workload_by_id("full-php-stack")
        assert stack is not None
        # Both components have network_isolation=true
        assert stack["network_isolation"] is True

    def test_stack_no_install_script(self):
        from astinuslab.catalog import workload_by_id

        stack = workload_by_id("full-php-stack")
        assert stack is not None
        assert stack["install_script"] is None

    def test_single_workload_is_not_stack(self):
        from astinuslab.catalog import workload_by_id

        wp = workload_by_id(WORDPRESS_ID)
        assert wp is not None
        assert wp["is_stack"] is False
        assert wp["components"] == []

    def test_all_cve_ids_includes_stack_cves(self):
        from astinuslab.catalog import all_cve_ids

        # Stack CVEs are aggregated from components — they should still appear
        # (since the components also appear as standalone)
        ids = all_cve_ids()
        assert "CVE-2022-21661" in ids


# ── API ────────────────────────────────────────────────────────────────────────


class TestStackAPI:
    def test_workloads_endpoint_includes_stack(self, client):
        r = client.get("/api/v1/workloads")
        ids = [w["id"] for w in r.json()]
        assert "full-php-stack" in ids

    def test_stack_has_is_stack_true_in_api(self, client):
        workloads = {w["id"]: w for w in client.get("/api/v1/workloads").json()}
        assert workloads["full-php-stack"]["is_stack"] is True

    def test_stack_components_in_api(self, client):
        workloads = {w["id"]: w for w in client.get("/api/v1/workloads").json()}
        assert WORDPRESS_ID in workloads["full-php-stack"]["components"]
        assert "dvwa" in workloads["full-php-stack"]["components"]

    def test_stack_services_aggregated_in_api(self, client):
        workloads = {w["id"]: w for w in client.get("/api/v1/workloads").json()}
        assert len(workloads["full-php-stack"]["services"]) >= 2

    def test_stack_job_creation_accepted(self, client):
        r = client.post(
            "/api/v1/jobs",
            json={
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "lab-session",
                "execution_mode": "manual_trigger",
                "workload_id": "full-php-stack",
            },
        )
        assert r.status_code == 201
        assert r.json()["workload_id"] == "full-php-stack"

    def test_cve_filter_returns_stack_via_component_cve(self, client):
        # Stack aggregates a WordPress component CVE
        r = client.get("/api/v1/workloads?cve=CVE-2022-21661")
        ids = [w["id"] for w in r.json()]
        assert WORDPRESS_ID in ids


# ── Orchestrator — stack provisioning ─────────────────────────────────────────


class TestStackProvisioning:
    def test_provision_stack_calls_each_component(self, client, monkeypatch):
        from astinuslab.orchestration.deps import JobRunDeps

        installed: list[str] = []

        def fake_install(job_id, workload, guest_ip, guest_user, target_context=None):
            installed.append(workload["id"])

        deps = JobRunDeps.default()
        deps.run_workload_install = fake_install
        deps.finalize_workload_provisioning = lambda *a, **k: None
        deps.run_command = lambda *a, **k: ""

        from astinuslab.catalog import workload_by_id

        stack = workload_by_id("full-php-stack")
        assert stack is not None
        job_id = _make_job(client, "full-php-stack")

        deps.provision_stack(job_id, stack, "192.168.122.10", "ubuntu")

        assert WORDPRESS_ID in installed
        assert "dvwa" in installed

    def test_provision_stack_preserves_order(self, client, monkeypatch):
        from astinuslab.orchestration.deps import JobRunDeps

        installed: list[str] = []
        deps = JobRunDeps.default()
        deps.run_workload_install = lambda job_id, w, ip, user, target_context=None: installed.append(w["id"])
        deps.finalize_workload_provisioning = lambda *a, **k: None
        deps.run_command = lambda *a, **k: ""

        from astinuslab.catalog import workload_by_id

        stack = workload_by_id("full-php-stack")
        assert stack is not None
        job_id = _make_job(client, "full-php-stack")
        deps.provision_stack(job_id, stack, "192.168.122.10", "ubuntu")

        assert installed == [WORDPRESS_ID, "dvwa"]

    def test_provision_stack_raises_on_missing_component(self, client, monkeypatch):
        from astinuslab.orchestration.deps import JobRunDeps

        deps = JobRunDeps.default()
        deps.run_workload_install = lambda *a, **k: None
        deps.finalize_workload_provisioning = lambda *a, **k: None
        deps.run_command = lambda *a, **k: ""

        job_id = _make_job(client, "full-php-stack")
        fake_stack = {
            "id": "bad-stack",
            "display_name": "Bad Stack",
            "is_stack": True,
            "components": ["nonexistent-workload"],
        }
        with pytest.raises(RuntimeError, match="not found"):
            deps.provision_stack(job_id, fake_stack, "192.168.122.10", "ubuntu")

    def test_provision_dispatch_calls_stack_path(self, client, monkeypatch):
        from astinuslab.orchestration.deps import JobRunDeps

        stack_called = []
        deps = JobRunDeps.default()
        deps.provision_stack = lambda *a, **k: stack_called.append(True)

        from astinuslab.catalog import workload_by_id

        stack = workload_by_id("full-php-stack")
        assert stack is not None
        job_id = _make_job(client, "full-php-stack")
        deps.provision_workload(job_id, stack, "192.168.122.10", "ubuntu")
        assert stack_called

    def test_provision_dispatch_calls_single_path(self, client, monkeypatch):
        from astinuslab.orchestration.deps import JobRunDeps

        single_called = []
        deps = JobRunDeps.default()
        deps.run_workload_install = lambda *a, **k: single_called.append(True)
        deps.finalize_workload_provisioning = lambda *a, **k: None

        from astinuslab.catalog import workload_by_id

        wp = workload_by_id(WORDPRESS_ID)
        assert wp is not None
        job_id = _make_job(client, WORDPRESS_ID)
        deps.provision_workload(job_id, wp, "192.168.122.10", "ubuntu")
        assert single_called


# ── Admin UI ──────────────────────────────────────────────────────────────────


class TestStackAdminUI:
    def test_admin_page_shows_stack(self, client):
        r = client.get("/workloads")
        assert "full-php-stack" in r.text

    def test_admin_page_shows_stack_badge(self, client):
        r = client.get("/workloads")
        assert "stack" in r.text.lower()

    def test_admin_page_shows_component_ids(self, client):
        r = client.get("/workloads")
        assert WORDPRESS_ID in r.text
        assert "dvwa" in r.text


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
