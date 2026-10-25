"""Post-commit, idempotent execution of package instance operations."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import stat
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from ..core.config import settings
from ..core.locks import _safe_lock_name, blocking_file_lock
from ..db import session_scope
from ..db.models import (
    JobNetwork,
    JobVM,
    LabInstance,
    LabInstanceAction,
    LabOperationOutbox,
    PlaygroundSession,
    PlaygroundTopologyLayout,
)
from ..lab_packages.experience_runner import probe_ready
from ..lab_packages.image_binding import ImageBindingError, validate_bound_immutable_layer
from ..lab_packages.instance_manager import append_action, enqueue_operation, transition_instance
from ..lab_packages.models import LabReadinessProbe
from ..lab_packages.preflight import DiskReservation, DiskReservationError, reserve_disk_space
from ..lab_packages.verification_evaluators import lowest_ordinal_nic_ip
from ..playground import orchestrator, repositories
from .celery_app import celery_app

_CLAIM_LEASE_SECONDS = 300
_WORKLOAD_WAIT_SECONDS = 120
_COORDINATOR_METADATA = "_coordinator"
_PHASE_PENDING = "pending"
_PHASE_RUNNING = "running"
_PHASE_RESET_CLEANUP = "reset_cleanup"
_PHASE_COMPENSATING = "compensating"
_RESERVATION_CLAIMED_AT = "reservation_claimed_at"


@dataclass(frozen=True)
class _OperationClaim:
    """Exact durable ownership needed before a worker mutates lifecycle state."""

    instance_id: str
    action_type: str
    action_id: str
    generation: int
    session_id: str
    claimed_at: datetime
    payload: dict[str, Any]
    phase: str
    stale_claim: bool


def _lifecycle_lock_path(instance_id: str) -> Path:
    return settings.storage_dir / "tmp" / "package-instance-locks" / f"{_safe_lock_name(instance_id)}.lock"


def _coordinator_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = payload.get(_COORDINATOR_METADATA)
    return dict(metadata) if isinstance(metadata, dict) else {}


def _with_coordinator_metadata(payload: dict[str, Any], **updates: Any) -> dict[str, Any]:
    updated = dict(payload)
    metadata = _coordinator_metadata(updated)
    metadata.update(updates)
    updated[_COORDINATOR_METADATA] = metadata
    return updated


def _dump_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo is not None else value


def _action(session: Session, instance_id: str, action_type: str = "launch") -> LabInstanceAction | None:
    return (
        session.query(LabInstanceAction)
        .filter(LabInstanceAction.instance_id == instance_id, LabInstanceAction.action_type == action_type)
        .order_by(LabInstanceAction.created_at.desc())
        .first()
    )


def _action_by_id(session: Session, instance_id: str, action_type: str, action_id: str) -> LabInstanceAction | None:
    return (
        session.query(LabInstanceAction)
        .filter_by(id=action_id, instance_id=instance_id, action_type=action_type)
        .one_or_none()
    )


def _active_action(session: Session, instance_id: str, action_type: str) -> LabInstanceAction | None:
    return (
        session.query(LabInstanceAction)
        .filter(
            LabInstanceAction.instance_id == instance_id,
            LabInstanceAction.action_type == action_type,
            LabInstanceAction.state.in_(["queued", "running"]),
        )
        .order_by(LabInstanceAction.created_at.desc(), LabInstanceAction.id.desc())
        .first()
    )


def _payload(event: LabOperationOutbox) -> dict[str, Any]:
    try:
        value = json.loads(event.payload_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _complete_event(event: LabOperationOutbox, state: str) -> None:
    event.state = state
    event.completed_at = datetime.now(UTC)


def _claim_rows(
    session: Session,
    claim: _OperationClaim,
    *,
    phases: set[str],
    require_session_pointer: bool = True,
) -> tuple[LabInstance, LabOperationOutbox, LabInstanceAction] | None:
    """Re-read and verify the exact event lease, generation, action and session."""

    instance = session.query(LabInstance).filter_by(id=claim.instance_id).with_for_update().one_or_none()
    event = (
        session.query(LabOperationOutbox)
        .filter_by(
            operation_kind=f"package_instance_{claim.action_type}",
            operation_id=claim.instance_id,
        )
        .with_for_update()
        .one_or_none()
    )
    if instance is None or event is None or event.state != "claimed":
        return None
    payload = _payload(event)
    metadata = _coordinator_metadata(payload)
    if (
        _naive_utc(event.claimed_at) != _naive_utc(claim.claimed_at)
        or metadata.get("action_id") != claim.action_id
        or metadata.get("generation") != claim.generation
        or metadata.get("session_id") != claim.session_id
        or metadata.get("phase") not in phases
        or (require_session_pointer and instance.playground_session_id != claim.session_id)
    ):
        return None
    action = _action_by_id(session, claim.instance_id, claim.action_type, claim.action_id)
    if action is None or action.state not in {"queued", "running"}:
        return None
    return instance, event, action


def _invoke(task: Any, **kwargs: Any) -> Any:
    """Run an existing Playground task in worker custody, not a request UoW."""

    run = getattr(task, "run", None)
    return run(**kwargs) if callable(run) else task(**kwargs)


def _claim_operation(instance_id: str, action_type: str) -> _OperationClaim | None:
    """Acquire one exact outbox lease while serialized with lifecycle cleanup."""

    kind = f"package_instance_{action_type}"
    now = datetime.now(UTC).replace(tzinfo=None)
    stale_before = now - timedelta(seconds=_CLAIM_LEASE_SECONDS)
    with blocking_file_lock(_lifecycle_lock_path(instance_id)):
        with session_scope() as session:
            repositories.serialize_write_transaction(session)
            event = (
                session.query(LabOperationOutbox)
                .filter_by(operation_kind=kind, operation_id=instance_id)
                .with_for_update()
                .one_or_none()
            )
            instance = session.query(LabInstance).filter_by(id=instance_id).with_for_update().one_or_none()
            if event is None or instance is None or event.state in {"completed", "failed"}:
                return None
            if instance.playground_session_id is None and action_type in {"cancel", "teardown"}:
                instance.state = "destroyed"
                instance.updated_at = datetime.now(UTC)
                _complete_event(event, "completed")
                action_id = _coordinator_metadata(_payload(event)).get("action_id")
                action = (
                    _action_by_id(session, instance_id, action_type, action_id)
                    if isinstance(action_id, str)
                    else _active_action(session, instance_id, action_type)
                )
                if action is not None:
                    action.state = "completed"
                    action.updated_at = datetime.now(UTC)
                return None

            valid_states = {
                "launch": {"registered", "creating"},
                "reset": {"resetting"},
                "cancel": {"cancel_requested"},
                "teardown": {"destroying"},
            }[action_type]
            if instance.state not in valid_states:
                if action_type == "launch" and instance.state in {
                    "cancel_requested",
                    "rollback_pending",
                    "destroying",
                }:
                    _complete_event(event, "completed")
                return None

            persisted_claimed_at = _naive_utc(event.claimed_at)
            stale_claim = event.state == "claimed" and (
                persisted_claimed_at is None or persisted_claimed_at <= stale_before
            )
            if event.state != "pending" and not stale_claim:
                return None
            existing_payload = _payload(event)
            existing_metadata = _coordinator_metadata(existing_payload)
            existing_phase = existing_metadata.get("phase")
            if stale_claim and existing_phase in {_PHASE_RESET_CLEANUP, _PHASE_COMPENSATING}:
                expected_session_id = existing_metadata.get("session_id")
                if not isinstance(expected_session_id, str) or instance.playground_session_id not in {
                    None,
                    expected_session_id,
                }:
                    return None
            claimed = (
                session.query(LabOperationOutbox)
                .filter(LabOperationOutbox.id == event.id)
                .filter(
                    or_(
                        LabOperationOutbox.state == "pending",
                        and_(LabOperationOutbox.state == "claimed", LabOperationOutbox.claimed_at <= stale_before),
                        and_(LabOperationOutbox.state == "claimed", LabOperationOutbox.claimed_at.is_(None)),
                    )
                )
                .update({"state": "claimed", "claimed_at": now}, synchronize_session=False)
            )
            if claimed != 1:
                return None
            session.expire(event)

            payload = _payload(event)
            metadata = _coordinator_metadata(payload)
            action_id = metadata.get("action_id")
            action = _action_by_id(session, instance_id, action_type, action_id) if isinstance(action_id, str) else None
            if action is None:
                action = _active_action(session, instance_id, action_type)
            if action is None:
                raise RuntimeError(f"{action_type} action disappeared")
            generation = metadata.get("generation")
            if not isinstance(generation, int) or isinstance(generation, bool):
                generation = 1 if action_type == "reset" else 0
            phase = metadata.get("phase") if stale_claim else _PHASE_RUNNING
            if phase not in {_PHASE_RUNNING, _PHASE_RESET_CLEANUP, _PHASE_COMPENSATING}:
                phase = _PHASE_RUNNING
            session_id = metadata.get("session_id") if phase in {_PHASE_RESET_CLEANUP, _PHASE_COMPENSATING} else None
            if not isinstance(session_id, str):
                session_id = instance.playground_session_id
            if session_id is None:
                raise RuntimeError("package instance has no Playground session")
            payload = _with_coordinator_metadata(
                payload,
                action_id=action.id,
                generation=generation,
                phase=phase,
                session_id=session_id,
            )
            event.payload_json = _dump_payload(payload)
            action.payload_json = _dump_payload(payload)
            action.state = "running"
            action.updated_at = datetime.now(UTC)
            if action_type == "launch" and instance.state == "registered":
                transition_instance(session, instance, "creating")
            session.flush()
            return _OperationClaim(
                instance_id=instance_id,
                action_type=action_type,
                action_id=action.id,
                generation=generation,
                session_id=session_id,
                claimed_at=now,
                payload=payload,
                phase=phase,
                stale_claim=stale_claim,
            )


def _claim_launch(instance_id: str) -> _OperationClaim | None:
    """Commit exact creating ownership before invoking nested worker UoWs."""

    return _claim_operation(instance_id, "launch")


def _reserve(payload: dict[str, Any], instance_id: str) -> DiskReservation | None:
    requirements: list[tuple[Path, int]] = []
    for item in payload.get("disk_requirements", []):
        if not isinstance(item, dict):
            raise ValueError("invalid disk reservation intent")
        directory = Path(str(item["directory"]))
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        requirements.append((directory, int(item["bytes"])))
    return reserve_disk_space(tuple(requirements), owner_id=instance_id) if requirements else None


def _reservation_directories(payload: dict[str, Any]) -> tuple[Path, ...]:
    directories = {
        Path(str(item["directory"])) for item in payload.get("disk_requirements", []) if isinstance(item, dict)
    }
    return tuple(sorted(directories))


@contextmanager
def _reservation_lock(payload: dict[str, Any], instance_id: str):
    """Serialize reservation journals on stable global per-target lock inodes."""

    descriptors: list[int] = []
    try:
        lock_dir = settings.storage_dir / "tmp" / "package-instance-reservation-locks"
        lock_paths: set[Path] = set()
        for directory in _reservation_directories(payload):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            target_key = hashlib.sha256(os.fsencode(str(directory.resolve()))).hexdigest()
            lock_paths.add(lock_dir / f"{_safe_lock_name(instance_id)}-{target_key}.lock")
        if lock_paths:
            lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for lock_path in sorted(lock_paths):
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            descriptors.append(descriptor)
        yield
    finally:
        for descriptor in reversed(descriptors):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
            except OSError:
                continue


def _reservation_owner(payload: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
    metadata = _coordinator_metadata(payload)
    return (
        metadata.get("action_id"),
        metadata.get("generation"),
        metadata.get("session_id"),
        metadata.get(_RESERVATION_CLAIMED_AT),
    )


def _release_recorded_reservations(
    instance_id: str,
    payload: dict[str, Any],
    *,
    action_type: str = "launch",
    action_id: str | None = None,
    sweep: bool = False,
) -> None:
    """Release only this claim's journal, plus crash-window files on takeover."""

    action_payload: dict[str, Any] | None = None
    with session_scope() as session:
        action = (
            _action_by_id(session, instance_id, action_type, action_id)
            if action_id is not None
            else _action(session, instance_id, action_type)
        )
        if action is not None:
            try:
                action_payload = json.loads(action.payload_json or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                action_payload = {}
    journal_owned = _reservation_owner(action_payload or {}) == _reservation_owner(payload) and isinstance(
        _coordinator_metadata(payload).get(_RESERVATION_CLAIMED_AT), str
    )
    paths = [
        Path(path)
        for path in (action_payload or {}).get("reservation_paths", [])
        if journal_owned and isinstance(path, str)
    ]
    if sweep:
        # A crash between fallocate and the journal update has no recorded
        # path. The stale taker performs this sweep before its own reserve,
        # while holding the persistent allocation lock.
        safe_id = "".join(character for character in instance_id if character.isalnum() or character in "-_")
        for directory in _reservation_directories(payload):
            try:
                paths.extend(directory.glob(f".astinuslab-reservation-{safe_id}-*"))
            except OSError:
                continue
    DiskReservation(tuple(dict.fromkeys(paths)), "journaled_write").release()
    if not journal_owned:
        return
    with session_scope() as session:
        repositories.serialize_write_transaction(session)
        action = (
            _action_by_id(session, instance_id, action_type, action_id)
            if action_id is not None
            else _action(session, instance_id, action_type)
        )
        if action is None:
            return
        try:
            current = json.loads(action.payload_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            current = {}
        if _reservation_owner(current) != _reservation_owner(payload):
            return
        current.pop("reservation_paths", None)
        current.pop("allocation_method", None)
        metadata = _coordinator_metadata(current)
        metadata.pop(_RESERVATION_CLAIMED_AT, None)
        current[_COORDINATOR_METADATA] = metadata
        action.payload_json = _dump_payload(current)


def _verify_overlay_allocation(
    session_id: str,
    payload: dict[str, Any],
    reservation: DiskReservation | None,
) -> None:
    """Verify a real overlay consumed capacity before releasing its reservation.

    ``qemu-img`` cannot rename a preallocated reservation inode into a qcow2
    overlay: it needs to write qcow metadata into a fresh output file.  The
    safe handoff is consequently a filesystem lock held from reservation
    creation through qemu allocation, followed by verification that every
    expected overlay was created in the *same directory and filesystem* as
    its reservation.  Only then may the reservation files be released.
    """

    with session_scope() as session:
        paths = [Path(vm.disk_path) for vm in session.query(JobVM).filter_by(session_id=session_id).all()]
    expected_directories = set(_reservation_directories(payload))
    reservation_devices: set[int] = set()
    for reserved in reservation.paths if reservation is not None else ():
        inspected = reserved.lstat()
        if not stat.S_ISREG(inspected.st_mode) or stat.S_ISLNK(inspected.st_mode):
            raise RuntimeError("reservation allocation verification failed")
        reservation_devices.add(inspected.st_dev)
    if expected_directories and not paths:
        raise RuntimeError("overlay allocation verification failed")
    for path in paths:
        inspected = path.lstat()
        if (
            not stat.S_ISREG(inspected.st_mode)
            or stat.S_ISLNK(inspected.st_mode)
            or inspected.st_size < 1
            or (expected_directories and path.parent not in expected_directories)
            or (reservation_devices and inspected.st_dev not in reservation_devices)
        ):
            raise RuntimeError("overlay allocation verification failed")


def _record_reservation(claim: _OperationClaim, reservation: DiskReservation | None) -> bool:
    """Journal a reservation only while the exact outbox lease is current."""

    with session_scope() as session:
        repositories.serialize_write_transaction(session)
        owned = _claim_rows(session, claim, phases={claim.phase})
        if owned is None:
            return False
        _instance, _event, action = owned
        payload = _action_payload(action)
        if reservation is not None:
            metadata = _coordinator_metadata(payload)
            claimed_at = _naive_utc(claim.claimed_at)
            if claimed_at is None:
                return False
            metadata[_RESERVATION_CLAIMED_AT] = claimed_at.isoformat()
            payload[_COORDINATOR_METADATA] = metadata
            payload["reservation_paths"] = [str(path) for path in reservation.paths]
            payload["allocation_method"] = reservation.allocation_method
            claim_metadata = _coordinator_metadata(claim.payload)
            claim_metadata[_RESERVATION_CLAIMED_AT] = claimed_at.isoformat()
            claim.payload[_COORDINATOR_METADATA] = claim_metadata
        action.payload_json = _dump_payload(payload)
        action.state = "running"
        action.updated_at = datetime.now(UTC)
        session.flush()
        return True


def _claim_is_owned(
    claim: _OperationClaim,
    *,
    phases: set[str] | None = None,
    require_session_pointer: bool = True,
) -> bool:
    with session_scope() as session:
        return (
            _claim_rows(
                session,
                claim,
                phases=phases or {claim.phase},
                require_session_pointer=require_session_pointer,
            )
            is not None
        )


def _require_claim_ownership(claim: _OperationClaim) -> None:
    if not _claim_is_owned(claim):
        raise RuntimeError("package operation claim lost")


@contextmanager
def _provisioning_custody(claim: _OperationClaim):
    """Acquire allocation then lifecycle custody without holding both for host I/O."""

    lifecycle = ExitStack()
    reservation: DiskReservation | None = None
    owned = False
    try:
        # Lock order is allocation -> lifecycle. The allocation lock covers
        # stale sweeping and journal mutation only; lifecycle remains held
        # after this block to fence requests and lease takeover during host I/O.
        with _reservation_lock(claim.payload, claim.instance_id):
            lifecycle.enter_context(blocking_file_lock(_lifecycle_lock_path(claim.instance_id)))
            if _claim_is_owned(claim):
                if claim.stale_claim:
                    _release_recorded_reservations(
                        claim.instance_id,
                        claim.payload,
                        action_type=claim.action_type,
                        action_id=claim.action_id,
                        sweep=True,
                    )
                if _claim_is_owned(claim):
                    reservation = _reserve(claim.payload, claim.instance_id)
                    owned = _record_reservation(claim, reservation)
                    if not owned and reservation is not None:
                        reservation.release()
                        reservation = None
        yield owned, reservation
    finally:
        lifecycle.close()


@contextmanager
def _cleanup_host_custody(claim: _OperationClaim):
    """Hold lifecycle custody, sweeping a stale reservation in lock order."""

    lifecycle = ExitStack()
    owned = False
    try:
        if claim.stale_claim:
            with _reservation_lock(claim.payload, claim.instance_id):
                lifecycle.enter_context(blocking_file_lock(_lifecycle_lock_path(claim.instance_id)))
                owned = _claim_is_owned(claim, require_session_pointer=False)
                if owned:
                    _release_recorded_reservations(
                        claim.instance_id,
                        claim.payload,
                        action_type=claim.action_type,
                        action_id=claim.action_id,
                        sweep=True,
                    )
        else:
            lifecycle.enter_context(blocking_file_lock(_lifecycle_lock_path(claim.instance_id)))
            owned = _claim_is_owned(claim, require_session_pointer=False)
        yield owned
    finally:
        lifecycle.close()


def _release_owned_reservation(claim: _OperationClaim, reservation: DiskReservation | None) -> None:
    """Release a local reservation and clear only its exact durable journal."""

    with _reservation_lock(claim.payload, claim.instance_id):
        if reservation is not None:
            reservation.release()
        _release_recorded_reservations(
            claim.instance_id,
            claim.payload,
            action_type=claim.action_type,
            action_id=claim.action_id,
        )


def _checked_destroy_session(
    claim: _OperationClaim,
    *,
    require_session_pointer: bool = True,
) -> None:
    """Destroy one exact claimed session and reject any non-clean residue."""

    if not _claim_is_owned(claim, require_session_pointer=require_session_pointer):
        raise RuntimeError("package operation claim lost")
    _invoke(orchestrator.destroy_session, job_id=claim.session_id)
    with session_scope() as session:
        playground = session.get(PlaygroundSession, claim.session_id)
        if playground is not None and playground.status != "cancelled":
            raise RuntimeError(f"Playground cleanup remained in {playground.status}")


def _execute_unlocked_playground(session_id: str, claim: _OperationClaim | None = None) -> None:
    """Create exact registered networks then domains through existing workers."""

    with session_scope() as session:
        networks = [
            {
                "network_id": network.id,
                "name": network.name,
                "subnet_cidr": network.subnet_cidr,
                "isolated": network.isolated,
            }
            for network in session.query(JobNetwork)
            .filter_by(session_id=session_id)
            .order_by(JobNetwork.created_at)
            .all()
        ]
        vms = list(session.query(JobVM).filter_by(session_id=session_id).order_by(JobVM.created_at).all())
        plans = [
            {
                "vm_id": vm.id,
                "label": vm.label,
                "image_id": vm.image_id,
                "role": vm.role,
                "workload_id": vm.workload_id,
                "vcpus": vm.vcpus,
                "memory_mb": vm.memory_mb,
                "disk_gb": vm.disk_gb,
                "nics": [
                    {"network_id": nic.job_network_id, "nic_id": nic.id, "mac": nic.mac}
                    for nic in sorted(vm.nics, key=lambda nic: nic.ordinal)
                ],
            }
            for vm in vms
        ]
    for network in networks:
        if claim is not None:
            _require_claim_ownership(claim)
        _invoke(
            orchestrator.create_network,
            job_id=session_id,
            network_id=network["network_id"],
            name=network["name"],
            subnet_cidr=network["subnet_cidr"],
            isolated=network["isolated"],
        )
    for plan in plans:
        if claim is not None:
            _require_claim_ownership(claim)
        _invoke(
            orchestrator.spawn_vm,
            job_id=session_id,
            vm_id=plan["vm_id"],
            label=plan["label"],
            image_id=plan["image_id"],
            nic_specs=plan["nics"],
            role=plan["role"],
            workload_id=plan["workload_id"],
            vcpus=plan["vcpus"],
            memory_mb=plan["memory_mb"],
            disk_gb=plan["disk_gb"],
            resources_resolved=True,
        )


def _execute_locked_playground(
    session_id: str,
    payload: dict[str, Any],
    claim: _OperationClaim | None = None,
) -> None:
    """Boot overlays from the launch-time verified immutable layers only."""

    raw_backings = payload.get("immutable_backings")
    if not isinstance(raw_backings, dict):
        raise RuntimeError("locked_boot_binding_missing")
    with session_scope() as session:
        networks = [
            {
                "network_id": network.id,
                "name": network.name,
                "subnet_cidr": network.subnet_cidr,
                "isolated": network.isolated,
            }
            for network in session.query(JobNetwork)
            .filter_by(session_id=session_id)
            .order_by(JobNetwork.created_at)
            .all()
        ]
        vms = list(session.query(JobVM).filter_by(session_id=session_id).order_by(JobVM.created_at).all())
        plans = [
            {
                "vm_id": vm.id,
                "label": vm.label,
                "image_id": vm.image_id,
                "role": vm.role,
                "vcpus": vm.vcpus,
                "memory_mb": vm.memory_mb,
                "disk_gb": vm.disk_gb,
                "nics": [
                    {"network_id": nic.job_network_id, "nic_id": nic.id, "mac": nic.mac}
                    for nic in sorted(vm.nics, key=lambda nic: nic.ordinal)
                ],
                "backing": raw_backings.get(vm.id),
            }
            for vm in vms
        ]
    expected_vm_ids = {plan["vm_id"] for plan in plans}
    if not expected_vm_ids or set(raw_backings) != expected_vm_ids:
        raise RuntimeError("locked_boot_binding_missing")
    for network in networks:
        if claim is not None:
            _require_claim_ownership(claim)
        _invoke(
            orchestrator.create_network,
            job_id=session_id,
            network_id=network["network_id"],
            name=network["name"],
            subnet_cidr=network["subnet_cidr"],
            isolated=network["isolated"],
        )
    for plan in plans:
        backing = plan["backing"]
        if not isinstance(backing, dict) or not isinstance(backing.get("path"), str) or not backing["path"]:
            raise RuntimeError("locked_boot_binding_missing")
        if (
            not isinstance(backing.get("sha256"), str)
            or len(backing["sha256"]) != 64
            or isinstance(backing.get("size_bytes"), bool)
            or not isinstance(backing.get("size_bytes"), int)
        ):
            raise RuntimeError("locked_boot_binding_invalid")
        disk_gb = plan["disk_gb"]
        if isinstance(disk_gb, bool) or not isinstance(disk_gb, int) or disk_gb < 1:
            raise RuntimeError("locked_boot_binding_invalid")
        try:
            binding_path = validate_bound_immutable_layer(
                Path(backing["path"]),
                sha256=backing["sha256"],
                size_bytes=backing["size_bytes"],
                virtual_size_limit=disk_gb * 1024**3,
            )
        except ImageBindingError as exc:
            raise RuntimeError("locked_boot_binding_invalid") from exc
        if claim is not None:
            _require_claim_ownership(claim)
        _invoke(
            orchestrator.spawn_vm,
            job_id=session_id,
            vm_id=plan["vm_id"],
            label=plan["label"],
            image_id=plan["image_id"],
            nic_specs=plan["nics"],
            role=plan["role"],
            # A locked clean layer already contains the declared workload;
            # never enqueue recipient-side mutable provisioning.
            workload_id=None,
            vcpus=plan["vcpus"],
            memory_mb=plan["memory_mb"],
            disk_gb=plan["disk_gb"],
            resources_resolved=True,
            immutable_backing_path=str(binding_path),
        )


def _drive_workloads(session_id: str, claim: _OperationClaim | None = None) -> None:
    """Finish package-requested Playground workloads before declaring readiness."""

    with session_scope() as session:
        initial = [vm.id for vm in session.query(JobVM).filter_by(session_id=session_id).all() if vm.workload_id]
    for vm_id in initial:
        if claim is not None:
            _require_claim_ownership(claim)
        _invoke(orchestrator.provision_vm_workload, vm_id=vm_id)
    deadline = monotonic() + _WORKLOAD_WAIT_SECONDS
    while True:
        with session_scope() as session:
            workloads = [vm for vm in session.query(JobVM).filter_by(session_id=session_id).all() if vm.workload_id]
            failed = [vm for vm in workloads if vm.state in {"failed", "teardown_failed", "destroyed"}]
            pending = [
                vm for vm in workloads if vm.state != "running" or vm.workload_state not in {"ready", "prebaked"}
            ]
        if failed:
            raise RuntimeError("workload provisioning failed")
        if not pending:
            return
        if monotonic() >= deadline:
            raise RuntimeError("workload provisioning timed out")
        sleep(0.2)


def _run_readiness(
    session_id: str,
    payload: dict[str, Any],
    claim: _OperationClaim | None = None,
) -> None:
    """Run declared bounded probes against persisted guest IPs before ready."""

    raw_by_vm = payload.get("readiness", {})
    if not isinstance(raw_by_vm, dict):
        raise RuntimeError("invalid readiness contract")
    with session_scope() as session:
        vms = {vm.id: vm for vm in session.query(JobVM).filter_by(session_id=session_id).all()}
        plans: list[tuple[str, LabReadinessProbe]] = []
        for vm_id, raw_probes in raw_by_vm.items():
            if not isinstance(vm_id, str) or not isinstance(raw_probes, list):
                raise RuntimeError("invalid readiness contract")
            vm = vms.get(vm_id)
            if vm is None:
                raise RuntimeError("readiness VM is missing")
            host = lowest_ordinal_nic_ip(vm)
            if host is None:
                raise RuntimeError("readiness VM has no guest IP")
            plans.extend((host, LabReadinessProbe.model_validate(raw)) for raw in raw_probes)
    for host, probe in plans:
        if claim is not None:
            _require_claim_ownership(claim)
        probe_ready(host, probe)


def _mark_ready(claim: _OperationClaim) -> bool:
    with session_scope() as session:
        repositories.serialize_write_transaction(session)
        owned = _claim_rows(session, claim, phases={_PHASE_RUNNING})
        if owned is None:
            return False
        instance, event, action = owned
        expected_state = "resetting" if claim.action_type == "reset" else "creating"
        if instance.state != expected_state:
            return False
        vms = list(session.query(JobVM).filter_by(session_id=claim.session_id).all())
        networks = list(session.query(JobNetwork).filter_by(session_id=claim.session_id).all())
        # A running libvirt domain with a NIC whose address could not be
        # discovered is not a usable lab.  Treat that as readiness failure so
        # it follows the same compensated path as a failed explicit probe.
        if (
            not vms
            or any(vm.state != "running" or not vm.nics or any(not nic.ip for nic in vm.nics) for vm in vms)
            or any(network.state != "active" for network in networks)
        ):
            raise RuntimeError("Playground resources did not reach ready state")
        transition_instance(session, instance, "ready")
        action.state = "completed"
        action.updated_at = datetime.now(UTC)
        _complete_event(event, "completed")
        return True


def _begin_compensation(claim: _OperationClaim, error: Exception) -> tuple[_OperationClaim, str] | None:
    with session_scope() as session:
        repositories.serialize_write_transaction(session)
        owned = _claim_rows(
            session,
            claim,
            phases={_PHASE_RUNNING, _PHASE_RESET_CLEANUP, _PHASE_COMPENSATING},
            require_session_pointer=False,
        )
        if owned is None:
            return None
        instance, event, action = owned
        metadata = _coordinator_metadata(_payload(event))
        if instance.playground_session_id not in {None, claim.session_id}:
            return None
        error_summary = metadata.get("compensation_error")
        if metadata.get("phase") != _PHASE_COMPENSATING or not isinstance(error_summary, str):
            error_summary = str(error)[:512]
        payload = _with_coordinator_metadata(
            _payload(event),
            phase=_PHASE_COMPENSATING,
            compensation_error=error_summary,
        )
        event.payload_json = _dump_payload(payload)
        action.payload_json = _dump_payload(payload)
        session.flush()
        return replace(claim, payload=payload, phase=_PHASE_COMPENSATING), error_summary


def _finish_compensation(
    claim: _OperationClaim,
    error_summary: str,
    cleanup_error: Exception | None,
) -> None:
    with session_scope() as session:
        repositories.serialize_write_transaction(session)
        owned = _claim_rows(
            session,
            claim,
            phases={_PHASE_COMPENSATING},
            require_session_pointer=False,
        )
        if owned is None:
            return
        instance, event, action = owned
        if instance.playground_session_id not in {None, claim.session_id}:
            return
        playground = session.get(PlaygroundSession, claim.session_id)
        if playground is not None and cleanup_error is None:
            session.delete(playground)
            session.flush()
        if cleanup_error is None and instance.playground_session_id == claim.session_id:
            instance.playground_session_id = None
        if cleanup_error is not None:
            error_summary = f"{error_summary}; cleanup retry required: {cleanup_error}"[:512]
            if instance.state == "creating":
                transition_instance(session, instance, "rollback_pending", error=error_summary)
                transition_instance(session, instance, "cleanup_incomplete", error=error_summary)
            elif instance.state == "resetting":
                transition_instance(session, instance, "cleanup_incomplete", error=error_summary)
            else:
                return
        elif instance.state in {"creating", "resetting"}:
            transition_instance(session, instance, "failed", error=error_summary)
        else:
            return
        action.state = "failed"
        action.error_summary = error_summary
        action.updated_at = datetime.now(UTC)
        _complete_event(event, "failed")


def _compensate_launch(claim: _OperationClaim, error: Exception) -> None:
    """Claim cleanup custody, perform host effects, then terminalize exact ownership."""

    with blocking_file_lock(_lifecycle_lock_path(claim.instance_id)):
        custody = _begin_compensation(claim, error)
        if custody is None:
            return
        compensating_claim, error_summary = custody
        cleanup_error: Exception | None = None
        try:
            _checked_destroy_session(compensating_claim, require_session_pointer=False)
        except Exception as exc:
            # Never drop the only durable link to partially created libvirt state.
            # Reconciliation needs the session and queued resources to retry it.
            cleanup_error = exc
        _finish_compensation(compensating_claim, error_summary, cleanup_error)


def run_instance_operation(instance_id: str) -> str:
    """Execute a committed unlocked launch once; retry safely after a crash."""

    claimed = _claim_launch(instance_id)
    if claimed is None:
        return instance_id
    if claimed.phase == _PHASE_COMPENSATING:
        _compensate_launch(claimed, RuntimeError("resuming interrupted compensation"))
        return instance_id
    payload = claimed.payload
    session_id = claimed.session_id
    reservation: DiskReservation | None = None
    try:
        from ..lab_packages.runner_preflight import require_runner_host_ready

        require_runner_host_ready()
        with _provisioning_custody(claimed) as (owned, reservation):
            if not owned:
                return instance_id
            if payload.get("locked"):
                _execute_locked_playground(session_id, payload, claimed)
            else:
                _execute_unlocked_playground(session_id, claimed)
            _verify_overlay_allocation(session_id, payload, reservation)
            _drive_workloads(session_id, claimed)
            _run_readiness(session_id, payload, claimed)
        # A terminal event is no longer reconciled. Clear the exact durable
        # reservation first, then re-enter lifecycle custody so a request or
        # lease takeover in this gap wins the final ownership re-read.
        _release_owned_reservation(claimed, reservation)
        reservation = None
        with blocking_file_lock(_lifecycle_lock_path(instance_id)):
            if not _mark_ready(claimed):
                return instance_id
    except (DiskReservationError, OSError, RuntimeError, ValueError) as exc:
        _release_owned_reservation(claimed, reservation)
        reservation = None
        _compensate_launch(claimed, exc)
    finally:
        if reservation is not None:
            _release_owned_reservation(claimed, reservation)
    return instance_id


def request_instance_action(session: Session, instance: LabInstance, action: str) -> None:
    """Serialize a requested lifecycle mutation with destructive worker custody."""

    with blocking_file_lock(_lifecycle_lock_path(instance.id)):
        _request_instance_action_locked(session, instance, action)


def _request_instance_action_locked(session: Session, instance: LabInstance, action: str) -> None:
    """Record an action after the caller owns the per-instance lifecycle lock."""

    instance_id = instance.id
    repositories.serialize_write_transaction(session)
    current = session.query(LabInstance).filter_by(id=instance_id).populate_existing().with_for_update().one_or_none()
    if current is None:
        raise ValueError("instance is not actionable")
    instance = current
    if action == "reset":
        if instance.state != "ready" or instance.playground_session_id is None:
            raise ValueError("instance is not ready to reset")
        previous_reset = (
            session.query(LabInstanceAction)
            .filter_by(instance_id=instance.id, action_type="reset", state="completed")
            .order_by(LabInstanceAction.updated_at.desc(), LabInstanceAction.id.desc())
            .first()
        )
        source_action = previous_reset or _action(session, instance.id, "launch")
        payload = _action_payload(source_action)
        if not payload.get("disk_requirements"):
            raise NotImplementedError("unsupported_reset")
        previous_metadata = _coordinator_metadata(payload)
        previous_generation = previous_metadata.get("generation", 0)
        if not isinstance(previous_generation, int) or isinstance(previous_generation, bool):
            previous_generation = 0
        metadata = {
            key: value
            for key, value in previous_metadata.items()
            if key not in {"action_id", "compensation_error", "phase", "reset_snapshot", "session_id"}
        }
        payload[_COORDINATOR_METADATA] = metadata
        payload.pop("reservation_paths", None)
        payload.pop("allocation_method", None)
        transition_instance(session, instance, "resetting")
        action_record = append_action(session, instance.id, action, payload)
        payload = _with_coordinator_metadata(
            payload,
            action_id=action_record.id,
            generation=previous_generation + 1,
            phase=_PHASE_PENDING,
            session_id=instance.playground_session_id,
        )
        action_record.payload_json = _dump_payload(payload)
        enqueue_operation(
            session,
            kind=f"package_instance_{action}",
            operation_id=instance.id,
            payload=payload,
            reopen_terminal=True,
        )
        return
    target = {"cancel": "cancel_requested", "teardown": "destroying"}.get(action)
    if target is None:
        raise ValueError("unsupported instance action")
    if instance.state in {"destroyed", "cleanup_incomplete", "failed"}:
        raise ValueError("instance is not actionable")
    _supersede_inflight_operation(session, instance.id, action)
    transition_instance(session, instance, target)
    action_record = append_action(session, instance.id, action)
    payload = _with_coordinator_metadata(
        {},
        action_id=action_record.id,
        generation=0,
        phase=_PHASE_PENDING,
        session_id=instance.playground_session_id,
    )
    action_record.payload_json = _dump_payload(payload)
    enqueue_operation(session, kind=f"package_instance_{action}", operation_id=instance.id, payload=payload)


def _action_generation(action: LabInstanceAction) -> int | None:
    generation = _coordinator_metadata(_action_payload(action)).get("generation")
    return generation if isinstance(generation, int) and not isinstance(generation, bool) else None


def _correlated_source_action(
    session: Session,
    instance_id: str,
    action_type: str,
    event: LabOperationOutbox | None,
) -> LabInstanceAction | None:
    active = (
        session.query(LabInstanceAction)
        .filter(
            LabInstanceAction.instance_id == instance_id,
            LabInstanceAction.action_type == action_type,
            LabInstanceAction.state.in_(["queued", "running"]),
        )
        .all()
    )
    if not active:
        return None
    if event is not None:
        event_metadata = _coordinator_metadata(_payload(event))
        action_id = event_metadata.get("action_id")
        if isinstance(action_id, str):
            correlated = next((action for action in active if action.id == action_id), None)
            if correlated is not None:
                event_generation = event_metadata.get("generation")
                action_generation = _action_generation(correlated)
                if (
                    isinstance(event_generation, int)
                    and not isinstance(event_generation, bool)
                    and action_generation is not None
                    and event_generation != action_generation
                ):
                    raise ValueError(f"ambiguous source action: {action_type}")
                return correlated
    if len(active) == 1:
        return active[0]
    generations = [(generation, action) for action in active if (generation := _action_generation(action)) is not None]
    if len(generations) != len(active):
        raise ValueError(f"ambiguous source action: {action_type}")
    highest_generation = max(generation for generation, _action in generations)
    newest = [action for generation, action in generations if generation == highest_generation]
    if len(newest) != 1:
        raise ValueError(f"ambiguous source action: {action_type}")
    return newest[0]


def _supersede_inflight_operation(session: Session, instance_id: str, requested_action: str) -> None:
    sources: list[tuple[LabOperationOutbox | None, LabInstanceAction | None]] = []
    action_types = ["launch", "reset"]
    if requested_action == "teardown":
        action_types.append("cancel")
    for action_type in action_types:
        event = (
            session.query(LabOperationOutbox)
            .filter_by(operation_kind=f"package_instance_{action_type}", operation_id=instance_id)
            .one_or_none()
        )
        sources.append((event, _correlated_source_action(session, instance_id, action_type, event)))

    now = datetime.now(UTC)
    for event, action in sources:
        if event is not None and event.state in {"pending", "claimed"}:
            _complete_event(event, "completed")
        if action is not None and action.state in {"queued", "running"}:
            action.state = "completed"
            action.error_summary = f"superseded by {requested_action}"
            action.updated_at = now


def _action_payload(action: LabInstanceAction | None) -> dict[str, Any]:
    if action is None:
        return {}
    try:
        value = json.loads(action.payload_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _reset_snapshot(session: Session, instance: LabInstance, session_id: str) -> dict[str, Any]:
    """Copy exact Playground registration intent before destructive cleanup."""

    playground = session.get(PlaygroundSession, session_id)
    if playground is None or instance.playground_session_id != session_id:
        raise RuntimeError("reset session is unavailable")
    networks = [
        {"id": net.id, "name": net.name, "subnet_cidr": net.subnet_cidr, "isolated": net.isolated}
        for net in session.query(JobNetwork).filter_by(session_id=session_id).order_by(JobNetwork.created_at).all()
    ]
    vms = [
        {
            "id": vm.id,
            "label": vm.label,
            "image_id": vm.image_id,
            "role": vm.role,
            "workload_id": vm.workload_id,
            "vcpus": vm.vcpus,
            "memory_mb": vm.memory_mb,
            "disk_gb": vm.disk_gb,
            "nics": [
                {"network_id": nic.job_network_id, "mac": nic.mac, "ordinal": nic.ordinal}
                for nic in sorted(vm.nics, key=lambda nic: nic.ordinal)
            ],
        }
        for vm in session.query(JobVM).filter_by(session_id=session_id).order_by(JobVM.created_at).all()
    ]
    topology = [
        {"node_id": node.node_id, "node_type": node.node_type, "x": node.x, "y": node.y}
        for node in session.query(PlaygroundTopologyLayout).filter_by(session_id=session_id).all()
    ]
    if not networks or not vms:
        raise RuntimeError("reset session has no registered resources")
    return {
        "package_record_id": instance.package_record_id,
        "networks": networks,
        "vms": vms,
        "topology": topology,
    }


def _prepare_reset_cleanup(claim: _OperationClaim) -> _OperationClaim | None:
    """Persist reset cleanup custody and its recovery snapshot before host effects."""

    with session_scope() as session:
        repositories.serialize_write_transaction(session)
        owned = _claim_rows(
            session,
            claim,
            phases={_PHASE_RUNNING, _PHASE_RESET_CLEANUP},
            require_session_pointer=False,
        )
        if owned is None:
            return None
        instance, event, action = owned
        payload = _payload(event)
        metadata = _coordinator_metadata(payload)
        snapshot = metadata.get("reset_snapshot")
        if instance.playground_session_id not in {None, claim.session_id}:
            return None
        if not isinstance(snapshot, dict):
            if instance.playground_session_id != claim.session_id:
                return None
            snapshot = _reset_snapshot(session, instance, claim.session_id)
        payload = _with_coordinator_metadata(
            payload,
            phase=_PHASE_RESET_CLEANUP,
            reset_snapshot=snapshot,
        )
        event.payload_json = _dump_payload(payload)
        action.payload_json = _dump_payload(payload)
        session.flush()
        return replace(claim, payload=payload, phase=_PHASE_RESET_CLEANUP)


def _create_reset_session(claim: _OperationClaim) -> _OperationClaim | None:
    """Register fresh resources; new overlays are created only by spawn workers."""

    session_id = f"pg-{secrets.token_hex(6)}"
    network_ids: dict[str, str] = {}
    vm_ids: dict[str, str] = {}
    with session_scope() as session:
        repositories.serialize_write_transaction(session)
        owned = _claim_rows(
            session,
            claim,
            phases={_PHASE_RESET_CLEANUP},
            require_session_pointer=False,
        )
        if owned is None:
            return None
        instance, event, reset_action = owned
        if instance.state != "resetting" or instance.playground_session_id not in {None, claim.session_id}:
            return None
        from ..db.repositories import has_active_non_playground_job

        if has_active_non_playground_job(session):
            raise RuntimeError("A forensic job or its cleanup is currently active")
        active_session = repositories.active_playground_session(session)
        if active_session is not None:
            raise RuntimeError("Another Playground session or its cleanup is active")
        payload = _payload(event)
        metadata = _coordinator_metadata(payload)
        snapshot = metadata.get("reset_snapshot")
        if not isinstance(snapshot, dict):
            raise RuntimeError("reset recovery snapshot is missing")
        if not payload.get("locked"):
            for vm in snapshot["vms"]:
                image = orchestrator.lookup_playground_image(vm["image_id"], role=vm["role"])
                if image is None or not image.get("path") or not Path(image["path"]).is_file():
                    raise RuntimeError(f"reset image is unavailable: {vm['image_id']}")
        session.add(
            PlaygroundSession(id=session_id, lab_package_record_id=snapshot["package_record_id"], status="queued")
        )
        session.flush()
        for net in snapshot["networks"]:
            network_id = f"net-{secrets.token_hex(6)}"
            network_ids[net["id"]] = network_id
            repositories.register_network(
                session,
                network_id=network_id,
                job_id=session_id,
                name=net["name"],
                subnet_cidr=net["subnet_cidr"],
                isolated=net["isolated"],
                libvirt_net_name=orchestrator.libvirt_net_name(session_id, net["name"]),
            )
        for vm in snapshot["vms"]:
            vm_id = f"vm-{secrets.token_hex(6)}"
            vm_ids[vm["id"]] = vm_id
            repositories.register_vm(
                session,
                vm_id=vm_id,
                job_id=session_id,
                label=vm["label"],
                image_id=vm["image_id"],
                domain_name=f"astinuslab-pg-{session_id}-{vm['label']}",
                disk_path=str(settings.playground_disk_dir / session_id / f"{vm['label']}.qcow2"),
                role=vm["role"],
                workload_id=vm["workload_id"],
                vcpus=vm["vcpus"],
                memory_mb=vm["memory_mb"],
                disk_gb=vm["disk_gb"],
            )
            for nic in vm["nics"]:
                repositories.attach_nic(
                    session,
                    nic_id=f"nic-{secrets.token_hex(6)}",
                    vm_id=vm_id,
                    network_id=network_ids[nic["network_id"]],
                    # The old MAC can still be held by a failed/slow cleanup
                    # record.  A fresh guest NIC identity prevents reset from
                    # colliding with that durable audit row.
                    mac=orchestrator.generate_mac(),
                    ordinal=nic["ordinal"],
                )
        for node in snapshot["topology"]:
            old_id = node["node_id"]
            new_id = network_ids.get(old_id, vm_ids.get(old_id))
            if new_id is not None:
                repositories.save_topology_node(
                    session,
                    session_id,
                    new_id,
                    {"node_id": new_id, "node_type": node["node_type"], "x": node["x"], "y": node["y"]},
                )
        remapped_readiness = {
            vm_ids[old_id]: probes for old_id, probes in payload.get("readiness", {}).items() if old_id in vm_ids
        }
        new_payload = dict(payload)
        new_payload["session_id"] = session_id
        new_payload["readiness"] = remapped_readiness
        if payload.get("locked"):
            raw_backings = payload.get("immutable_backings")
            if not isinstance(raw_backings, dict):
                raise RuntimeError("locked_boot_binding_missing")
            new_payload["immutable_backings"] = {
                vm_ids[old_id]: backing for old_id, backing in raw_backings.items() if old_id in vm_ids
            }
        new_payload["disk_requirements"] = [
            {
                "vm_id": vm_ids[vm["id"]],
                "directory": str(settings.playground_disk_dir / session_id),
                "bytes": int(vm["disk_gb"] or 0) * 1024**3,
            }
            for vm in snapshot["vms"]
        ]
        new_metadata = _coordinator_metadata(new_payload)
        new_metadata.pop("reset_snapshot", None)
        new_metadata["phase"] = _PHASE_RUNNING
        new_metadata["session_id"] = session_id
        new_payload[_COORDINATOR_METADATA] = new_metadata
        instance.playground_session_id = session_id
        reset_action.payload_json = _dump_payload(new_payload)
        event.payload_json = _dump_payload(new_payload)
        session.flush()
    return replace(
        claim,
        session_id=session_id,
        payload=new_payload,
        phase=_PHASE_RUNNING,
    )


def _claim_instance_action(instance_id: str, action: str) -> _OperationClaim | None:
    """CAS-claim one action outbox, recovering only an expired worker lease."""

    return _claim_operation(instance_id, action)


def _run_instance_reset(claim: _OperationClaim) -> str:
    """Replace unlocked writable overlays with a new catalog-backed session."""

    instance_id = claim.instance_id
    if claim.phase == _PHASE_COMPENSATING:
        _compensate_launch(claim, RuntimeError("resuming interrupted compensation"))
        return instance_id
    payload = claim.payload
    reservation: DiskReservation | None = None
    working_claim = claim
    try:
        with _cleanup_host_custody(working_claim) as owned:
            if not owned:
                return instance_id
            prepared = _prepare_reset_cleanup(working_claim)
            if prepared is None:
                return instance_id
            working_claim = prepared
            # The durable reset snapshot and phase above establish cleanup
            # custody before this destructive host effect starts.
            _checked_destroy_session(working_claim, require_session_pointer=False)
            created = _create_reset_session(working_claim)
            if created is None:
                return instance_id
            working_claim = replace(created, stale_claim=False)
        payload = working_claim.payload
        new_session_id = working_claim.session_id
        from ..lab_packages.runner_preflight import require_runner_host_ready

        require_runner_host_ready()
        with _provisioning_custody(working_claim) as (owned, reservation):
            if not owned:
                return instance_id
            if payload.get("locked"):
                _execute_locked_playground(new_session_id, payload, working_claim)
            else:
                _execute_unlocked_playground(new_session_id, working_claim)
            _verify_overlay_allocation(new_session_id, payload, reservation)
            _drive_workloads(new_session_id, working_claim)
            _run_readiness(new_session_id, payload, working_claim)
        _release_owned_reservation(working_claim, reservation)
        reservation = None
        with blocking_file_lock(_lifecycle_lock_path(instance_id)):
            if not _mark_ready(working_claim):
                return instance_id
    except (DiskReservationError, OSError, RuntimeError, ValueError) as exc:
        _release_owned_reservation(working_claim, reservation)
        reservation = None
        _compensate_launch(working_claim, exc)
    finally:
        if reservation is not None:
            _release_owned_reservation(working_claim, reservation)
    return instance_id


def run_instance_action(instance_id: str, action: str) -> str:
    """Consume cancellation/teardown after commit and remove Playground rows."""

    claim = _claim_instance_action(instance_id, action)
    if claim is None:
        return instance_id
    if action == "reset":
        return _run_instance_reset(claim)
    cleanup_error: Exception | None = None
    with blocking_file_lock(_lifecycle_lock_path(instance_id)):
        with session_scope() as session:
            repositories.serialize_write_transaction(session)
            owned = _claim_rows(
                session,
                claim,
                phases={_PHASE_RUNNING, _PHASE_COMPENSATING},
            )
            if owned is None:
                return instance_id
            _instance, event, action_record = owned
            payload = _with_coordinator_metadata(_payload(event), phase=_PHASE_COMPENSATING)
            event.payload_json = _dump_payload(payload)
            action_record.payload_json = _dump_payload(payload)
            session.flush()
            claim = replace(claim, payload=payload, phase=_PHASE_COMPENSATING)
        try:
            _checked_destroy_session(claim, require_session_pointer=False)
        except Exception as exc:
            cleanup_error = exc
        with session_scope() as session:
            repositories.serialize_write_transaction(session)
            owned = _claim_rows(session, claim, phases={_PHASE_COMPENSATING}, require_session_pointer=False)
            if owned is None:
                return instance_id
            instance, event, action_record = owned
            expected_state = "cancel_requested" if action == "cancel" else "destroying"
            if instance.state != expected_state or instance.playground_session_id not in {None, claim.session_id}:
                return instance_id
            if cleanup_error is not None:
                summary = str(cleanup_error)[:512]
                if instance.state == "cancel_requested":
                    transition_instance(session, instance, "rollback_pending", error=summary)
                    transition_instance(session, instance, "cleanup_incomplete", error=summary)
                else:
                    transition_instance(session, instance, "cleanup_incomplete", error=summary)
                action_record.state = "failed"
                action_record.error_summary = summary
                _complete_event(event, "failed")
                return instance_id
            playground = session.get(PlaygroundSession, claim.session_id)
            if playground is not None:
                session.delete(playground)
                session.flush()
            if instance.playground_session_id == claim.session_id:
                instance.playground_session_id = None
            transition_instance(session, instance, "destroyed")
            action_record.state = "completed"
            action_record.updated_at = datetime.now(UTC)
            _complete_event(event, "completed")
    return instance_id


@celery_app.task(name="packages.coordinate_instance")
def coordinate_instance(instance_id: str) -> str:
    return run_instance_operation(instance_id)


@celery_app.task(name="packages.coordinate_instance_action")
def coordinate_instance_action(instance_id: str, action: str) -> str:
    return run_instance_action(instance_id, action)


def reconcile_package_instances() -> int:
    """Resume every non-terminal package operation after a worker restart."""

    with session_scope() as session:
        events = list(
            session.query(LabOperationOutbox.operation_kind, LabOperationOutbox.operation_id)
            .filter(
                LabOperationOutbox.operation_kind.in_(
                    [
                        "package_instance_launch",
                        "package_instance_cancel",
                        "package_instance_teardown",
                        "package_instance_reset",
                    ]
                ),
                LabOperationOutbox.state.in_(["pending", "claimed"]),
            )
            .all()
        )
    for kind, instance_id in events:
        if kind == "package_instance_launch":
            coordinate_instance.delay(instance_id)
        else:
            coordinate_instance_action.delay(instance_id, kind.removeprefix("package_instance_"))
    return len(events)
