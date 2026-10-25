"""Measured, non-mutating launch preflight for portable lab packages.

Preflight deliberately does not create a Playground session or substitute a
network. It answers whether the exact definition can be launched on this host;
the launch service repeats the same check immediately before reserving resources.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import hmac
import json
import os
import platform
import secrets
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ..core.config import settings
from .models import LabDefinition, LabManifest

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

IssueSeverity = Literal["error", "warning"]
_CONSENT_TTL_SECONDS = 300
# A process restart intentionally invalidates short-lived acknowledgements.
_CONSENT_KEY = secrets.token_bytes(32)


@dataclass(frozen=True, slots=True)
class HostFacts:
    """Measured host facts; callers may provide deterministic values in tests."""

    cpu_threads: int
    memory_mb: int
    free_disk_bytes: int
    architecture: str
    kvm_available: bool
    capabilities: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class RunnerHostFacts:
    """Runner-owned measurement, never supplied by an HTTP client."""

    host: HostFacts
    disk_path: str
    device: int
    free_bytes: int
    writable: bool
    measured_at: float
    error: str | None = None
    libvirt_available: bool = False

    def current_for(self, path: Path) -> bool:
        return self.error is None and self.disk_path == str(path) and 0 <= time.time() - self.measured_at <= 30


@dataclass(frozen=True, slots=True)
class PreflightIssue:
    severity: IssueSeverity
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class PackagePreflight:
    host: HostFacts
    required_disk_bytes: int
    issues: tuple[PreflightIssue, ...]
    retained_disk_bytes: int = 0
    launch_disk_bytes: int = 0
    runner: RunnerHostFacts | None = None

    @property
    def ready(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    @property
    def warnings(self) -> tuple[PreflightIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "warning")

    @property
    def errors(self) -> tuple[PreflightIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "error")


class DiskReservationError(RuntimeError):
    """Raised when launch storage cannot be reserved safely."""


@dataclass(frozen=True, slots=True)
class DiskReservation:
    """Caller-owned files that reserve launch capacity until handoff or release.

    The future instance coordinator records the method in its action journal
    before handing these exact inodes to overlays. ``journaled_write`` is a
    deliberately weaker fallback for filesystems without ``posix_fallocate``.
    """

    paths: tuple[Path, ...]
    allocation_method: Literal["posix_fallocate", "journaled_write"]

    def release(self) -> None:
        for path in self.paths:
            try:
                inspected = path.lstat()
                if (
                    path.name.startswith(".astinuslab-reservation-")
                    and stat.S_ISREG(inspected.st_mode)
                    and inspected.st_nlink == 1
                ):
                    path.unlink()
            except OSError:
                continue


def reserve_disk_space(requirements: tuple[tuple[Path, int], ...], *, owner_id: str) -> DiskReservation:
    """Reserve bytes in journal-owned files after a launch intent is durable.

    This primitive has no database side effects. The coordinator must call it
    only after it has committed the matching intent/outbox row and must retain
    ``allocation_method`` in that action record for restart reconciliation.
    """

    if (
        not requirements
        or not owner_id
        or any(not isinstance(size, int) or isinstance(size, bool) or size < 1 for _, size in requirements)
    ):
        raise ValueError("invalid disk reservation requirements")
    safe_owner = "".join(character for character in owner_id if character.isalnum() or character in "-_")
    if not safe_owner:
        raise ValueError("invalid disk reservation owner")
    paths: list[Path] = []
    fallback_used = False
    try:
        for directory, size in requirements:
            directory = Path(directory)
            descriptor, temporary = tempfile.mkstemp(prefix=f".astinuslab-reservation-{safe_owner}-", dir=directory)
            path = Path(temporary)
            try:
                if hasattr(os, "posix_fallocate"):
                    try:
                        os.posix_fallocate(descriptor, 0, size)
                    except OSError as exc:
                        if exc.errno not in {errno.EOPNOTSUPP, errno.ENOSYS, errno.EINVAL}:
                            raise
                        fallback_used = True
                        os.lseek(descriptor, size - 1, os.SEEK_SET)
                        os.write(descriptor, b"\0")
                else:
                    fallback_used = True
                    os.lseek(descriptor, size - 1, os.SEEK_SET)
                    os.write(descriptor, b"\0")
                os.fsync(descriptor)
            except OSError:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            finally:
                os.close(descriptor)
            paths.append(path)
    except OSError as exc:
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise DiskReservationError("could not reserve launch storage") from exc
    return DiskReservation(tuple(paths), "journaled_write" if fallback_used else "posix_fallocate")


def measure_host(path: Path) -> HostFacts:
    """Measure local host state needed to decide whether launch may proceed."""

    try:
        free_disk_bytes = shutil.disk_usage(path).free
    except OSError:
        free_disk_bytes = 0
    architecture = platform.machine().lower()
    kvm_available = _kvm_openable()
    capabilities = {"offline"}
    if kvm_available:
        capabilities.add("kvm")
    if architecture in {"x86_64", "amd64"}:
        capabilities.add("x86_64")
    return HostFacts(
        os.cpu_count() or 1,
        _available_memory_mb(),
        free_disk_bytes,
        architecture,
        kvm_available,
        frozenset(capabilities),
    )


def issue_warning_consent_token(
    *,
    package_digest: str,
    warnings: tuple[PreflightIssue, ...],
    policy_revision: str,
    requested_capabilities: tuple[str, ...],
    nonce: str | None = None,
    now: int | None = None,
) -> str:
    """Issue short-lived acknowledgement bound to complete warning values."""

    issued = int(time.time()) if now is None else now
    payload = {
        "package_digest": package_digest,
        "warnings": [_issue_payload(issue) for issue in warnings],
        "policy_revision": policy_revision,
        "requested_capabilities": sorted(set(requested_capabilities)),
        "nonce": nonce or secrets.token_urlsafe(16),
        "expires_at": issued + _CONSENT_TTL_SECONDS,
    }
    encoded = _canonical_token_bytes(payload)
    signature = hmac.new(_CONSENT_KEY, encoded, hashlib.sha256).digest()
    return f"{_token_b64(encoded)}.{_token_b64(signature)}"


def validate_warning_consent_token(
    token: str | None,
    *,
    package_digest: str,
    warnings: tuple[PreflightIssue, ...],
    policy_revision: str,
    requested_capabilities: tuple[str, ...],
    now: int | None = None,
) -> bool:
    """Return whether a consent token still binds this exact launch decision."""

    if not token or not isinstance(token, str):
        return False
    try:
        encoded_b64, signature_b64 = token.split(".", 1)
        encoded = _token_b64_decode(encoded_b64)
        signature = _token_b64_decode(signature_b64)
        expected_signature = hmac.new(_CONSENT_KEY, encoded, hashlib.sha256).digest()
        if not hmac.compare_digest(expected_signature, signature):
            return False
        payload = json.loads(encoded)
    except (ValueError, TypeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or not isinstance(payload.get("expires_at"), int):
        return False
    expected = {
        "package_digest": package_digest,
        "warnings": [_issue_payload(issue) for issue in warnings],
        "policy_revision": policy_revision,
        "requested_capabilities": sorted(set(requested_capabilities)),
    }
    current = int(time.time()) if now is None else now
    return payload["expires_at"] >= current and all(payload.get(key) == value for key, value in expected.items())


def policy_revision() -> str:
    """Stable fingerprint of local policy that can change a launch decision."""

    policy = {
        "guest_egress": settings.packages_allow_guest_egress,
        "source_network": settings.packages_allow_source_network,
        "libvirt_uri": settings.libvirt_uri,
        "max_concurrent_vms": settings.max_concurrent_vms,
    }
    return hashlib.sha256(_canonical_token_bytes(policy)).hexdigest()


def preflight_package(
    definition: LabDefinition,
    manifest: LabManifest,
    *,
    host: HostFacts | None = None,
    storage_path: Path | None = None,
    content_store_path: Path | None = None,
    playground_disk_path: Path | None = None,
    available_capabilities: frozenset[str] | None = None,
    available_assets: frozenset[str] | None = None,
    session: Session | None = None,
    package_digest: str | None = None,
    signature_status: str | None = None,
    retained_present: bool = False,
    runner: RunnerHostFacts | None = None,
) -> PackagePreflight:
    """Return exact blocking errors and acknowledgement warnings before launch."""

    del package_digest  # Kept in the call shape for a single launch-plan contract.
    if definition.package_id != manifest.package_id or definition.version != manifest.version:
        raise ValueError("manifest and definition identity mismatch")
    facts = runner.host if runner is not None else host or measure_host(storage_path or Path.cwd())
    capabilities = available_capabilities if available_capabilities is not None else facts.capabilities
    retained_bytes = sum(payload.size_bytes for payload in manifest.payloads)
    overlay_bytes = sum(vm.disk_gb for vm in definition.vms) * 1024**3
    missing_layers: dict[str, int] = {}
    issues: list[PreflightIssue] = []
    if runner is not None and (playground_disk_path is None or not runner.current_for(playground_disk_path)):
        issues.append(
            PreflightIssue("error", "runner-host-unavailable", "Current runner-owned host measurements are unavailable")
        )
    for vm in definition.vms:
        clean_layer = vm.effective_boot_layer
        if clean_layer is not None and available_assets is not None and clean_layer.sha256 not in available_assets:
            missing_layers[clean_layer.sha256] = max(missing_layers.get(clean_layer.sha256, 0), clean_layer.size_bytes)
            issues.append(
                PreflightIssue(
                    "error",
                    "missing-clean-layer",
                    f"VM {vm.id} clean-start layer is not available in the local content cache",
                )
            )
    launch_bytes = overlay_bytes + sum(missing_layers.values())
    required_disk = retained_bytes + launch_bytes
    _check_resources(
        definition,
        facts,
        retained_bytes,
        launch_bytes,
        issues,
        retained_present=retained_present,
        check_global_disk=storage_path is None and content_store_path is None and playground_disk_path is None,
    )
    _check_host_and_capabilities(manifest, facts, capabilities, issues)
    # Direct callers without a database session retain the deterministic
    # host-only contract used by tooling. The API/launch path always supplies
    # a session and therefore performs the full runtime validation below.
    if session is not None:
        _check_definition_runtime(definition, session, issues, runner)
    _check_storage(
        facts,
        retained_bytes,
        overlay_bytes,
        sum(missing_layers.values()),
        storage_path,
        content_store_path,
        playground_disk_path,
        issues,
        retained_present=retained_present,
        runner=runner,
    )
    _add_warnings(definition, signature_status, issues)
    return PackagePreflight(facts, required_disk, tuple(issues), retained_bytes, launch_bytes, runner)


def _check_resources(
    definition: LabDefinition,
    facts: HostFacts,
    retained: int,
    launch: int,
    issues: list[PreflightIssue],
    *,
    retained_present: bool,
    check_global_disk: bool,
) -> None:
    minimum = definition.resources.minimum
    recommended = definition.resources.recommended
    if facts.cpu_threads < minimum.cpu_threads:
        issues.append(
            PreflightIssue("error", "insufficient-cpu", f"Package requires {minimum.cpu_threads} CPU threads")
        )
    elif facts.cpu_threads < recommended.cpu_threads:
        issues.append(
            PreflightIssue(
                "warning", "below-recommended-cpu", f"Package recommends {recommended.cpu_threads} CPU threads"
            )
        )
    if facts.memory_mb < minimum.memory_mb:
        issues.append(PreflightIssue("error", "insufficient-memory", f"Package requires {minimum.memory_mb} MiB RAM"))
    elif facts.memory_mb < recommended.memory_mb:
        issues.append(
            PreflightIssue("warning", "below-recommended-memory", f"Package recommends {recommended.memory_mb} MiB RAM")
        )
    if check_global_disk:
        additional = launch if retained_present else retained + launch
        required = max(minimum.free_disk_bytes, additional)
        recommended_required = max(recommended.free_disk_bytes, additional)
        if facts.free_disk_bytes < required:
            issues.append(PreflightIssue("error", "insufficient-disk", f"Package needs {required} bytes of free disk"))
        elif facts.free_disk_bytes < recommended_required:
            issues.append(
                PreflightIssue(
                    "warning", "below-recommended-disk", f"Package recommends {recommended_required} bytes of free disk"
                )
            )


def _check_host_and_capabilities(
    manifest: LabManifest, facts: HostFacts, capabilities: frozenset[str], issues: list[PreflightIssue]
) -> None:
    if not facts.kvm_available:
        issues.append(PreflightIssue("error", "kvm-unavailable", "KVM is not available and openable at /dev/kvm"))
    if facts.architecture not in {"x86_64", "amd64"}:
        issues.append(PreflightIssue("error", "unsupported-host-architecture", "V1 packages require an x86_64 host"))
    missing = sorted(set(manifest.required_capabilities) - set(capabilities))
    if missing:
        issues.append(
            PreflightIssue("error", "unsupported-capability", f"Host is missing required capability: {missing[0]}")
        )


def _check_definition_runtime(
    definition: LabDefinition,
    session: Session | None,
    issues: list[PreflightIssue],
    runner: RunnerHostFacts | None = None,
) -> None:
    for network in definition.networks:
        if network.host_bridge is not None:
            issues.append(
                PreflightIssue(
                    "error", "unsupported-bridge", f"Network {network.id} requests an unsupported host bridge"
                )
            )
        if network.guest_egress:
            if not settings.packages_allow_guest_egress:
                issues.append(
                    PreflightIssue("error", "guest-egress-policy-denied", "Local policy disallows guest egress")
                )
            issues.append(PreflightIssue("warning", "guest-egress", f"Network {network.id} permits guest egress"))
    if definition.reproducibility == "unlocked":
        _check_unlocked_catalog(definition, issues)
    else:
        _check_locked_contract(definition, issues)
    if runner is None:
        _check_libvirt_reachability(issues)
    elif not runner.libvirt_available:
        issues.append(PreflightIssue("error", "libvirt-unreachable", "Runner could not connect to configured libvirt"))
    if session is not None:
        _check_database_conflicts(definition, session, issues)


def _check_unlocked_catalog(definition: LabDefinition, issues: list[PreflightIssue]) -> None:
    from ..playground.orchestrator import lookup_playground_image
    from ..playground.workloads import validate_playground_workload_selection

    for vm in definition.vms:
        image = lookup_playground_image(vm.image_id, role=vm.role)
        if image is None:
            issues.append(
                PreflightIssue("error", "image-unavailable", f"VM {vm.id} image is unavailable in this host catalog")
            )
            continue
        validation = validate_playground_workload_selection(role=vm.role, workload_id=vm.workload_id, image=image)
        if not validation.ok:
            reason = validation.detail.strip() or "workload is not compatible"
            issues.append(PreflightIssue("error", "workload-incompatible", f"VM {vm.id}: {reason}"))


def _check_locked_contract(definition: LabDefinition, issues: list[PreflightIssue]) -> None:
    for vm in definition.vms:
        if vm.base_image is None or vm.effective_boot_layer is None or vm.workload is None:
            issues.append(
                PreflightIssue("error", "locked-contract-incomplete", f"VM {vm.id} lacks immutable launch metadata")
            )
            continue
        if vm.base_image.architecture != vm.architecture:
            issues.append(
                PreflightIssue(
                    "error", "image-architecture-mismatch", f"VM {vm.id} base image architecture is incompatible"
                )
            )
        if vm.base_image.firmware != vm.firmware:
            issues.append(
                PreflightIssue("error", "firmware-mismatch", f"VM {vm.id} base image firmware is incompatible")
            )
        if vm.firmware != "bios":
            issues.append(
                PreflightIssue(
                    "error", "unsupported-firmware", f"VM {vm.id} requires unsupported firmware: {vm.firmware}"
                )
            )
        if vm.base_image.image_format is not None and vm.base_image.image_format not in {"qcow2", "raw"}:
            issues.append(
                PreflightIssue("error", "unsupported-image-format", f"VM {vm.id} base image format is unsupported")
            )
        if (
            "qcow2" not in vm.effective_boot_layer.media_type
            and vm.effective_boot_layer.media_type != "application/octet-stream"
        ):
            issues.append(
                PreflightIssue(
                    "error", "unsupported-boot-layer-format", f"VM {vm.id} boot layer is not a supported disk image"
                )
            )
        if not vm.workload.workload_baked:
            issues.append(
                PreflightIssue(
                    "error", "workload-not-baked", f"VM {vm.id} workload is not immutable for a locked launch"
                )
            )
        for probe in vm.workload.readiness_probes:
            if probe.kind == "command":
                issues.append(
                    PreflightIssue(
                        "error",
                        "unsupported-readiness-probe",
                        f"VM {vm.id} requires an unsupported command readiness probe",
                    )
                )


def _check_libvirt_reachability(issues: list[PreflightIssue]) -> None:
    try:
        from ..playground import orchestrator

        if orchestrator.libvirt is None:
            issues.append(PreflightIssue("error", "libvirt-unavailable", "libvirt is unavailable on this host"))
            return
        connection = orchestrator.libvirt.open(settings.libvirt_uri)
        if connection is None:
            issues.append(PreflightIssue("error", "libvirt-unreachable", "Could not connect to configured libvirt"))
            return
        try:
            connection.getCapabilities()
        finally:
            connection.close()
    except Exception:
        issues.append(PreflightIssue("error", "libvirt-unreachable", "Could not connect to configured libvirt"))


def _check_database_conflicts(definition: LabDefinition, session: Session, issues: list[PreflightIssue]) -> None:
    from ..db.models import JobNetwork, JobVM
    from ..db.repositories import has_active_non_playground_job, has_active_playground_session

    if has_active_non_playground_job(session):
        issues.append(
            PreflightIssue("error", "session-occupied", "Active forensic Jobs or their resources require cleanup first")
        )
    if has_active_playground_session(session):
        issues.append(
            PreflightIssue("error", "session-occupied", "Another Playground is active, resetting, or awaiting cleanup")
        )
    live_networks = session.query(JobNetwork).filter(JobNetwork.state != "destroyed").all()
    for network in definition.networks:
        conflict = next((row for row in live_networks if _cidrs_overlap(network.subnet_cidr, row.subnet_cidr)), None)
        if conflict is not None:
            issues.append(
                PreflightIssue(
                    "error",
                    "cidr-conflict",
                    f"Requested CIDR {network.subnet_cidr} conflicts with {conflict.subnet_cidr}",
                )
            )
    existing_labels = {row.label for row in session.query(JobVM).filter(JobVM.state != "destroyed").all()}
    for vm in definition.vms:
        if vm.id in existing_labels:
            issues.append(PreflightIssue("error", "vm-name-conflict", f"VM label {vm.id} is already in use"))
    if settings.max_concurrent_vms is not None:
        live_count = session.query(JobVM).filter(JobVM.state != "destroyed").count()
        if live_count + len(definition.vms) > settings.max_concurrent_vms:
            issues.append(PreflightIssue("error", "vm-capacity-exhausted", "Host VM capacity limit would be exceeded"))


def _check_storage(
    facts: HostFacts,
    retained: int,
    overlay: int,
    missing_layers: int,
    storage_path: Path | None,
    content_store_path: Path | None,
    playground_disk_path: Path | None,
    issues: list[PreflightIssue],
    *,
    retained_present: bool,
    runner: RunnerHostFacts | None = None,
) -> None:
    del facts
    if storage_path is None and content_store_path is None and playground_disk_path is None:
        return
    retained_needed = 0 if retained_present else retained
    # Missing immutable layers are acquired into the content store; writable
    # overlays belong on the Playground disk filesystem. If a caller does not
    # expose a content-store path, charge the combined demand to the launch
    # path rather than silently omitting it.
    if content_store_path is None:
        overlay += missing_layers
        missing_layers = 0
    entries = (
        (Path(storage_path) if storage_path else None, retained_needed, False),
        (Path(content_store_path) if content_store_path else None, missing_layers, False),
        (Path(playground_disk_path) if playground_disk_path else None, overlay, True),
    )
    by_device: dict[int, tuple[int, int, Path]] = {}
    for path, bytes_needed, runner_owned in entries:
        if path is None:
            continue
        if runner is not None and runner_owned:
            if not runner.current_for(path):
                continue  # The caller already records an unavailable runner error.
            device, free, writable = runner.device, runner.free_bytes, runner.writable
        else:
            try:
                inspected = path.stat()
                device = inspected.st_dev
                free = shutil.disk_usage(path).free
            except OSError:
                issues.append(
                    PreflightIssue("error", "storage-unavailable", f"Storage directory is unavailable: {path}")
                )
                continue
            writable = _directory_writable(path)
        if not writable:
            issues.append(PreflightIssue("error", "storage-not-writable", f"Storage directory is not writable: {path}"))
        old = by_device.get(device)
        by_device[device] = (min(free, old[0]) if old else free, bytes_needed + (old[1] if old else 0), path)
    for free, bytes_needed, path in by_device.values():
        if free < bytes_needed:
            issues.append(
                PreflightIssue(
                    "error", "insufficient-disk", f"Filesystem for {path} needs {bytes_needed} bytes of free disk"
                )
            )


def _add_warnings(definition: LabDefinition, signature_status: str | None, issues: list[PreflightIssue]) -> None:
    if definition.reproducibility == "unlocked":
        issues.append(
            PreflightIssue("warning", "unlocked-package", "This package is topology/runbook-only, not reproducible")
        )
    if signature_status not in {None, "valid-locally-trusted-key"}:
        issues.append(
            PreflightIssue("warning", "unsigned-or-untrusted", "Package signature is unsigned or not locally trusted")
        )
    if any(
        (layer := vm.effective_boot_layer) is not None and not layer.redistribution_confirmed for vm in definition.vms
    ):
        issues.append(
            PreflightIssue(
                "warning", "unconfirmed-redistribution", "One or more assets lack redistribution confirmation"
            )
        )


def _directory_writable(path: Path) -> bool:
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".astinuslab-preflight-", dir=path)
        os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _kvm_openable() -> bool:
    try:
        descriptor = os.open("/dev/kvm", os.O_RDWR | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return False
    os.close(descriptor)
    return True


def _cidrs_overlap(left: str, right: str) -> bool:
    import ipaddress

    return ipaddress.ip_network(left, strict=False).overlaps(ipaddress.ip_network(right, strict=False))


def _issue_payload(issue: PreflightIssue) -> dict[str, str]:
    return {"severity": issue.severity, "code": issue.code, "message": issue.message}


def _canonical_token_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _token_b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _token_b64_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _available_memory_mb() -> int:
    try:
        with Path("/proc/meminfo").open("r", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return max(0, int(line.split()[1]) // 1024)
    except (OSError, ValueError, IndexError):
        pass
    return 0
