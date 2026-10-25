from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import logging
import re
import secrets
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from .. import catalog
from ..core import fake_mode
from ..core.compatibility import compatibility_fingerprint

# `settings`, `session_scope` and `libvirt` are re-exported deliberately, with
# the redundant-looking `as` form that marks them explicit under
# `--no-implicit-reexport`. They are the module attributes every caller and test
# reaches through this module -- `orchestrator.settings` is what a test patches,
# and `acquisition.py` and the route modules read them back the same way -- so
# they are part of this module's surface, not incidental imports.
from ..core.config import settings as settings
from ..core.locks import CaptureLockBusy, capture_lock
from ..db import session_scope as session_scope
from ..db.transactions import reserve_sqlite_write
from ..jobs.celery_app import celery_app
from ..virtualization import vm_lifecycle as vmlc
from ..virtualization.host_helpers import (
    fix_libvirt_path_acl_helper,
)
from ..workloads import workload_cache_support
from . import repositories as pr
from .resources import VMResourceTuple, validate_vm_resources

try:
    import libvirt as libvirt
except ImportError:
    libvirt = None

logger = logging.getLogger(__name__)

_SAFE_PLAYGROUND_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


@celery_app.task(name="playground.create_session")
def create_session(*, job_id: str) -> str:
    with session_scope() as s:
        reserve_sqlite_write(s)
        if pr.active_playground_session(s) is not None:
            raise RuntimeError("a playground session is already active")
        from ..db.repositories import has_active_non_playground_job

        if has_active_non_playground_job(s):
            raise RuntimeError("a forensic job is currently active")
        pr.create_playground_session(s, job_id=job_id)
    logger.info("playground session %s created", job_id)
    return job_id


@celery_app.task(name="playground.destroy_session")
def destroy_session(*, job_id: str) -> str:
    from ..orchestration.lab_lifecycle import cleanup_terminated_job

    cleanup_terminated_job(job_id=job_id)
    return job_id


def libvirt_net_name(job_id: str, name: str) -> str:
    digest = hashlib.sha256(f"{job_id}/{name}".encode()).hexdigest()[:4]
    return f"astinuslab-pg-{job_id}-{name}-{digest}"


def _vm_lifecycle() -> vmlc.VMLifecycle:
    if fake_mode.FAKE_PLAYGROUND:
        # _FakePlaygroundLifecycle is a duck-typed stand-in used only on the
        # ASTINUSLAB_FAKE_PLAYGROUND path; it satisfies the VMLifecycle interface
        # at runtime but is not a subclass, so we cast to satisfy mypy.
        return cast(vmlc.VMLifecycle, _FakePlaygroundLifecycle())

    import libvirt

    def _run_command(cmd, **kwargs):
        kwargs.pop("job_id", None)
        completed = subprocess.run(cmd, check=True, **kwargs)
        completed.check_returncode()
        return completed

    return vmlc.VMLifecycle(
        run_command=_run_command,
        libvirt_module=libvirt,
        prepare_overlay_access=lambda overlay_path: _apply_capture_acl(
            fix_libvirt_path_acl_helper(), overlay_path.parent
        ),
    )


class _FakePlaygroundLifecycle:
    def boot_playground(
        self,
        *,
        domain_name: str,
        memory_mb: int,
        vcpus: int,
        base_image_path: str | Path,
        disk_path: Path,
        disk_gb: int | None,
        networks: list[dict],
        libvirt_uri: str,
        machine_type: str | None = None,
        cwd: Path | None = None,
        job_id: str | None = None,
        seed_iso_path: Path | None = None,
        user_data_path: Path | None = None,
        meta_data_path: Path | None = None,
        network_config_path: Path | None = None,
    ) -> vmlc.VMBootResult:
        disk_path.parent.mkdir(parents=True, exist_ok=True)
        disk_path.write_bytes(b"astinuslab fake playground disk\n")
        return vmlc.VMBootResult(
            vm_name=domain_name,
            guest_ip="",
            disk_path=disk_path,
            seed_iso_path=Path(),
            share_dir=Path(),
        )

    def destroy(self, conn, vm_name: str) -> None:
        return None

    def get_ip(
        self,
        conn,
        network_name: str,
        guest_mac: str,
        *,
        domain_name: str | None = None,
        timeout_seconds: float = 180.0,
        cancel_requested=None,
    ) -> str:
        del domain_name
        # Derive the lease from the requested network's subnet so NIC<->subnet
        # wiring regressions can fail in the hermetic e2e tier; keep the
        # historical constant when no network row matches.
        from ..db.models import JobNetwork

        subnet = None
        try:
            with session_scope() as s:
                net = s.query(JobNetwork).filter_by(libvirt_net_name=network_name).first()
                subnet = net.subnet_cidr if net else None
        except Exception:
            subnet = None
        if not subnet:
            return "10.42.1.10"
        try:
            return str(next(ipaddress.ip_network(subnet, strict=False).hosts()) + 9)
        except (StopIteration, ValueError):
            return "10.42.1.10"

    def attach_nic(self, conn, domain_name: str, network_name: str, mac: str) -> None:
        return None

    def detach_nic(self, conn, domain_name: str, mac: str) -> None:
        return None


def lookup_playground_image(image_id: str, *, role: str = "target") -> dict | None:
    raw_by_id = {}
    if role in {"target", "vm"}:
        raw_by_id = {entry["id"]: entry for entry in catalog.list_playground_usable_images()}
    for entry in catalog.list_playground_images_for_role(role):
        if entry["id"] == image_id:
            raw_entry = raw_by_id.get(image_id, {})
            return {**entry, "cloud_init": bool(raw_entry.get("cloud_init", entry.get("cloud_init", False)))}
    return None


def _playground_guest_user(image: dict) -> str:
    """SSH login for a playground guest."""
    return image.get("username") or image.get("default_username") or "ubuntu"


def _default_resources_for_role(role: str, image: dict) -> tuple[object, object, object]:
    defaults = image.get("defaults") or {}
    if role == "network":
        return (
            defaults.get("vcpus", 1),
            defaults.get("memory_mb", 512),
            defaults.get("disk_gb", 4),
        )
    return (
        settings.playground_default_vcpus,
        settings.playground_default_memory_mb,
        image.get("disk_gb"),
    )


def resolve_vm_resources(
    role: str,
    image: dict,
    *,
    vcpus: int | None = None,
    memory_mb: int | None = None,
    disk_gb: int | None = None,
) -> VMResourceTuple:
    default_vcpus, default_memory_mb, default_disk_gb = _default_resources_for_role(role, image)
    return validate_vm_resources(
        default_vcpus if vcpus is None else vcpus,
        default_memory_mb if memory_mb is None else memory_mb,
        default_disk_gb if disk_gb is None else disk_gb,
    )


def generate_mac() -> str:
    suffix = secrets.token_hex(4)
    return f"52:54:{suffix[0:2]}:{suffix[2:4]}:{suffix[4:6]}:{suffix[6:8]}"


def _validate_playground_name(kind: str, value: str) -> None:
    if not _SAFE_PLAYGROUND_NAME.fullmatch(value):
        raise RuntimeError(f"invalid playground {kind}: {value!r}")


def _apply_capture_acl(acl_helper: Path, target_dir: Path) -> None:
    if fake_mode.FAKE_LIBVIRT:
        return
    subprocess.run(["sudo", "-n", str(acl_helper), str(target_dir)], check=True)


def _apply_workload_cache_acl(cache_path: Path) -> None:
    # The cached qcow2 is used as a libvirt backing file. Grant qemu traverse/read
    # access to the cache directory before creating the per-VM overlay.
    _apply_capture_acl(fix_libvirt_path_acl_helper(), cache_path.parent)


def _spawn_artifact_paths(disk_path: Path) -> tuple[Path, ...]:
    return (
        disk_path,
        disk_path.with_suffix(".iso"),
        disk_path.with_name(f"{disk_path.stem}-seed.iso"),
    )


# The no-follow filesystem primitives are `filesystem_safety.py` now. These three
# names stay reachable on this module because that is where their callers and the
# suite reach them: `reconciliation_phases` imports the two artifact entry points
# from here, `acquisition` and `vm_lifecycle` call `po._create_directory_no_follow`,
# and the tests patch all three here to intercept the calls those modules make. A
# `from .filesystem_safety import ...` would both break that seam and be circular,
# because the sibling imports this module to read `settings` at call time.


def _playground_artifacts_present(disk_path: Path) -> bool:
    from . import filesystem_safety as fs

    return fs._playground_artifacts_present(disk_path)


def _remove_playground_artifacts(disk_path: Path) -> None:
    from . import filesystem_safety as fs

    fs._remove_playground_artifacts(disk_path)


def _create_directory_no_follow(root: Path, *components: str, final_mode: int | None = None) -> None:
    from . import filesystem_safety as fs

    fs._create_directory_no_follow(root, *components, final_mode=final_mode)


def _cleanup_spawn_artifacts(
    *,
    lifecycle: vmlc.VMLifecycle | None,
    domain_name: str,
    disk_path: Path,
    domain_may_exist: bool = True,
) -> str | None:
    errors: list[str] = []
    domain_destroyed = False
    if lifecycle is None:
        if domain_may_exist:
            errors.append("libvirt cleanup not confirmed: lifecycle unavailable")
        else:
            domain_destroyed = True
    else:
        try:
            lifecycle.destroy(settings.libvirt_uri, domain_name)
            domain_destroyed = True
        except Exception as cleanup_exc:
            logger.warning("spawn_vm: cleanup failed for %s: %s", domain_name, cleanup_exc)
            errors.append(f"libvirt cleanup failed: {cleanup_exc}")

    # A domain that may still be active keeps custody of its overlay. Deleting
    # the disk first can corrupt a running VM and makes the teardown unrecoverable.
    if not domain_destroyed:
        return "; ".join(errors)

    try:
        _remove_playground_artifacts(disk_path)
    except (OSError, RuntimeError) as cleanup_exc:
        logger.warning("spawn_vm: artifact cleanup failed for %s: %s", disk_path, cleanup_exc)
        errors.append(f"artifact cleanup failed: {cleanup_exc}")
    return "; ".join(errors) if errors else None


def _pending_recipe_reservation(row, job_id: str, token: str) -> datetime:
    try:
        expected = datetime.fromisoformat(token).replace(tzinfo=None)
    except ValueError as exc:
        raise RuntimeError("invalid recipe import reservation token") from exc
    if (
        row is None
        or row.session_id != job_id
        or row.state != "pending"
        or row.updated_at.replace(tzinfo=None) != expected
    ):
        raise RuntimeError("recipe import reservation is no longer pending")
    return expected


@celery_app.task(name="playground.create_network")
def create_network(
    *, job_id: str, network_id: str, name: str, subnet_cidr: str, isolated: bool, reservation_token: str | None = None
) -> str:
    from . import network_lifecycle as nl

    return nl.create_network(
        job_id=job_id,
        network_id=network_id,
        name=name,
        subnet_cidr=subnet_cidr,
        isolated=isolated,
        reservation_token=reservation_token,
    )


@celery_app.task(name="playground.destroy_network")
def destroy_network(*, network_id: str) -> str:
    from . import network_lifecycle as nl

    return nl.destroy_network(network_id=network_id)


def _playground_workload_cache_key(target_id: str, workload_id: str, image_path: Path) -> str:
    """Cache key identical to the Jobs path (``orchestration.deps.workload_cache_key``).

    Parity is load-bearing: only when the Playground computes the same key as the
    Jobs bake does a baked entry get reused. Both fold in the target id, workload
    id, install-script fingerprint, and base-image identity.
    """
    workload = catalog.workload_by_id(workload_id)
    fingerprint = compatibility_fingerprint(workload or {"id": workload_id}, base_dir=settings.base_dir)
    return workload_cache_support.workload_cache_key(
        target_id, workload_id, image_path, workload_fingerprint=fingerprint
    )


def _playground_workload_cache_path(target_id: str, workload_id: str, key: str) -> Path:
    return workload_cache_support.workload_cache_path(settings, target_id, workload_id, key)


def _check_playground_workload_cache(target_id: str, workload_id: str, image_path: Path) -> Path | None:
    """Return a prebaked qcow2 for this image+workload, or None on a miss."""
    return workload_cache_support.check_workload_cache(
        logger,
        settings,
        target_id,
        workload_id,
        image_path,
        cache_key_fn=_playground_workload_cache_key,
        cache_path_fn=_playground_workload_cache_path,
    )


@celery_app.task(name="playground.spawn_vm")
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
    from . import vm_lifecycle as vml

    return vml.spawn_vm(
        job_id=job_id,
        vm_id=vm_id,
        label=label,
        image_id=image_id,
        nic_specs=nic_specs,
        role=role,
        workload_id=workload_id,
        vcpus=vcpus,
        memory_mb=memory_mb,
        disk_gb=disk_gb,
        resources_resolved=resources_resolved,
        immutable_backing_path=immutable_backing_path,
        reservation_token=reservation_token,
    )


@celery_app.task(name="playground.provision_vm_workload")
def provision_vm_workload(*, vm_id: str) -> str:
    from . import workload_provisioning as wp

    return wp.provision_vm_workload(vm_id=vm_id)


@celery_app.task(name="playground.destroy_vm")
def destroy_vm(*, vm_id: str) -> str:
    from . import vm_lifecycle as vml

    return vml.destroy_vm(vm_id=vm_id)


@celery_app.task(name="playground.capture_memory")
def capture_memory(*, vm_id: str, acquisition_id: str | None = None) -> str:
    from . import acquisition as acq

    with session_scope() as s:
        from ..db.models import JobVM

        vm = s.query(JobVM).filter_by(id=vm_id).one()
        job_id = vm.session_id
        vm_label = vm.label
        domain_name = vm.domain_name
        if vm.state != "running":
            if acquisition_id:
                acq._upsert_playground_acquisition(
                    acquisition_id=acquisition_id,
                    vm_id=vm_id,
                    name=f"playground-{vm_id}-memory-failed",
                    status="failed",
                    kind="memory",
                    session_id=job_id,
                    vm_label=vm_label,
                )
            raise RuntimeError(f"VM {vm_id} not running (state={vm.state})")

    try:
        with capture_lock(vm_id, base_dir=acq._playground_capture_lock_dir()):
            return acq._capture_memory_locked(
                vm_id=vm_id,
                acquisition_id=acquisition_id,
                job_id=job_id,
                vm_label=vm_label,
                domain_name=domain_name,
            )
    except CaptureLockBusy:
        return acq._mark_capture_already_running(
            acquisition_id=acquisition_id,
            vm_id=vm_id,
            job_id=job_id,
            vm_label=vm_label,
            kind="memory",
        )


def _domain_xml(domain_name: str) -> str | None:
    if libvirt is None:
        return None
    conn = libvirt.open(settings.libvirt_uri)
    if conn is None:
        return None
    try:
        return conn.lookupByName(domain_name).XMLDesc()
    except Exception as exc:
        logger.warning("capture manifest: domain XML unavailable for %s: %s", domain_name, exc)
        return None
    finally:
        close = getattr(conn, "close", None)
        if callable(close):
            close()


@celery_app.task(name="playground.capture_disk")
def capture_disk(*, vm_id: str, acquisition_id: str | None = None) -> str:
    from . import acquisition as acq

    with session_scope() as s:
        from ..db.models import JobVM

        vm = s.query(JobVM).filter_by(id=vm_id).one()
        job_id = vm.session_id
        vm_label = vm.label
        domain_name = vm.domain_name
        disk_path = vm.disk_path
        if vm.state != "running":
            if acquisition_id:
                acq._upsert_playground_acquisition(
                    acquisition_id=acquisition_id,
                    vm_id=vm_id,
                    name=f"playground-{vm_id}-disk-failed",
                    status="failed",
                    kind="disk",
                    session_id=job_id,
                    vm_label=vm_label,
                )
            raise RuntimeError(f"VM {vm_id} not running (state={vm.state})")

    try:
        with capture_lock(vm_id, base_dir=acq._playground_capture_lock_dir()):
            return acq._capture_disk_locked(
                vm_id=vm_id,
                acquisition_id=acquisition_id,
                job_id=job_id,
                vm_label=vm_label,
                domain_name=domain_name,
                disk_path=disk_path,
            )
    except CaptureLockBusy:
        return acq._mark_capture_already_running(
            acquisition_id=acquisition_id,
            vm_id=vm_id,
            job_id=job_id,
            vm_label=vm_label,
            kind="disk",
        )


@contextlib.contextmanager
def _libvirt_conn():
    conn = None
    if libvirt is not None:
        conn = libvirt.open(settings.libvirt_uri)
    try:
        yield conn
    finally:
        if conn:
            close_fn = getattr(conn, "close", None)
            if callable(close_fn):
                close_fn()


@celery_app.task(name="playground.attach_nic")
def attach_nic(*, job_id: str, vm_id: str, network_id: str, nic_id: str | None = None) -> dict[str, str]:
    from . import nic_lifecycle as nic

    return nic.attach_nic(job_id=job_id, vm_id=vm_id, network_id=network_id, nic_id=nic_id)


@celery_app.task(name="playground.detach_nic")
def detach_nic(*, job_id: str, vm_id: str, nic_id: str, detach_token: str | None = None) -> str:
    from . import nic_lifecycle as nic

    return nic.detach_nic(job_id=job_id, vm_id=vm_id, nic_id=nic_id, detach_token=detach_token)
