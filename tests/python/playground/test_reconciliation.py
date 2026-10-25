from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from astinuslab.db.models import CapacityReservation, Job, JobNetwork, JobRuntime, JobVM, JobVMNic, PlaygroundSession


class _FakeLibvirtError(Exception):
    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self._code = code

    def get_error_code(self) -> int | None:
        return self._code


class _FakeDomain:
    def __init__(self, name: str, *, active: bool = True, destroy_error: bool = False) -> None:
        self._name = name
        self._active = active
        self._destroy_error = destroy_error
        self.destroyed = False
        self.destroy_calls = 0
        self.undefined = False

    def name(self) -> str:
        return self._name

    def isActive(self) -> bool:
        return self._active

    def destroy(self) -> None:
        self.destroy_calls += 1
        if self._destroy_error:
            raise _FakeLibvirtError("destroy failed")
        self.destroyed = True
        self._active = False

    def undefine(self) -> None:
        self.undefined = True


class _FakeNetwork(_FakeDomain):
    def __init__(
        self,
        name: str,
        *,
        active: bool = True,
        destroy_error: bool = False,
        undefine_error: bool = False,
    ) -> None:
        super().__init__(name, active=active, destroy_error=destroy_error)
        self._undefine_error = undefine_error

    def undefine(self) -> None:
        if self._undefine_error:
            raise _FakeLibvirtError("undefine failed")
        super().undefine()


class _FakeConn:
    def __init__(self, *, domains: list[_FakeDomain] | None = None, networks: list[_FakeNetwork] | None = None) -> None:
        self._domains = domains or []
        self._networks = networks or []
        self.closed = False

    def listAllDomains(self):
        return self._domains

    def listAllNetworks(self):
        return self._networks

    def lookupByName(self, name: str):
        for domain in self._domains:
            if domain.name() == name:
                return domain
        raise _FakeLibvirtError(f"domain not found: {name}", code=42)

    def networkLookupByName(self, name: str):
        for network in self._networks:
            if network.name() == name:
                return network
        raise _FakeLibvirtError(f"network not found: {name}", code=43)

    def close(self) -> None:
        self.closed = True


class _LateLookupConn(_FakeConn):
    """Expose resources only to the post-scan exact-name lookup."""

    def __init__(
        self,
        *,
        late_domain: _FakeDomain | None = None,
        late_network: _FakeNetwork | None = None,
    ) -> None:
        super().__init__()
        self._late_domain = late_domain
        self._late_network = late_network

    def lookupByName(self, name: str):
        if self._late_domain is not None and self._late_domain.name() == name:
            return self._late_domain
        return super().lookupByName(name)

    def networkLookupByName(self, name: str):
        if self._late_network is not None and self._late_network.name() == name:
            return self._late_network
        return super().networkLookupByName(name)


class _FakeLibvirt:
    libvirtError = _FakeLibvirtError
    VIR_ERR_NO_DOMAIN = 42
    VIR_ERR_NO_NETWORK = 43

    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn
        self.opened_uri: str | None = None

    def open(self, uri: str) -> _FakeConn:
        self.opened_uri = uri
        return self.conn


def _create_running_playground(scope, *, created_at: datetime) -> None:
    with scope() as session:
        session.add(
            PlaygroundSession(
                id="pg-1",
                status="running",
                started_at=created_at,
            )
        )
        session.add(
            JobVM(
                id="vm-1",
                session_id="pg-1",
                label="web",
                image_id="img",
                domain_name="astinuslab-pg-pg-1-web",
                disk_path="/tmp/web.qcow2",
                state="running",
                created_at=created_at,
                updated_at=created_at,
            )
        )
        session.add(
            JobNetwork(
                id="net-1",
                session_id="pg-1",
                name="lan",
                subnet_cidr="10.42.0.0/24",
                isolated=True,
                libvirt_net_name="astinuslab-pg-pg-1-lan",
                state="active",
                created_at=created_at,
                updated_at=created_at,
            )
        )


def test_reconciler_destroys_astinuslab_orphan_domains_and_networks(in_memory_session_scope):
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    orphan_domain = _FakeDomain("astinuslab-pg-missing-web")
    old_domain = _FakeDomain("alab-pg-missing-web")
    external_domain = _FakeDomain("external-vm")
    orphan_network = _FakeNetwork("astinuslab-pg-missing-lan")
    external_network = _FakeNetwork("default")
    conn = _FakeConn(
        domains=[orphan_domain, old_domain, external_domain],
        networks=[orphan_network, external_network],
    )
    fake_libvirt = _FakeLibvirt(conn)

    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=fake_libvirt,
        libvirt_uri="qemu:///test",
        grace_seconds=0,
    )

    assert result["orphan_domains_destroyed"] == 1
    assert result["orphan_networks_destroyed"] == 1
    assert orphan_domain.destroyed is True
    assert orphan_domain.undefined is True
    assert orphan_network.destroyed is True
    assert orphan_network.undefined is True
    assert old_domain.destroyed is False
    assert old_domain.undefined is False
    assert external_domain.destroyed is False
    assert external_network.destroyed is False
    assert fake_libvirt.opened_uri == "qemu:///test"
    assert conn.closed is True


def test_reap_orphan_networks_destroys_only_dead_session_networks(in_memory_session_scope):
    """A network left active on the host by a dead session must be destroyed so
    its subnet is freed; networks of a live session and non-owned networks (e.g.
    the host `default`) must be preserved. Regression for the playground
    "Network is already in use" subnet collision."""
    from astinuslab.playground.reconciliation import reap_orphan_networks

    _create_running_playground(scope=in_memory_session_scope, created_at=datetime.now(UTC))

    live_network = _FakeNetwork("astinuslab-pg-pg-1-lan")  # owned by live session pg-1
    orphan_network = _FakeNetwork("astinuslab-pg-pg-dead-default-2d7b")  # dead session
    external_network = _FakeNetwork("default")  # host network, not ours
    conn = _FakeConn(networks=[live_network, orphan_network, external_network])
    fake_libvirt = _FakeLibvirt(conn)

    destroyed = reap_orphan_networks(
        session_scope=in_memory_session_scope,
        libvirt_module=fake_libvirt,
        libvirt_uri="qemu:///test",
    )

    assert destroyed == 1
    assert orphan_network.destroyed is True
    assert orphan_network.undefined is True
    assert live_network.destroyed is False
    assert external_network.destroyed is False
    assert fake_libvirt.opened_uri == "qemu:///test"
    assert conn.closed is True


def test_orphan_reaper_cannot_destroy_a_registered_network_during_create_retry(
    in_memory_session_scope,
):
    from astinuslab.playground import repositories as pr
    from astinuslab.playground.reconciliation import reap_orphan_networks

    _create_running_playground(scope=in_memory_session_scope, created_at=datetime.now(UTC))
    with in_memory_session_scope() as session:
        network = session.get(JobNetwork, "net-1")
        assert network is not None
        network.state = "failed"

    @contextmanager
    def claim_after_reaper_snapshot():
        with in_memory_session_scope() as session:
            yield session
        with in_memory_session_scope() as session:
            assert pr.claim_network_for_create(session, "net-1") is True

    host_network = _FakeNetwork("astinuslab-pg-pg-1-lan")
    destroyed = reap_orphan_networks(
        session_scope=claim_after_reaper_snapshot,
        libvirt_module=_FakeLibvirt(_FakeConn(networks=[host_network])),
        libvirt_uri="qemu:///test",
    )

    assert destroyed == 0
    assert host_network.destroyed is False
    with in_memory_session_scope() as session:
        network = session.get(JobNetwork, "net-1")
        assert network is not None
        assert network.state == "creating"


def test_reconciler_skips_domain_of_live_job_without_runtime_row(in_memory_session_scope):
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    with in_memory_session_scope() as session:
        session.add(
            Job(
                id="job-live-1",
                target_id="ubuntu",
                scenario_id="capture",
                execution_mode="immediate",
                status="preparing_image",
            )
        )

    target_domain = _FakeDomain("astinuslab-job-live-1")
    attacker_domain = _FakeDomain("astinuslab-job-live-1-attacker")
    true_orphan = _FakeDomain("astinuslab-job-gone")
    conn = _FakeConn(domains=[target_domain, attacker_domain, true_orphan])

    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(conn),
        libvirt_uri="qemu:///test",
        grace_seconds=0,
    )

    assert target_domain.destroyed is False
    assert target_domain.undefined is False
    assert attacker_domain.destroyed is False
    assert attacker_domain.undefined is False
    assert true_orphan.destroyed is True
    assert true_orphan.undefined is True
    assert result["orphan_domains_destroyed"] == 1


def test_reconciler_reaps_domain_of_terminal_job(in_memory_session_scope):
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    with in_memory_session_scope() as session:
        session.add(
            Job(
                id="job-terminal-1",
                target_id="ubuntu",
                scenario_id="capture",
                execution_mode="immediate",
                status="failed",
            )
        )

    target_domain = _FakeDomain("astinuslab-job-terminal-1")
    conn = _FakeConn(domains=[target_domain])

    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(conn),
        libvirt_uri="qemu:///test",
        grace_seconds=0,
    )

    assert target_domain.destroyed is True
    assert target_domain.undefined is True
    assert result["orphan_domains_destroyed"] == 1


@pytest.mark.parametrize("runtime_row", [None, "running", "teardown_failed"])
@pytest.mark.parametrize(
    "status,reservation_state,boot_started,preserve,retained",
    [
        ("failed", "reserved", True, True, True),
        ("failed", "reserved", True, False, False),
        ("failed", "released", True, True, False),
        ("failed", "reserved", False, True, False),
        ("cancelled", "reserved", True, True, False),
        ("completed", "reserved", True, True, False),
    ],
)
def test_reconciler_respects_failed_job_retention(
    in_memory_session_scope, runtime_row, status, reservation_state, boot_started, preserve, retained
):
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(hours=1)
    domains = [_FakeDomain("astinuslab-job-retained"), _FakeDomain("astinuslab-job-retained-attacker")]
    with in_memory_session_scope() as session:
        session.add(
            Job(
                id="job-retained",
                target_id="debian-12",
                scenario_id="capture",
                execution_mode="manual",
                status=status,
                created_at=created_at,
            )
        )
        session.flush()
        session.add(
            CapacityReservation(
                job_id="job-retained",
                vm_count=2,
                memory_mb=2048,
                storage_bytes=1,
                state=reservation_state,
                preserve_failed_vm=preserve,
                boot_started=boot_started,
            )
        )
        if runtime_row:
            for role, domain in zip(("target", "attacker"), domains, strict=True):
                session.add(
                    JobRuntime(
                        job_id="job-retained",
                        role=role,
                        vm_name=domain.name(),
                        state=runtime_row,
                        power_state="on",
                        created_at=created_at,
                        updated_at=created_at,
                    )
                )
    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn(domains=domains)),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )
    assert result["orphan_domains_destroyed"] == (0 if retained else 2)
    assert all(domain.destroyed is not retained for domain in domains)
    assert all(domain.undefined is not retained for domain in domains)


def test_reconciler_logs_orphan_network_cleanup_failure(in_memory_session_scope, caplog):
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    failing_network = _FakeNetwork("astinuslab-pg-missing-lan", undefine_error=True)

    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn(networks=[failing_network])),
        libvirt_uri="qemu:///test",
        grace_seconds=0,
    )

    assert result["orphan_networks_destroyed"] == 0
    assert failing_network.destroyed is True
    assert failing_network.undefined is False
    assert "reconciler: failed to destroy orphan network astinuslab-pg-missing-lan" in caplog.text


def test_reconciler_does_not_undefine_active_orphans_when_stop_fails(in_memory_session_scope, caplog):
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    failing_domain = _FakeDomain("astinuslab-pg-missing-web", destroy_error=True)
    failing_network = _FakeNetwork("astinuslab-pg-missing-lan", destroy_error=True)

    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn(domains=[failing_domain], networks=[failing_network])),
        libvirt_uri="qemu:///test",
        grace_seconds=0,
    )

    assert result["orphan_domains_destroyed"] == 0
    assert result["orphan_networks_destroyed"] == 0
    assert failing_domain.undefined is False
    assert failing_network.undefined is False
    assert "failed to destroy orphan domain" in caplog.text
    assert "failed to destroy orphan network" in caplog.text


def test_orphan_network_reaper_does_not_undefine_when_stop_fails(in_memory_session_scope, caplog):
    from astinuslab.playground.reconciliation import reap_orphan_networks

    failing_network = _FakeNetwork("astinuslab-pg-missing-lan", destroy_error=True)
    destroyed = reap_orphan_networks(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn(networks=[failing_network])),
        libvirt_uri="qemu:///test",
    )

    assert destroyed == 0
    assert failing_network.undefined is False
    assert "reaper: failed to destroy orphan network" in caplog.text


def test_reconciler_marks_old_missing_playground_rows_failed(in_memory_session_scope):
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)

    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn()),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    assert result["zombie_vms_failed"] == 1
    assert result["zombie_networks_failed"] == 0
    with in_memory_session_scope() as session:
        vm = session.get(JobVM, "vm-1")
        net = session.get(JobNetwork, "net-1")
        playground_session = session.get(PlaygroundSession, "pg-1")
        assert vm.state == "teardown_failed"
        assert net.state == "destroyed"
        assert playground_session.status == "failure_cleanup"
        assert "reconciler" in (playground_session.error_summary or "")


def test_reconciler_rechecks_host_names_before_marking_old_rows_missing(in_memory_session_scope):
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    late_domain = _FakeDomain("astinuslab-pg-pg-1-web")
    late_network = _FakeNetwork("astinuslab-pg-pg-1-lan")

    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_LateLookupConn(late_domain=late_domain, late_network=late_network)),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    assert result["zombie_vms_failed"] == 0
    assert result["zombie_networks_failed"] == 0
    with in_memory_session_scope() as session:
        assert session.get(JobVM, "vm-1").state == "running"
        assert session.get(JobNetwork, "net-1").state == "active"
        assert session.get(PlaygroundSession, "pg-1").status == "running"


def test_reconciler_marks_missing_teardown_failed_vm_destroyed(in_memory_session_scope, monkeypatch):
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    cleanup_calls: list[Path] = []
    monkeypatch.setattr(po, "_remove_playground_artifacts", cleanup_calls.append)
    with in_memory_session_scope() as session:
        vm = session.get(JobVM, "vm-1")
        net = session.get(JobNetwork, "net-1")
        vm.state = "teardown_failed"
        net.state = "destroyed"

    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn()),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    assert result["zombie_vms_failed"] == 0
    with in_memory_session_scope() as session:
        vm = session.get(JobVM, "vm-1")
        playground_session = session.get(PlaygroundSession, "pg-1")
        assert vm.state == "destroyed"
        assert playground_session.status == "running"
    assert cleanup_calls == [Path("/tmp/web.qcow2")]


def test_reconciler_keeps_teardown_failed_when_artifact_retry_fails(in_memory_session_scope, monkeypatch):
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    with in_memory_session_scope() as session:
        vm = session.get(JobVM, "vm-1")
        vm.state = "teardown_failed"
        vm.workload_error = "stale retry error"
        session.get(JobNetwork, "net-1").state = "destroyed"

    def refuse_replaced_artifact(_path: Path) -> None:
        raise RuntimeError("replacement detected")

    monkeypatch.setattr(po, "_remove_playground_artifacts", refuse_replaced_artifact)

    reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn()),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    with in_memory_session_scope() as session:
        vm = session.get(JobVM, "vm-1")
        assert vm.state == "teardown_failed"
        assert "replacement detected" in (vm.error_summary or "")


def test_reconciler_destroys_teardown_failed_domain_only_in_retry_pass(in_memory_session_scope, monkeypatch):
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    with in_memory_session_scope() as session:
        session.get(JobVM, "vm-1").state = "teardown_failed"
        session.get(JobNetwork, "net-1").state = "destroyed"

    domain = _FakeDomain("astinuslab-pg-pg-1-web")
    conn = _FakeConn(domains=[domain])
    monkeypatch.setattr(po, "_remove_playground_artifacts", lambda _path: None)

    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(conn),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    assert domain.destroy_calls == 1
    assert result["orphan_domains_destroyed"] == 0
    with in_memory_session_scope() as session:
        assert session.get(JobVM, "vm-1").state == "destroyed"


def test_reconciler_freshly_looks_up_domain_after_teardown_claim_before_artifact_cleanup(
    in_memory_session_scope, monkeypatch
):
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    with in_memory_session_scope() as session:
        session.get(JobVM, "vm-1").state = "teardown_failed"
        session.get(JobNetwork, "net-1").state = "destroyed"

    events: list[str] = []
    domain = _FakeDomain("astinuslab-pg-pg-1-web")
    original_destroy = domain.destroy
    original_undefine = domain.undefine

    def record_destroy() -> None:
        events.append("destroy")
        original_destroy()

    def record_undefine() -> None:
        events.append("undefine")
        original_undefine()

    monkeypatch.setattr(domain, "destroy", record_destroy)
    monkeypatch.setattr(domain, "undefine", record_undefine)
    monkeypatch.setattr(po, "_remove_playground_artifacts", lambda _path: events.append("artifacts"))

    reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_LateLookupConn(late_domain=domain)),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    assert events == ["destroy", "undefine", "artifacts"]
    with in_memory_session_scope() as session:
        assert session.get(JobVM, "vm-1").state == "destroyed"


def test_reconciler_preserves_retryable_artifacts_when_domain_stop_fails(
    in_memory_session_scope, monkeypatch, tmp_path
):
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    disk = tmp_path / "web.qcow2"
    disk.write_bytes(b"live overlay")
    with in_memory_session_scope() as session:
        vm = session.get(JobVM, "vm-1")
        vm.state = "teardown_failed"
        vm.disk_path = str(disk)
        session.get(JobNetwork, "net-1").state = "destroyed"

    domain = _FakeDomain("astinuslab-pg-pg-1-web")
    monkeypatch.setattr(
        domain,
        "destroy",
        lambda: (_ for _ in ()).throw(_FakeLibvirtError("destroy failed")),
    )
    cleanup_calls: list[Path] = []
    monkeypatch.setattr(po, "_remove_playground_artifacts", cleanup_calls.append)

    reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn(domains=[domain])),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    assert domain.undefined is False
    assert cleanup_calls == []
    assert disk.read_bytes() == b"live overlay"
    with in_memory_session_scope() as session:
        vm = session.get(JobVM, "vm-1")
        assert vm.state == "teardown_failed"
        assert "destroy failed" in (vm.error_summary or "")


def test_spawn_claim_prevents_cleanup_from_a_stale_teardown_scan(in_memory_session_scope):
    from astinuslab.playground import repositories as pr

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    with in_memory_session_scope() as session:
        session.get(JobVM, "vm-1").state = "teardown_failed"

    # The reconciler may already hold this id from its one-time host/DB scan.
    stale_teardown_id = "vm-1"
    with in_memory_session_scope() as session:
        assert pr.claim_vm_for_spawn(session, stale_teardown_id) is True
    with in_memory_session_scope() as session:
        assert pr.claim_vm_for_teardown_retry(session, stale_teardown_id) is False
        vm = session.get(JobVM, stale_teardown_id)
        assert vm.state == "starting"
        assert vm.workload_error is None


def test_spawn_claim_wins_over_legacy_terminal_domain_recovery(in_memory_session_scope):
    from astinuslab.playground import repositories as pr
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    with in_memory_session_scope() as session:
        session.get(JobVM, "vm-1").state = "failed"
    with in_memory_session_scope() as session:
        assert pr.claim_vm_for_spawn(session, "vm-1") is True
        claimed_vm = session.get(JobVM, "vm-1")
        session.refresh(claimed_vm)
        claim_token = claimed_vm.updated_at

    domain = _FakeDomain("astinuslab-pg-pg-1-web")
    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn(domains=[domain])),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    assert result["orphan_domains_destroyed"] == 0
    assert domain.destroyed is False
    with in_memory_session_scope() as session:
        vm = session.get(JobVM, "vm-1")
        assert vm.state == "starting"
        assert (
            pr.finish_vm_spawn(
                session,
                "vm-1",
                "provisioning_workload",
                expected_updated_at=claim_token,
                workload_state="provisioning",
            )
            is True
        )
    with in_memory_session_scope() as session:
        vm = session.get(JobVM, "vm-1")
        assert vm.state == "provisioning_workload"
        assert vm.workload_state == "provisioning"


def test_legacy_terminal_row_with_live_domain_uses_strict_cas_teardown(in_memory_session_scope, monkeypatch, tmp_path):
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    disk = tmp_path / "legacy.qcow2"
    disk.write_bytes(b"legacy overlay")
    with in_memory_session_scope() as session:
        vm = session.get(JobVM, "vm-1")
        vm.state = "failed"
        vm.disk_path = str(disk)
    cleanup_calls: list[Path] = []
    monkeypatch.setattr(po, "_remove_playground_artifacts", cleanup_calls.append)
    domain = _FakeDomain("astinuslab-pg-pg-1-web")

    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn(domains=[domain])),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    assert result["orphan_domains_destroyed"] == 0
    assert result["legacy_vm_teardowns_recovered"] == 0
    assert domain.destroyed is True
    assert domain.undefined is True
    assert cleanup_calls == [disk]
    with in_memory_session_scope() as session:
        assert session.get(JobVM, "vm-1").state == "destroyed"


def test_legacy_failed_row_without_domain_recovers_staged_artifacts(in_memory_session_scope, monkeypatch, tmp_path):
    from types import SimpleNamespace

    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    root = tmp_path / "playground"
    staging = root / "pg-1"
    staging.mkdir(parents=True)
    disk = staging / "web.qcow2"
    artifacts = [disk, disk.with_suffix(".iso"), disk.with_name("web-seed.iso")]
    for artifact in artifacts:
        artifact.write_bytes(b"legacy residue")
    cloud_init = staging / "web-cloud-init"
    cloud_init.mkdir()
    for name in ("user-data", "meta-data", "network-config"):
        (cloud_init / name).write_text("legacy\n", encoding="utf-8")
    monkeypatch.setattr(po, "settings", SimpleNamespace(playground_disk_dir=root))
    with in_memory_session_scope() as session:
        vm = session.get(JobVM, "vm-1")
        vm.state = "failed"
        vm.disk_path = str(disk)
        vm.error_summary = "legacy spawn failed"
        vm.updated_at = created_at

    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn(networks=[_FakeNetwork("astinuslab-pg-pg-1-lan")])),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    assert result["legacy_vm_teardowns_recovered"] == 1
    assert not staging.exists()
    with in_memory_session_scope() as session:
        vm = session.get(JobVM, "vm-1")
        assert vm.state == "destroyed"
        assert vm.error_summary == "legacy spawn failed"


def test_spawn_retry_transition_gets_a_fresh_missing_domain_grace(in_memory_session_scope):
    from astinuslab.playground import repositories as pr
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    with in_memory_session_scope() as session:
        vm = session.get(JobVM, "vm-1")
        vm.state = "failed"
        vm.updated_at = created_at
    with in_memory_session_scope() as session:
        assert pr.claim_vm_for_spawn(session, "vm-1") is True

    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn(networks=[_FakeNetwork("astinuslab-pg-pg-1-lan")])),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    assert result["zombie_vms_failed"] == 0
    with in_memory_session_scope() as session:
        assert session.get(JobVM, "vm-1").state == "starting"
        assert session.get(PlaygroundSession, "pg-1").status == "running"


def test_missing_vm_reconciliation_cannot_overwrite_concurrent_spawn_claim(in_memory_session_scope, monkeypatch):
    from astinuslab.playground import reconciliation as reconciliation
    from astinuslab.playground import repositories as pr

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    with in_memory_session_scope() as session:
        vm = session.get(JobVM, "vm-1")
        vm.state = "failed"
        vm.updated_at = created_at

    original = pr.claim_legacy_vm_for_teardown

    def claim_before_teardown_cas(session, vm_id, expected_state, expected_updated_at):
        assert pr.claim_vm_for_spawn(session, "vm-1") is True
        return original(session, vm_id, expected_state, expected_updated_at)

    monkeypatch.setattr(pr, "claim_legacy_vm_for_teardown", claim_before_teardown_cas)
    result = reconciliation.reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn(networks=[_FakeNetwork("astinuslab-pg-pg-1-lan")])),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    assert result["zombie_vms_failed"] == 0
    with in_memory_session_scope() as session:
        assert session.get(JobVM, "vm-1").state == "starting"
        assert session.get(PlaygroundSession, "pg-1").status == "running"


def test_terminal_session_transition_tears_down_every_sibling_resource(in_memory_session_scope, monkeypatch):
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    with in_memory_session_scope() as session:
        session.add(
            JobVM(
                id="vm-2",
                session_id="pg-1",
                label="db",
                image_id="img",
                domain_name="astinuslab-pg-pg-1-db",
                disk_path="/tmp/db.qcow2",
                state="running",
                created_at=created_at,
                updated_at=created_at,
            )
        )
        session.add(
            JobNetwork(
                id="net-2",
                session_id="pg-1",
                name="backend",
                subnet_cidr="10.43.0.0/24",
                isolated=True,
                libvirt_net_name="astinuslab-pg-pg-1-backend",
                state="active",
                created_at=created_at,
                updated_at=created_at,
            )
        )

    sibling_domain = _FakeDomain("astinuslab-pg-pg-1-db")
    networks = [
        _FakeNetwork("astinuslab-pg-pg-1-lan"),
        _FakeNetwork("astinuslab-pg-pg-1-backend"),
    ]
    monkeypatch.setattr(po, "_remove_playground_artifacts", lambda _path: None)

    reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn(domains=[sibling_domain], networks=networks)),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    assert sibling_domain.destroyed is True
    assert sibling_domain.undefined is True
    assert all(network.destroyed and network.undefined for network in networks)
    with in_memory_session_scope() as session:
        assert session.get(PlaygroundSession, "pg-1").status == "failed"
        assert {vm.state for vm in session.query(JobVM).all()} == {"destroyed"}
        assert {network.state for network in session.query(JobNetwork).all()} == {"destroyed"}


def test_terminal_session_cleanup_never_steals_active_worker_custody(in_memory_session_scope):
    from astinuslab.playground import reconciliation

    now = datetime.now(UTC)
    _create_running_playground(in_memory_session_scope, created_at=now - timedelta(minutes=10))
    with in_memory_session_scope() as session:
        session.get(JobVM, "vm-1").state = "starting"
        session.get(JobNetwork, "net-1").state = "creating"
        reconciliation._mark_session_failed(session, "pg-1", now)
        reconciliation._queue_terminal_session_resources(session, now)

    with in_memory_session_scope() as session:
        assert session.get(PlaygroundSession, "pg-1").status == "failure_cleanup"
        assert session.get(JobVM, "vm-1").state == "starting"
        assert session.get(JobNetwork, "net-1").state == "creating"
        # Once the workers publish their own terminal handoff, the next pass can
        # safely transfer those now-passive rows to teardown custody.
        session.get(JobVM, "vm-1").state = "running"
        session.get(JobNetwork, "net-1").state = "active"
        session.flush()
        reconciliation._queue_terminal_session_resources(session, now)
        session.expire_all()
        assert session.get(JobVM, "vm-1").state == "teardown_failed"
        assert session.get(JobNetwork, "net-1").state == "failed"


def test_destroy_failed_parent_stays_blocking_then_reconciliation_finishes_cleanup(
    in_memory_session_scope, monkeypatch
):
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    with in_memory_session_scope() as session:
        session.get(PlaygroundSession, "pg-1").status = "destroy_failed"

    domain = _FakeDomain("astinuslab-pg-pg-1-web")
    network = _FakeNetwork("astinuslab-pg-pg-1-lan")
    monkeypatch.setattr(po, "_remove_playground_artifacts", lambda _path: None)

    reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn(domains=[domain], networks=[network])),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    assert domain.destroyed is True
    assert network.destroyed is True
    with in_memory_session_scope() as session:
        assert session.get(JobVM, "vm-1").state == "destroyed"
        assert session.get(JobNetwork, "net-1").state == "destroyed"
        assert session.get(PlaygroundSession, "pg-1").status == "cancelled"


def test_reconciliation_cleanup_failure_keeps_failed_parent_globally_blocking(in_memory_session_scope, monkeypatch):
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground import repositories as pr
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    domain = _FakeDomain("astinuslab-pg-pg-1-web", destroy_error=True)
    monkeypatch.setattr(po, "_remove_playground_artifacts", lambda _path: None)

    reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn(domains=[domain], networks=[])),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    with in_memory_session_scope() as session:
        parent = session.get(PlaygroundSession, "pg-1")
        assert parent is not None
        assert parent.status == "failure_cleanup"
        active = pr.active_playground_session(session)
        assert active is not None
        assert active.id == "pg-1"
        assert session.get(JobVM, "vm-1").state == "teardown_failed"
        assert session.get(JobNetwork, "net-1").state == "destroyed"


def test_reconciler_recovers_stale_live_parent_worker_claims(in_memory_session_scope, monkeypatch):
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    with in_memory_session_scope() as session:
        vm = session.get(JobVM, "vm-1")
        network_row = session.get(JobNetwork, "net-1")
        vm.state = "starting"
        vm.updated_at = created_at
        network_row.state = "creating"
        network_row.updated_at = created_at

    domain = _FakeDomain("astinuslab-pg-pg-1-web")
    network = _FakeNetwork("astinuslab-pg-pg-1-lan")
    monkeypatch.setattr(po, "_remove_playground_artifacts", lambda _path: None)

    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn(domains=[domain], networks=[network])),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    assert result["abandoned_vm_claims_recovered"] == 1
    assert result["abandoned_network_claims_recovered"] == 1
    assert domain.destroyed is True
    assert network.destroyed is True
    with in_memory_session_scope() as session:
        assert session.get(JobVM, "vm-1").state == "destroyed"
        assert session.get(JobNetwork, "net-1").state == "destroyed"
        assert session.get(PlaygroundSession, "pg-1").status == "failed"


@pytest.mark.parametrize("claim_state", ["powering_on", "powering_off", "rebooting"])
def test_reconciler_strictly_cleans_stale_power_action_claims(
    in_memory_session_scope,
    monkeypatch,
    claim_state,
):
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    with in_memory_session_scope() as session:
        vm = session.get(JobVM, "vm-1")
        vm.state = claim_state
        vm.updated_at = created_at

    domain = _FakeDomain("astinuslab-pg-pg-1-web")
    network = _FakeNetwork("astinuslab-pg-pg-1-lan")
    monkeypatch.setattr(po, "_remove_playground_artifacts", lambda _path: None)

    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn(domains=[domain], networks=[network])),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    assert result["abandoned_vm_claims_recovered"] == 1
    assert domain.destroyed is True
    with in_memory_session_scope() as session:
        assert session.get(JobVM, "vm-1").state == "destroyed"
        assert session.get(PlaygroundSession, "pg-1").status == "failed"


@pytest.mark.parametrize("claim_state", ["provisioning_workload_active", "powering_on", "powering_off", "rebooting"])
def test_reconciler_does_not_steal_fresh_cleanup_worker_claims(in_memory_session_scope, claim_state):
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    now = datetime.now(UTC)
    _create_running_playground(in_memory_session_scope, created_at=now)
    with in_memory_session_scope() as session:
        session.get(PlaygroundSession, "pg-1").status = "destroying"
        session.get(JobVM, "vm-1").state = claim_state
        session.get(JobNetwork, "net-1").state = "creating"

    domain = _FakeDomain("astinuslab-pg-pg-1-web")
    network = _FakeNetwork("astinuslab-pg-pg-1-lan")
    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn(domains=[domain], networks=[network])),
        libvirt_uri="qemu:///test",
        grace_seconds=120,
    )

    assert result["abandoned_vm_claims_recovered"] == 0
    assert result["abandoned_network_claims_recovered"] == 0
    assert domain.destroyed is False
    assert network.destroyed is False
    with in_memory_session_scope() as session:
        assert session.get(JobVM, "vm-1").state == claim_state
        assert session.get(JobNetwork, "net-1").state == "creating"
        assert session.get(PlaygroundSession, "pg-1").status == "destroying"


def test_parent_completion_wins_over_missing_child_failure(in_memory_session_scope, monkeypatch):
    from astinuslab.playground import reconciliation

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    original = reconciliation._fail_missing_vm_if_unchanged

    def complete_parent_after_child_cas(session, **kwargs):
        changed = original(session, **kwargs)
        assert changed is True
        session.query(PlaygroundSession).filter_by(id="pg-1").update(
            {PlaygroundSession.status: "completed"},
            synchronize_session=False,
        )
        session.flush()
        return changed

    monkeypatch.setattr(reconciliation, "_fail_missing_vm_if_unchanged", complete_parent_after_child_cas)
    reconciliation.reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn(networks=[_FakeNetwork("astinuslab-pg-pg-1-lan")])),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    with in_memory_session_scope() as session:
        assert session.get(PlaygroundSession, "pg-1").status == "completed"


def test_terminal_nic_prune_preserves_a_concurrent_spawn_claim(in_memory_session_scope):
    from astinuslab.playground import repositories as pr
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    with in_memory_session_scope() as session:
        vm = session.get(JobVM, "vm-1")
        vm.state = "failed"
        session.add(
            JobVMNic(
                id="nic-1",
                job_vm_id="vm-1",
                job_network_id="net-1",
                mac="52:54:00:aa:bb:01",
                state="active",
            )
        )
    with in_memory_session_scope() as session:
        assert pr.claim_vm_for_spawn(session, "vm-1") is True

    reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn(networks=[_FakeNetwork("astinuslab-pg-pg-1-lan")])),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    with in_memory_session_scope() as session:
        assert session.get(JobVM, "vm-1").state == "starting"
        assert session.get(JobVMNic, "nic-1") is not None


def test_missing_network_reconciliation_cannot_overwrite_concurrent_create_claim(in_memory_session_scope, monkeypatch):
    from astinuslab.playground import reconciliation as reconciliation
    from astinuslab.playground import repositories as pr

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    with in_memory_session_scope() as session:
        network = session.get(JobNetwork, "net-1")
        network.state = "pending"
        network.updated_at = created_at

    original = reconciliation._fail_missing_network_if_unchanged

    def claim_before_missing_cas(session, **kwargs):
        assert pr.claim_network_for_create(session, "net-1") is True
        return original(session, **kwargs)

    monkeypatch.setattr(reconciliation, "_fail_missing_network_if_unchanged", claim_before_missing_cas)
    result = reconciliation.reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn(domains=[_FakeDomain("astinuslab-pg-pg-1-web")])),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    assert result["zombie_networks_failed"] == 0
    with in_memory_session_scope() as session:
        assert session.get(JobNetwork, "net-1").state == "creating"
        assert session.get(PlaygroundSession, "pg-1").status == "running"


def test_network_create_claim_wins_over_terminal_host_recovery(in_memory_session_scope):
    from astinuslab.playground import repositories as pr
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    with in_memory_session_scope() as session:
        session.get(JobNetwork, "net-1").state = "failed"
    with in_memory_session_scope() as session:
        assert pr.claim_network_for_create(session, "net-1") is True

    network = _FakeNetwork("astinuslab-pg-pg-1-lan")
    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn(networks=[network])),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    assert result["orphan_networks_destroyed"] == 0
    assert result["legacy_network_teardowns_recovered"] == 0
    assert network.destroyed is False
    with in_memory_session_scope() as session:
        assert session.get(JobNetwork, "net-1").state == "creating"


def test_reconciler_freshly_looks_up_network_after_teardown_claim(in_memory_session_scope):
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    with in_memory_session_scope() as session:
        session.get(PlaygroundSession, "pg-1").status = "failure_cleanup"
        session.get(JobVM, "vm-1").state = "destroyed"
        session.get(JobNetwork, "net-1").state = "failed"

    network = _FakeNetwork("astinuslab-pg-pg-1-lan")
    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_LateLookupConn(late_network=network)),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    assert network.destroyed is True
    assert network.undefined is True
    assert result["legacy_network_teardowns_recovered"] == 1
    with in_memory_session_scope() as session:
        assert session.get(JobNetwork, "net-1").state == "destroyed"


def test_network_create_retry_transition_gets_a_fresh_missing_host_grace(in_memory_session_scope):
    from astinuslab.playground import repositories as pr
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    with in_memory_session_scope() as session:
        network = session.get(JobNetwork, "net-1")
        network.state = "failed"
        network.updated_at = created_at
    with in_memory_session_scope() as session:
        assert pr.claim_network_for_create(session, "net-1") is True

    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn(domains=[_FakeDomain("astinuslab-pg-pg-1-web")])),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    assert result["zombie_networks_failed"] == 0
    with in_memory_session_scope() as session:
        assert session.get(JobNetwork, "net-1").state == "creating"
        assert session.get(PlaygroundSession, "pg-1").status == "running"


def test_reconciler_skips_fresh_missing_rows_within_grace_period(in_memory_session_scope):
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    _create_running_playground(in_memory_session_scope, created_at=datetime.now(UTC))

    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn()),
        libvirt_uri="qemu:///test",
        grace_seconds=120,
    )

    assert result["zombie_vms_failed"] == 0
    assert result["zombie_networks_failed"] == 0
    with in_memory_session_scope() as session:
        assert session.get(JobVM, "vm-1").state == "running"
        assert session.get(JobNetwork, "net-1").state == "active"
        assert session.get(PlaygroundSession, "pg-1").status == "running"


@pytest.mark.parametrize("job_status,reservation_state", [("queued", "reserved"), ("waiting_for_capacity", "waiting")])
def test_reconciler_preserves_unstarted_job_placeholders(in_memory_session_scope, job_status, reservation_state):
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    with in_memory_session_scope() as session:
        session.add(
            Job(
                id="job-waiter",
                target_id="ubuntu",
                scenario_id="capture",
                execution_mode="manual",
                status=job_status,
                current_stage=job_status,
                created_at=created_at,
                capacity_reason="Waiting for capacity: VM limit 2",
            )
        )
        session.flush()
        session.add(
            CapacityReservation(
                job_id="job-waiter",
                vm_count=2,
                memory_mb=4608,
                storage_bytes=48 * 1024**3,
                state=reservation_state,
                boot_started=False,
                created_at=created_at,
            )
        )
        for role, suffix in [("target", ""), ("attacker", "-attacker")]:
            session.add(
                JobRuntime(
                    job_id="job-waiter",
                    role=role,
                    vm_name=f"astinuslab-job-waiter{suffix}",
                    state="pending",
                    power_state="unknown",
                    created_at=created_at,
                    updated_at=created_at,
                )
            )

    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn()),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    assert result["zombie_runtimes_failed"] == 0
    with in_memory_session_scope() as session:
        job = session.get(Job, "job-waiter")
        assert job.status == job_status
        assert job.current_stage == job_status
        assert job.started_at is None
        assert job.finished_at is None
        assert job.error_summary is None
        assert job.capacity_reason == "Waiting for capacity: VM limit 2"
        assert {row.role: row.state for row in job.runtimes} == {"target": "pending", "attacker": "pending"}
        reservation = session.get(CapacityReservation, job.id)
        assert reservation.state == reservation_state
        assert reservation.boot_started is False
        assert reservation.released_at is None


@pytest.mark.parametrize(
    "job_status,runtime_state,started",
    [("queued", "pending", True), ("waiting_for_capacity", "running", False), ("preparing_image", "pending", False)],
)
def test_reconciler_still_fails_missing_runtime_after_launch(
    in_memory_session_scope, job_status, runtime_state, started
):
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    with in_memory_session_scope() as session:
        session.add(
            Job(
                id="job-started",
                target_id="ubuntu",
                scenario_id="capture",
                execution_mode="manual",
                status=job_status,
                started_at=created_at if started else None,
            )
        )
        session.add(
            JobRuntime(
                job_id="job-started",
                role="target",
                vm_name="astinuslab-job-started",
                state=runtime_state,
                power_state="unknown",
                created_at=created_at,
                updated_at=created_at,
            )
        )

    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn()),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    assert result["zombie_runtimes_failed"] == 1
    with in_memory_session_scope() as session:
        job = session.get(Job, "job-started")
        assert job.status == "failed"
        assert job.runtimes[0].state == "teardown_failed"


def test_reconciler_marks_missing_forensic_runtime_job_failed(in_memory_session_scope):
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    with in_memory_session_scope() as session:
        session.add(
            Job(
                id="job-1",
                target_id="ubuntu",
                scenario_id="capture",
                execution_mode="immediate",
                status="waiting_for_capture",
                started_at=created_at,
            )
        )
        session.add(
            JobRuntime(
                job_id="job-1",
                role="target",
                vm_name="astinuslab-job-1",
                state="running",
                power_state="on",
                created_at=created_at,
                updated_at=created_at,
            )
        )

    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn()),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    assert result["zombie_runtimes_failed"] == 1
    with in_memory_session_scope() as session:
        runtime = session.query(JobRuntime).filter_by(job_id="job-1", role="target").one()
        job = session.get(Job, "job-1")
        assert runtime.state == "teardown_failed"
        assert runtime.power_state == "unknown"
        assert job.status == "failed"
        assert "reconciler" in (job.error_summary or "")


def test_reconciler_promotes_booting_vm_to_running(in_memory_session_scope):
    from astinuslab.playground import repositories as pr
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    with in_memory_session_scope() as session:
        pr.create_playground_session(session, job_id="pg-1")
        pr.register_vm(
            session,
            vm_id="vm-1",
            job_id="pg-1",
            label="tgt",
            image_id="ubuntu-22.04",
            domain_name="astinuslab-pg-1-tgt",
            disk_path="/x.qcow2",
            role="target",
        )
        pr.update_vm_state(session, "vm-1", "booting")
        session.get(JobVM, "vm-1").updated_at = datetime.now(UTC) - timedelta(minutes=10)

    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn(domains=[_FakeDomain("astinuslab-pg-1-tgt", active=True)])),
        libvirt_uri="qemu:///test",
        grace_seconds=0,
    )

    assert result["power_states_synced"] == 1
    with in_memory_session_scope() as session:
        assert session.query(JobVM).filter_by(id="vm-1").one().state == "running"


def test_power_probe_cannot_overwrite_concurrent_destroy_claim(in_memory_session_scope):
    from astinuslab.playground import repositories as pr
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)

    class RacingDomain(_FakeDomain):
        def isActive(self) -> bool:
            with in_memory_session_scope() as session:
                if session.get(JobVM, "vm-1").state == "running":
                    assert pr.queue_vm_for_destroy(session, "vm-1") is True
                    assert pr.claim_vm_for_destroy(session, "vm-1") is True
            return False

    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(
            _FakeConn(
                domains=[RacingDomain("astinuslab-pg-pg-1-web")],
                networks=[_FakeNetwork("astinuslab-pg-pg-1-lan")],
            )
        ),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    assert result["power_states_synced"] == 0
    with in_memory_session_scope() as session:
        assert session.get(JobVM, "vm-1").state == "destroying"


def test_orphan_nic_snapshot_rechecks_new_pending_owner_before_detach(in_memory_session_scope):
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    mac = "52:54:00:aa:bb:44"

    class AttachRaceDomain(_FakeDomain):
        detached = False
        inserted = False

        def XMLDesc(self, _flags: int) -> str:
            if not self.inserted:
                with in_memory_session_scope() as session:
                    session.add(
                        JobVMNic(
                            id="nic-race",
                            job_vm_id="vm-1",
                            job_network_id="net-1",
                            mac=mac,
                            state="pending",
                            created_at=created_at,
                        )
                    )
                self.inserted = True
            return f"<domain><devices><interface><mac address='{mac}'/></interface></devices></domain>"

        def detachDeviceFlags(self, _xml: str, _flags: int) -> None:
            self.detached = True

    domain = AttachRaceDomain("astinuslab-pg-pg-1-web")
    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn(domains=[domain], networks=[_FakeNetwork("astinuslab-pg-pg-1-lan")])),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    assert result["host_orphan_nics_detached"] == 0
    assert domain.detached is False
    with in_memory_session_scope() as session:
        assert session.get(JobVMNic, "nic-race").state == "active"


def test_pending_nic_cleanup_cannot_delete_same_vm_spawn_reclaim(in_memory_session_scope):
    from astinuslab.playground import repositories as pr
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    with in_memory_session_scope() as session:
        session.add(
            JobVMNic(
                id="nic-pending-race",
                job_vm_id="vm-1",
                job_network_id="net-1",
                mac="52:54:00:aa:bb:55",
                state="pending",
                created_at=created_at,
            )
        )

    class SpawnRaceDomain(_FakeDomain):
        xml_calls = 0

        def XMLDesc(self, _flags: int) -> str:
            self.xml_calls += 1
            if self.xml_calls == 2:
                with in_memory_session_scope() as session:
                    vm = session.get(JobVM, "vm-1")
                    vm.state = "failed"
                    vm.updated_at = datetime.now(UTC)
                    session.flush()
                    assert pr.claim_vm_for_spawn(session, "vm-1") is True
            return "<domain><devices/></domain>"

    domain = SpawnRaceDomain("astinuslab-pg-pg-1-web")
    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn(domains=[domain], networks=[_FakeNetwork("astinuslab-pg-pg-1-lan")])),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    assert result["pending_nics_dropped"] == 0
    with in_memory_session_scope() as session:
        assert session.get(JobVM, "vm-1").state == "starting"
        assert session.get(JobVMNic, "nic-pending-race") is not None


def test_pending_nic_reconcile_cannot_promote_recreated_same_id_row(in_memory_session_scope):
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    original_mac = "52:54:00:aa:bb:61"
    replacement_mac = "52:54:00:aa:bb:62"
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    with in_memory_session_scope() as session:
        session.add(
            JobVMNic(
                id="nic-reused",
                job_vm_id="vm-1",
                job_network_id="net-1",
                mac=original_mac,
                state="pending",
                created_at=created_at,
            )
        )

    class RecreatedNicDomain(_FakeDomain):
        xml_calls = 0

        def XMLDesc(self, _flags: int) -> str:
            self.xml_calls += 1
            if self.xml_calls == 2:
                with in_memory_session_scope() as session:
                    original = session.get(JobVMNic, "nic-reused")
                    session.delete(original)
                    session.flush()
                    session.add(
                        JobVMNic(
                            id="nic-reused",
                            job_vm_id="vm-1",
                            job_network_id="net-1",
                            mac=replacement_mac,
                            state="pending",
                            created_at=datetime.now(UTC),
                        )
                    )
            return f"<domain><devices><interface><mac address='{original_mac}'/></interface></devices></domain>"

    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(
            _FakeConn(
                domains=[RecreatedNicDomain("astinuslab-pg-pg-1-web")],
                networks=[_FakeNetwork("astinuslab-pg-pg-1-lan")],
            )
        ),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    assert result["pending_nics_promoted"] == 0
    with in_memory_session_scope() as session:
        replacement = session.get(JobVMNic, "nic-reused")
        assert replacement.mac == replacement_mac
        assert replacement.state == "pending"


def test_reconciler_demotes_stopping_vm_when_domain_inactive(in_memory_session_scope):
    from astinuslab.playground import repositories as pr
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    with in_memory_session_scope() as session:
        pr.create_playground_session(session, job_id="pg-1")
        pr.register_vm(
            session,
            vm_id="vm-1",
            job_id="pg-1",
            label="tgt",
            image_id="ubuntu-22.04",
            domain_name="astinuslab-pg-1-tgt",
            disk_path="/x.qcow2",
            role="target",
        )
        pr.update_vm_state(session, "vm-1", "stopping")

    reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn(domains=[_FakeDomain("astinuslab-pg-1-tgt", active=False)])),
        libvirt_uri="qemu:///test",
        grace_seconds=0,
    )

    with in_memory_session_scope() as session:
        assert session.query(JobVM).filter_by(id="vm-1").one().state == "stopped"


@pytest.mark.parametrize("vm_state", ["stopping", "teardown_failed"])
@pytest.mark.parametrize("nic_state", ["active", "pending", "attaching"])
def test_cleanup_recovery_retains_network_custody_until_unresponsive_vm_teardown_succeeds(
    in_memory_session_scope: Any, monkeypatch: pytest.MonkeyPatch, vm_state: str, nic_state: str
) -> None:
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    now = datetime.now(UTC)
    _create_running_playground(in_memory_session_scope, created_at=now)
    with in_memory_session_scope() as session:
        session.get(PlaygroundSession, "pg-1").status = "destroy_failed"
        session.get(JobVM, "vm-1").state = vm_state
        session.add(
            JobVMNic(
                id="nic-retained",
                job_vm_id="vm-1",
                job_network_id="net-1",
                mac="52:54:00:aa:bb:88",
                state=nic_state,
                created_at=now,
            )
        )
    domain = _FakeDomain("astinuslab-pg-pg-1-web", destroy_error=True)

    class OrderedNetwork(_FakeNetwork):
        def destroy(self) -> None:
            assert domain.destroyed and domain.undefined
            super().destroy()

    network = OrderedNetwork("astinuslab-pg-pg-1-lan")
    conn = _FakeConn(domains=[domain], networks=[network])
    # Host artifact cleanup is outside this test; its ordering is observed.
    removed: list[Path] = []
    monkeypatch.setattr(po, "_remove_playground_artifacts", removed.append)

    def reconcile() -> dict:
        return reconcile_libvirt_resources(
            session_scope=in_memory_session_scope,
            libvirt_module=_FakeLibvirt(conn),
            libvirt_uri="qemu:///test",
            grace_seconds=120,
        )

    reconcile()
    assert domain.destroy_calls == 1
    assert domain.isActive() and not domain.undefined
    assert network.destroy_calls == 0
    assert removed == []
    with in_memory_session_scope() as session:
        vm = session.get(JobVM, "vm-1")
        nic = session.get(JobVMNic, "nic-retained")
        parent = session.get(PlaygroundSession, "pg-1")
        assert vm is not None and nic is not None and parent is not None
        assert vm.state == "teardown_failed"
        assert nic.state == nic_state
        assert parent.status == "destroy_failed"

    domain._destroy_error = False
    reconcile()
    assert domain.destroyed and domain.undefined
    assert network.destroyed and network.undefined
    assert removed == [Path("/tmp/web.qcow2")]
    with in_memory_session_scope() as session:
        vm = session.get(JobVM, "vm-1")
        parent = session.get(PlaygroundSession, "pg-1")
        assert vm is not None and parent is not None
        assert vm.state == "destroyed"
        assert session.get(JobVMNic, "nic-retained") is None
        assert parent.status == "cancelled"


def test_reconciler_releases_runtimes_whose_job_is_gone(in_memory_session_scope):
    """Runtime rows outliving their job must not block admission forever.

    has_active_non_playground_job() counts every JobRuntime row outside
    (destroyed, pending) as a live forensic job, and capacity.shortage()
    charges it host memory. The zombie sweep above only visits runtimes of
    live jobs, so rows left behind by a deleted or terminal job were never
    re-checked against libvirt and stayed charged forever.
    """
    from astinuslab.db.repositories import has_active_non_playground_job
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(days=7)
    with in_memory_session_scope() as session:
        # Orphan: parent job row no longer exists at all.
        session.add(
            JobRuntime(
                job_id="job-deleted",
                role="target",
                vm_name="astinuslab-job-deleted",
                state="teardown_failed",
                power_state="unknown",
                created_at=created_at,
                updated_at=created_at,
            )
        )
        # Terminal parent whose teardown was recorded as failed.
        session.add(
            Job(
                id="job-done",
                target_id="ubuntu",
                scenario_id="capture",
                execution_mode="manual",
                status="completed",
                created_at=created_at,
                started_at=created_at,
            )
        )
        session.add(
            JobRuntime(
                job_id="job-done",
                role="attacker",
                vm_name="astinuslab-job-done-attacker",
                state="teardown_failed",
                power_state="unknown",
                created_at=created_at,
                updated_at=created_at,
            )
        )

    with in_memory_session_scope() as session:
        assert has_active_non_playground_job(session) is True

    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn()),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    assert result["stale_runtimes_released"] == 2
    with in_memory_session_scope() as session:
        states = {r.vm_name: (r.state, r.power_state) for r in session.query(JobRuntime).all()}
        assert states["astinuslab-job-deleted"] == ("destroyed", "off")
        assert states["astinuslab-job-done-attacker"] == ("destroyed", "off")
        assert has_active_non_playground_job(session) is False


def test_reconciler_keeps_stale_runtime_whose_domain_still_exists(in_memory_session_scope):
    """A leftover domain is real residue: keep the row charged until it is gone."""
    from astinuslab.db.repositories import has_active_non_playground_job
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(days=7)
    with in_memory_session_scope() as session:
        session.add(
            JobRuntime(
                job_id="job-deleted",
                role="target",
                vm_name="astinuslab-job-deleted",
                state="teardown_failed",
                power_state="unknown",
                created_at=created_at,
                updated_at=created_at,
            )
        )

    conn = _FakeConn(domains=[_FakeDomain("astinuslab-job-deleted")])
    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(conn),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    assert result["stale_runtimes_released"] == 0
    with in_memory_session_scope() as session:
        assert session.query(JobRuntime).one().state == "teardown_failed"
        assert has_active_non_playground_job(session) is True


def test_reconciler_respects_grace_for_recently_terminal_runtimes(in_memory_session_scope):
    """Teardown that just finished may still be publishing; wait out the grace."""
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC)
    with in_memory_session_scope() as session:
        session.add(
            JobRuntime(
                job_id="job-deleted",
                role="target",
                vm_name="astinuslab-job-deleted",
                state="teardown_failed",
                power_state="unknown",
                created_at=created_at,
                updated_at=created_at,
            )
        )

    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn()),
        libvirt_uri="qemu:///test",
        grace_seconds=600,
    )

    assert result["stale_runtimes_released"] == 0
    with in_memory_session_scope() as session:
        assert session.query(JobRuntime).one().state == "teardown_failed"


def _terminal_playground_with_destroyed_network(scope, *, created_at: datetime) -> None:
    with scope() as session:
        session.add(PlaygroundSession(id="pg-old", status="cancelled", started_at=created_at))
        session.add(
            JobNetwork(
                id="net-old",
                session_id="pg-old",
                name="default",
                subnet_cidr="10.43.0.0/24",
                isolated=True,
                libvirt_net_name="astinuslab-pg-pg-old-default",
                state="destroyed",
                created_at=created_at,
                updated_at=created_at,
            )
        )


def test_reconciler_leaves_settled_legacy_networks_alone(in_memory_session_scope):
    """A destroyed row with no libvirt network left is finished work.

    The candidate query used to match any terminal row under a terminal parent
    whether or not host state leaked, so every pass re-claimed the same rows
    (destroyed -> destroying -> destroyed), reported them as recovered and
    rewrote updated_at. On a settled DB that never converged: eight rows
    rewritten once a minute, forever.
    """
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(days=1)
    _terminal_playground_with_destroyed_network(in_memory_session_scope, created_at=created_at)

    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn()),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    assert result["legacy_network_teardowns_recovered"] == 0
    with in_memory_session_scope() as session:
        row = session.get(JobNetwork, "net-old")
        assert row.state == "destroyed"
        # SQLite hands back naive datetimes; compare the instant, not tzinfo.
        assert row.updated_at.replace(tzinfo=UTC) == created_at  # untouched, not rewritten


def test_reconciler_legacy_network_recovery_converges(in_memory_session_scope):
    """A leaked network is torn down once; the next pass finds nothing to do."""
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(days=1)
    _terminal_playground_with_destroyed_network(in_memory_session_scope, created_at=created_at)
    leaked = _FakeNetwork("astinuslab-pg-pg-old-default")

    first = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn(networks=[leaked])),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )
    assert first["legacy_network_teardowns_recovered"] == 1
    assert leaked.destroyed is True

    # The teardown removed it from libvirt, so the second pass sees no residue.
    second = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn()),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )
    assert second["legacy_network_teardowns_recovered"] == 0


def test_reconciler_finalizes_failed_legacy_network_once(in_memory_session_scope):
    """A failed row still blocks session finalization, so settle it — but once."""
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(days=1)
    with in_memory_session_scope() as session:
        session.add(PlaygroundSession(id="pg-old", status="failure_cleanup", started_at=created_at))
        session.add(
            JobNetwork(
                id="net-old",
                session_id="pg-old",
                name="default",
                subnet_cidr="10.43.0.0/24",
                isolated=True,
                libvirt_net_name="astinuslab-pg-pg-old-default",
                state="failed",
                created_at=created_at,
                updated_at=created_at,
            )
        )

    first = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn()),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )
    assert first["legacy_network_teardowns_recovered"] == 1
    with in_memory_session_scope() as session:
        assert session.get(JobNetwork, "net-old").state == "destroyed"
        # No dirty children left, so the parent is released too.
        assert session.get(PlaygroundSession, "pg-old").status == "failed"

    second = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn()),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )
    assert second["legacy_network_teardowns_recovered"] == 0


def test_reconciler_survives_duplicate_session_name_network_rows(in_memory_session_scope):
    """Duplicate (session_id, name) rows must not abort the whole reconcile run.

    ux_job_networks_session_id_name admits duplicates only while destroyed, so
    moving a second sibling out of 'destroyed' in the same pass raises
    IntegrityError. That escaped the per-row error handling and killed every
    remaining step of the run.
    """
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    created_at = datetime.now(UTC) - timedelta(days=1)
    with in_memory_session_scope() as session:
        session.add(PlaygroundSession(id="pg-old", status="cancelled", started_at=created_at))
        for network_id in ("net-dup-a", "net-dup-b"):
            session.add(
                JobNetwork(
                    id=network_id,
                    session_id="pg-old",
                    name="default",
                    subnet_cidr="10.43.0.0/24",
                    isolated=True,
                    libvirt_net_name="astinuslab-pg-pg-old-default",
                    state="destroyed",
                    created_at=created_at,
                    updated_at=created_at,
                )
            )

    # Teardown of the first sibling fails, parking that row in 'failed' —
    # which occupies the session+name slot the second sibling's claim needs.
    leaked = _FakeNetwork("astinuslab-pg-pg-old-default", destroy_error=True)
    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn(networks=[leaked])),
        libvirt_uri="qemu:///test",
        grace_seconds=60,
    )

    # The blocked sibling is skipped, and the rest of the run still completes.
    assert result["legacy_network_teardowns_recovered"] == 0
    with in_memory_session_scope() as session:
        states = sorted(n.state for n in session.query(JobNetwork).all())
        assert states == ["destroyed", "failed"]


# ---------------------------------------------------------------------------
# Stale NIC detach recovery
# ---------------------------------------------------------------------------
#
# A worker can die after claiming a queued detach and before the libvirt call,
# which strands the NIC in `detach_claimed` with nothing left to finish it. The
# recovery pass reopens only *stale, exact* claims. Getting the boundary wrong
# in either direction is a real fault: too eager and two workers mutate the same
# host interface, too timid and the NIC is stranded until an operator notices.


class _RecordingDetachTask:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def delay(self, **kwargs) -> None:
        self.calls.append(kwargs)


def _nic_recovery_fixture(scope, *, created_at: datetime) -> None:
    _create_running_playground(scope, created_at=created_at)
    with scope() as session:
        session.add(
            JobVMNic(
                id="nic-1",
                job_vm_id="vm-1",
                job_network_id="net-1",
                mac="52:54:00:aa:bb:01",
                ordinal=1,
                state="active",
                created_at=created_at,
            )
        )


def _set_nic(scope, **fields) -> None:
    with scope() as session:
        nic = session.get(JobVMNic, "nic-1")
        for key, value in fields.items():
            setattr(nic, key, value)


def _nic(scope) -> dict:
    with scope() as session:
        nic = session.get(JobVMNic, "nic-1")
        return {"state": nic.state, "token": nic.detach_token, "claimed_at": nic.detach_claimed_at}


def _run_reconcile(scope, task, monkeypatch):
    from astinuslab.playground import orchestrator
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    # The Celery task, not the moved body: reconciliation_phases reaches
    # `orchestrator.detach_nic` and calls `.delay(...)` on it. The wrapper and
    # the function it delegates to share a name, so patching the wrong one here
    # silently stops intercepting.
    monkeypatch.setattr(orchestrator, "detach_nic", task)
    # Present the fixture's own domain and network. An empty connection would
    # make vm-1 a zombie in the row-reconciliation phase and hand it to legacy
    # teardown recovery, so the pass under test would no longer be this one.
    conn = _FakeConn(
        domains=[_FakeDomain("astinuslab-pg-pg-1-web")],
        networks=[_FakeNetwork("astinuslab-pg-pg-1-lan")],
    )
    return reconcile_libvirt_resources(
        session_scope=scope,
        libvirt_module=_FakeLibvirt(conn),
        libvirt_uri="qemu:///test",
        grace_seconds=0,
    )


def test_queued_detach_is_requeued_so_a_dead_worker_does_not_strand_the_nic(in_memory_session_scope, monkeypatch):
    created_at = datetime.now(UTC) - timedelta(hours=1)
    _nic_recovery_fixture(in_memory_session_scope, created_at=created_at)
    _set_nic(in_memory_session_scope, state="detaching", detach_token="tok-queued")
    task = _RecordingDetachTask()

    result = _run_reconcile(in_memory_session_scope, task, monkeypatch)

    assert result["detaching_nics_requeued"] == 1
    assert task.calls == [{"job_id": "pg-1", "vm_id": "vm-1", "nic_id": "nic-1", "detach_token": "tok-queued"}]
    # Requeueing the durable queued state keeps the token: the worker's
    # queued -> claimed CAS is what admits a single host mutator, so a duplicate
    # delivery is already handled and a new token would only orphan the old one.
    assert _nic(in_memory_session_scope)["token"] == "tok-queued"


def test_a_queued_detach_without_a_token_is_not_requeued(in_memory_session_scope, monkeypatch):
    created_at = datetime.now(UTC) - timedelta(hours=1)
    _nic_recovery_fixture(in_memory_session_scope, created_at=created_at)
    _set_nic(in_memory_session_scope, state="detaching", detach_token=None)
    task = _RecordingDetachTask()

    result = _run_reconcile(in_memory_session_scope, task, monkeypatch)

    assert result["detaching_nics_requeued"] == 0
    assert task.calls == []


def test_a_stale_detach_claim_is_reopened_under_a_fresh_token(in_memory_session_scope, monkeypatch):
    from astinuslab.playground import repositories as pr

    created_at = datetime.now(UTC) - timedelta(hours=1)
    _nic_recovery_fixture(in_memory_session_scope, created_at=created_at)
    stale_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=pr.NIC_DETACH_CLAIM_SECONDS + 60)
    _set_nic(
        in_memory_session_scope,
        state="detach_claimed",
        detach_token="tok-abandoned",
        detach_claimed_at=stale_at,
    )
    task = _RecordingDetachTask()

    result = _run_reconcile(in_memory_session_scope, task, monkeypatch)

    assert result["detaching_nics_requeued"] == 1
    after = _nic(in_memory_session_scope)
    assert after["state"] == "detaching"
    assert after["claimed_at"] is None
    # A new token, not the abandoned one: if the original worker is merely slow
    # rather than dead, its token must no longer admit a host mutation.
    assert after["token"] != "tok-abandoned"
    assert task.calls[0]["detach_token"] == after["token"]


def test_a_fresh_detach_claim_stays_exclusive(in_memory_session_scope, monkeypatch):
    created_at = datetime.now(UTC) - timedelta(hours=1)
    _nic_recovery_fixture(in_memory_session_scope, created_at=created_at)
    _set_nic(
        in_memory_session_scope,
        state="detach_claimed",
        detach_token="tok-live",
        detach_claimed_at=datetime.now(UTC).replace(tzinfo=None),
    )
    task = _RecordingDetachTask()

    result = _run_reconcile(in_memory_session_scope, task, monkeypatch)

    # The worker holding this claim is still within its lease. Reopening it here
    # would put a second mutator on the same host interface.
    assert result["detaching_nics_requeued"] == 0
    assert task.calls == []
    assert _nic(in_memory_session_scope)["token"] == "tok-live"


def test_a_stale_claim_that_moves_under_the_reopen_is_not_requeued(in_memory_session_scope, monkeypatch):
    from astinuslab.playground import repositories as pr

    created_at = datetime.now(UTC) - timedelta(hours=1)
    _nic_recovery_fixture(in_memory_session_scope, created_at=created_at)
    stale_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=pr.NIC_DETACH_CLAIM_SECONDS + 60)
    _set_nic(
        in_memory_session_scope,
        state="detach_claimed",
        detach_token="tok-abandoned",
        detach_claimed_at=stale_at,
    )
    task = _RecordingDetachTask()
    monkeypatch.setattr(pr, "reopen_nic_detach_claim", lambda *args, **kwargs: False)

    result = _run_reconcile(in_memory_session_scope, task, monkeypatch)

    # The CAS lost, so something else owns the claim now. Dispatching anyway
    # would hand a token to a worker the database never admitted.
    assert result["detaching_nics_requeued"] == 0
    assert task.calls == []


def test_a_stale_claim_whose_vm_is_gone_is_skipped(in_memory_session_scope, monkeypatch):
    from astinuslab.playground import repositories as pr

    created_at = datetime.now(UTC) - timedelta(hours=1)
    _nic_recovery_fixture(in_memory_session_scope, created_at=created_at)
    stale_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=pr.NIC_DETACH_CLAIM_SECONDS + 60)
    _set_nic(
        in_memory_session_scope,
        state="detach_claimed",
        detach_token="tok-abandoned",
        detach_claimed_at=stale_at,
        job_vm_id="vm-does-not-exist",
    )
    task = _RecordingDetachTask()

    result = _run_reconcile(in_memory_session_scope, task, monkeypatch)

    assert result["detaching_nics_requeued"] == 0
    assert task.calls == []


# ---------------------------------------------------------------------------
# Incomplete NIC rows
# ---------------------------------------------------------------------------
#
# A NIC row is written before the host attach and promoted after it. A crash
# between the two leaves it `pending`/`attaching` forever. This pass decides
# each one by asking the host, and every drop is a compare-and-swap against the
# row and its parent: a concurrent retry that has already moved either one must
# win, or reconciliation deletes an interface a live attach is still using.


class _MacDomain(_FakeDomain):
    def __init__(self, name: str, macs: tuple[str, ...] = ()) -> None:
        super().__init__(name)
        self._macs = macs

    def XMLDesc(self, _flags: int) -> str:
        interfaces = "".join(f"<interface><mac address='{mac}'/></interface>" for mac in self._macs)
        return f"<domain><devices>{interfaces}</devices></domain>"


def _reconcile_with(scope, *, domain, grace_seconds=0, monkeypatch=None):
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    if monkeypatch is not None:
        # With no domain on the host the VM row becomes a zombie, and legacy
        # teardown recovery then reaches for its real disk path. That is a
        # different pass; stub its file removal so these cases exercise the
        # incomplete-NIC decision and nothing else.
        from astinuslab.playground import orchestrator as po

        monkeypatch.setattr(po, "_remove_playground_artifacts", lambda _path: None)
        monkeypatch.setattr(po, "_playground_artifacts_present", lambda _path: False)
    domains = [domain] if domain is not None else []
    conn = _FakeConn(domains=domains, networks=[_FakeNetwork("astinuslab-pg-pg-1-lan")])
    return reconcile_libvirt_resources(
        session_scope=scope,
        libvirt_module=_FakeLibvirt(conn),
        libvirt_uri="qemu:///test",
        grace_seconds=grace_seconds,
    )


def _add_incomplete_nic(scope, *, state="pending", created_at, nic_id="nic-p", mac="52:54:00:aa:bb:77"):
    with scope() as session:
        session.add(
            JobVMNic(
                id=nic_id,
                job_vm_id="vm-1",
                job_network_id="net-1",
                mac=mac,
                ordinal=3,
                state=state,
                created_at=created_at,
            )
        )


def _nic_state(scope, nic_id="nic-p"):
    with scope() as session:
        nic = session.get(JobVMNic, nic_id)
        return None if nic is None else nic.state


@pytest.mark.parametrize("state", ["pending", "attaching"])
def test_an_incomplete_nic_whose_mac_is_on_the_host_is_promoted(in_memory_session_scope, state):
    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    mac = "52:54:00:aa:bb:77"
    _add_incomplete_nic(in_memory_session_scope, state=state, created_at=created_at, mac=mac)

    result = _reconcile_with(
        in_memory_session_scope,
        domain=_MacDomain("astinuslab-pg-pg-1-web", (mac,)),
    )

    assert result["pending_nics_promoted"] == 1
    assert _nic_state(in_memory_session_scope) == "active"


def test_promotion_matches_the_host_mac_case_insensitively(in_memory_session_scope):
    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    _add_incomplete_nic(in_memory_session_scope, created_at=created_at, mac="52:54:00:AA:BB:77")

    result = _reconcile_with(
        in_memory_session_scope,
        domain=_MacDomain("astinuslab-pg-pg-1-web", ("52:54:00:aa:bb:77",)),
    )

    # libvirt reports lowercase; a row written in upper case is the same NIC.
    assert result["pending_nics_promoted"] == 1
    assert _nic_state(in_memory_session_scope) == "active"


def test_an_incomplete_nic_absent_from_the_host_is_dropped_once_it_is_old_enough(in_memory_session_scope):
    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    _add_incomplete_nic(in_memory_session_scope, created_at=created_at)

    result = _reconcile_with(
        in_memory_session_scope,
        domain=_MacDomain("astinuslab-pg-pg-1-web", ()),
        grace_seconds=0,
    )

    assert result["pending_nics_dropped"] == 1
    assert _nic_state(in_memory_session_scope) is None


def test_a_recent_incomplete_nic_is_left_for_the_attach_still_in_flight(in_memory_session_scope):
    created_at = datetime.now(UTC)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    _add_incomplete_nic(in_memory_session_scope, created_at=created_at)

    result = _reconcile_with(
        in_memory_session_scope,
        domain=_MacDomain("astinuslab-pg-pg-1-web", ()),
        grace_seconds=3600,
    )

    # Within the grace window the attach may simply not have reached the host
    # yet. Dropping here would delete a row the running task still owns.
    assert result["pending_nics_dropped"] == 0
    assert _nic_state(in_memory_session_scope) == "pending"


def test_an_incomplete_nic_on_a_terminal_vm_is_pruned_before_this_pass_sees_it(in_memory_session_scope, monkeypatch):
    created_at = datetime.now(UTC)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    _add_incomplete_nic(in_memory_session_scope, created_at=created_at)
    with in_memory_session_scope() as session:
        session.get(JobVM, "vm-1").state = "destroyed"

    result = _reconcile_with(in_memory_session_scope, domain=None, grace_seconds=3600, monkeypatch=monkeypatch)

    # The orphan-row prune runs first and deletes every NIC whose parent is
    # destroyed, so it -- not the incomplete-NIC pass -- accounts for this row.
    # The destroyed-parent branch inside the incomplete-NIC pass is a guard
    # against the parent being destroyed *between* the two, which a single
    # sequential pass cannot produce.
    assert result["orphan_nic_rows_pruned"] == 1
    assert result["pending_nics_dropped"] == 0
    assert _nic_state(in_memory_session_scope) is None


def test_an_incomplete_nic_whose_parent_row_does_not_exist_is_dropped(in_memory_session_scope, monkeypatch):
    created_at = datetime.now(UTC)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    _add_incomplete_nic(in_memory_session_scope, created_at=created_at)
    with in_memory_session_scope() as session:
        # Point the row at a parent that was never written. The prune above
        # matches only NICs whose parent exists and is terminal, so this row
        # reaches the incomplete-NIC pass, which deletes it under a
        # compare-and-swap that also asserts the parent's absence.
        session.get(JobVMNic, "nic-p").job_vm_id = "vm-never-existed"

    result = _reconcile_with(in_memory_session_scope, domain=None, grace_seconds=3600, monkeypatch=monkeypatch)

    assert result["pending_nics_dropped"] == 1
    assert _nic_state(in_memory_session_scope) is None


def test_a_missing_host_domain_cleans_its_nics_after_confirmed_vm_teardown(in_memory_session_scope, monkeypatch):
    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    _add_incomplete_nic(in_memory_session_scope, created_at=created_at)

    result = _reconcile_with(in_memory_session_scope, domain=None, grace_seconds=0, monkeypatch=monkeypatch)

    # Keep NIC custody through the failed state. The later teardown verifies
    # domain absence and removes artifacts before deleting these NIC rows.
    assert result["zombie_vms_failed"] == 1
    assert result["orphan_nic_rows_pruned"] == 0
    assert result["pending_nics_dropped"] == 0
    with in_memory_session_scope() as session:
        assert session.get(JobVM, "vm-1").state == "destroyed"
    assert _nic_state(in_memory_session_scope) is None


# ---------------------------------------------------------------------------
# Custody loss during legacy network teardown recovery
# ---------------------------------------------------------------------------
#
# Recovery destroys a host network and then records the outcome. Between the
# two, another worker can take the row. Every one of these paths must end with
# the pass declining to claim credit: counting a teardown whose row it no
# longer owns is how a network gets reported destroyed while something else is
# still using it.


def _legacy_failed_network(scope, *, created_at):
    _create_running_playground(scope, created_at=created_at)
    with scope() as session:
        session.get(JobNetwork, "net-1").state = "failed"
        session.get(JobNetwork, "net-1").updated_at = created_at


def _run_network_recovery(scope, network):
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    return reconcile_libvirt_resources(
        session_scope=scope,
        libvirt_module=_FakeLibvirt(_FakeConn(domains=[_FakeDomain("astinuslab-pg-pg-1-web")], networks=[network])),
        libvirt_uri="qemu:///test",
        grace_seconds=0,
    )


@pytest.mark.parametrize("network_state", ["failed", "destroyed", "teardown_failed"])
def test_a_legacy_network_with_attached_vms_is_left_alone(in_memory_session_scope, network_state):
    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    with in_memory_session_scope() as session:
        session.get(JobNetwork, "net-1").state = network_state
        session.get(JobNetwork, "net-1").updated_at = created_at
        session.add(
            JobVMNic(
                id="nic-attached",
                job_vm_id="vm-1",
                job_network_id="net-1",
                mac="52:54:00:aa:bb:99",
                ordinal=2,
                state="active",
                created_at=created_at,
            )
        )

    network = _FakeNetwork("astinuslab-pg-pg-1-lan")
    result = _run_network_recovery(in_memory_session_scope, network)

    # A VM is still plugged into it. Destroying the host network here would cut
    # the interface out from under a running guest.
    assert result["legacy_network_teardowns_recovered"] == 0
    assert network.destroyed is False
    with in_memory_session_scope() as session:
        assert session.get(JobNetwork, "net-1").state == network_state


def test_a_network_claimed_by_another_worker_is_left_alone(in_memory_session_scope, monkeypatch):
    from astinuslab.playground import repositories as pr

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _legacy_failed_network(in_memory_session_scope, created_at=created_at)
    monkeypatch.setattr(pr, "claim_legacy_network_for_teardown", lambda *a, **k: False)

    network = _FakeNetwork("astinuslab-pg-pg-1-lan")
    result = _run_network_recovery(in_memory_session_scope, network)

    # The compare-and-swap lost, so this pass never owned the row.
    assert result["legacy_network_teardowns_recovered"] == 0
    assert network.destroyed is False


def test_a_network_torn_down_but_lost_before_the_record_is_not_counted(in_memory_session_scope, monkeypatch):
    from astinuslab.playground import repositories as pr

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _legacy_failed_network(in_memory_session_scope, created_at=created_at)
    monkeypatch.setattr(pr, "finish_network_destroy", lambda *a, **k: False)

    network = _FakeNetwork("astinuslab-pg-pg-1-lan")
    result = _run_network_recovery(in_memory_session_scope, network)

    # The host network is gone, but the row moved under us between the destroy
    # and the record. Claiming the teardown would attribute it to a row this
    # pass no longer owns.
    assert result["legacy_network_teardowns_recovered"] == 0


def test_a_failed_network_teardown_that_also_loses_custody_is_not_counted(in_memory_session_scope, monkeypatch):
    from astinuslab.playground import repositories as pr

    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _legacy_failed_network(in_memory_session_scope, created_at=created_at)
    monkeypatch.setattr(pr, "finish_network_destroy", lambda *a, **k: False)

    # Both halves fail: the host destroy raises, and the attempt to record the
    # failure then finds the row already taken.
    network = _FakeNetwork("astinuslab-pg-pg-1-lan", destroy_error=True)
    result = _run_network_recovery(in_memory_session_scope, network)

    assert result["legacy_network_teardowns_recovered"] == 0


# ---------------------------------------------------------------------------
# Power-state sync
# ---------------------------------------------------------------------------


def test_a_domain_that_cannot_be_probed_leaves_the_recorded_power_state_alone(in_memory_session_scope):
    created_at = datetime.now(UTC) - timedelta(minutes=10)
    _create_running_playground(in_memory_session_scope, created_at=created_at)
    with in_memory_session_scope() as session:
        session.get(JobVM, "vm-1").state = "booting"

    class _UnprobableDomain(_FakeDomain):
        def isActive(self):
            raise _FakeLibvirtError("cannot probe")

    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(
            _FakeConn(
                domains=[_UnprobableDomain("astinuslab-pg-pg-1-web")],
                networks=[_FakeNetwork("astinuslab-pg-pg-1-lan")],
            )
        ),
        libvirt_uri="qemu:///test",
        grace_seconds=0,
    )

    # An unreadable domain is not evidence the guest stopped. Recording
    # "stopped" here would tell the operator a running lab had gone away.
    assert result["power_states_synced"] == 0
    with in_memory_session_scope() as session:
        assert session.get(JobVM, "vm-1").state == "booting"


# ---------------------------------------------------------------------------
# The phases module reaches primitives at call time
# ---------------------------------------------------------------------------


def test_a_patched_primitive_reaches_the_phases_module(in_memory_session_scope, monkeypatch):
    """`reconciliation_phases` must resolve shared helpers through the module.

    The phases were split out of `reconciliation.py` but still use its
    primitives. They reach them as `rc.<name>`, resolved at call time, so a
    test or caller patching `reconciliation._owned_domain_name` affects them.
    A `from .reconciliation import _owned_domain_name` in the phases module
    would bind the original instead and ignore the patch.

    The phases module is imported here *before* the patch on purpose. The
    entry point imports it lazily, so in a process where nothing has
    reconciled yet a by-name import would happen after the patch and pick the
    patched value up anyway -- which would make this case pass under either
    design and prove nothing. Importing first reproduces the normal state of a
    long-running worker, where the binding is already made.
    """
    from astinuslab.playground import reconciliation, reconciliation_phases
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    assert reconciliation_phases  # imported before the patch, see above

    orphan = _FakeDomain("astinuslab-pg-missing-web")
    monkeypatch.setattr(reconciliation, "_owned_domain_name", lambda _name: False)

    result = reconcile_libvirt_resources(
        session_scope=in_memory_session_scope,
        libvirt_module=_FakeLibvirt(_FakeConn(domains=[orphan], networks=[])),
        libvirt_uri="qemu:///test",
        grace_seconds=0,
    )

    # With ownership denied, the orphan pass must claim nothing and destroy
    # nothing. If the patch had not reached the phase, this domain would have
    # been torn down.
    assert result["orphan_domains_destroyed"] == 0
    assert orphan.destroyed is False
