from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from astinuslab.db import Base
from astinuslab.db.models import (
    JobNetwork,
    JobVM,
    JobVMNic,
    LabInstance,
    LabInstanceAction,
    LabOperationOutbox,
    PlaygroundSession,
)
from astinuslab.lab_packages.archive import write_archive
from astinuslab.lab_packages.content_store import ContentStore
from astinuslab.lab_packages.models import (
    AssetReference,
    LabImageSpec,
    LabManifest,
    LabWorkloadSpec,
)
from astinuslab.lab_packages.package_service import LabPackageService
from astinuslab.lab_packages.preflight import HostFacts, PackagePreflight
from tests.python._support.lab_packages import file_session_scope as _file_session_scope
from tests.python._support.lab_packages import launch_definition as _definition


def test_launch_records_one_idempotent_instance_and_outbox(in_memory_session_scope, monkeypatch, tmp_path: Path):
    from astinuslab.lab_packages import launch_service

    source = tmp_path / "launch.astinuslab"
    source.write_bytes(write_archive(_definition(), "# runbook\n"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    monkeypatch.setattr(
        launch_service,
        "preflight_package",
        lambda *_args, **_kwargs: PackagePreflight(HostFacts(4, 8192, 2**40, "x86_64", True), 1, ()),
    )
    monkeypatch.setattr(launch_service, "validate_warning_consent_token", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(launch_service, "lookup_playground_image", lambda *_args, **_kwargs: {"path": str(source)})
    monkeypatch.setattr(
        launch_service, "validate_playground_workload_selection", lambda **_kwargs: type("Result", (), {"ok": True})()
    )

    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        definition = _definition()
        manifest = LabManifest.model_validate(json.loads(record.manifest_json))
        first = launch_service.launch_package(
            session,
            record=record,
            definition=definition,
            manifest=manifest,
            service=service,
            idempotency_key="launch-1",
            acknowledged_warning_token="ack",
        )
        second = launch_service.launch_package(
            session,
            record=record,
            definition=definition,
            manifest=manifest,
            service=service,
            idempotency_key="launch-1",
            acknowledged_warning_token="ack",
        )
        assert first.instance_id == second.instance_id
        assert session.get(LabInstance, first.instance_id).state == "registered"
        assert session.get(PlaygroundSession, first.session_id).lab_package_record_id == record.id
        assert session.query(LabOperationOutbox).filter_by(operation_id=first.instance_id).count() == 1
        event = session.query(LabOperationOutbox).filter_by(operation_id=first.instance_id).one()
        assert Path(json.loads(event.payload_json)["disk_requirements"][0]["directory"]).name == first.session_id


def test_transactional_launch_preserves_refusal_without_durable_intent(in_memory_session_scope, monkeypatch, tmp_path):
    from astinuslab.lab_packages import launch_service
    from astinuslab.playground.workloads import PlaygroundWorkloadValidation

    source = tmp_path / "refused.astinuslab"
    source.write_bytes(write_archive(_definition(), "# Runbook\n"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    monkeypatch.setattr(
        launch_service,
        "preflight_package",
        lambda *a, **k: PackagePreflight(HostFacts(4, 8192, 2**40, "x86_64", True), 1, ()),
    )
    monkeypatch.setattr(launch_service, "validate_warning_consent_token", lambda *a, **k: True)
    monkeypatch.setattr(launch_service, "lookup_playground_image", lambda *a, **k: {"path": str(source)})
    detail = "Workload requires a 20 GiB disk"
    monkeypatch.setattr(
        launch_service,
        "validate_playground_workload_selection",
        lambda **k: PlaygroundWorkloadValidation(ok=False, detail=detail),
    )
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        with pytest.raises(launch_service.PackageLaunchError) as caught:
            launch_service.launch_package(
                session,
                record=record,
                definition=_definition(),
                manifest=LabManifest.model_validate(json.loads(record.manifest_json)),
                service=service,
                idempotency_key="refusal",
                acknowledged_warning_token="ack",
            )
        assert caught.value.code == "workload-incompatible"
        assert detail in str(caught.value) and _definition().vms[0].id in str(caught.value)
        session.rollback()
    with in_memory_session_scope() as session:
        for model in (PlaygroundSession, LabInstance, LabInstanceAction, LabOperationOutbox):
            assert session.query(model).count() == 0


def test_dirty_file_backed_request_session_launches_without_second_writer_lock(monkeypatch, tmp_path: Path):
    from astinuslab.lab_packages import launch_service

    engine = create_engine(f"sqlite:///{tmp_path / 'launch.db'}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, future=True)

    @contextmanager
    def scope():
        session = Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    source = tmp_path / "file-backed.astinuslab"
    source.write_bytes(write_archive(_definition(), "# runbook\n"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    monkeypatch.setattr(
        launch_service,
        "preflight_package",
        lambda *_args, **_kwargs: PackagePreflight(HostFacts(4, 8192, 2**40, "x86_64", True), 1, ()),
    )
    monkeypatch.setattr(launch_service, "validate_warning_consent_token", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(launch_service, "lookup_playground_image", lambda *_args, **_kwargs: {"path": str(source)})
    monkeypatch.setattr(
        launch_service,
        "validate_playground_workload_selection",
        lambda **_kwargs: type("Result", (), {"ok": True})(),
    )

    with scope() as session:
        record = service.import_path(source, session).record
        # This flush intentionally takes SQLite's writer lock before launch.
        record.last_used_at = record.imported_at
        session.flush()
        operation = launch_service.launch_package(
            session,
            record=record,
            definition=_definition(),
            manifest=LabManifest.model_validate(json.loads(record.manifest_json)),
            service=service,
            idempotency_key="file-backed",
            acknowledged_warning_token="ack",
        )
        assert session.get(LabInstance, operation.instance_id) is not None
        assert session.query(LabOperationOutbox).filter_by(operation_id=operation.instance_id).count() == 1


def test_locked_launch_records_verified_immutable_backing_without_catalog_lookup(
    in_memory_session_scope, monkeypatch, tmp_path: Path
):
    from astinuslab.lab_packages import launch_service

    source = tmp_path / "locked.astinuslab"
    source.write_bytes(write_archive(_definition(), "# runbook\n"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    monkeypatch.setattr(
        launch_service,
        "preflight_package",
        lambda *_args, **_kwargs: PackagePreflight(HostFacts(4, 8192, 2**40, "x86_64", True), 1, ()),
    )
    monkeypatch.setattr(launch_service, "validate_warning_consent_token", lambda *_args, **_kwargs: True)
    immutable_layer = tmp_path / "immutable.qcow2"
    immutable_layer.write_bytes(b"immutable layer")
    monkeypatch.setattr(launch_service, "immutable_boot_layer_path", lambda *_args: immutable_layer)
    monkeypatch.setattr(
        launch_service,
        "lookup_playground_image",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("locked launch must not use catalog")),
    )
    layer = AssetReference(
        sha256="a" * 64,
        size_bytes=1,
        media_type="application/x-qcow2",
        license="CC0-1.0",
        redistribution_confirmed=True,
    )
    vm = (
        _definition()
        .vms[0]
        .model_copy(
            update={
                "base_image": LabImageSpec(
                    image_id="ubuntu-24.04", sha256="b" * 64, size_bytes=1, media_type="application/x-qcow2"
                ),
                "boot_layer": layer,
                "workload": LabWorkloadSpec(workload_baked=True),
            }
        )
    )
    definition = _definition().model_copy(
        update={
            "reproducibility": "locked",
            "networks": (_definition().networks[0].model_copy(update={"reachability": "isolated"}),),
            "vms": (vm,),
        }
    )

    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        manifest = LabManifest.model_validate(json.loads(record.manifest_json))
        operation = launch_service.launch_package(
            session,
            record=record,
            definition=definition,
            manifest=manifest,
            service=service,
            idempotency_key="locked",
            acknowledged_warning_token="ack",
        )
        event = session.query(LabOperationOutbox).filter_by(operation_id=operation.instance_id).one()
        payload = json.loads(event.payload_json)
        assert payload["locked"] is True
        assert payload["immutable_backings"]
        binding = next(iter(payload["immutable_backings"].values()))
        assert binding["path"] == str(immutable_layer)
        assert binding["sha256"] == layer.sha256
        assert session.get(LabInstance, operation.instance_id) is not None
        assert session.get(PlaygroundSession, operation.session_id) is not None


def test_coordinator_failure_compensates_durable_playground_rows(in_memory_session_scope, monkeypatch, tmp_path: Path):
    from astinuslab.jobs import package_instance_coordinator as coordinator

    source = tmp_path / "failure.astinuslab"
    source.write_bytes(write_archive(_definition(), "# runbook\n"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        session.add(PlaygroundSession(id="pg-failure", lab_package_record_id=record.id, status="queued"))
        session.add(
            LabInstance(
                id="lpi-failure",
                package_record_id=record.id,
                playground_session_id="pg-failure",
                idempotency_key="failure",
                state="registered",
            )
        )
        session.add(
            LabInstanceAction(id="lia-failure", instance_id="lpi-failure", action_type="launch", payload_json="{}")
        )
        session.add(
            LabOperationOutbox(
                id="loo-failure",
                operation_kind="package_instance_launch",
                operation_id="lpi-failure",
                payload_json='{"disk_requirements": []}',
                state="pending",
            )
        )
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(
        coordinator, "_execute_unlocked_playground", lambda *_args: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    def finish_cleanup(_task, *, job_id: str):
        with in_memory_session_scope() as session:
            session.get(PlaygroundSession, job_id).status = "cancelled"

    monkeypatch.setattr(coordinator, "_invoke", finish_cleanup)

    coordinator.run_instance_operation("lpi-failure")

    with in_memory_session_scope() as session:
        instance = session.get(LabInstance, "lpi-failure")
        assert instance is not None and instance.state == "failed"
        assert instance.playground_session_id is None
        assert session.get(PlaygroundSession, "pg-failure") is None
        assert session.get(LabOperationOutbox, "loo-failure").state == "failed"


def test_locked_coordinator_executes_immutable_backing_and_marks_ready(
    in_memory_session_scope, monkeypatch, tmp_path: Path
):
    from astinuslab.jobs import package_instance_coordinator as coordinator

    monkeypatch.setattr("astinuslab.lab_packages.runner_preflight.require_runner_host_ready", lambda: None)

    source = tmp_path / "locked-coordinator.astinuslab"
    source.write_bytes(write_archive(_definition(), "# runbook\n"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    immutable_layer = tmp_path / "immutable.qcow2"
    immutable_layer.write_bytes(b"immutable")
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        session.add(PlaygroundSession(id="pg-locked", lab_package_record_id=record.id, status="queued"))
        session.add(
            LabInstance(
                id="lpi-locked",
                package_record_id=record.id,
                playground_session_id="pg-locked",
                idempotency_key="locked",
                state="registered",
            )
        )
        session.add(
            JobNetwork(
                id="net-locked",
                session_id="pg-locked",
                name="isolated",
                subnet_cidr="192.0.2.0/29",
                isolated=True,
                libvirt_net_name="astinuslab-pg-locked-net",
                state="pending",
            )
        )
        session.add(
            JobVM(
                id="vm-locked",
                session_id="pg-locked",
                label="target",
                image_id="immutable-target",
                state="pending",
                domain_name="astinuslab-pg-locked-target",
                disk_path=str(tmp_path / "pg-locked" / "target.qcow2"),
            )
        )
        session.add(
            JobVMNic(
                id="nic-locked",
                job_vm_id="vm-locked",
                job_network_id="net-locked",
                mac="52:54:00:00:00:04",
                ip=None,
                ordinal=0,
            )
        )
        payload = {
            "locked": True,
            "immutable_backings": {"vm-locked": {"path": str(immutable_layer), "sha256": "a" * 64, "size_bytes": 9}},
            "disk_requirements": [],
            "readiness": {},
        }
        session.add(
            LabInstanceAction(
                id="lia-locked", instance_id="lpi-locked", action_type="launch", payload_json=json.dumps(payload)
            )
        )
        session.add(
            LabOperationOutbox(
                id="loo-locked",
                operation_kind="package_instance_launch",
                operation_id="lpi-locked",
                payload_json=json.dumps(payload),
                state="pending",
            )
        )
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)
    launched: list[tuple[str, dict]] = []
    reservation_lock_active = False
    host_reservation_lock_states: list[bool] = []
    release_event_states: list[str] = []
    original_reservation_lock = coordinator._reservation_lock
    original_release = coordinator._release_owned_reservation

    @contextmanager
    def observe_reservation_lock(payload, instance_id):
        nonlocal reservation_lock_active
        with original_reservation_lock(payload, instance_id):
            reservation_lock_active = True
            try:
                yield
            finally:
                reservation_lock_active = False

    def boot_locked(session_id: str, runtime_payload: dict, *_args):
        launched.append((session_id, runtime_payload))
        host_reservation_lock_states.append(reservation_lock_active)
        with in_memory_session_scope() as session:
            session.get(JobNetwork, "net-locked").state = "active"
            session.get(JobVM, "vm-locked").state = "running"
            session.get(JobVMNic, "nic-locked").ip = "192.0.2.10"

    def observe_release(claim, reservation):
        with in_memory_session_scope() as session:
            event = session.get(LabOperationOutbox, "loo-locked")
            release_event_states.append(event.state)
        original_release(claim, reservation)

    monkeypatch.setattr(coordinator, "_reservation_lock", observe_reservation_lock)
    monkeypatch.setattr(coordinator, "_release_owned_reservation", observe_release)
    monkeypatch.setattr(coordinator, "_execute_locked_playground", boot_locked)
    monkeypatch.setattr(coordinator, "_verify_overlay_allocation", lambda *_args: None)

    coordinator.run_instance_operation("lpi-locked")

    assert len(launched) == 1
    assert host_reservation_lock_states == [False]
    assert release_event_states == ["claimed"]
    launched_session_id, launched_payload = launched[0]
    assert launched_session_id == "pg-locked"
    assert {key: launched_payload[key] for key in payload} == payload
    assert launched_payload["_coordinator"]["action_id"] == "lia-locked"
    with in_memory_session_scope() as session:
        assert session.get(LabInstance, "lpi-locked").state == "ready"


def test_locked_executor_revalidates_bound_layer_before_spawning(in_memory_session_scope, monkeypatch, tmp_path: Path):
    from astinuslab.jobs import package_instance_coordinator as coordinator

    immutable_layer = tmp_path / "immutable.qcow2"
    immutable_layer.write_bytes(b"immutable")
    with in_memory_session_scope() as session:
        session.add(PlaygroundSession(id="pg-locked-executor", status="queued"))
        session.add(
            JobNetwork(
                id="net-locked-executor",
                session_id="pg-locked-executor",
                name="isolated",
                subnet_cidr="192.0.2.0/29",
                isolated=True,
                libvirt_net_name="astinuslab-pg-locked-executor-net",
                state="pending",
            )
        )
        session.add(
            JobVM(
                id="vm-locked-executor",
                session_id="pg-locked-executor",
                label="target",
                image_id="immutable-target",
                state="pending",
                domain_name="astinuslab-pg-locked-executor-target",
                disk_path=str(tmp_path / "pg-locked-executor" / "target.qcow2"),
                disk_gb=1,
            )
        )
        session.add(
            JobVMNic(
                id="nic-locked-executor",
                job_vm_id="vm-locked-executor",
                job_network_id="net-locked-executor",
                mac="52:54:00:00:00:07",
                ordinal=0,
            )
        )
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)
    verified: list[Path] = []
    calls: list[dict] = []

    def validate(path: Path, **_kwargs) -> Path:
        verified.append(path)
        return path

    monkeypatch.setattr(coordinator, "validate_bound_immutable_layer", validate)
    monkeypatch.setattr(coordinator, "_invoke", lambda _task, **kwargs: calls.append(kwargs))
    payload = {
        "immutable_backings": {
            "vm-locked-executor": {"path": str(immutable_layer), "sha256": "a" * 64, "size_bytes": 9}
        }
    }

    coordinator._execute_locked_playground("pg-locked-executor", payload)

    assert verified == [immutable_layer]
    spawn = next(call for call in calls if call.get("vm_id") == "vm-locked-executor")
    assert spawn["immutable_backing_path"] == str(immutable_layer)
    assert spawn["workload_id"] is None


def test_locked_binding_failure_is_reported_as_a_stable_operation_error(
    in_memory_session_scope, monkeypatch, tmp_path: Path
):
    from astinuslab.jobs import package_instance_coordinator as coordinator

    monkeypatch.setattr("astinuslab.lab_packages.runner_preflight.require_runner_host_ready", lambda: None)

    source = tmp_path / "locked-binding-failure.astinuslab"
    source.write_bytes(write_archive(_definition(), "# runbook\n"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        session.add(PlaygroundSession(id="pg-locked-binding", lab_package_record_id=record.id, status="queued"))
        session.add(
            LabInstance(
                id="lpi-locked-binding",
                package_record_id=record.id,
                playground_session_id="pg-locked-binding",
                idempotency_key="locked-binding",
                state="registered",
            )
        )
        session.add(
            JobNetwork(
                id="net-locked-binding",
                session_id="pg-locked-binding",
                name="isolated",
                subnet_cidr="192.0.2.0/29",
                isolated=True,
                libvirt_net_name="astinuslab-pg-locked-binding-net",
                state="pending",
            )
        )
        session.add(
            JobVM(
                id="vm-locked-binding",
                session_id="pg-locked-binding",
                label="target",
                image_id="immutable-target",
                state="pending",
                domain_name="astinuslab-pg-locked-binding-target",
                disk_path=str(tmp_path / "pg-locked-binding" / "target.qcow2"),
                disk_gb=1,
            )
        )
        payload = {"locked": True, "immutable_backings": {}, "disk_requirements": [], "readiness": {}}
        session.add(
            LabInstanceAction(
                id="lia-locked-binding",
                instance_id="lpi-locked-binding",
                action_type="launch",
                payload_json=json.dumps(payload),
            )
        )
        session.add(
            LabOperationOutbox(
                id="loo-locked-binding",
                operation_kind="package_instance_launch",
                operation_id="lpi-locked-binding",
                payload_json=json.dumps(payload),
                state="pending",
            )
        )
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)

    def finish_cleanup(_task, *, job_id: str):
        with in_memory_session_scope() as session:
            session.get(PlaygroundSession, job_id).status = "cancelled"

    monkeypatch.setattr(coordinator, "_invoke", finish_cleanup)

    coordinator.run_instance_operation("lpi-locked-binding")

    with in_memory_session_scope() as session:
        instance = session.get(LabInstance, "lpi-locked-binding")
        assert instance is not None and instance.state == "failed"
        assert instance.error_summary == "locked_boot_binding_missing"


def test_qcow_gate_rejects_format_specific_external_data_file():
    from astinuslab.lab_packages.image_binding import _contains_external_reference

    assert _contains_external_reference(
        {
            "format": "qcow2",
            "format-specific": {"type": "qcow2", "data": {"data-file": "/tmp/attacker-controlled.raw"}},
        }
    )


def test_bound_immutable_layer_rechecks_digest_before_worker_boot(monkeypatch, tmp_path: Path):
    from astinuslab.lab_packages import image_binding

    layer = tmp_path / "layer.qcow2"
    layer.write_bytes(b"immutable")
    monkeypatch.setattr(image_binding, "_apply_qemu_acl", lambda _path: None)

    with pytest.raises(image_binding.ImageBindingError, match="unsafe-boot-layer"):
        image_binding.validate_bound_immutable_layer(
            layer,
            sha256="a" * 64,
            size_bytes=len(b"immutable"),
            virtual_size_limit=1024**3,
        )


def test_coordinator_claim_is_exclusive_for_duplicate_workers(in_memory_session_scope, monkeypatch, tmp_path: Path):
    from astinuslab.jobs import package_instance_coordinator as coordinator

    source = tmp_path / "claim.astinuslab"
    source.write_bytes(write_archive(_definition(), "# runbook\n"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        session.add(PlaygroundSession(id="pg-claim", lab_package_record_id=record.id, status="queued"))
        session.add(
            LabInstance(
                id="lpi-claim",
                package_record_id=record.id,
                playground_session_id="pg-claim",
                idempotency_key="claim",
                state="registered",
            )
        )
        session.add(
            LabInstanceAction(
                id="lia-claim",
                instance_id="lpi-claim",
                action_type="launch",
                state="queued",
            )
        )
        session.add(
            LabOperationOutbox(
                id="loo-claim",
                operation_kind="package_instance_launch",
                operation_id="lpi-claim",
                payload_json="{}",
                state="pending",
            )
        )
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)

    assert coordinator._claim_launch("lpi-claim") is not None
    assert coordinator._claim_launch("lpi-claim") is None


def test_action_claim_is_exclusive_for_duplicate_workers(in_memory_session_scope, monkeypatch, tmp_path: Path):
    from astinuslab.jobs import package_instance_coordinator as coordinator

    source = tmp_path / "action-claim.astinuslab"
    source.write_bytes(write_archive(_definition(), "# runbook\n"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        session.add(PlaygroundSession(id="pg-action-claim", lab_package_record_id=record.id, status="running"))
        session.add(
            LabInstance(
                id="lpi-action-claim",
                package_record_id=record.id,
                playground_session_id="pg-action-claim",
                idempotency_key="action-claim",
                state="destroying",
            )
        )
        session.add(
            LabInstanceAction(
                id="lia-action-claim",
                instance_id="lpi-action-claim",
                action_type="teardown",
                state="queued",
            )
        )
        session.add(
            LabOperationOutbox(
                id="loo-action-claim",
                operation_kind="package_instance_teardown",
                operation_id="lpi-action-claim",
                payload_json="{}",
                state="pending",
            )
        )
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)

    claim = coordinator._claim_instance_action("lpi-action-claim", "teardown")
    assert claim is not None and claim.session_id == "pg-action-claim"
    assert coordinator._claim_instance_action("lpi-action-claim", "teardown") is None


def test_coordinator_runs_declared_readiness_probes(in_memory_session_scope, monkeypatch):
    from astinuslab.jobs import package_instance_coordinator as coordinator

    with in_memory_session_scope() as session:
        session.add(PlaygroundSession(id="pg-ready", status="running"))
        session.add(
            JobNetwork(
                id="net-ready",
                session_id="pg-ready",
                name="isolated",
                subnet_cidr="192.0.2.0/29",
                isolated=True,
                libvirt_net_name="astinuslab-pg-ready-net",
                state="active",
            )
        )
        session.add(
            JobVM(
                id="vm-ready",
                session_id="pg-ready",
                label="target",
                image_id="ubuntu-24.04",
                state="running",
                domain_name="astinuslab-pg-ready-target",
                disk_path="/tmp/ready.qcow2",
            )
        )
        session.add(
            JobVMNic(
                id="nic-ready",
                job_vm_id="vm-ready",
                job_network_id="net-ready",
                mac="52:54:00:00:00:01",
                ip="192.0.2.10",
                ordinal=0,
            )
        )
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)
    calls: list[tuple[str, int | None]] = []
    monkeypatch.setattr(coordinator, "probe_ready", lambda host, probe: calls.append((host, probe.port)))

    coordinator._run_readiness("pg-ready", {"readiness": {"vm-ready": [{"kind": "tcp", "port": 8080}]}})

    assert calls == [("192.0.2.10", 8080)]


def test_coordinator_drives_valid_unlocked_workload_to_running(in_memory_session_scope, monkeypatch):
    from astinuslab.jobs import package_instance_coordinator as coordinator

    with in_memory_session_scope() as session:
        session.add(PlaygroundSession(id="pg-workload", status="running"))
        session.add(
            JobVM(
                id="vm-workload",
                session_id="pg-workload",
                label="target",
                image_id="ubuntu-24.04",
                state="provisioning_workload",
                role="target",
                workload_id="workload-sample-post-script",
                workload_state="provisioning",
                domain_name="astinuslab-pg-workload-target",
                disk_path="/tmp/workload.qcow2",
            )
        )
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)
    invoked: list[str] = []

    def complete_workload(_task, *, vm_id: str):
        invoked.append(vm_id)
        with in_memory_session_scope() as session:
            vm = session.get(JobVM, vm_id)
            vm.state = "running"
            vm.workload_state = "ready"

    monkeypatch.setattr(coordinator, "_invoke", complete_workload)

    coordinator._drive_workloads("pg-workload")

    assert invoked == ["vm-workload"]


def test_readiness_failure_compensates_instance(in_memory_session_scope, monkeypatch, tmp_path: Path):
    from astinuslab.jobs import package_instance_coordinator as coordinator

    source = tmp_path / "readiness-failure.astinuslab"
    source.write_bytes(write_archive(_definition(), "# runbook\n"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        session.add(PlaygroundSession(id="pg-probe-failure", lab_package_record_id=record.id, status="queued"))
        session.add(
            LabInstance(
                id="lpi-probe-failure",
                package_record_id=record.id,
                playground_session_id="pg-probe-failure",
                idempotency_key="probe-failure",
                state="registered",
            )
        )
        session.add(
            LabInstanceAction(
                id="lia-probe-failure", instance_id="lpi-probe-failure", action_type="launch", payload_json="{}"
            )
        )
        session.add(
            LabOperationOutbox(
                id="loo-probe-failure",
                operation_kind="package_instance_launch",
                operation_id="lpi-probe-failure",
                payload_json='{"disk_requirements": []}',
                state="pending",
            )
        )
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(coordinator, "_execute_unlocked_playground", lambda *_args: None)
    monkeypatch.setattr(coordinator, "_verify_overlay_allocation", lambda *_args: None)
    monkeypatch.setattr(coordinator, "_drive_workloads", lambda *_args: None)
    monkeypatch.setattr(
        coordinator, "_run_readiness", lambda *_args: (_ for _ in ()).throw(RuntimeError("probe failed"))
    )

    def finish_cleanup(_task, *, job_id: str):
        with in_memory_session_scope() as session:
            session.get(PlaygroundSession, job_id).status = "cancelled"

    monkeypatch.setattr(coordinator, "_invoke", finish_cleanup)

    coordinator.run_instance_operation("lpi-probe-failure")

    with in_memory_session_scope() as session:
        assert session.get(LabInstance, "lpi-probe-failure").state == "failed"
        assert session.get(PlaygroundSession, "pg-probe-failure") is None


def test_missing_guest_nic_ip_prevents_ready_state(in_memory_session_scope, monkeypatch):
    from astinuslab.jobs import package_instance_coordinator as coordinator

    with in_memory_session_scope() as session:
        session.add(PlaygroundSession(id="pg-no-ip", status="running"))
        session.add(
            LabInstance(
                id="lpi-no-ip",
                package_record_id="pkg-no-ip",
                playground_session_id="pg-no-ip",
                idempotency_key="no-ip",
                state="creating",
            )
        )
        session.add(
            JobNetwork(
                id="net-no-ip",
                session_id="pg-no-ip",
                name="isolated",
                subnet_cidr="192.0.2.0/29",
                isolated=True,
                libvirt_net_name="astinuslab-pg-no-ip-net",
                state="active",
            )
        )
        session.add(
            JobVM(
                id="vm-no-ip",
                session_id="pg-no-ip",
                label="target",
                image_id="ubuntu-24.04",
                state="running",
                domain_name="astinuslab-pg-no-ip-target",
                disk_path="/tmp/no-ip.qcow2",
            )
        )
        session.add(
            JobVMNic(
                id="nic-no-ip",
                job_vm_id="vm-no-ip",
                job_network_id="net-no-ip",
                mac="52:54:00:00:00:02",
                ip=None,
                ordinal=0,
            )
        )
        session.add(
            LabInstanceAction(
                id="lia-no-ip",
                instance_id="lpi-no-ip",
                action_type="launch",
                state="queued",
            )
        )
        session.add(
            LabOperationOutbox(
                id="loo-no-ip",
                operation_kind="package_instance_launch",
                operation_id="lpi-no-ip",
                state="pending",
            )
        )
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)
    claim = coordinator._claim_launch("lpi-no-ip")
    assert claim is not None

    with pytest.raises(RuntimeError, match="did not reach ready"):
        coordinator._mark_ready(claim)

    with in_memory_session_scope() as session:
        assert session.get(LabInstance, "lpi-no-ip").state == "creating"


def test_superseded_worker_cannot_mark_a_destroying_instance_ready(in_memory_session_scope, monkeypatch):
    from astinuslab.jobs import package_instance_coordinator as coordinator

    with in_memory_session_scope() as session:
        session.add(PlaygroundSession(id="pg-superseded-ready", status="running"))
        session.add(
            LabInstance(
                id="lpi-superseded-ready",
                package_record_id="pkg-superseded-ready",
                playground_session_id="pg-superseded-ready",
                idempotency_key="superseded-ready",
                state="destroying",
            )
        )
        session.add(
            LabInstanceAction(
                id="lia-superseded-ready",
                instance_id="lpi-superseded-ready",
                action_type="launch",
                state="completed",
            )
        )
        session.add(
            LabOperationOutbox(
                id="loo-superseded-ready",
                operation_kind="package_instance_launch",
                operation_id="lpi-superseded-ready",
                state="completed",
            )
        )
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)

    stale_claim = coordinator._OperationClaim(
        instance_id="lpi-superseded-ready",
        action_type="launch",
        action_id="lia-superseded-ready",
        generation=0,
        session_id="pg-superseded-ready",
        claimed_at=datetime.now(UTC).replace(tzinfo=None),
        payload={},
        phase="running",
        stale_claim=False,
    )
    assert coordinator._mark_ready(stale_claim) is False

    with in_memory_session_scope() as session:
        assert session.get(LabInstance, "lpi-superseded-ready").state == "destroying"


def test_readiness_completes_only_the_claimed_reset_generation(in_memory_session_scope, monkeypatch, tmp_path: Path):
    from astinuslab.jobs import package_instance_coordinator as coordinator

    with in_memory_session_scope() as session:
        session.add(PlaygroundSession(id="pg-reset-generation", status="running"))
        session.add(
            LabInstance(
                id="lpi-reset-generation",
                package_record_id="pkg-reset-generation",
                playground_session_id="pg-reset-generation",
                idempotency_key="reset-generation",
                state="resetting",
            )
        )
        session.add(
            JobNetwork(
                id="net-reset-generation",
                session_id="pg-reset-generation",
                name="isolated",
                subnet_cidr="192.0.2.0/29",
                isolated=True,
                libvirt_net_name="astinuslab-pg-reset-generation-net",
                state="active",
            )
        )
        session.add(
            JobVM(
                id="vm-reset-generation",
                session_id="pg-reset-generation",
                label="target",
                image_id="ubuntu-24.04",
                state="running",
                domain_name="astinuslab-pg-reset-generation-target",
                disk_path=str(tmp_path / "reset-generation.qcow2"),
            )
        )
        session.add(
            JobVMNic(
                id="nic-reset-generation",
                job_vm_id="vm-reset-generation",
                job_network_id="net-reset-generation",
                mac="52:54:00:00:00:0b",
                ip="192.0.2.21",
                ordinal=0,
            )
        )
        session.add(
            LabInstanceAction(
                id="lia-reset-generation-1",
                instance_id="lpi-reset-generation",
                action_type="reset",
                state="queued",
            )
        )
        session.add(
            LabOperationOutbox(
                id="loo-reset-generation",
                operation_kind="package_instance_reset",
                operation_id="lpi-reset-generation",
                payload_json='{"_coordinator":{"action_id":"lia-reset-generation-1","generation":1}}',
                state="pending",
            )
        )
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)
    claim = coordinator._claim_instance_action("lpi-reset-generation", "reset")
    assert claim is not None
    with in_memory_session_scope() as session:
        session.add(
            LabInstanceAction(
                id="lia-reset-generation-2",
                instance_id="lpi-reset-generation",
                action_type="reset",
                state="queued",
            )
        )

    assert coordinator._mark_ready(claim) is True

    with in_memory_session_scope() as session:
        assert session.get(LabInstanceAction, "lia-reset-generation-1").state == "completed"
        assert session.get(LabInstanceAction, "lia-reset-generation-2").state == "queued"


@pytest.mark.parametrize(
    ("requested_action", "expected_state"), [("cancel", "cancel_requested"), ("teardown", "destroying")]
)
def test_readiness_publication_race_serializes_supersession(
    monkeypatch, tmp_path: Path, requested_action: str, expected_state: str
):
    from astinuslab.jobs import package_instance_coordinator as coordinator

    scope = _file_session_scope(tmp_path / f"readiness-{requested_action}.db")
    source = tmp_path / f"readiness-{requested_action}.astinuslab"
    source.write_bytes(write_archive(_definition(), "# runbook\n"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    with scope() as session:
        record = service.import_path(source, session).record
        session.add(PlaygroundSession(id="pg-readiness-race", lab_package_record_id=record.id, status="running"))
        session.add(
            LabInstance(
                id="lpi-readiness-race",
                package_record_id=record.id,
                playground_session_id="pg-readiness-race",
                idempotency_key="readiness-race",
                state="registered",
            )
        )
        session.add(
            JobNetwork(
                id="net-readiness-race",
                session_id="pg-readiness-race",
                name="isolated",
                subnet_cidr="192.0.2.0/29",
                isolated=True,
                libvirt_net_name="astinuslab-pg-readiness-race-net",
                state="active",
            )
        )
        session.add(
            JobVM(
                id="vm-readiness-race",
                session_id="pg-readiness-race",
                label="target",
                image_id="ubuntu-24.04",
                state="running",
                domain_name="astinuslab-pg-readiness-race-target",
                disk_path=str(tmp_path / "readiness-race.qcow2"),
            )
        )
        session.add(
            JobVMNic(
                id="nic-readiness-race",
                job_vm_id="vm-readiness-race",
                job_network_id="net-readiness-race",
                mac="52:54:00:00:00:0a",
                ip="192.0.2.20",
                ordinal=0,
            )
        )
        session.add(
            LabInstanceAction(
                id="lia-readiness-race",
                instance_id="lpi-readiness-race",
                action_type="launch",
                state="running",
            )
        )
        session.add(
            LabOperationOutbox(
                id="loo-readiness-race",
                operation_kind="package_instance_launch",
                operation_id="lpi-readiness-race",
                state="pending",
            )
        )
    monkeypatch.setattr(coordinator, "session_scope", scope)
    claim = coordinator._claim_launch("lpi-readiness-race")
    assert claim is not None
    reached_publication = Barrier(2)
    release_publication = Barrier(2)
    request_started = Event()
    request_finished = Event()
    original_transition = coordinator.transition_instance

    def pause_ready_transition(session, instance, state: str, *, error: str | None = None):
        if state == "ready":
            reached_publication.wait(timeout=10)
            release_publication.wait(timeout=10)
        return original_transition(session, instance, state, error=error)

    def publish_ready():
        try:
            return coordinator._mark_ready(claim)
        except Exception as exc:  # pragma: no cover - asserted as the RED failure below
            return exc

    def supersede():
        request_started.set()
        try:
            with scope() as session:
                instance = session.get(LabInstance, "lpi-readiness-race")
                coordinator.request_instance_action(session, instance, requested_action)
        finally:
            request_finished.set()

    monkeypatch.setattr(coordinator, "transition_instance", pause_ready_transition)
    with ThreadPoolExecutor(max_workers=2) as pool:
        ready_future = pool.submit(publish_ready)
        reached_publication.wait(timeout=10)
        request_future = pool.submit(supersede)
        assert request_started.wait(timeout=10)
        request_finished_before_publication = request_finished.wait(timeout=0.5)
        release_publication.wait(timeout=10)
        ready_result = ready_future.result(timeout=10)
        request_future.result(timeout=10)

    with scope() as session:
        instance = session.get(LabInstance, "lpi-readiness-race")
        replacement_event = (
            session.query(LabOperationOutbox)
            .filter_by(operation_kind=f"package_instance_{requested_action}", operation_id=instance.id)
            .one()
        )
        replacement_action = (
            session.query(LabInstanceAction).filter_by(action_type=requested_action, instance_id=instance.id).one()
        )
        assert request_finished_before_publication is False
        assert ready_result is True
        assert instance.state == expected_state
        assert replacement_event.state == "pending"
        assert replacement_action.state == "queued"


def test_compensation_keeps_session_when_destroy_fails(in_memory_session_scope, monkeypatch, tmp_path: Path):
    from astinuslab.jobs import package_instance_coordinator as coordinator

    source = tmp_path / "cleanup-failure.astinuslab"
    source.write_bytes(write_archive(_definition(), "# runbook\n"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        session.add(PlaygroundSession(id="pg-cleanup-failure", lab_package_record_id=record.id, status="queued"))
        session.add(
            LabInstance(
                id="lpi-cleanup-failure",
                package_record_id=record.id,
                playground_session_id="pg-cleanup-failure",
                idempotency_key="cleanup-failure",
                state="creating",
            )
        )
        session.add(
            LabInstanceAction(
                id="lia-cleanup-failure",
                instance_id="lpi-cleanup-failure",
                action_type="launch",
                state="queued",
            )
        )
        session.add(
            LabOperationOutbox(
                id="loo-cleanup-failure",
                operation_kind="package_instance_launch",
                operation_id="lpi-cleanup-failure",
                payload_json="{}",
                state="pending",
            )
        )
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(
        coordinator,
        "_invoke",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("libvirt cleanup failed")),
    )

    claim = coordinator._claim_launch("lpi-cleanup-failure")
    assert claim is not None
    coordinator._compensate_launch(claim, RuntimeError("probe failed"))

    with in_memory_session_scope() as session:
        instance = session.get(LabInstance, "lpi-cleanup-failure")
        assert instance is not None and instance.state == "cleanup_incomplete"
        assert instance.playground_session_id == "pg-cleanup-failure"
        assert session.get(PlaygroundSession, "pg-cleanup-failure") is not None


def test_compensation_treats_normal_return_destroy_failed_as_cleanup_failure(
    in_memory_session_scope,
    monkeypatch,
    tmp_path: Path,
):
    from astinuslab.jobs import package_instance_coordinator as coordinator

    source = tmp_path / "normal-return-cleanup-failure.astinuslab"
    source.write_bytes(write_archive(_definition(), "# runbook\n"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        session.add(
            PlaygroundSession(
                id="pg-normal-return-cleanup-failure",
                lab_package_record_id=record.id,
                status="queued",
            )
        )
        session.add(
            LabInstance(
                id="lpi-normal-return-cleanup-failure",
                package_record_id=record.id,
                playground_session_id="pg-normal-return-cleanup-failure",
                idempotency_key="normal-return-cleanup-failure",
                state="creating",
            )
        )
        session.add(
            LabInstanceAction(
                id="lia-normal-return-cleanup-failure",
                instance_id="lpi-normal-return-cleanup-failure",
                action_type="launch",
                state="queued",
            )
        )
        session.add(
            LabOperationOutbox(
                id="loo-normal-return-cleanup-failure",
                operation_kind="package_instance_launch",
                operation_id="lpi-normal-return-cleanup-failure",
                payload_json="{}",
                state="pending",
            )
        )
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)

    def record_destroy_failure(_task, *, job_id: str):
        with in_memory_session_scope() as session:
            session.get(PlaygroundSession, job_id).status = "destroy_failed"

    monkeypatch.setattr(coordinator, "_invoke", record_destroy_failure)
    claim = coordinator._claim_launch("lpi-normal-return-cleanup-failure")
    assert claim is not None

    coordinator._compensate_launch(claim, RuntimeError("launch failed"))

    with in_memory_session_scope() as session:
        instance = session.get(LabInstance, "lpi-normal-return-cleanup-failure")
        playground = session.get(PlaygroundSession, "pg-normal-return-cleanup-failure")
        action = session.get(LabInstanceAction, "lia-normal-return-cleanup-failure")
        event = session.get(LabOperationOutbox, "loo-normal-return-cleanup-failure")
        assert instance.state == "cleanup_incomplete"
        assert instance.playground_session_id == "pg-normal-return-cleanup-failure"
        assert playground is not None and playground.status == "destroy_failed"
        assert action.state == "failed"
        assert event.state == "failed"


def test_lease_takeover_compensation_cannot_destroy_or_fail_the_newer_reset(monkeypatch, tmp_path: Path):
    from astinuslab.jobs import package_instance_coordinator as coordinator

    scope = _file_session_scope(tmp_path / "lease-takeover.db")
    source = tmp_path / "lease-takeover.astinuslab"
    source.write_bytes(write_archive(_definition(), "# runbook\n"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    stale_claimed_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=coordinator._CLAIM_LEASE_SECONDS + 1)
    with scope() as session:
        record = service.import_path(source, session).record
        session.add_all(
            [
                PlaygroundSession(id="pg-stale-reset", lab_package_record_id=record.id, status="cancelled"),
                PlaygroundSession(id="pg-newer-reset", lab_package_record_id=record.id, status="running"),
            ]
        )
        session.add(
            LabInstance(
                id="lpi-lease-takeover",
                package_record_id=record.id,
                playground_session_id="pg-newer-reset",
                idempotency_key="lease-takeover",
                state="resetting",
            )
        )
        session.add(
            LabInstanceAction(
                id="lia-lease-takeover",
                instance_id="lpi-lease-takeover",
                action_type="reset",
                state="running",
                payload_json='{"disk_requirements":[]}',
            )
        )
        session.add(
            LabOperationOutbox(
                id="loo-lease-takeover",
                operation_kind="package_instance_reset",
                operation_id="lpi-lease-takeover",
                payload_json='{"disk_requirements":[]}',
                state="claimed",
                claimed_at=stale_claimed_at,
            )
        )
    monkeypatch.setattr(coordinator, "session_scope", scope)

    stale_claim = coordinator._OperationClaim(
        instance_id="lpi-lease-takeover",
        action_type="reset",
        action_id="lia-lease-takeover",
        generation=1,
        session_id="pg-stale-reset",
        claimed_at=stale_claimed_at,
        payload={"disk_requirements": []},
        phase="running",
        stale_claim=False,
    )
    newer_claim = coordinator._claim_instance_action("lpi-lease-takeover", "reset")
    assert newer_claim is not None
    host_calls: list[str] = []
    monkeypatch.setattr(coordinator, "_invoke", lambda _task, *, job_id: host_calls.append(job_id))

    coordinator._compensate_launch(stale_claim, RuntimeError("stale reset failed"))

    with scope() as session:
        instance = session.get(LabInstance, "lpi-lease-takeover")
        event = session.get(LabOperationOutbox, "loo-lease-takeover")
        action = session.get(LabInstanceAction, "lia-lease-takeover")
        assert newer_claim.session_id == "pg-newer-reset"
        assert newer_claim.claimed_at != stale_claimed_at
        assert host_calls == []
        assert instance.playground_session_id == "pg-newer-reset"
        assert instance.state == "resetting"
        assert session.get(PlaygroundSession, "pg-newer-reset") is not None
        assert event.state == "claimed"
        assert event.claimed_at != stale_claimed_at
        assert action.state == "running"


@pytest.mark.parametrize("cleanup_fails", [False, True])
@pytest.mark.parametrize("requested_action", ["cancel", "teardown"])
def test_compensation_host_boundary_serializes_supersession(
    monkeypatch, tmp_path: Path, requested_action: str, cleanup_fails: bool
):
    from astinuslab.jobs import package_instance_coordinator as coordinator

    scope = _file_session_scope(tmp_path / f"compensation-{requested_action}-{cleanup_fails}.db")
    source = tmp_path / f"compensation-{requested_action}-{cleanup_fails}.astinuslab"
    source.write_bytes(write_archive(_definition(), "# runbook\n"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    with scope() as session:
        record = service.import_path(source, session).record
        session.add(PlaygroundSession(id="pg-compensation-race", lab_package_record_id=record.id, status="running"))
        session.add(
            LabInstance(
                id="lpi-compensation-race",
                package_record_id=record.id,
                playground_session_id="pg-compensation-race",
                idempotency_key="compensation-race",
                state="registered",
            )
        )
        session.add(
            LabInstanceAction(
                id="lia-compensation-race",
                instance_id="lpi-compensation-race",
                action_type="launch",
                state="queued",
            )
        )
        session.add(
            LabOperationOutbox(
                id="loo-compensation-race",
                operation_kind="package_instance_launch",
                operation_id="lpi-compensation-race",
                state="pending",
            )
        )
    monkeypatch.setattr(coordinator, "session_scope", scope)
    claim = coordinator._claim_launch("lpi-compensation-race")
    assert claim is not None
    entered_host_boundary = Barrier(2)
    release_host_boundary = Barrier(2)
    host_calls: list[str] = []

    def destroy_after_barrier(_task, *, job_id: str):
        entered_host_boundary.wait(timeout=10)
        release_host_boundary.wait(timeout=10)
        host_calls.append(job_id)
        if cleanup_fails:
            raise RuntimeError("host cleanup failed")
        with scope() as session:
            session.get(PlaygroundSession, job_id).status = "cancelled"

    monkeypatch.setattr(coordinator, "_invoke", destroy_after_barrier)
    request_finished = Event()

    def request_supersession():
        try:
            with scope() as session:
                instance = session.get(LabInstance, "lpi-compensation-race")
                coordinator.request_instance_action(session, instance, requested_action)
        finally:
            request_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        compensation_future = pool.submit(
            coordinator._compensate_launch,
            claim,
            RuntimeError("launch failed"),
        )
        entered_host_boundary.wait(timeout=10)
        request_future = pool.submit(request_supersession)
        request_finished_during_host_effect = request_finished.wait(timeout=0.5)
        release_host_boundary.wait(timeout=10)
        compensation_future.result(timeout=10)
        with pytest.raises(ValueError, match="instance is not actionable"):
            request_future.result(timeout=10)

    with scope() as session:
        assert request_finished_during_host_effect is False
        assert host_calls == ["pg-compensation-race"]
        instance = session.get(LabInstance, "lpi-compensation-race")
        assert instance.state == ("cleanup_incomplete" if cleanup_fails else "failed")
        assert instance.playground_session_id == ("pg-compensation-race" if cleanup_fails else None)
        assert (session.get(PlaygroundSession, "pg-compensation-race") is not None) is cleanup_fails
        assert session.get(LabOperationOutbox, "loo-compensation-race").state == "failed"
        assert session.get(LabInstanceAction, "lia-compensation-race").state == "failed"
        assert (
            session.query(LabOperationOutbox)
            .filter_by(operation_kind=f"package_instance_{requested_action}", operation_id="lpi-compensation-race")
            .count()
            == 0
        )
        assert (
            session.query(LabInstanceAction)
            .filter_by(action_type=requested_action, instance_id="lpi-compensation-race")
            .count()
            == 0
        )


@pytest.mark.parametrize("instance_state", ["failed", "cleanup_incomplete"])
@pytest.mark.parametrize("requested_action", ["cancel", "teardown"])
def test_terminal_cleanup_retry_is_refused_without_stranding_new_work(
    in_memory_session_scope,
    tmp_path: Path,
    instance_state: str,
    requested_action: str,
):
    from astinuslab.jobs import package_instance_coordinator as coordinator

    source = tmp_path / f"terminal-cleanup-{instance_state}-{requested_action}.astinuslab"
    source.write_bytes(write_archive(_definition(), "# runbook\n"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        playground_session_id = None
        if instance_state == "cleanup_incomplete":
            playground_session_id = "pg-terminal-cleanup"
            session.add(
                PlaygroundSession(
                    id=playground_session_id,
                    lab_package_record_id=record.id,
                    status="running",
                )
            )
        instance = LabInstance(
            id="lpi-terminal-cleanup",
            package_record_id=record.id,
            playground_session_id=playground_session_id,
            idempotency_key="terminal-cleanup",
            state=instance_state,
            error_summary="cleanup previously failed",
        )
        previous_action = LabInstanceAction(
            id="lia-terminal-cleanup",
            instance_id=instance.id,
            action_type=requested_action,
            state="failed",
            payload_json='{"attempt":1}',
            error_summary="cleanup previously failed",
        )
        previous_event = LabOperationOutbox(
            id="loo-terminal-cleanup",
            operation_kind=f"package_instance_{requested_action}",
            operation_id=instance.id,
            state="failed",
            payload_json='{"attempt":1}',
            claimed_at=datetime(2026, 1, 1, tzinfo=UTC),
            completed_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        session.add_all([instance, previous_action, previous_event])
        session.flush()
        session.expire_all()
        instance = session.query(LabInstance).filter_by(id=instance.id).one()
        previous_action = session.query(LabInstanceAction).filter_by(id=previous_action.id).one()
        previous_event = session.query(LabOperationOutbox).filter_by(id=previous_event.id).one()
        instance_snapshot = (
            instance.state,
            instance.playground_session_id,
            instance.error_summary,
            instance.updated_at,
        )
        action_snapshot = (
            previous_action.state,
            previous_action.payload_json,
            previous_action.error_summary,
            previous_action.updated_at,
        )
        event_snapshot = (
            previous_event.state,
            previous_event.payload_json,
            previous_event.claimed_at,
            previous_event.completed_at,
        )

        with pytest.raises(ValueError, match="instance is not actionable"):
            coordinator.request_instance_action(session, instance, requested_action)

        session.expire_all()
        current_instance = session.get(LabInstance, instance.id)
        current_action = session.get(LabInstanceAction, previous_action.id)
        current_event = session.get(LabOperationOutbox, previous_event.id)
        assert (
            current_instance.state,
            current_instance.playground_session_id,
            current_instance.error_summary,
            current_instance.updated_at,
        ) == instance_snapshot
        assert (
            current_action.state,
            current_action.payload_json,
            current_action.error_summary,
            current_action.updated_at,
        ) == action_snapshot
        assert (
            current_event.state,
            current_event.payload_json,
            current_event.claimed_at,
            current_event.completed_at,
        ) == event_snapshot
        assert (
            session.query(LabInstanceAction).filter_by(instance_id=instance.id, action_type=requested_action).count()
            == 1
        )
        assert (
            session.query(LabOperationOutbox)
            .filter_by(operation_kind=f"package_instance_{requested_action}", operation_id=instance.id)
            .count()
            == 1
        )


@pytest.mark.parametrize("source_phase", ["reset_cleanup", "compensating"])
@pytest.mark.parametrize(
    ("requested_action", "expected_state"), [("cancel", "cancel_requested"), ("teardown", "destroying")]
)
def test_request_supersedes_abandoned_cleanup_custody(
    in_memory_session_scope,
    monkeypatch,
    tmp_path: Path,
    source_phase: str,
    requested_action: str,
    expected_state: str,
):
    from astinuslab.jobs import package_instance_coordinator as coordinator

    source = tmp_path / f"abandoned-{source_phase}-{requested_action}.astinuslab"
    source.write_bytes(write_archive(_definition(), "# runbook\n"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    host_calls: list[str] = []
    monkeypatch.setattr(coordinator, "_invoke", lambda _task, *, job_id: host_calls.append(job_id))
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        session.add(PlaygroundSession(id="pg-abandoned-custody", lab_package_record_id=record.id, status="running"))
        instance = LabInstance(
            id="lpi-abandoned-custody",
            package_record_id=record.id,
            playground_session_id="pg-abandoned-custody",
            idempotency_key="abandoned-custody",
            state="resetting",
        )
        source_action = LabInstanceAction(
            id="lia-abandoned-custody",
            instance_id=instance.id,
            action_type="reset",
            state="running",
            payload_json=json.dumps(
                {
                    "_coordinator": {
                        "action_id": "lia-abandoned-custody",
                        "generation": 4,
                        "phase": source_phase,
                        "session_id": "pg-abandoned-custody",
                    }
                }
            ),
        )
        source_event = LabOperationOutbox(
            id="loo-abandoned-custody",
            operation_kind="package_instance_reset",
            operation_id=instance.id,
            payload_json=source_action.payload_json,
            state="claimed",
            claimed_at=datetime.now(UTC),
        )
        session.add_all([instance, source_action, source_event])
        session.flush()

        coordinator.request_instance_action(session, instance, requested_action)

        replacement_event = (
            session.query(LabOperationOutbox)
            .filter_by(operation_kind=f"package_instance_{requested_action}", operation_id=instance.id)
            .one()
        )
        replacement_action = (
            session.query(LabInstanceAction).filter_by(action_type=requested_action, instance_id=instance.id).one()
        )
        assert instance.state == expected_state
        assert instance.playground_session_id == "pg-abandoned-custody"
        assert source_event.state == "completed"
        assert source_action.state == "completed"
        assert source_action.error_summary == f"superseded by {requested_action}"
        assert replacement_event.state == "pending"
        assert replacement_action.state == "queued"
        assert host_calls == []


@pytest.mark.parametrize("event_state", ["completed", None])
@pytest.mark.parametrize(
    ("requested_action", "expected_state"), [("cancel", "cancel_requested"), ("teardown", "destroying")]
)
def test_request_finalizes_correlated_action_when_source_event_is_terminal_or_missing(
    in_memory_session_scope,
    tmp_path: Path,
    event_state: str | None,
    requested_action: str,
    expected_state: str,
):
    from astinuslab.jobs import package_instance_coordinator as coordinator

    source = tmp_path / f"terminal-source-{event_state}-{requested_action}.astinuslab"
    source.write_bytes(write_archive(_definition(), "# runbook\n"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        session.add(PlaygroundSession(id="pg-terminal-source", lab_package_record_id=record.id, status="running"))
        instance = LabInstance(
            id="lpi-terminal-source",
            package_record_id=record.id,
            playground_session_id="pg-terminal-source",
            idempotency_key="terminal-source",
            state="resetting",
        )
        generation_two = LabInstanceAction(
            id="lia-terminal-source-2",
            instance_id=instance.id,
            action_type="reset",
            state="running",
            payload_json=json.dumps({"_coordinator": {"action_id": "lia-terminal-source-2", "generation": 2}}),
        )
        generation_three = LabInstanceAction(
            id="lia-terminal-source-3",
            instance_id=instance.id,
            action_type="reset",
            state="queued",
            payload_json=json.dumps({"_coordinator": {"action_id": "lia-terminal-source-3", "generation": 3}}),
        )
        session.add_all([instance, generation_two, generation_three])
        if event_state is not None:
            session.add(
                LabOperationOutbox(
                    id="loo-terminal-source",
                    operation_kind="package_instance_reset",
                    operation_id=instance.id,
                    payload_json=json.dumps(
                        {"_coordinator": {"action_id": generation_two.id, "generation": 2, "phase": "running"}}
                    ),
                    state=event_state,
                )
            )
        session.flush()

        coordinator.request_instance_action(session, instance, requested_action)

        target = generation_two if event_state == "completed" else generation_three
        adjacent = generation_three if event_state == "completed" else generation_two
        replacement_event = (
            session.query(LabOperationOutbox)
            .filter_by(operation_kind=f"package_instance_{requested_action}", operation_id=instance.id)
            .one()
        )
        assert instance.state == expected_state
        assert target.state == "completed"
        assert target.error_summary == f"superseded by {requested_action}"
        assert adjacent.state in {"queued", "running"}
        assert replacement_event.state == "pending"


def test_request_fails_closed_when_missing_event_has_ambiguous_active_generation(
    in_memory_session_scope, tmp_path: Path
):
    from astinuslab.jobs import package_instance_coordinator as coordinator

    source = tmp_path / "ambiguous-source.astinuslab"
    source.write_bytes(write_archive(_definition(), "# runbook\n"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        session.add(PlaygroundSession(id="pg-ambiguous-source", lab_package_record_id=record.id, status="running"))
        instance = LabInstance(
            id="lpi-ambiguous-source",
            package_record_id=record.id,
            playground_session_id="pg-ambiguous-source",
            idempotency_key="ambiguous-source",
            state="resetting",
        )
        actions = [
            LabInstanceAction(
                id=f"lia-ambiguous-source-{suffix}",
                instance_id=instance.id,
                action_type="reset",
                state="running",
                payload_json=json.dumps({"_coordinator": {"generation": 5}}),
            )
            for suffix in ("a", "b")
        ]
        session.add_all([instance, *actions])
        session.flush()

        with pytest.raises(ValueError, match="ambiguous source action"):
            coordinator.request_instance_action(session, instance, "teardown")

        assert instance.state == "resetting"
        assert all(action.state == "running" for action in actions)
        assert (
            session.query(LabOperationOutbox)
            .filter_by(operation_kind="package_instance_teardown", operation_id=instance.id)
            .count()
            == 0
        )


def test_request_fails_closed_when_event_action_generation_disagrees(in_memory_session_scope, tmp_path: Path):
    from astinuslab.jobs import package_instance_coordinator as coordinator

    source = tmp_path / "mismatched-source.astinuslab"
    source.write_bytes(write_archive(_definition(), "# runbook\n"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        session.add(PlaygroundSession(id="pg-mismatched-source", lab_package_record_id=record.id, status="running"))
        instance = LabInstance(
            id="lpi-mismatched-source",
            package_record_id=record.id,
            playground_session_id="pg-mismatched-source",
            idempotency_key="mismatched-source",
            state="resetting",
        )
        source_action = LabInstanceAction(
            id="lia-mismatched-source",
            instance_id=instance.id,
            action_type="reset",
            state="running",
            payload_json=json.dumps({"_coordinator": {"action_id": "lia-mismatched-source", "generation": 8}}),
        )
        source_event = LabOperationOutbox(
            id="loo-mismatched-source",
            operation_kind="package_instance_reset",
            operation_id=instance.id,
            payload_json=json.dumps(
                {"_coordinator": {"action_id": "lia-mismatched-source", "generation": 7, "phase": "running"}}
            ),
            state="completed",
        )
        session.add_all([instance, source_action, source_event])
        session.flush()

        with pytest.raises(ValueError, match="ambiguous source action"):
            coordinator.request_instance_action(session, instance, "teardown")

        assert instance.state == "resetting"
        assert source_action.state == "running"


def test_reset_queues_unlocked_instance_without_mutating_current_session(in_memory_session_scope, tmp_path: Path):
    from astinuslab.jobs.package_instance_coordinator import request_instance_action

    source = tmp_path / "reset.astinuslab"
    source.write_bytes(write_archive(_definition(), "# runbook\n"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        session.add(PlaygroundSession(id="pg-reset", lab_package_record_id=record.id, status="running"))
        instance = LabInstance(
            id="lpi-reset",
            package_record_id=record.id,
            playground_session_id="pg-reset",
            idempotency_key="reset",
            state="ready",
        )
        session.add(instance)
        session.flush()
        session.add(
            LabInstanceAction(
                id="lia-reset-launch",
                instance_id=instance.id,
                action_type="launch",
                payload_json='{"disk_requirements":[{"directory":"/tmp","bytes":1}]}',
            )
        )
        session.flush()
        request_instance_action(session, instance, "reset")
        assert instance.state == "resetting"
        assert (
            session.query(LabOperationOutbox)
            .filter_by(operation_kind="package_instance_reset", operation_id=instance.id)
            .count()
            == 1
        )


def test_consecutive_resets_reopen_the_terminal_outbox(in_memory_session_scope, tmp_path: Path):
    from astinuslab.jobs.package_instance_coordinator import request_instance_action
    from astinuslab.lab_packages.instance_manager import transition_instance

    source = tmp_path / "consecutive-reset.astinuslab"
    source.write_bytes(write_archive(_definition(), "# runbook\n"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        session.add(PlaygroundSession(id="pg-consecutive-reset", lab_package_record_id=record.id, status="running"))
        instance = LabInstance(
            id="lpi-consecutive-reset",
            package_record_id=record.id,
            playground_session_id="pg-consecutive-reset",
            idempotency_key="consecutive-reset",
            state="ready",
        )
        session.add(instance)
        session.flush()
        session.add(
            LabInstanceAction(
                id="lia-consecutive-reset-launch",
                instance_id=instance.id,
                action_type="launch",
                payload_json='{"disk_requirements":[{"directory":"/tmp","bytes":1}]}',
            )
        )
        session.flush()

        request_instance_action(session, instance, "reset")
        event = (
            session.query(LabOperationOutbox)
            .filter_by(operation_kind="package_instance_reset", operation_id=instance.id)
            .one()
        )
        event_id = event.id
        event.state = "completed"
        event.claimed_at = datetime.now(UTC)
        event.completed_at = datetime.now(UTC)
        transition_instance(session, instance, "ready")

        request_instance_action(session, instance, "reset")

        assert event.id == event_id
        assert event.state == "pending"
        assert event.claimed_at is None
        assert event.completed_at is None
        assert session.query(LabInstanceAction).filter_by(instance_id=instance.id, action_type="reset").count() == 2


@pytest.mark.parametrize(("source_action", "source_state"), [("launch", "creating"), ("reset", "resetting")])
@pytest.mark.parametrize(
    ("requested_action", "expected_state"), [("cancel", "cancel_requested"), ("teardown", "destroying")]
)
def test_request_supersedes_inflight_package_operation(
    in_memory_session_scope,
    monkeypatch,
    tmp_path: Path,
    source_action: str,
    source_state: str,
    requested_action: str,
    expected_state: str,
):
    from astinuslab.jobs import package_instance_coordinator as coordinator

    source = tmp_path / f"supersede-{source_action}.astinuslab"
    source.write_bytes(write_archive(_definition(), "# runbook\n"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    instance_id = f"lpi-supersede-{source_action}"
    session_id = f"pg-supersede-{source_action}"
    action_id = f"lia-supersede-{source_action}"
    event_id = f"loo-supersede-{source_action}"
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        session.add(PlaygroundSession(id=session_id, lab_package_record_id=record.id, status="running"))
        instance = LabInstance(
            id=instance_id,
            package_record_id=record.id,
            playground_session_id=session_id,
            idempotency_key=f"supersede-{source_action}",
            state=source_state,
        )
        session.add(instance)
        session.flush()
        source_record = LabInstanceAction(
            id=action_id,
            instance_id=instance.id,
            action_type=source_action,
            state="running",
        )
        source_event = LabOperationOutbox(
            id=event_id,
            operation_kind=f"package_instance_{source_action}",
            operation_id=instance.id,
            state="claimed",
            claimed_at=datetime.now(UTC),
        )
        session.add_all([source_record, source_event])
        session.flush()
        assert source_event.claimed_at is not None
        stale_claim = coordinator._OperationClaim(
            instance_id=instance_id,
            action_type=source_action,
            action_id=action_id,
            generation=1 if source_action == "reset" else 0,
            session_id=session_id,
            claimed_at=source_event.claimed_at.replace(tzinfo=None),
            payload={},
            phase="running",
            stale_claim=False,
        )

        coordinator.request_instance_action(session, instance, requested_action)

        replacement_event = (
            session.query(LabOperationOutbox)
            .filter_by(operation_kind=f"package_instance_{requested_action}", operation_id=instance.id)
            .one()
        )
        assert instance.state == expected_state
        assert source_event.state == "completed"
        assert source_event.completed_at is not None
        assert source_record.state == "completed"
        assert f"superseded by {requested_action}" in (source_record.error_summary or "")
        assert replacement_event.state == "pending"

    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)
    cleanup_calls: list[str] = []
    monkeypatch.setattr(coordinator, "_invoke", lambda _task, *, job_id: cleanup_calls.append(job_id))

    coordinator._compensate_launch(stale_claim, RuntimeError("stale worker failure"))

    with in_memory_session_scope() as session:
        persisted_instance = session.get(LabInstance, instance_id)
        persisted_action = session.get(LabInstanceAction, action_id)
        persisted_event = session.get(LabOperationOutbox, event_id)
        assert cleanup_calls == []
        assert persisted_instance.state == expected_state
        assert persisted_instance.playground_session_id == session_id
        assert persisted_action.state == "completed"
        assert f"superseded by {requested_action}" in (persisted_action.error_summary or "")
        assert persisted_event.state == "completed"


@pytest.mark.parametrize("source_action", ["launch", "reset"])
@pytest.mark.parametrize(
    ("requested_action", "expected_state"),
    [("cancel", "cancel_requested"), ("teardown", "destroying")],
)
def test_preallocation_supersession_cannot_resurrect_source_or_create_host_resources(
    in_memory_session_scope,
    monkeypatch,
    tmp_path: Path,
    source_action: str,
    requested_action: str,
    expected_state: str,
):
    from astinuslab.jobs import package_instance_coordinator as coordinator

    source = tmp_path / f"preallocation-{source_action}-{requested_action}.astinuslab"
    source.write_bytes(write_archive(_definition(), "# runbook\n"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    instance_id = f"lpi-preallocation-{source_action}-{requested_action}"
    original_session_id = f"pg-preallocation-{source_action}-{requested_action}"
    disk_directory = tmp_path / f"disks-{source_action}-{requested_action}"
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        session.add(
            PlaygroundSession(
                id=original_session_id,
                lab_package_record_id=record.id,
                status="running",
            )
        )
        instance = LabInstance(
            id=instance_id,
            package_record_id=record.id,
            playground_session_id=original_session_id,
            idempotency_key=f"preallocation-{source_action}-{requested_action}",
            state="registered" if source_action == "launch" else "ready",
        )
        session.add(instance)
        session.add(
            JobNetwork(
                id=f"net-preallocation-{source_action}-{requested_action}",
                session_id=original_session_id,
                name="isolated",
                subnet_cidr="192.0.2.0/29",
                isolated=True,
                libvirt_net_name=f"astinuslab-{original_session_id}-net",
                state="active",
            )
        )
        vm_id = f"vm-preallocation-{source_action}-{requested_action}"
        session.add(
            JobVM(
                id=vm_id,
                session_id=original_session_id,
                label="target",
                image_id="ubuntu-24.04",
                state="running",
                domain_name=f"astinuslab-{original_session_id}-target",
                disk_path=str(disk_directory / "target.qcow2"),
                vcpus=1,
                memory_mb=512,
                disk_gb=1,
            )
        )
        session.add(
            JobVMNic(
                id=f"nic-preallocation-{source_action}-{requested_action}",
                job_vm_id=vm_id,
                job_network_id=f"net-preallocation-{source_action}-{requested_action}",
                mac="52:54:00:00:00:31",
                ip="192.0.2.31",
                ordinal=0,
            )
        )
        payload = {
            "disk_requirements": [{"vm_id": vm_id, "directory": str(disk_directory), "bytes": 1}],
            "readiness": {vm_id: []},
        }
        session.add(
            LabInstanceAction(
                id=f"lia-preallocation-{source_action}-{requested_action}",
                instance_id=instance.id,
                action_type="launch",
                state="queued",
                payload_json=json.dumps(payload),
            )
        )
        if source_action == "launch":
            session.add(
                LabOperationOutbox(
                    id=f"loo-preallocation-{source_action}-{requested_action}",
                    operation_kind="package_instance_launch",
                    operation_id=instance.id,
                    state="pending",
                    payload_json=json.dumps(payload),
                )
            )
        else:
            session.flush()
            coordinator.request_instance_action(session, instance, "reset")

    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)
    boundary_reached = Event()
    release_boundary = Event()
    original_reservation_lock = coordinator._reservation_lock
    paused = False

    @contextmanager
    def pause_before_reservation(payload, current_instance_id):
        nonlocal paused
        if not paused:
            paused = True
            boundary_reached.set()
            assert release_boundary.wait(timeout=10)
        with original_reservation_lock(payload, current_instance_id):
            yield

    def destroy_old_session(_task, *, job_id: str):
        with in_memory_session_scope() as session:
            session.get(PlaygroundSession, job_id).status = "cancelled"

    created_sessions: list[str] = []

    def create_host_resources(session_id: str, *_args):
        created_sessions.append(session_id)
        with in_memory_session_scope() as session:
            for network in session.query(JobNetwork).filter_by(session_id=session_id):
                network.state = "active"
            for vm in session.query(JobVM).filter_by(session_id=session_id):
                vm.state = "running"
                for nic in vm.nics:
                    nic.ip = "192.0.2.32"

    reservation_path = tmp_path / f".astinuslab-reservation-{instance_id}-current"

    def reserve(*_args):
        reservation_path.write_bytes(b"reserved")
        return coordinator.DiskReservation((reservation_path,), "journaled_write")

    monkeypatch.setattr(coordinator, "_reservation_lock", pause_before_reservation)
    monkeypatch.setattr(coordinator, "_invoke", destroy_old_session)
    monkeypatch.setattr(coordinator, "_execute_unlocked_playground", create_host_resources)
    monkeypatch.setattr(coordinator, "_verify_overlay_allocation", lambda *_args: None)
    monkeypatch.setattr(coordinator, "_drive_workloads", lambda *_args: None)
    monkeypatch.setattr(coordinator, "_run_readiness", lambda *_args: None)
    monkeypatch.setattr(coordinator, "_reserve", reserve)

    worker = coordinator.run_instance_operation if source_action == "launch" else coordinator.run_instance_action
    worker_args = (instance_id,) if source_action == "launch" else (instance_id, "reset")
    expected_session_id: str | None = None
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker, *worker_args)
        assert boundary_reached.wait(timeout=10)
        with in_memory_session_scope() as session:
            instance = session.get(LabInstance, instance_id)
            expected_session_id = instance.playground_session_id
            assert expected_session_id is not None
            coordinator.request_instance_action(session, instance, requested_action)
        release_boundary.set()
        future.result(timeout=10)

    with in_memory_session_scope() as session:
        instance = session.get(LabInstance, instance_id)
        source_event = (
            session.query(LabOperationOutbox)
            .filter_by(operation_kind=f"package_instance_{source_action}", operation_id=instance_id)
            .one()
        )
        source_record = (
            session.query(LabInstanceAction)
            .filter_by(action_type=source_action, instance_id=instance_id)
            .order_by(LabInstanceAction.created_at.desc())
            .first()
        )
        replacement_event = (
            session.query(LabOperationOutbox)
            .filter_by(operation_kind=f"package_instance_{requested_action}", operation_id=instance_id)
            .one()
        )
        replacement_action = (
            session.query(LabInstanceAction).filter_by(action_type=requested_action, instance_id=instance_id).one()
        )
        assert created_sessions == []
        assert instance.state == expected_state
        assert instance.playground_session_id == expected_session_id
        assert source_event.state == "completed"
        assert source_record.state == "completed"
        assert source_record.error_summary == f"superseded by {requested_action}"
        assert replacement_event.state == "pending"
        assert replacement_action.state == "queued"


@pytest.mark.parametrize("source_action", ["launch", "reset"])
def test_stale_lease_takeover_sweeps_reservation_under_allocation_lock(
    in_memory_session_scope,
    monkeypatch,
    tmp_path: Path,
    source_action: str,
):
    from astinuslab.jobs import package_instance_coordinator as coordinator

    source = tmp_path / f"stale-reservation-{source_action}.astinuslab"
    source.write_bytes(write_archive(_definition(), "# runbook\n"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    instance_id = f"lpi-stale-reservation-{source_action}"
    session_id = f"pg-stale-reservation-{source_action}"
    action_id = f"lia-stale-reservation-{source_action}"
    event_id = f"loo-stale-reservation-{source_action}"
    disk_directory = tmp_path / f"stale-reservation-disks-{source_action}"
    disk_directory.mkdir()
    stale_reservation = disk_directory / f".astinuslab-reservation-{instance_id}-stale"
    stale_reservation.write_bytes(b"stale reservation")
    stale_claimed_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=coordinator._CLAIM_LEASE_SECONDS + 1)
    generation = 1 if source_action == "reset" else 0
    coordinator_metadata: dict[str, object] = {
        "action_id": action_id,
        "generation": generation,
        "phase": "running",
        "session_id": session_id,
    }
    payload: dict[str, Any] = {
        "disk_requirements": [{"vm_id": "vm-stale-reservation", "directory": str(disk_directory), "bytes": 1}],
        "readiness": {"vm-stale-reservation": []},
        "_coordinator": coordinator_metadata,
    }
    action_payload: dict[str, Any] = {
        **payload,
        "reservation_paths": [str(stale_reservation)],
        "allocation_method": "journaled_write",
        "_coordinator": {
            **coordinator_metadata,
            "reservation_claimed_at": stale_claimed_at.isoformat(),
        },
    }
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        session.add(PlaygroundSession(id=session_id, lab_package_record_id=record.id, status="running"))
        session.add(
            LabInstance(
                id=instance_id,
                package_record_id=record.id,
                playground_session_id=session_id,
                idempotency_key=f"stale-reservation-{source_action}",
                state="resetting" if source_action == "reset" else "creating",
            )
        )
        session.add(
            JobNetwork(
                id=f"net-stale-reservation-{source_action}",
                session_id=session_id,
                name="isolated",
                subnet_cidr="192.0.2.0/29",
                isolated=True,
                libvirt_net_name=f"astinuslab-{session_id}-net",
                state="active",
            )
        )
        session.add(
            JobVM(
                id="vm-stale-reservation",
                session_id=session_id,
                label="target",
                image_id="ubuntu-24.04",
                state="running",
                domain_name=f"astinuslab-{session_id}-target",
                disk_path=str(disk_directory / "target.qcow2"),
                vcpus=1,
                memory_mb=512,
                disk_gb=1,
            )
        )
        session.add(
            JobVMNic(
                id=f"nic-stale-reservation-{source_action}",
                job_vm_id="vm-stale-reservation",
                job_network_id=f"net-stale-reservation-{source_action}",
                mac="52:54:00:00:00:33",
                ip="192.0.2.33",
                ordinal=0,
            )
        )
        session.add(
            LabInstanceAction(
                id=action_id,
                instance_id=instance_id,
                action_type=source_action,
                state="running",
                payload_json=json.dumps(action_payload),
            )
        )
        session.add(
            LabOperationOutbox(
                id=event_id,
                operation_kind=f"package_instance_{source_action}",
                operation_id=instance_id,
                state="claimed",
                claimed_at=stale_claimed_at,
                payload_json=json.dumps(payload),
            )
        )

    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)
    allocation_lock_active = False
    release_observations: list[tuple[bool, bool]] = []
    original_reservation_lock = coordinator._reservation_lock
    original_release = coordinator._release_recorded_reservations

    @contextmanager
    def observe_reservation_lock(runtime_payload, current_instance_id):
        nonlocal allocation_lock_active
        with original_reservation_lock(runtime_payload, current_instance_id):
            allocation_lock_active = True
            try:
                yield
            finally:
                allocation_lock_active = False

    def observe_release(*args, **kwargs):
        release_observations.append((allocation_lock_active, stale_reservation.exists()))
        return original_release(*args, **kwargs)

    def destroy_old_session(_task, *, job_id: str):
        with in_memory_session_scope() as session:
            session.get(PlaygroundSession, job_id).status = "cancelled"

    def activate_session(current_session_id: str, *_args):
        with in_memory_session_scope() as session:
            for network in session.query(JobNetwork).filter_by(session_id=current_session_id):
                network.state = "active"
            for vm in session.query(JobVM).filter_by(session_id=current_session_id):
                vm.state = "running"
                for nic in vm.nics:
                    nic.ip = "192.0.2.34"

    monkeypatch.setattr(coordinator, "_reservation_lock", observe_reservation_lock)
    monkeypatch.setattr(coordinator, "_release_recorded_reservations", observe_release)
    monkeypatch.setattr(coordinator, "_invoke", destroy_old_session)
    monkeypatch.setattr(coordinator, "_execute_unlocked_playground", activate_session)
    monkeypatch.setattr(coordinator, "_verify_overlay_allocation", lambda *_args: None)
    monkeypatch.setattr(coordinator, "_drive_workloads", lambda *_args: None)
    monkeypatch.setattr(coordinator, "_run_readiness", lambda *_args: None)
    monkeypatch.setattr(coordinator, "_reserve", lambda *_args: None)

    if source_action == "launch":
        coordinator.run_instance_operation(instance_id)
    else:
        coordinator.run_instance_action(instance_id, "reset")

    assert not stale_reservation.exists()
    assert (True, True) in release_observations
    assert all(lock_active for lock_active, reservation_existed in release_observations if reservation_existed)


def test_teardown_upgrade_terminalizes_reordered_cancel_delivery(
    in_memory_session_scope,
    monkeypatch,
    tmp_path: Path,
):
    from astinuslab.jobs import package_instance_coordinator as coordinator

    source = tmp_path / "cancel-teardown-upgrade.astinuslab"
    source.write_bytes(write_archive(_definition(), "# runbook\n"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        session.add(
            PlaygroundSession(
                id="pg-cancel-teardown-upgrade",
                lab_package_record_id=record.id,
                status="running",
            )
        )
        instance = LabInstance(
            id="lpi-cancel-teardown-upgrade",
            package_record_id=record.id,
            playground_session_id="pg-cancel-teardown-upgrade",
            idempotency_key="cancel-teardown-upgrade",
            state="ready",
        )
        session.add(instance)
        session.flush()
        coordinator.request_instance_action(session, instance, "cancel")
        coordinator.request_instance_action(session, instance, "teardown")

    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)

    def destroy_session(_task, *, job_id: str):
        with in_memory_session_scope() as session:
            session.get(PlaygroundSession, job_id).status = "cancelled"

    monkeypatch.setattr(coordinator, "_invoke", destroy_session)
    coordinator.run_instance_action("lpi-cancel-teardown-upgrade", "cancel")
    coordinator.run_instance_action("lpi-cancel-teardown-upgrade", "teardown")

    with in_memory_session_scope() as session:
        instance = session.get(LabInstance, "lpi-cancel-teardown-upgrade")
        cancel_action = session.query(LabInstanceAction).filter_by(instance_id=instance.id, action_type="cancel").one()
        cancel_event = (
            session.query(LabOperationOutbox)
            .filter_by(operation_id=instance.id, operation_kind="package_instance_cancel")
            .one()
        )
        teardown_action = (
            session.query(LabInstanceAction).filter_by(instance_id=instance.id, action_type="teardown").one()
        )
        teardown_event = (
            session.query(LabOperationOutbox)
            .filter_by(operation_id=instance.id, operation_kind="package_instance_teardown")
            .one()
        )
        assert instance.state == "destroyed"
        assert instance.playground_session_id is None
        assert cancel_action.state == "completed"
        assert cancel_action.error_summary == "superseded by teardown"
        assert cancel_event.state == "completed"
        assert teardown_action.state == "completed"
        assert teardown_event.state == "completed"


@pytest.mark.parametrize("image_available", [True, False], ids=["two_cycles", "missing_source"])
def test_two_unlocked_reset_cycles_preserve_readiness_and_current_action(
    in_memory_session_scope, monkeypatch, tmp_path: Path, image_available: bool
):
    from astinuslab.jobs import package_instance_coordinator as coordinator

    source = tmp_path / "reset-run.astinuslab"
    source.write_bytes(write_archive(_definition(), "# runbook\n"))
    image = tmp_path / "reset-base.qcow2"
    if image_available:
        image.write_bytes(b"isolated image availability fixture")
    monkeypatch.setattr(
        coordinator.orchestrator, "lookup_playground_image", lambda *_args, **_kwargs: {"path": str(image)}
    )
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        session.add(PlaygroundSession(id="pg-reset-run", lab_package_record_id=record.id, status="running"))
        instance = LabInstance(
            id="lpi-reset-run",
            package_record_id=record.id,
            playground_session_id="pg-reset-run",
            idempotency_key="reset-run",
            state="ready",
        )
        session.add(instance)
        session.add(
            JobNetwork(
                id="net-reset-run",
                session_id="pg-reset-run",
                name="isolated",
                subnet_cidr="192.0.2.0/29",
                isolated=True,
                libvirt_net_name="astinuslab-pg-reset-run-net",
                state="active",
            )
        )
        session.add(
            JobVM(
                id="vm-reset-run",
                session_id="pg-reset-run",
                label="target",
                image_id="ubuntu-24.04",
                state="running",
                domain_name="astinuslab-pg-reset-run-target",
                disk_path=str(tmp_path / "pg-reset-run" / "target.qcow2"),
                disk_gb=1,
            )
        )
        session.add(
            JobVMNic(
                id="nic-reset-run",
                job_vm_id="vm-reset-run",
                job_network_id="net-reset-run",
                mac="52:54:00:00:00:03",
                ip="192.0.2.10",
                ordinal=0,
            )
        )
        session.add(
            LabInstanceAction(
                id="lia-reset-run-launch",
                instance_id=instance.id,
                action_type="launch",
                payload_json=json.dumps(
                    {
                        "disk_requirements": [
                            {"vm_id": "vm-reset-run", "directory": str(tmp_path / "pg-reset-run"), "bytes": 1}
                        ],
                        "readiness": {"vm-reset-run": [{"kind": "tcp", "port": 8080}]},
                    }
                ),
            )
        )
        session.flush()
        coordinator.request_instance_action(session, instance, "reset")
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)
    release_event_states: list[str] = []
    original_release = coordinator._release_owned_reservation

    def observe_release(claim, reservation):
        with in_memory_session_scope() as session:
            event = (
                session.query(LabOperationOutbox)
                .filter_by(operation_kind="package_instance_reset", operation_id=claim.instance_id)
                .one()
            )
            release_event_states.append(event.state)
        original_release(claim, reservation)

    def destroy_old(_task, *, job_id: str):
        with in_memory_session_scope() as session:
            session.get(PlaygroundSession, job_id).status = "cancelled"

    def activate_new(_session_id: str, *_args):
        with in_memory_session_scope() as session:
            for net in session.query(JobNetwork).filter_by(session_id=_session_id):
                net.state = "active"
            for vm in session.query(JobVM).filter_by(session_id=_session_id):
                vm.state = "running"
                for nic in vm.nics:
                    nic.ip = "192.0.2.20"

    monkeypatch.setattr(coordinator, "_invoke", destroy_old)
    monkeypatch.setattr(coordinator, "_release_owned_reservation", observe_release)
    monkeypatch.setattr(coordinator, "_execute_unlocked_playground", activate_new)
    monkeypatch.setattr(coordinator, "_verify_overlay_allocation", lambda *_args: None)
    monkeypatch.setattr(coordinator, "_drive_workloads", lambda *_args: None)
    monkeypatch.setattr(coordinator, "_reserve", lambda *_args: None)
    readiness_runs: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        coordinator,
        "_run_readiness",
        lambda session_id, payload, *_args: readiness_runs.append((session_id, payload["readiness"])),
    )

    coordinator.run_instance_action("lpi-reset-run", "reset")

    with in_memory_session_scope() as session:
        instance = session.get(LabInstance, "lpi-reset-run")
        if not image_available:
            assert instance is not None and instance.state == "failed"
            assert "reset image is unavailable" in instance.error_summary
            assert session.query(PlaygroundSession).filter(PlaygroundSession.id != "pg-reset-run").count() == 0
            assert session.query(JobVM).filter(JobVM.id != "vm-reset-run").count() == 0
            assert readiness_runs == []
            return
        assert instance is not None and instance.state == "ready"
        assert instance.playground_session_id not in {None, "pg-reset-run"}
        assert session.query(JobVM).filter_by(session_id=instance.playground_session_id).count() == 1
        first_reset_session_id = instance.playground_session_id
        first_reset_vm_id = session.query(JobVM).filter_by(session_id=first_reset_session_id).one().id
        reset_event_id = (
            session.query(LabOperationOutbox)
            .filter_by(operation_kind="package_instance_reset", operation_id=instance.id)
            .one()
            .id
        )
        coordinator.request_instance_action(session, instance, "reset")

    coordinator.run_instance_action("lpi-reset-run", "reset")

    with in_memory_session_scope() as session:
        instance = session.get(LabInstance, "lpi-reset-run")
        current_vm = session.query(JobVM).filter_by(session_id=instance.playground_session_id).one()
        reset_event = session.get(LabOperationOutbox, reset_event_id)
        reset_actions = (
            session.query(LabInstanceAction)
            .filter_by(instance_id=instance.id, action_type="reset")
            .order_by(LabInstanceAction.created_at)
            .all()
        )
        assert instance.state == "ready"
        assert instance.playground_session_id not in {None, "pg-reset-run", first_reset_session_id}
        assert len(readiness_runs) == 2
        assert [set(readiness) for _session_id, readiness in readiness_runs] == [
            {first_reset_vm_id},
            {current_vm.id},
        ]
        assert reset_event.state == "completed"
        assert release_event_states == ["claimed", "claimed"]
        assert len(reset_actions) == 2
        assert [action.state for action in reset_actions] == ["completed", "completed"]
        generations = [json.loads(action.payload_json)["_coordinator"]["generation"] for action in reset_actions]
        assert generations == [1, 2]
        assert json.loads(reset_event.payload_json)["_coordinator"]["action_id"] == reset_actions[-1].id


def test_two_locked_reset_cycles_preserve_the_same_immutable_backing(
    in_memory_session_scope, monkeypatch, tmp_path: Path
):
    from astinuslab.jobs import package_instance_coordinator as coordinator

    source = tmp_path / "locked-reset.astinuslab"
    source.write_bytes(write_archive(_definition(), "# runbook\n"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    immutable_layer = tmp_path / "immutable.qcow2"
    immutable_layer.write_bytes(b"immutable")
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        session.add(PlaygroundSession(id="pg-locked-reset", lab_package_record_id=record.id, status="running"))
        instance = LabInstance(
            id="lpi-locked-reset",
            package_record_id=record.id,
            playground_session_id="pg-locked-reset",
            idempotency_key="locked-reset",
            state="ready",
        )
        session.add(instance)
        session.add(
            JobNetwork(
                id="net-locked-reset",
                session_id="pg-locked-reset",
                name="isolated",
                subnet_cidr="192.0.2.0/29",
                isolated=True,
                libvirt_net_name="astinuslab-pg-locked-reset-net",
                state="active",
            )
        )
        session.add(
            JobVM(
                id="vm-locked-reset",
                session_id="pg-locked-reset",
                label="target",
                image_id="immutable-target",
                state="running",
                domain_name="astinuslab-pg-locked-reset-target",
                disk_path=str(tmp_path / "pg-locked-reset" / "target.qcow2"),
                vcpus=1,
                memory_mb=512,
                disk_gb=1,
            )
        )
        session.add(
            JobVMNic(
                id="nic-locked-reset",
                job_vm_id="vm-locked-reset",
                job_network_id="net-locked-reset",
                mac="52:54:00:00:00:06",
                ip="192.0.2.10",
                ordinal=0,
            )
        )
        launch_payload: dict[str, Any] = {
            "locked": True,
            "immutable_backings": {
                "vm-locked-reset": {"path": str(immutable_layer), "sha256": "a" * 64, "size_bytes": 9}
            },
            "disk_requirements": [
                {"vm_id": "vm-locked-reset", "directory": str(tmp_path / "pg-locked-reset"), "bytes": 1}
            ],
            "readiness": {},
        }
        session.add(
            LabInstanceAction(
                id="lia-locked-reset-launch",
                instance_id=instance.id,
                action_type="launch",
                payload_json=json.dumps(launch_payload),
            )
        )
        session.flush()
        coordinator.request_instance_action(session, instance, "reset")
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)
    invoked: list[dict[str, Any]] = []

    def destroy_old(_task, *, job_id: str):
        with in_memory_session_scope() as session:
            session.get(PlaygroundSession, job_id).status = "cancelled"

    def boot_reset_locked(session_id: str, payload: dict, *_args):
        invoked.append(payload)
        with in_memory_session_scope() as session:
            for network in session.query(JobNetwork).filter_by(session_id=session_id):
                network.state = "active"
            for vm in session.query(JobVM).filter_by(session_id=session_id):
                vm.state = "running"
                for nic in vm.nics:
                    nic.ip = "192.0.2.20"

    monkeypatch.setattr(coordinator, "_invoke", destroy_old)
    monkeypatch.setattr(coordinator, "_execute_locked_playground", boot_reset_locked)
    monkeypatch.setattr(coordinator, "_verify_overlay_allocation", lambda *_args: None)
    monkeypatch.setattr(coordinator, "_reserve", lambda *_args: None)

    coordinator.run_instance_action("lpi-locked-reset", "reset")

    with in_memory_session_scope() as session:
        instance = session.get(LabInstance, "lpi-locked-reset")
        first_reset_session_id = instance.playground_session_id
        coordinator.request_instance_action(session, instance, "reset")

    coordinator.run_instance_action("lpi-locked-reset", "reset")

    assert len(invoked) == 2
    for remapped_payload in invoked:
        remapped = remapped_payload["immutable_backings"]
        assert list(remapped.values()) == [launch_payload["immutable_backings"]["vm-locked-reset"]]
        assert "vm-locked-reset" not in remapped
    with in_memory_session_scope() as session:
        instance = session.get(LabInstance, "lpi-locked-reset")
        reset_event = (
            session.query(LabOperationOutbox)
            .filter_by(operation_kind="package_instance_reset", operation_id=instance.id)
            .one()
        )
        reset_actions = session.query(LabInstanceAction).filter_by(instance_id=instance.id, action_type="reset").all()
        assert instance.state == "ready"
        assert instance.playground_session_id not in {None, "pg-locked-reset", first_reset_session_id}
        assert reset_event.state == "completed"
        assert len(reset_actions) == 2
        assert all(action.state == "completed" for action in reset_actions)


def test_stale_reservation_cleanup_removes_recorded_file(in_memory_session_scope, monkeypatch, tmp_path: Path):
    from astinuslab.jobs import package_instance_coordinator as coordinator

    source = tmp_path / "reservation.astinuslab"
    source.write_bytes(write_archive(_definition(), "# runbook\n"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    reservation = tmp_path / ".astinuslab-reservation-lpi-reservation-stale"
    reservation.write_bytes(b"reserved")
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        session.add(
            LabInstance(
                id="lpi-reservation",
                package_record_id=record.id,
                idempotency_key="reservation",
                state="creating",
            )
        )
        session.add(
            LabInstanceAction(
                id="lia-reservation",
                instance_id="lpi-reservation",
                action_type="launch",
                payload_json=json.dumps({"reservation_paths": [str(reservation)]}),
            )
        )
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)

    coordinator._release_recorded_reservations(
        "lpi-reservation",
        {"disk_requirements": [{"directory": str(tmp_path)}]},
        sweep=True,
    )

    assert not reservation.exists()


@pytest.mark.parametrize(
    "ownership", ["queued", "waiting_for_capture", "waiting_for_capacity", "reserved", "runtime", "released"]
)
def test_package_launch_and_preflight_respect_forensic_resource_ownership(
    in_memory_session_scope, monkeypatch, tmp_path: Path, ownership: str
):
    from astinuslab.api.schemas import JobCreate
    from astinuslab.db.models import CapacityReservation
    from astinuslab.db.repositories import JobRepository
    from astinuslab.lab_packages import launch_service, preflight

    source = tmp_path / "mutual-exclusion.astinuslab"
    source.write_bytes(write_archive(_definition(), "# runbook\n"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    # The transactional guard must survive even a stale successful preflight.
    monkeypatch.setattr(
        launch_service,
        "preflight_package",
        lambda *_args, **_kwargs: PackagePreflight(HostFacts(4, 8192, 2**40, "x86_64", True), 1, ()),
    )
    monkeypatch.setattr(launch_service, "validate_warning_consent_token", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(launch_service, "lookup_playground_image", lambda *_args, **_kwargs: {"path": str(source)})
    monkeypatch.setattr(
        launch_service, "validate_playground_workload_selection", lambda **_kwargs: type("Result", (), {"ok": True})()
    )
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(JobCreate(target_id="fixture", scenario_id="manual", execution_mode="manual_trigger"))
        job.status = ownership if ownership in {"queued", "waiting_for_capture", "waiting_for_capacity"} else "failed"
        if ownership in {"reserved", "released"}:
            session.add(
                CapacityReservation(job_id=job.id, vm_count=1, memory_mb=512, storage_bytes=1024, state=ownership)
            )
        if ownership == "runtime":
            repo.upsert_runtime(job.id, "target", f"astinuslab-{job.id}", state="running")
        session.flush()
        issues: list[preflight.PreflightIssue] = []
        preflight._check_database_conflicts(_definition(), session, issues)
        blocked = ownership != "released"
        assert any(issue.code == "session-occupied" and "forensic" in issue.message for issue in issues) == blocked
        record = service.import_path(source, session).record
        kwargs: dict[str, Any] = dict(
            record=record,
            definition=_definition(),
            manifest=LabManifest.model_validate(json.loads(record.manifest_json)),
            service=service,
            idempotency_key="mutual-exclusion",
            acknowledged_warning_token="ack",
        )
        if blocked:
            with pytest.raises(launch_service.PackageLaunchError, match="forensic") as caught:
                launch_service.launch_package(session, **kwargs)
            assert caught.value.code == "session_occupied"
            assert session.query(PlaygroundSession).count() == 0
            assert session.query(LabInstance).count() == 0
            assert session.query(LabOperationOutbox).count() == 0
        else:
            assert launch_service.launch_package(session, **kwargs).created


@pytest.mark.parametrize("ownership", ["resetting", "destroy_cleanup_pending", "failure_cleanup_pending"])
def test_package_launch_does_not_enter_another_playgrounds_cleanup_or_reset_gap(
    in_memory_session_scope, monkeypatch, tmp_path: Path, ownership: str
):
    from astinuslab.lab_packages import launch_service, preflight

    source = tmp_path / "reset-gap.astinuslab"
    source.write_bytes(write_archive(_definition(), "# runbook\n"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    monkeypatch.setattr(
        launch_service,
        "preflight_package",
        lambda *_args, **_kwargs: PackagePreflight(HostFacts(4, 8192, 2**40, "x86_64", True), 1, ()),
    )
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        if ownership == "resetting":
            session.add(
                LabInstance(
                    id="prior-instance", package_record_id=record.id, idempotency_key="prior", state="resetting"
                )
            )
        else:
            session.add(PlaygroundSession(id="prior-playground", status=ownership))
        session.flush()
        issues: list[preflight.PreflightIssue] = []
        preflight._check_database_conflicts(_definition(), session, issues)
        assert any(issue.code == "session-occupied" for issue in issues)
        with pytest.raises(launch_service.PackageLaunchError, match="Playground") as caught:
            launch_service.launch_package(
                session,
                record=record,
                definition=_definition(),
                manifest=LabManifest.model_validate(json.loads(record.manifest_json)),
                service=service,
                idempotency_key="new-launch",
                acknowledged_warning_token=None,
            )
        assert caught.value.code == "session_occupied"
        assert session.query(PlaygroundSession).count() == int(ownership != "resetting")
        assert session.query(LabInstance).count() == int(ownership == "resetting")
        assert session.query(LabOperationOutbox).count() == 0
