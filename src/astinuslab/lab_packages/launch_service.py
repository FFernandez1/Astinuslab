"""Transactional creation of package-backed Playground instances.

The API owns one SQLAlchemy transaction through this module.  It records all
resource intent before a worker mutates libvirt, avoiding the route-to-route
calls and nested sessions that previously caused SQLite writer-lock failures.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import settings
from ..db.models import LabInstance, LabPackage, LabPackageTombstone, PlaygroundSession
from ..db.repositories import has_active_non_playground_job, has_active_playground_session
from ..playground import admission, orchestrator, repositories
from ..playground.workloads import validate_playground_workload_selection
from .image_binding import ImageBindingError, immutable_boot_layer_path
from .instance_manager import append_action, enqueue_operation
from .models import LabDefinition, LabManifest
from .package_service import LabPackageService
from .preflight import RunnerHostFacts, policy_revision, preflight_package, validate_warning_consent_token


class PackageLaunchError(ValueError):
    """Structured failure suitable for the package API."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class LaunchOperation:
    instance_id: str
    session_id: str
    created: bool


def _available_layers(definition: LabDefinition, service: LabPackageService) -> frozenset[str]:
    available: set[str] = set()
    for vm in definition.vms:
        layer = vm.effective_boot_layer
        if layer is None:
            continue
        try:
            if service.content_store.has(layer.sha256, size_bytes=layer.size_bytes):
                available.add(layer.sha256)
        except (OSError, ValueError):
            continue
    return frozenset(available)


def _requested_capabilities(definition: LabDefinition) -> tuple[str, ...]:
    return ("guest_egress",) if any(network.guest_egress for network in definition.networks) else ()


def _preflight(
    record,
    definition: LabDefinition,
    manifest: LabManifest,
    service: LabPackageService,
    session: Session,
    runner: RunnerHostFacts | None = None,
):
    return preflight_package(
        definition,
        manifest,
        storage_path=service.package_root,
        content_store_path=service.content_store.root,
        playground_disk_path=settings.playground_disk_dir,
        available_assets=_available_layers(definition, service),
        session=session,
        package_digest=record.archive_sha256,
        signature_status=record.signature_status,
        retained_present=True,
        runner=runner,
    )


def launch_package(
    session: Session,
    *,
    record,
    definition: LabDefinition,
    manifest: LabManifest,
    service: LabPackageService,
    idempotency_key: str,
    acknowledged_warning_token: str | None,
    acknowledge_guest_egress: bool = False,
    runner: RunnerHostFacts | None = None,
) -> LaunchOperation:
    """Create durable launch intent using *only* the caller's transaction."""

    # Take the SQLite writer lock before reading idempotency/capacity/session
    # state.  No route and no secondary session is called below.
    repositories.lock_resource_transitions(session)
    # Deletion commits its tombstone before archive I/O. Recheck under the
    # writer reservation so launch and deletion cannot both admit new intent.
    if session.get(LabPackage, record.id, populate_existing=True) is None or session.scalar(
        select(LabPackageTombstone.id).where(
            LabPackageTombstone.package_record_id == record.id,
            LabPackageTombstone.state.in_(("pending", "cleanup_incomplete")),
        )
    ):
        raise PackageLaunchError("package_deleting", "Package is being deleted or has been removed")
    existing = (
        session.query(LabInstance)
        .filter(LabInstance.package_record_id == record.id, LabInstance.idempotency_key == idempotency_key)
        .one_or_none()
    )
    if existing is not None:
        if existing.playground_session_id is None:
            raise PackageLaunchError("launch_reconciliation_required", "Existing launch is awaiting reconciliation")
        return LaunchOperation(existing.id, existing.playground_session_id, False)

    # Package launch creates its session directly, so it must enforce the same
    # Jobs exclusion as ordinary Playground creation while holding the lock.
    if has_active_non_playground_job(session):
        raise PackageLaunchError("session_occupied", "Active forensic Jobs or their resources require cleanup first")
    if has_active_playground_session(session):
        raise PackageLaunchError("session_occupied", "Another Playground is active, resetting, or awaiting cleanup")

    result = _preflight(record, definition, manifest, service, session, runner)
    if not result.ready:
        first = result.errors[0]
        raise PackageLaunchError(first.code, first.message)
    requested_capabilities = _requested_capabilities(definition)
    if "guest_egress" in requested_capabilities and not acknowledge_guest_egress:
        raise PackageLaunchError("warnings_unacknowledged", "Guest egress requires explicit acknowledgement")
    if not validate_warning_consent_token(
        acknowledged_warning_token,
        package_digest=record.archive_sha256,
        warnings=result.warnings,
        policy_revision=policy_revision(),
        requested_capabilities=requested_capabilities,
    ):
        raise PackageLaunchError("warnings_unacknowledged", "Preflight warnings must be acknowledged again")
    if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 128:
        raise PackageLaunchError("invalid_idempotency_key", "A client launch idempotency key is required")

    immutable_backings: dict[str, Path] = {}
    if definition.reproducibility == "locked":
        # This checks content-addressed ownership, qcow2 metadata (including
        # external backing/data files), and libvirt path ACLs before durable
        # resource intent exists.  The coordinator persists only this verified
        # layer as the overlay backing and never consults a mutable catalog.
        for vm in definition.vms:
            try:
                immutable_backings[vm.id] = immutable_boot_layer_path(vm, service.content_store)
            except ImageBindingError as exc:
                raise PackageLaunchError(str(exc), f"Locked VM {vm.id} boot layer is unavailable or unsafe") from exc
    else:
        # Validate catalog/workload inputs before recording a session.
        for vm in definition.vms:
            image = lookup_playground_image(vm.image_id, role=vm.role)
            if image is None or not _regular_image_path(image):
                raise PackageLaunchError("image-unavailable", f"VM {vm.id} image is unavailable")
            workload = validate_playground_workload_selection(role=vm.role, workload_id=vm.workload_id, image=image)
            if not workload.ok:
                reason = workload.detail.strip() or "workload is incompatible"
                raise PackageLaunchError("workload-incompatible", f"VM {vm.id}: {reason}")

    try:
        admission.ensure_vm_capacity(session, requested=len(definition.vms))
    except Exception as exc:
        # Admission already uses the same SQLite lock. Keep the API code
        # stable instead of leaking implementation-specific messages.
        raise PackageLaunchError("vm-capacity-exhausted", str(exc)) from exc
    session_id = f"pg-{secrets.token_hex(6)}"
    instance = LabInstance(
        id=f"lpi-{secrets.token_hex(8)}",
        package_record_id=record.id,
        idempotency_key=idempotency_key,
        state="registered",
    )
    session.add(instance)
    # queued is a durable, active session and accepts registrations.  The
    # coordinator advances it after the database transaction commits.
    playground = PlaygroundSession(
        id=session_id,
        lab_package_record_id=record.id,
        status="queued",
    )
    session.add(playground)
    session.flush()
    instance.playground_session_id = session_id

    network_ids: dict[str, str] = {}
    for network in definition.networks:
        network_id = f"net-{secrets.token_hex(6)}"
        network_ids[network.id] = network_id
        repositories.register_network(
            session,
            network_id=network_id,
            job_id=session_id,
            name=network.id,
            subnet_cidr=network.subnet_cidr,
            isolated=network.isolated,
            libvirt_net_name=orchestrator.libvirt_net_name(session_id, network.id),
        )

    vm_ids: dict[str, str] = {}
    for vm in definition.vms:
        vm_id = f"vm-{secrets.token_hex(6)}"
        vm_ids[vm.id] = vm_id
        resources = _resources_for_vm(vm)
        repositories.register_vm(
            session,
            vm_id=vm_id,
            job_id=session_id,
            label=vm.id,
            image_id=vm.image_id,
            domain_name=f"astinuslab-pg-{session_id}-{vm.id}",
            disk_path=str(settings.playground_disk_dir / session_id / f"{vm.id}.qcow2"),
            role=vm.role,
            # A locked baked layer must never receive authoring-time workload
            # provisioning on the recipient.
            workload_id=None if definition.reproducibility == "locked" else vm.workload_id,
            vcpus=resources[0],
            memory_mb=resources[1],
            disk_gb=resources[2],
        )
        for ordinal, logical_network_id in enumerate(vm.network_ids):
            repositories.attach_nic(
                session,
                nic_id=f"nic-{secrets.token_hex(6)}",
                vm_id=vm_id,
                network_id=network_ids[logical_network_id],
                mac=orchestrator.generate_mac(),
                ordinal=ordinal,
            )

    for node in definition.topology:
        actual_id = network_ids[node.node_id] if node.node_type == "network" else vm_ids[node.node_id]
        repositories.save_topology_node(
            session,
            session_id,
            actual_id,
            {"node_id": actual_id, "node_type": node.node_type, "x": node.x, "y": node.y},
        )
    payload = {
        "session_id": session_id,
        "package_record_id": record.id,
        "locked": definition.reproducibility == "locked",
        "immutable_backings": {
            vm_ids[vm.id]: {
                "path": str(immutable_backings[vm.id]),
                "sha256": vm.boot_layer.sha256,
                "size_bytes": vm.boot_layer.size_bytes,
            }
            for vm in definition.vms
            if vm.id in immutable_backings and vm.boot_layer is not None
        },
        "readiness": {
            vm_ids[vm.id]: [probe.model_dump(mode="json") for probe in vm.workload.readiness_probes]
            for vm in definition.vms
            if vm.workload is not None and vm.workload.readiness_probes
        },
        "disk_requirements": [
            # The reservation lives beside the overlay it protects.  Holding
            # the directory lock across qemu allocation makes the journaled
            # capacity reservation and the eventual writable layer one
            # filesystem-scoped handoff, rather than a broad-root estimate.
            {
                "vm_id": vm_ids[vm.id],
                "directory": str(settings.playground_disk_dir / session_id),
                "bytes": vm.disk_gb * 1024**3,
            }
            for vm in definition.vms
        ],
    }
    append_action(session, instance.id, "launch", payload)
    enqueue_operation(session, kind="package_instance_launch", operation_id=instance.id, payload=payload)
    record.last_used_at = datetime.now(UTC)
    session.flush()
    return LaunchOperation(instance.id, session_id, True)


def _regular_image_path(image: dict) -> bool:
    path = image.get("path")
    if not isinstance(path, str) or not path:
        return False
    try:
        return Path(path).is_file() and not Path(path).is_symlink()
    except OSError:
        return False


def _resources_for_vm(vm) -> tuple[int, int, int]:
    # Unlocked catalog values are only defaults; the definition contains the
    # effective resource contract exported by the sender.
    return vm.vcpus, vm.memory_mb, vm.disk_gb


# Kept as module aliases to make the service readily testable and to avoid
# importing API routes (which own request/session lifetime) into launch code.
lookup_playground_image = orchestrator.lookup_playground_image
