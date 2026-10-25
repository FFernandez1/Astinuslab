from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from astinuslab.db import SessionLocal
from astinuslab.jobs.celery_worker import celery_app
from tests.python._support.helpers import autospec_setattr, isolated_playground_database


@pytest.fixture(autouse=True)
def _eager(monkeypatch, tmp_path):
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    with isolated_playground_database(monkeypatch, tmp_path, globals()):
        yield


def test_cleanup_manager_destroys_all_vms_and_networks_when_playground_session_ends(monkeypatch, tmp_path):
    from astinuslab.orchestration.lab_lifecycle import cleanup_terminated_job
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import (
        create_network,
        create_session,
        spawn_vm,
    )

    fake_lifecycle = MagicMock()
    fake_lifecycle.get_ip.return_value = "10.0.0.5"
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)
    base_image = tmp_path / "base.qcow2"
    base_image.write_bytes(b"placeholder image")
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda _, *, role="target": {
            "id": "img",
            "path": str(base_image),
            "format": "qcow2",
            "architecture": "x86_64",
            "credentials": {"username": "u", "password": "p"},
        },
    )
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(
            playground_disk_dir=tmp_path / "playground",
            playground_default_vcpus=2,
            playground_default_memory_mb=2048,
            libvirt_uri="qemu:///system",
            base_dir=tmp_path,
            data_dir=tmp_path,
        ),
    )

    create_session.delay(job_id="pg-1").get()
    create_network.delay(
        job_id="pg-1",
        network_id="n1",
        name="ab",
        subnet_cidr="10.0.0.0/24",
        isolated=True,
    ).get()
    spawn_vm.delay(
        job_id="pg-1",
        vm_id="v1",
        label="x",
        image_id="img",
        nic_specs=[{"network_id": "n1", "nic_id": "nic1"}],
    ).get()

    cleanup_terminated_job(job_id="pg-1")

    with SessionLocal() as s:
        from astinuslab.db.models import JobNetwork, JobVM, PlaygroundSession

        assert all(v.state == "destroyed" for v in s.query(JobVM).filter_by(session_id="pg-1"))
        assert all(n.state == "destroyed" for n in s.query(JobNetwork).filter_by(session_id="pg-1"))
        assert s.query(PlaygroundSession).filter_by(id="pg-1").one().status == "cancelled"


def test_cleanup_terminated_job_delegates_playground_sessions_to_shared_purge(monkeypatch):
    from astinuslab.orchestration.lab_lifecycle import cleanup_terminated_job
    from astinuslab.playground import cleanup as playground_cleanup
    from astinuslab.playground.orchestrator import create_session

    calls = []

    def fake_purge_session_resources(*, job_id: str, final_status: str, error_summary: str | None = None):
        calls.append((job_id, final_status, error_summary))

    monkeypatch.setattr(playground_cleanup, "purge_session_resources", fake_purge_session_resources)

    create_session.delay(job_id="pg-shared").get()

    cleanup_terminated_job(job_id="pg-shared")

    assert calls == [("pg-shared", "cancelled", None)]


def test_purge_session_resources_marks_destroying_before_running_cleanups(monkeypatch, tmp_path):
    from astinuslab.db import SessionLocal
    from astinuslab.db.models import PlaygroundSession
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground import repositories as pr
    from astinuslab.playground.cleanup import purge_session_resources
    from astinuslab.playground.orchestrator import create_session

    create_session.delay(job_id="pg-marker").get()
    pr_register = pr.register_vm

    states_seen: list[str] = []

    def capture_state_then_register(*args, **kwargs):
        with SessionLocal() as s:
            states_seen.append(s.query(PlaygroundSession).filter_by(id="pg-marker").one().status)
        return pr_register(*args, **kwargs)

    # Spawn a placeholder VM directly so purge has work to do
    with SessionLocal() as s:
        pr.register_vm(
            s,
            vm_id="vm-marker",
            job_id="pg-marker",
            label="m",
            image_id="img",
            domain_name="dom-marker",
            disk_path=str(tmp_path / "m.qcow2"),
        )
        pr.update_vm_state(s, "vm-marker", "running")
        s.commit()

    seen: list[str] = []

    original_destroy_vm = po.destroy_vm

    def observing_destroy_vm(*, vm_id):
        with SessionLocal() as s:
            seen.append(s.query(PlaygroundSession).filter_by(id="pg-marker").one().status)
        with SessionLocal() as s:
            pr.update_vm_state(s, vm_id, "destroyed")
            s.commit()
        return vm_id

    autospec_setattr(monkeypatch, po.destroy_vm, "si", lambda **kw: original_destroy_vm.si(**kw))
    autospec_setattr(monkeypatch, po.destroy_vm, "apply", lambda *a, **kw: observing_destroy_vm(**kw.get("kwargs", {})))

    purge_session_resources(job_id="pg-marker", final_status="cancelled")

    assert seen == ["destroying"]


def test_purge_session_resources_marks_cancelled_when_all_cleanups_succeed(monkeypatch, tmp_path):
    from astinuslab.db import SessionLocal
    from astinuslab.db.models import PlaygroundSession
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground import repositories as pr
    from astinuslab.playground.cleanup import purge_session_resources
    from astinuslab.playground.orchestrator import create_session

    playground_disk_dir = tmp_path / "playground"
    disk = playground_disk_dir / "pg-final" / "f.qcow2"
    disk.parent.mkdir(parents=True)
    disk.write_bytes(b"overlay")
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(playground_disk_dir=playground_disk_dir, libvirt_uri="qemu:///system"),
    )

    create_session.delay(job_id="pg-final").get()
    with SessionLocal() as s:
        pr.register_vm(
            s,
            vm_id="vm-final",
            job_id="pg-final",
            label="f",
            image_id="img",
            domain_name="dom-final",
            disk_path=str(disk),
        )
        pr.update_vm_state(s, "vm-final", "running")
        s.commit()

    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: MagicMock())

    purge_session_resources(job_id="pg-final", final_status="cancelled")

    with SessionLocal() as s:
        assert s.query(PlaygroundSession).filter_by(id="pg-final").one().status == "cancelled"


def test_purge_session_resources_marks_destroy_failed_when_vm_teardown_fails(monkeypatch, tmp_path):
    from astinuslab.db import SessionLocal
    from astinuslab.db.models import PlaygroundSession
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground import repositories as pr
    from astinuslab.playground.cleanup import purge_session_resources
    from astinuslab.playground.orchestrator import create_session

    create_session.delay(job_id="pg-failvm").get()
    with SessionLocal() as s:
        pr.register_vm(
            s,
            vm_id="vm-fail",
            job_id="pg-failvm",
            label="x",
            image_id="img",
            domain_name="dom-fail",
            disk_path=str(tmp_path / "x.qcow2"),
        )
        pr.update_vm_state(s, "vm-fail", "running")
        s.commit()

    failing_lifecycle = MagicMock()
    failing_lifecycle.destroy.side_effect = RuntimeError("libvirt down")
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: failing_lifecycle)

    purge_session_resources(job_id="pg-failvm", final_status="cancelled")

    with SessionLocal() as s:
        assert s.query(PlaygroundSession).filter_by(id="pg-failvm").one().status == "destroy_failed"


def test_session_destroy_during_fresh_spawn_and_create_stays_blocking_until_handoff(tmp_path):
    from astinuslab.db import SessionLocal
    from astinuslab.db.models import JobNetwork, JobVM, PlaygroundSession
    from astinuslab.playground import reconciliation
    from astinuslab.playground import repositories as pr
    from astinuslab.playground.cleanup import purge_session_resources

    with SessionLocal() as session:
        pr.create_playground_session(session, job_id="pg-race")
        pr.register_network(
            session,
            network_id="net-race",
            job_id="pg-race",
            name="lan",
            subnet_cidr="10.42.80.0/24",
            isolated=True,
            libvirt_net_name="astinuslab-pg-pg-race-lan",
        )
        pr.register_vm(
            session,
            vm_id="vm-race",
            job_id="pg-race",
            label="web",
            image_id="img",
            domain_name="astinuslab-pg-pg-race-web",
            disk_path=str(tmp_path / "web.qcow2"),
        )
        assert pr.claim_network_for_create(session, "net-race") is True
        assert pr.claim_vm_for_spawn(session, "vm-race") is True
        network_claim = session.get(JobNetwork, "net-race")
        vm_claim = session.get(JobVM, "vm-race")
        assert network_claim is not None
        assert vm_claim is not None
        session.refresh(network_claim)
        session.refresh(vm_claim)
        network_token = network_claim.updated_at
        vm_token = vm_claim.updated_at
        session.commit()

    purge_session_resources(job_id="pg-race", final_status="cancelled")

    with SessionLocal() as session:
        parent = session.get(PlaygroundSession, "pg-race")
        assert parent is not None
        assert parent.status == "destroy_failed"
        assert pr.active_playground_session(session) is not None
        # The workers retain fresh custody and can publish; the next
        # reconciliation pass transfers their passive handoff to cleanup.
        assert (
            pr.finish_vm_spawn(
                session,
                "vm-race",
                "running",
                expected_updated_at=vm_token,
            )
            is True
        )
        assert (
            pr.finish_network_create(
                session,
                "net-race",
                "active",
                expected_updated_at=network_token,
            )
            is True
        )
        reconciliation._queue_terminal_session_resources(session, datetime.now(UTC))
        session.expire_all()
        vm = session.get(JobVM, "vm-race")
        network = session.get(JobNetwork, "net-race")
        assert vm is not None
        assert network is not None
        assert vm.state == "teardown_failed"
        assert network.state == "failed"


def test_active_playground_session_includes_destroy_states(tmp_path):
    from astinuslab.db import SessionLocal
    from astinuslab.db.models import PlaygroundSession
    from astinuslab.playground import repositories as pr
    from astinuslab.playground.orchestrator import create_session

    create_session.delay(job_id="pg-destroy-req").get()
    with SessionLocal() as s:
        job = s.query(PlaygroundSession).filter_by(id="pg-destroy-req").one()
        job.status = "destroy_requested"
        s.commit()
        assert pr.active_playground_session(s) is not None
        active = pr.active_playground_session(s)
        assert active is not None
        assert active.id == "pg-destroy-req"

        job.status = "destroying"
        s.commit()
        assert pr.active_playground_session(s) is not None

        job.status = "destroy_failed"
        s.commit()
        assert pr.active_playground_session(s) is not None
