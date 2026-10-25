"""Playground VM lifecycle: spawn a lab VM, and tear one down.

Not to be confused with `astinuslab.virtualization.vm_lifecycle`, which is the
libvirt driver. This module is the orchestration above it, and reaches it only
through `po._vm_lifecycle()`.

Split out of `orchestrator.py` so that module stays readable. The two Celery
tasks stay there -- `playground.spawn_vm` and `playground.destroy_vm` -- as
wrappers that delegate straight here, because a task has to be defined where it
is imported for registration to happen, and this module is imported from inside
those wrappers.

This is the largest of the domain slices and it carries three helpers named for
other domains. `_dispatch_workload_provisioning` publishes one provisioning
message and then tears the VM down; `_reserve_nic_rows` creates NIC rows inside
the spawn transaction, under the spawn claim token; `_resolve_nic_addresses`
lets guest-IP lookup failures travel into the spawn summary. All three read as
workload or NIC code and are none of it, which is why the earlier slices left
them behind.

`settings`, `session_scope` and the shared primitives this needs are reached
through the orchestrator module object (`po.<name>`) rather than imported by
name. That is not style. `orchestrator` is what the tests monkeypatch -- 82 sites
patch `po._vm_lifecycle` alone -- and a `from .orchestrator import settings`
here would bind the original at import time, so a patched value would silently
not apply to this module. Attribute access resolves at call time and keeps that
working.

What every custody check here guards against is a VM whose disk exists and whose
domain may be defined, with a database row that no longer says so. That is the
one outcome an operator cannot resolve without a manual libvirt sweep, and each
check has a case in tests/python/playground/test_orchestrator.py.
"""

from __future__ import annotations

import logging
import secrets
import stat
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from ..virtualization.host_helpers import ensure_evidence_dir, storage_root
from . import cloud_init_seed
from . import orchestrator as po
from . import repositories as pr
from .resource_custody import LostResourceCustody, ResourceClaimLease
from .resources import validate_vm_resources

logger = logging.getLogger(__name__)


def _checked_immutable_backing_path(value: str) -> Path:
    """Defend the worker boundary after package binding checked qcow metadata."""

    try:
        path = Path(value)
        inspected = path.lstat()
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeError("immutable backing is unavailable") from exc
    if stat.S_ISLNK(inspected.st_mode) or not stat.S_ISREG(inspected.st_mode):
        raise RuntimeError("immutable backing is unsafe")
    return path


def _wait_for_active_networks(
    *,
    job_id: str,
    network_ids: set[str],
    timeout_seconds: float = 120.0,
    poll_seconds: float = 0.5,
) -> dict[str, str]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        with po.session_scope() as s:
            networks = {n.id: n for n in pr.list_session_networks(s, job_id) if n.id in network_ids}
            missing = network_ids - set(networks)
            failed = [
                n for n in networks.values() if n.state in {"failed", "destroyed", "destroying", "destroy_queued"}
            ]
            if missing:
                raise RuntimeError(f"unknown network(s): {', '.join(sorted(missing))}")
            if failed:
                states = ", ".join(f"{n.id}={n.state}" for n in failed)
                raise RuntimeError(f"network not usable: {states}")
            if all(n.state == "active" for n in networks.values()):
                return {n.id: n.libvirt_net_name for n in networks.values()}

        if time.monotonic() >= deadline:
            pending = ", ".join(sorted(network_ids))
            raise RuntimeError(f"timed out waiting for active network(s): {pending}")
        time.sleep(poll_seconds)


def _claim_vm_row_for_spawn(
    *,
    vm_id: str,
    job_id: str,
    label: str,
    image_id: str,
    domain_name: str,
    disk_path: Path,
    role: str,
    workload_id: str | None,
    reservation_token: str | None,
    effective_vcpus: int | None,
    effective_memory_mb: int | None,
    effective_disk_gb: int | None,
    source_image_path: Path | None = None,
) -> Any:
    """Register or re-adopt the VM row, then claim it for this spawn.

    Returns the claim's lease token, or ``None`` when the compare-and-swap lost
    and another delivery owns the spawn.

    A spawn can be delivered twice. The second delivery must finish the *same*
    VM or refuse: every identifying field of an existing row is compared, and a
    mismatch raises rather than letting a redelivery quietly re-point a row at a
    different image, disk or workload.
    """
    with po.session_scope() as s:
        from ..db.models import JobVM

        pr.lock_resource_transitions(s)
        pr.require_session_open_for_authoring(s, job_id)
        existing_vm = s.query(JobVM).filter_by(id=vm_id).one_or_none()
        expected_reservation = (
            po._pending_recipe_reservation(existing_vm, job_id, reservation_token)
            if reservation_token is not None
            else None
        )
        if existing_vm is None:
            if source_image_path is not None and not source_image_path.is_file():
                raise RuntimeError(f"image not available: {image_id}")
            pr.register_vm(
                s,
                vm_id=vm_id,
                job_id=job_id,
                label=label,
                image_id=image_id,
                domain_name=domain_name,
                disk_path=str(disk_path),
                role=role,
                workload_id=workload_id,
                vcpus=effective_vcpus,
                memory_mb=effective_memory_mb,
                disk_gb=effective_disk_gb,
            )
        else:
            expected = {
                "session_id": job_id,
                "label": label,
                "image_id": image_id,
                "domain_name": domain_name,
                "disk_path": str(disk_path),
                "role": role,
                "workload_id": workload_id,
            }
            mismatched = [name for name, value in expected.items() if getattr(existing_vm, name) != value]
            persisted_resources = (existing_vm.vcpus, existing_vm.memory_mb, existing_vm.disk_gb)
            effective_resources = (effective_vcpus, effective_memory_mb, effective_disk_gb)
            if persisted_resources == (None, None, None):
                existing_vm.vcpus, existing_vm.memory_mb, existing_vm.disk_gb = effective_resources
            elif persisted_resources != effective_resources:
                mismatched.extend(
                    name
                    for name, persisted, effective in zip(
                        ("vcpus", "memory_mb", "disk_gb"), persisted_resources, effective_resources, strict=True
                    )
                    if persisted != effective
                )
            if mismatched:
                raise RuntimeError(f"VM retry identity mismatch for {vm_id}: {', '.join(mismatched)}")
        claimed = (
            pr.claim_vm_for_spawn(s, vm_id, expected_updated_at=expected_reservation)
            if expected_reservation is not None
            else pr.claim_vm_for_spawn(s, vm_id)
        )
        if not claimed:
            return None
        claimed_vm = s.get(JobVM, vm_id)
        assert claimed_vm is not None  # noqa: S101 - successful CAS guarantees the row
        s.refresh(claimed_vm)
        lease_token = claimed_vm.updated_at
    return lease_token


def _reserve_nic_rows(
    *,
    vm_id: str,
    job_id: str,
    nic_specs: list[dict[str, Any]],
    nic_publication_token: Any,
) -> list[dict]:
    """Reserve or re-adopt this VM's NIC rows, under the transition lock.

    A redelivered spawn must find the same interfaces it created, so an
    existing row is refused if it has moved to another VM or network, or if its
    MAC or ordinal no longer match the spec.
    """
    with po.session_scope() as s:
        from ..db.models import JobNetwork, JobVM, JobVMNic, PlaygroundSession

        pr.lock_resource_transitions(s)
        parent = s.query(PlaygroundSession).filter(PlaygroundSession.id == job_id).with_for_update().one_or_none()
        claimed_vm = (
            s.query(JobVM)
            .filter(
                JobVM.id == vm_id,
                JobVM.session_id == job_id,
                JobVM.state == "starting",
                JobVM.updated_at == nic_publication_token,
            )
            .with_for_update()
            .one_or_none()
        )
        network_ids = sorted({spec["network_id"] for spec in nic_specs})
        networks = (
            s.query(JobNetwork)
            .filter(JobNetwork.id.in_(network_ids), JobNetwork.session_id == job_id)
            .order_by(JobNetwork.id)
            .with_for_update()
            .all()
        )
        networks_by_id = {network.id: network for network in networks}
        if parent is None or parent.status not in {"queued", "running"} or claimed_vm is None:
            raise LostResourceCustody(f"VM {vm_id} lost spawn custody before NIC reservation")
        unusable = [
            network_id
            for network_id in network_ids
            if network_id not in networks_by_id or networks_by_id[network_id].state != "active"
        ]
        if unusable:
            raise LostResourceCustody(
                f"VM {vm_id} lost active network custody before NIC reservation: {', '.join(unusable)}"
            )

        nic_rows: list[dict] = []
        for ordinal, spec in enumerate(nic_specs):
            reserved_nic_id = spec.get("nic_id") or secrets.token_hex(8)
            nic = s.query(JobVMNic).filter_by(id=reserved_nic_id).with_for_update().one_or_none()
            if nic is None:
                mac = spec.get("mac") or po.generate_mac()
                pr.attach_nic(
                    s,
                    nic_id=reserved_nic_id,
                    vm_id=vm_id,
                    network_id=spec["network_id"],
                    mac=mac,
                    ordinal=ordinal,
                )
            else:
                if nic.job_vm_id != vm_id or nic.job_network_id != spec["network_id"]:
                    raise LostResourceCustody(f"NIC {reserved_nic_id} belongs to a different VM or network")
                if spec.get("mac") is not None and nic.mac != spec["mac"]:
                    raise LostResourceCustody(f"NIC {reserved_nic_id} retry MAC identity mismatch")
                if nic.ordinal != ordinal:
                    raise LostResourceCustody(f"NIC {reserved_nic_id} retry ordinal identity mismatch")
                mac = nic.mac
            nic_rows.append(
                {
                    "mac": mac,
                    "libvirt_net": networks_by_id[spec["network_id"]].libvirt_net_name,
                    "nic_id": reserved_nic_id,
                }
            )
    return nic_rows


def _dispatch_workload_provisioning(
    *,
    vm_id: str,
    domain_name: str,
    disk_path: Path,
    lifecycle,
    claim_lease,
) -> None:
    """Hand the booted VM to workload provisioning, tearing down if that fails."""
    from ..db.models import JobVM

    try:
        po.provision_vm_workload.delay(vm_id=vm_id)
    except Exception as exc:
        # A publish failure can be ambiguous: claim cleanup custody before
        # touching the live domain. If a worker already promoted the row,
        # the message was delivered and that worker owns the VM.
        with po.session_scope() as s:
            cleanup_claimed = pr.claim_vm_for_workload_enqueue_cleanup(s, vm_id)
            if cleanup_claimed:
                cleanup_vm = s.get(JobVM, vm_id)
                assert cleanup_vm is not None  # noqa: S101 - successful CAS guarantees the row
                s.refresh(cleanup_vm)
                cleanup_lease_token = cleanup_vm.updated_at
        if not cleanup_claimed:
            logger.warning(
                "spawn_vm: workload enqueue raised after worker claimed VM %s: %s",
                vm_id,
                exc,
            )
            # Was `return vm_id` when this lived in spawn_vm. That sat
            # immediately before the function's own final `return vm_id`,
            # so leaving the helper here reaches the same result.
            return
        cleanup_lease = ResourceClaimLease(
            kind="vm",
            resource_id=vm_id,
            state="destroying",
            token=cleanup_lease_token,
            session_scope_factory=po.session_scope,
            grace_seconds=getattr(po.settings, "celery_reconciliation_grace_seconds", 120),
        )
        try:
            cleanup_lease.start()
            cleanup_lease.assert_owned()
            cleanup_error = po._cleanup_spawn_artifacts(
                lifecycle=lifecycle,
                domain_name=domain_name,
                disk_path=disk_path,
                domain_may_exist=True,
            )
            cleanup_lease.assert_owned()
            cleanup_expected_updated_at = cleanup_lease.publication_token()
        finally:
            cleanup_lease.stop()
        error_summary = f"workload enqueue failed: {exc}"
        if cleanup_error is not None:
            error_summary = f"{error_summary}; cleanup retry required: {cleanup_error}"
        with po.session_scope() as s:
            if not pr.finish_vm_destroy(
                s,
                vm_id,
                "teardown_failed" if cleanup_error is not None else "failed",
                expected_updated_at=cleanup_expected_updated_at,
                error=error_summary,
            ):
                raise RuntimeError(f"VM {vm_id} lost workload-enqueue cleanup custody") from exc
            if cleanup_error is None:
                pr.delete_nics_for_vm(s, vm_id)
        raise


def _resolve_spawn_resources(
    *,
    vm_id: str,
    job_id: str,
    label: str,
    image_id: str,
    image: dict | None,
    role: str,
    workload_id: str | None,
    domain_name: str,
    disk_path: Path,
    vcpus: int | None,
    memory_mb: int | None,
    disk_gb: int | None,
    resources_resolved: bool,
) -> tuple[int, int, int | None]:
    """Settle this VM's vcpu, memory and disk sizing before anything is created.

    A rejected size fences the row first: the caller would otherwise raise with
    a registered VM left in a state no later pass can attribute.
    """
    try:
        if resources_resolved:
            effective_vcpus, effective_memory_mb, effective_disk_gb = validate_vm_resources(vcpus, memory_mb, disk_gb)
        else:
            assert image is not None  # noqa: S101 - mutable path resolved above
            effective_vcpus, effective_memory_mb, effective_disk_gb = po.resolve_vm_resources(
                role, image, vcpus=vcpus, memory_mb=memory_mb, disk_gb=disk_gb
            )
    except ValueError as exc:
        error = f"VM resource validation failed: {exc}"
        _fence_vm_resource_validation_failure(
            vm_id=vm_id,
            job_id=job_id,
            label=label,
            image_id=image_id,
            role=role,
            workload_id=workload_id,
            domain_name=domain_name,
            disk_path=str(disk_path),
            vcpus=vcpus,
            memory_mb=memory_mb,
            disk_gb=disk_gb,
            error=error,
        )
        raise RuntimeError(error) from exc
    return effective_vcpus, effective_memory_mb, effective_disk_gb


def _resolve_nic_addresses(*, nic_rows: list[dict], lifecycle, domain_name: str | None = None) -> list[str]:
    """Look up each NIC's guest address, in parallel, and record what resolved.

    A lookup failure is collected rather than raised: the VM is booted and its
    other interfaces are usable, so the spawn completes and the errors travel
    into the row's summary.
    """
    ip_errors: list[str] = []

    def resolve_nic_ip(nic_row: dict) -> tuple[str, str | None, str | None]:
        try:
            ip = lifecycle.get_ip(
                po.settings.libvirt_uri,
                nic_row["libvirt_net"],
                nic_row["mac"],
                domain_name=domain_name,
            )
            return nic_row["nic_id"], ip, None
        except Exception as exc:
            return nic_row["nic_id"], None, str(exc)

    with ThreadPoolExecutor(max_workers=max(1, min(8, len(nic_rows)))) as executor:
        futures = [executor.submit(resolve_nic_ip, nic_row) for nic_row in nic_rows]
        for future in as_completed(futures):
            resolved_nic_id, resolved_ip, lookup_error = future.result()
            if lookup_error is not None:
                message = f"IP lookup failed for {resolved_nic_id}: {lookup_error}"
                ip_errors.append(message)
                logger.warning("spawn_vm: get_ip failed for nic %s: %s", resolved_nic_id, lookup_error)
                continue
            if resolved_ip:
                with po.session_scope() as s:
                    pr.update_nic_ip(s, resolved_nic_id, resolved_ip)
    return ip_errors


def _boot_playground_vm(
    *,
    job_id: str,
    vm_id: str,
    label: str,
    image: dict | None,
    domain_name: str,
    disk_path: Path,
    base_image_path: Path,
    nic_rows: list[dict],
    effective_vcpus: int,
    effective_memory_mb: int,
    effective_disk_gb: int | None,
    lifecycle,
    claim_lease,
) -> None:
    """Write cloud-init if the image wants it, then define and start the domain.

    ``lifecycle`` is created by the caller rather than here: if this raises, the
    caller's failure cleanup needs the same lifecycle object to destroy whatever
    got as far as libvirt.
    """
    claim_lease.assert_owned()
    cloud_init_paths: cloud_init_seed._PlaygroundCloudInitPaths = (
        cloud_init_seed._write_playground_cloud_init(
            job_id=job_id, vm_id=vm_id, label=label, image=image, disk_path=disk_path
        )
        if image is not None and cloud_init_seed._playground_cloud_init_enabled(image)
        else {}
    )
    with po.session_scope() as s:
        pr.require_session_open_for_authoring(s, job_id)
    lifecycle.boot_playground(
        job_id=job_id,
        domain_name=domain_name,
        disk_path=disk_path,
        base_image_path=base_image_path,
        vcpus=effective_vcpus,
        memory_mb=effective_memory_mb,
        networks=[{"libvirt_net": n["libvirt_net"], "mac": n["mac"]} for n in nic_rows],
        disk_gb=effective_disk_gb,
        libvirt_uri=po.settings.libvirt_uri,
        cwd=po.settings.base_dir,
        **cloud_init_paths,
    )


def _publish_spawn_result(
    *,
    vm_id: str,
    workload_id: str | None,
    prebaked: bool,
    ip_errors: list[str],
    claim_lease,
) -> None:
    """Record the finished spawn against the row this task still owns."""
    expected_updated_at = claim_lease.publication_token()
    with po.session_scope() as s:
        ip_error_summary = "; ".join(ip_errors) if ip_errors else None
        if workload_id:
            finished = pr.finish_vm_spawn(
                s,
                vm_id,
                "provisioning_workload",
                expected_updated_at=expected_updated_at,
                error=ip_error_summary,
                workload_state="prebaked" if prebaked else "provisioning",
            )
        else:
            finished = pr.finish_vm_spawn(
                s,
                vm_id,
                "running",
                expected_updated_at=expected_updated_at,
                error=ip_error_summary,
            )
        if not finished:
            raise RuntimeError(f"VM {vm_id} lost spawn custody before completion")


def _resolve_base_image(
    *,
    image: dict | None,
    image_id: str,
    locked_backing: Path | None,
    workload_id: str | None,
) -> tuple[Path, bool]:
    """Decide which file this VM boots from, and whether it is already baked.

    Returns the backing path and whether a workload cache supplied it, which
    decides the workload state the finished row is published with.
    """
    prebaked = False
    if locked_backing is not None:
        # The package launcher supplied this only after validating its
        # content-store identity and qcow2 metadata.  Do not look up a
        # catalog image, generate cloud-init, or apply workload caches.
        base_image_path = locked_backing
    else:
        assert image is not None  # noqa: S101 - mutable path resolved above
        image_path = image.get("path")
        if not image_path or not Path(image_path).is_file():
            raise RuntimeError(f"image not available: {image_id} ({image_path or 'missing path'})")
        base_image_path = Path(image["path"])
    if workload_id:
        cached = po._check_playground_workload_cache(image_id, workload_id, base_image_path)
        if cached is not None:
            po._apply_workload_cache_acl(cached)
            base_image_path = cached
            prebaked = True
    return base_image_path, prebaked


def _resolve_spawn_targets(
    *,
    job_id: str,
    label: str,
    image_id: str,
    role: str,
    workload_id: str | None,
    immutable_backing_path: str | None,
    resources_resolved: bool,
) -> tuple[str, Path, Path, Path, dict | None, Path | None]:
    """Settle the domain name, disk location and backing image for this spawn.

    The disk path is re-checked against the resolved root even though the label
    is already validated: a symlink inside the playground root would otherwise
    place the disk somewhere this installation does not own.

    An immutable backing is a shared read-only file, so it refuses a mutable
    workload and requires sizing to have been settled by the caller.
    """
    domain_name = f"astinuslab-pg-{job_id}-{label}"
    configured_disk_root = po.settings.playground_disk_dir
    disk_root = configured_disk_root.resolve()
    disk_path = disk_root / job_id / f"{label}.qcow2"
    if not disk_path.resolve().is_relative_to(disk_root):
        raise RuntimeError(f"playground disk path escapes root: {disk_path}")
    image: dict | None = None
    locked_backing: Path | None = None
    if immutable_backing_path is not None:
        if workload_id is not None:
            raise RuntimeError("immutable backing cannot receive mutable workload provisioning")
        if not resources_resolved:
            raise RuntimeError("immutable backing requires resolved VM resources")
        locked_backing = _checked_immutable_backing_path(immutable_backing_path)
    else:
        image = po.lookup_playground_image(image_id, role=role)
        if image is None:
            raise RuntimeError(f"unknown or unavailable playground image: {image_id}")
    return domain_name, configured_disk_root, disk_root, disk_path, image, locked_backing


def _fence_vm_resource_validation_failure(
    *,
    vm_id: str,
    job_id: str,
    label: str,
    image_id: str,
    role: str,
    workload_id: str | None,
    domain_name: str,
    disk_path: str,
    vcpus: int | None,
    memory_mb: int | None,
    disk_gb: int | None,
    error: str,
) -> bool:
    with po.session_scope() as s:
        return pr.fail_pending_vm_for_resource_validation(
            s,
            vm_id=vm_id,
            session_id=job_id,
            label=label,
            image_id=image_id,
            role=role,
            workload_id=workload_id,
            domain_name=domain_name,
            disk_path=disk_path,
            vcpus=vcpus,
            memory_mb=memory_mb,
            disk_gb=disk_gb,
            error=error,
        )


def spawn_vm(
    *,
    job_id: str,
    vm_id: str,
    label: str,
    image_id: str,
    nic_specs: list[dict[str, Any]],
    role: str = "target",
    workload_id: str | None = None,
    vcpus: int | None = None,
    memory_mb: int | None = None,
    disk_gb: int | None = None,
    resources_resolved: bool = False,
    immutable_backing_path: str | None = None,
    reservation_token: str | None = None,
) -> str:
    po._validate_playground_name("job_id", job_id)
    po._validate_playground_name("label", label)

    # The API registers a pending row before queueing this task. Verification
    # may freeze the parent before delivery, so reject at task entry before
    # image resolution or any filesystem/libvirt side effect.
    with po.session_scope() as s:
        pr.require_session_open_for_authoring(s, job_id)
        if reservation_token is not None:
            from ..db.models import JobVM

            po._pending_recipe_reservation(s.get(JobVM, vm_id), job_id, reservation_token)

    domain_name, configured_disk_root, disk_root, disk_path, image, locked_backing = _resolve_spawn_targets(
        job_id=job_id,
        label=label,
        image_id=image_id,
        role=role,
        workload_id=workload_id,
        immutable_backing_path=immutable_backing_path,
        resources_resolved=resources_resolved,
    )
    effective_vcpus, effective_memory_mb, effective_disk_gb = _resolve_spawn_resources(
        vm_id=vm_id,
        job_id=job_id,
        label=label,
        image_id=image_id,
        image=image,
        role=role,
        workload_id=workload_id,
        domain_name=domain_name,
        disk_path=disk_path,
        vcpus=vcpus,
        memory_mb=memory_mb,
        disk_gb=disk_gb,
        resources_resolved=resources_resolved,
    )

    vm_registered = False
    lifecycle = None
    prebaked = False
    lease_token = _claim_vm_row_for_spawn(
        vm_id=vm_id,
        job_id=job_id,
        label=label,
        image_id=image_id,
        domain_name=domain_name,
        disk_path=disk_path,
        role=role,
        workload_id=workload_id,
        reservation_token=reservation_token,
        effective_vcpus=effective_vcpus,
        effective_memory_mb=effective_memory_mb,
        effective_disk_gb=effective_disk_gb,
        source_image_path=locked_backing or (Path(image["path"]) if image and image.get("path") else None),
    )
    if lease_token is None:
        # Another delivery owns this spawn. Returning the id is the whole
        # contract for a duplicate: no boot, and nothing written to a row this
        # task never claimed.
        return vm_id
    vm_registered = True
    claim_lease = ResourceClaimLease(
        kind="vm",
        resource_id=vm_id,
        state="starting",
        token=lease_token,
        session_scope_factory=po.session_scope,
        grace_seconds=getattr(po.settings, "celery_reconciliation_grace_seconds", 120),
    )
    try:
        claim_lease.start()
        # Not a bare mkdir: it inherits the umask, and 0002 leaves every root it
        # creates group-writable. The privileged ACL helper walks each component
        # from the storage root down to the overlay and refuses a group-writable
        # one, so the pre-boot grant failed and the domain was never defined.
        ensure_evidence_dir(configured_disk_root, storage_root(po.settings))
        po._create_directory_no_follow(disk_root, job_id, final_mode=0o700)
        base_image_path, prebaked = _resolve_base_image(
            image=image,
            image_id=image_id,
            locked_backing=locked_backing,
            workload_id=workload_id,
        )
        _wait_for_active_networks(
            job_id=job_id,
            network_ids={spec["network_id"] for spec in nic_specs},
        )
        nic_publication_token = claim_lease.publication_token()
        nic_rows = _reserve_nic_rows(
            vm_id=vm_id,
            job_id=job_id,
            nic_specs=nic_specs,
            nic_publication_token=nic_publication_token,
        )
        claim_lease = ResourceClaimLease(
            kind="vm",
            resource_id=vm_id,
            state="starting",
            token=nic_publication_token,
            session_scope_factory=po.session_scope,
            grace_seconds=getattr(po.settings, "celery_reconciliation_grace_seconds", 120),
        )
        claim_lease.start()

        lifecycle = po._vm_lifecycle()
        _boot_playground_vm(
            job_id=job_id,
            vm_id=vm_id,
            label=label,
            image=image,
            domain_name=domain_name,
            disk_path=disk_path,
            base_image_path=base_image_path,
            nic_rows=nic_rows,
            effective_vcpus=effective_vcpus,
            effective_memory_mb=effective_memory_mb,
            effective_disk_gb=effective_disk_gb,
            lifecycle=lifecycle,
            claim_lease=claim_lease,
        )
        claim_lease.assert_owned()
        ip_errors = _resolve_nic_addresses(nic_rows=nic_rows, lifecycle=lifecycle, domain_name=domain_name)
        claim_lease.assert_owned()
    except (LostResourceCustody, pr.AuthoringClosed):
        raise
    except Exception as exc:
        if vm_registered:
            try:
                claim_lease.assert_owned()
            except LostResourceCustody as custody_error:
                raise custody_error from exc
            cleanup_error = po._cleanup_spawn_artifacts(
                lifecycle=lifecycle,
                domain_name=domain_name,
                disk_path=disk_path,
            )
            spawn_error_summary = str(exc)
            if cleanup_error is not None:
                spawn_error_summary = f"{spawn_error_summary}; cleanup retry required: {cleanup_error}"
            try:
                expected_updated_at = claim_lease.publication_token()
            except LostResourceCustody as custody_error:
                raise custody_error from exc
            with po.session_scope() as s:
                if not pr.finish_vm_spawn(
                    s,
                    vm_id,
                    "teardown_failed" if cleanup_error is not None else "failed",
                    expected_updated_at=expected_updated_at,
                    error=spawn_error_summary,
                ):
                    raise LostResourceCustody(f"VM {vm_id} lost spawn custody on failure") from exc
                pr.delete_nics_for_vm(s, vm_id)
        raise
    finally:
        claim_lease.stop()

    _publish_spawn_result(
        vm_id=vm_id,
        workload_id=workload_id,
        prebaked=prebaked,
        ip_errors=ip_errors,
        claim_lease=claim_lease,
    )
    if workload_id:
        _dispatch_workload_provisioning(
            vm_id=vm_id,
            domain_name=domain_name,
            disk_path=disk_path,
            lifecycle=lifecycle,
            claim_lease=claim_lease,
        )
    return vm_id


def destroy_vm(*, vm_id: str) -> str:
    attaching = False
    with po.session_scope() as s:
        from ..db.models import JobVM

        pr.lock_resource_transitions(s)
        vm = s.query(JobVM).filter_by(id=vm_id).with_for_update().one()
        if vm.state == "destroyed":
            return vm_id
        domain_name = vm.domain_name
        disk_path = Path(vm.disk_path)
        if pr.vm_has_attaching_nics(s, vm_id):
            pr.cancel_queued_vm_destroy(
                s,
                vm_id,
                "running",
                error="VM destroy cancelled because NIC attachment is in progress",
            )
            attaching = True
        elif not pr.claim_vm_for_destroy(s, vm_id):
            raise RuntimeError(f"VM {vm_id} is busy in state {vm.state}")
        else:
            s.refresh(vm)
            lease_token = vm.updated_at

    if attaching:
        raise RuntimeError(f"VM {vm_id} destroy unavailable while NIC attachment is in progress")

    teardown_error: str | None = None
    claim_lease = ResourceClaimLease(
        kind="vm",
        resource_id=vm_id,
        state="destroying",
        token=lease_token,
        session_scope_factory=po.session_scope,
        grace_seconds=getattr(po.settings, "celery_reconciliation_grace_seconds", 120),
    )
    try:
        claim_lease.start()
        claim_lease.assert_owned()
        lifecycle = po._vm_lifecycle()
        lifecycle.destroy(po.settings.libvirt_uri, domain_name)
        claim_lease.assert_owned()
    except LostResourceCustody:
        claim_lease.stop()
        raise
    except Exception as exc:
        claim_lease.assert_owned()
        logger.warning("destroy_vm: libvirt cleanup failed for %s: %s", domain_name, exc)
        teardown_error = str(exc)

    if teardown_error is None:
        try:
            claim_lease.assert_owned()
            po._remove_playground_artifacts(disk_path)
            claim_lease.assert_owned()
        except LostResourceCustody:
            claim_lease.stop()
            raise
        except (OSError, RuntimeError) as exc:
            claim_lease.assert_owned()
            logger.warning("destroy_vm: artifact cleanup failed for %s: %s", disk_path, exc)
            teardown_error = str(exc)

    try:
        expected_updated_at = claim_lease.publication_token()
        with po.session_scope() as s:
            if teardown_error is None:
                if not pr.finish_vm_destroy(
                    s,
                    vm_id,
                    "destroyed",
                    expected_updated_at=expected_updated_at,
                ):
                    raise RuntimeError(f"VM {vm_id} lost destroy custody before completion")
                pr.delete_nics_for_vm(s, vm_id)
            elif not pr.finish_vm_destroy(
                s,
                vm_id,
                "teardown_failed",
                expected_updated_at=expected_updated_at,
                error=teardown_error,
            ):
                raise LostResourceCustody(f"VM {vm_id} lost destroy custody on failure")
    finally:
        claim_lease.stop()
    if teardown_error is not None:
        raise RuntimeError(f"VM {vm_id} teardown incomplete: {teardown_error}")
    return vm_id
