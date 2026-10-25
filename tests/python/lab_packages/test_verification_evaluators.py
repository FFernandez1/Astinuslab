from __future__ import annotations

import base64
import json

import pytest

from astinuslab.lab_packages import experience_runner
from astinuslab.lab_packages.models import GuestAssertion, GuestObservation, LabReadinessProbe
from astinuslab.lab_packages.verification_evaluators import (
    build_checkpoint_evaluator,
    build_readiness_checker,
)
from astinuslab.playground import repositories
from astinuslab.virtualization.guest_observation import (
    GuestObservationError,
    QgaGuestObservationProvider,
    normalize_observation_path,
)


def _session_with_vm(session, *, session_id: str = "pg-evaluator"):
    repositories.create_playground_session(session, job_id=session_id)
    repositories.register_network(
        session,
        network_id=f"net-{session_id}",
        job_id=session_id,
        name="isolated",
        subnet_cidr="192.0.2.0/29",
        isolated=True,
        libvirt_net_name=f"astinuslab-{session_id}",
    )
    vm = repositories.register_vm(
        session,
        vm_id=f"vm-{session_id}",
        job_id=session_id,
        label="target",
        image_id="ubuntu",
        domain_name=f"domain-{session_id}",
        disk_path="/tmp/unused.qcow2",
    )
    repositories.attach_nic(
        session,
        nic_id=f"nic-a-{session_id}",
        vm_id=vm.id,
        network_id=f"net-{session_id}",
        mac="52:54:00:00:00:01",
        ordinal=1,
    ).ip = "192.0.2.2"
    repositories.attach_nic(
        session,
        nic_id=f"nic-b-{session_id}",
        vm_id=vm.id,
        network_id=f"net-{session_id}",
        mac="52:54:00:00:00:02",
        ordinal=0,
    ).ip = "192.0.2.1"
    session.flush()
    return session_id, vm


def test_readiness_uses_lowest_ordinal_guest_ip_and_rejects_command(in_memory_session_scope, monkeypatch):
    calls: list[tuple[str, int]] = []

    def probe(host, probe):
        calls.append((host, probe.port or 0))

    monkeypatch.setattr(experience_runner, "probe_ready", probe)
    with in_memory_session_scope() as session:
        session_id, _ = _session_with_vm(session)
        check = build_readiness_checker(session, session_id)
        assert check("target", (LabReadinessProbe(kind="tcp", port=8080),)) is True
        assert calls == [("192.0.2.1", 8080)]
        assert check("target", (LabReadinessProbe(kind="command", command="true"),)) is False


def test_readiness_returns_false_for_probe_errors_and_missing_vm(in_memory_session_scope, monkeypatch):
    monkeypatch.setattr(
        experience_runner,
        "probe_ready",
        lambda *_args: (_ for _ in ()).throw(experience_runner.ReadinessError("unreachable")),
    )
    with in_memory_session_scope() as session:
        session_id, _ = _session_with_vm(session)
        check = build_readiness_checker(session, session_id)
        assert check("target", (LabReadinessProbe(kind="tcp", port=8080),)) is False
        assert check("missing", (LabReadinessProbe(kind="tcp", port=8080),)) is False


class _Provider:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def observe(self, vm_name, operation):
        self.calls.append((vm_name, operation.path))
        return self.value


def test_checkpoint_evaluator_compares_finite_guest_json_and_observations(in_memory_session_scope):
    with in_memory_session_scope() as session:
        session_id, vm = _session_with_vm(session, session_id="pg-checkpoint")
        provider = _Provider({"ready": True})
        evaluate = build_checkpoint_evaluator(session, session_id, provider=provider)
        assertion = GuestAssertion(vm_id="target", path="state.json", expected={"ready": True})
        observation = GuestObservation(vm_id="target", path="state.json")
        assert evaluate(assertion) is True
        assert evaluate(GuestAssertion(vm_id="target", path="state.json", expected=False)) is False
        assert evaluate(observation) is True
        assert provider.calls == [(vm.domain_name, "state.json")] * 3


def test_checkpoint_assertions_are_type_strict(in_memory_session_scope):
    with in_memory_session_scope() as session:
        session_id, _ = _session_with_vm(session, session_id="pg-checkpoint-types")
        evaluate = build_checkpoint_evaluator(session, session_id, provider=_Provider(True))
        assert evaluate(GuestAssertion(vm_id="target", path="state.json", expected=True)) is True
        assert evaluate(GuestAssertion(vm_id="target", path="state.json", expected=1)) is False


def test_checkpoint_evaluator_fails_closed_for_scope_or_transport(in_memory_session_scope):
    class Broken:
        def observe(self, *_args):
            raise GuestObservationError("agent unavailable")

    with in_memory_session_scope() as session:
        session_id, _ = _session_with_vm(session, session_id="pg-checkpoint-fail")
        evaluate = build_checkpoint_evaluator(session, session_id, provider=Broken())
        assert evaluate(GuestAssertion(vm_id="missing", path="state.json", expected=True)) is False
        assert evaluate(GuestAssertion(vm_id="target", path="state.json", expected=True)) is False


def test_observation_path_is_rooted_and_traversal_is_rejected():
    assert normalize_observation_path("state.json") == "/var/lib/astinuslab/observations/state.json"
    assert normalize_observation_path("/var/lib/astinuslab/observations/state.json") == (
        "/var/lib/astinuslab/observations/state.json"
    )
    for path in ("../state.json", "/etc/passwd", "nested/../state.json", "state\\x.json", "state\x00.json"):
        with pytest.raises(GuestObservationError):
            normalize_observation_path(path)


def test_qga_provider_reads_json_and_closes_handle():
    content = json.dumps({"ready": True}).encode()
    calls = []

    def qga(_uri, _vm, payload, _timeout):
        calls.append(payload)
        command = payload["execute"]
        if command == "guest-file-open":
            return '{"return": 7}'
        if command == "guest-file-read":
            return json.dumps(
                {"return": {"count": len(content), "eof": True, "buf-b64": base64.b64encode(content).decode()}}
            )
        if command == "guest-file-close":
            return '{"return": {}}'
        raise AssertionError(command)

    provider = QgaGuestObservationProvider(libvirt_uri="qemu:///system", qga_command=qga)
    operation = GuestObservation(vm_id="target", path="state.json")
    assert provider.observe("domain", operation) == {"ready": True}
    assert [call["execute"] for call in calls] == ["guest-file-open", "guest-file-read", "guest-file-close"]
    assert calls[0]["arguments"]["path"] == "/var/lib/astinuslab/observations/state.json"


def test_qga_provider_accepts_assertion_operations():
    content = b"true"

    def qga(_uri, _vm, payload, _timeout):
        if payload["execute"] == "guest-file-open":
            return '{"return": 1}'
        if payload["execute"] == "guest-file-read":
            return json.dumps(
                {"return": {"count": len(content), "eof": True, "buf-b64": base64.b64encode(content).decode()}}
            )
        return '{"return": {}}'

    provider = QgaGuestObservationProvider(libvirt_uri="qemu:///system", qga_command=qga)
    operation = GuestAssertion(vm_id="target", path="state.json", expected=True)
    assert provider.observe("domain", operation) is True


def test_qga_provider_rejects_oversized_read():
    def qga(_uri, _vm, payload, _timeout):
        if payload["execute"] == "guest-file-open":
            return '{"return": 1}'
        if payload["execute"] == "guest-file-read":
            data = base64.b64encode(b"x" * 8192).decode()
            return json.dumps({"return": {"count": 8192, "eof": False, "buf-b64": data}})
        return '{"return": {}}'

    provider = QgaGuestObservationProvider(libvirt_uri="qemu:///system", qga_command=qga)
    with pytest.raises(GuestObservationError, match="64 KiB"):
        provider.observe("domain", GuestObservation(vm_id="target", path="state.json"))
