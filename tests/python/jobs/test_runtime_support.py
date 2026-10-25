"""Unit tests for runtime_support — VM name generation and runtime record management."""

from __future__ import annotations

from astinuslab.api.schemas import JobCreate
from astinuslab.db.repositories import JobRepository
from astinuslab.jobs.runtime_support import (
    _build_guest_ip_error,
    _parse_dhcp_lease_candidates,
    _parse_qga_ip_candidates,
    _parse_virsh_domifaddr_candidates,
    mark_runtime_destroyed,
    mark_runtime_teardown_failed,
    runtime_vm_name,
    sync_runtime_record,
)


def _create_job(scope):
    with scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
            )
        )
        return job.id


class TestGuestIpParsers:
    def test_parse_dhcp_lease_candidates_matches_mac_and_strips_prefix(self):
        leases = "\n".join(
            [
                " Expiry Time           MAC address         Protocol   IP address           Hostname   Client ID or DUID",
                " 2026-01-01 00:00:00  52:54:00:aa:bb:dd  ipv4       192.168.122.88/24   other      *",
                " 2026-01-01 00:00:00  52:54:00:AA:BB:CC  ipv4       192.168.122.77/24   target     *",
            ]
        )

        assert _parse_dhcp_lease_candidates(leases, "52:54:00:aa:bb:cc") == ["192.168.122.77/24"]

    def test_parse_virsh_domifaddr_candidates_accepts_mac_or_ipv4_line(self):
        domifaddr = "\n".join(
            [
                " Name       MAC address          Protocol     Address",
                " vnet0      52:54:00:aa:bb:cc    ipv6         fe80::5054:ff:feaa:bbcc/64",
                " vnet0      52:54:00:aa:bb:cc    ipv4         192.168.122.77/24",
                " vnet1      52:54:00:aa:bb:dd    ipv4         192.168.122.88/24",
            ]
        )

        assert _parse_virsh_domifaddr_candidates(domifaddr, "52:54:00:aa:bb:cc") == [
            "192.168.122.77/24",
            "192.168.122.88/24",
        ]

    def test_parse_qga_ip_candidates_skips_loopback_and_link_local(self):
        agent = "\n".join(
            [
                " Name       MAC address          Protocol     Address",
                " lo         00:00:00:00:00:00    ipv4         127.0.0.1/8",
                " enp1s0     52:54:00:aa:bb:cc    ipv4         169.254.2.10/16",
                " enp1s0     52:54:00:aa:bb:cc    ipv4         10.0.0.42/24",
            ]
        )

        assert _parse_qga_ip_candidates(agent) == ["10.0.0.42"]

    def test_build_guest_ip_error_preserves_diagnostic_sections(self):
        error = _build_guest_ip_error(
            vm_state="running",
            libvirt_uri="qemu:///system",
            libvirt_network="default",
            mac="52:54:00:aa:bb:cc",
            domiflist_detail="vnet0 network default virtio 52:54:00:aa:bb:cc",
            last_leases="lease output",
            last_domifaddr="domifaddr output",
            last_domifaddr_err="domifaddr stderr",
            serial_tail="serial line",
        )

        assert error.startswith("Could not resolve guest IP from libvirt DHCP/domifaddr\n")
        assert "VM state: running" in error
        assert "Libvirt URI: qemu:///system" in error
        assert "Libvirt network: default" in error
        assert "Detected MAC: 52:54:00:aa:bb:cc" in error
        assert "Domain interfaces:\nvnet0 network default virtio 52:54:00:aa:bb:cc" in error
        assert "DHCP leases (final poll):\nlease output" in error
        assert "domifaddr (final poll):\ndomifaddr output" in error
        assert "domifaddr stderr: domifaddr stderr" in error
        assert "Serial console (last 60 lines):\nserial line" in error


class TestRuntimeVmName:
    def test_target_role_returns_plain_name(self):
        assert runtime_vm_name("abc123", "target") == "astinuslab-abc123"

    def test_attacker_role_returns_attacker_suffix(self):
        assert runtime_vm_name("abc123", "attacker") == "astinuslab-abc123-attacker"

    def test_name_is_deterministic(self):
        assert runtime_vm_name("job-id", "target") == runtime_vm_name("job-id", "target")


class TestSyncRuntimeRecord:
    def test_updates_ip_address(self, in_memory_session_scope):
        job_id = _create_job(in_memory_session_scope)
        sync_runtime_record(
            in_memory_session_scope,
            JobRepository,
            job_id,
            "target",
            ip_address="10.0.0.5",
        )
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            rt = repo.get_runtime(job_id, "target")
            assert rt is not None
            assert rt.ip_address == "10.0.0.5"

    def test_updates_power_state(self, in_memory_session_scope):
        job_id = _create_job(in_memory_session_scope)
        sync_runtime_record(
            in_memory_session_scope,
            JobRepository,
            job_id,
            "target",
            power_state="on",
        )
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            rt = repo.get_runtime(job_id, "target")
            assert rt is not None
            assert rt.power_state == "on"

    def test_partial_update_leaves_other_fields_unchanged(self, in_memory_session_scope):
        job_id = _create_job(in_memory_session_scope)
        # First set ip
        sync_runtime_record(in_memory_session_scope, JobRepository, job_id, "target", ip_address="1.2.3.4")
        # Then update only state
        sync_runtime_record(in_memory_session_scope, JobRepository, job_id, "target", state="running")
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            rt = repo.get_runtime(job_id, "target")
            assert rt is not None
            assert rt.ip_address == "1.2.3.4"
            assert rt.state == "running"


class TestMarkRuntimeDestroyed:
    def test_sets_state_to_destroyed(self, in_memory_session_scope):
        job_id = _create_job(in_memory_session_scope)
        mark_runtime_destroyed(in_memory_session_scope, JobRepository, job_id, "target")
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            rt = repo.get_runtime(job_id, "target")
            assert rt is not None
            assert rt.state == "destroyed"
            assert rt.power_state == "off"

    def test_clears_ip_address(self, in_memory_session_scope):
        job_id = _create_job(in_memory_session_scope)
        sync_runtime_record(in_memory_session_scope, JobRepository, job_id, "target", ip_address="10.0.0.1")
        mark_runtime_destroyed(in_memory_session_scope, JobRepository, job_id, "target")
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            rt = repo.get_runtime(job_id, "target")
            assert rt is not None
            assert rt.ip_address is None


class TestMarkRuntimeTeardownFailed:
    def test_sets_state_to_teardown_failed(self, in_memory_session_scope):
        job_id = _create_job(in_memory_session_scope)
        mark_runtime_teardown_failed(in_memory_session_scope, JobRepository, job_id, "target")
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            rt = repo.get_runtime(job_id, "target")
            assert rt is not None
            assert rt.state == "teardown_failed"
            assert rt.power_state == "unknown"
