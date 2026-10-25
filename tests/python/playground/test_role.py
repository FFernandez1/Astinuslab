from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from astinuslab.api.schemas import PlaygroundSpawnVMRequest, PlaygroundVMOut
from astinuslab.db.models import JobVM, PlaygroundSession
from astinuslab.playground import repositories as pr


def _vm_out_payload(**overrides):
    base = dict(
        id="vm-1",
        job_id="pg-abc",
        label="t1",
        image_id="ubuntu-22-04-base",
        state="running",
        domain_name="astinuslab-pg-pg-abc-t1",
        role="target",
        created_at="2026-05-09T12:00:00+00:00",
        nics=[],
    )
    base.update(overrides)
    return base


def _spawn_payload(**overrides):
    base = dict(
        label="t1",
        image_id="ubuntu-22-04-base",
        nics=[{"network_id": "n1"}],
    )
    base.update(overrides)
    return base


def test_spawn_request_default_role_is_target():
    req = PlaygroundSpawnVMRequest(**_spawn_payload())
    assert req.role == "target"


def test_spawn_request_accepts_attacker():
    req = PlaygroundSpawnVMRequest(**_spawn_payload(role="attacker"))
    assert req.role == "attacker"


def test_spawn_request_rejects_unknown_role():
    with pytest.raises(ValidationError):
        PlaygroundSpawnVMRequest(**_spawn_payload(role="bystander"))


def test_vm_out_includes_role_and_created_at():
    out = PlaygroundVMOut(**_vm_out_payload())
    assert out.role == "target"
    assert out.created_at.year == 2026


def _fake_vm(**overrides):
    from types import SimpleNamespace

    base = dict(
        id="vm-1",
        session_id="pg-abc",
        label="fw",
        image_id="openwrt",
        state="running",
        domain_name="astinuslab-pg-pg-abc-fw",
        role="network",
        vcpus=1,
        memory_mb=512,
        disk_gb=4,
        created_at=datetime.now(UTC),
        workload_id=None,
        workload_state=None,
        workload_error=None,
        error_summary=None,
        nics=[],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_vm_out_carries_catalog_function_tags(monkeypatch):
    """The map cannot tell a firewall from any other appliance without these."""
    from astinuslab.playground import serializers

    monkeypatch.setattr(
        serializers,
        "playground_image_for",
        lambda role, image_id: {"label": "OpenWrt Network Node", "function_tags": ["firewall", "router"]},
    )

    out = serializers.vm_out(_fake_vm(), session_active=True)

    assert out.function_tags == ["firewall", "router"]


def test_vm_out_function_tags_default_to_empty_without_catalog_entry(monkeypatch):
    from astinuslab.playground import serializers

    monkeypatch.setattr(serializers, "playground_image_for", lambda role, image_id: None)

    out = serializers.vm_out(_fake_vm(role="target"), session_active=True)

    assert out.function_tags == []


def test_register_vm_persists_role_attacker(in_memory_session_scope, monkeypatch):
    from astinuslab import db as db_mod

    monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)
    with in_memory_session_scope() as s:
        j = PlaygroundSession(
            id="pg-x",
            status="running",
            created_at=datetime.now(UTC),
            started_at=datetime.now(UTC),
        )
        s.add(j)
        s.commit()
        pr.register_vm(
            s,
            vm_id="v1",
            job_id="pg-x",
            label="atk",
            image_id="kali-rolling-base",
            domain_name="d",
            disk_path="/tmp/d.qcow2",
            role="attacker",
        )
    with in_memory_session_scope() as s:
        vm = s.query(JobVM).filter_by(id="v1").one()
        assert vm.role == "attacker"


def test_register_vm_default_role_is_target(in_memory_session_scope):
    with in_memory_session_scope() as s:
        j = PlaygroundSession(
            id="pg-x2",
            status="running",
            created_at=datetime.now(UTC),
            started_at=datetime.now(UTC),
        )
        s.add(j)
        s.commit()
        pr.register_vm(
            s,
            vm_id="v2",
            job_id="pg-x2",
            label="tgt",
            image_id="ubuntu-22-04-base",
            domain_name="d",
            disk_path="/tmp/d.qcow2",
        )
    with in_memory_session_scope() as s:
        vm = s.query(JobVM).filter_by(id="v2").one()
        assert vm.role == "target"


def test_serialize_session_includes_role_and_created_at(in_memory_session_scope, monkeypatch):
    from astinuslab import db as db_mod
    from astinuslab.playground import serializers as playground_serializers

    monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)
    with in_memory_session_scope() as s:
        j = PlaygroundSession(
            id="pg-y",
            status="running",
            created_at=datetime.now(UTC),
            started_at=datetime.now(UTC),
        )
        s.add(j)
        s.commit()
        pr.register_vm(
            s,
            vm_id="v3",
            job_id="pg-y",
            label="t",
            image_id="ubuntu-22-04-base",
            domain_name="d",
            disk_path="/tmp/d.qcow2",
            role="attacker",
        )
    with in_memory_session_scope() as s:
        playground_session = s.query(PlaygroundSession).filter_by(id="pg-y").one()
        out = playground_serializers.serialize_session(s, playground_session)
        assert out.vms[0].role == "attacker"
        assert out.vms[0].created_at is not None
