from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypedDict

from .windows_bundle import parse_windows_bundle

logger = logging.getLogger(__name__)

SIDECAR_SCHEMA_VERSION = 1
WINDOWS_SIDECAR_SCHEMA_VERSION = 3

Source = Literal["inspected", "filename", "user", "operator_prepared"]

BOOTABLE_IMAGE_EXTENSIONS = {".qcow2", ".img"}
ISF_SUPPORTED_DISTROS = {
    "ubuntu",
    "debian",
    "kali",
    "parrot",
    "fedora",
    "rhel",
    "centos",
    "rocky",
    "almalinux",
    "ol",
    "opensuse",
    "opensuse-leap",
    "opensuse-tumbleweed",
    "sles",
    "suse",
    "arch",
    "archlinux",
    "manjaro",
}


@dataclass
class ImageMetadata:
    schema_version: int
    distro: str
    version: str
    family: str
    default_username: str
    os_variant: str
    machine_type: str | None
    kernel_release: str | None
    arch: str | None
    cloud_init_profile: str | None
    inject_network_config: bool | None
    source: Source
    detected_at: str
    user_edited_fields: list[str] = field(default_factory=list)
    inspect_error: str | None = None
    build_id: str | None = None


@dataclass
class WindowsImageMetadata:
    """Explicit provenance for an operator-prepared Windows base image.

    Windows images do not use the Linux cloud-init/ISF metadata contract.  The
    structured fields below intentionally keep the firmware and guest-access
    invariants visible in the sidecar instead of inferring them from a filename.
    """

    schema_version: int
    platform: Literal["windows"]
    distro: str
    version: str
    family: str
    default_username: str
    os_variant: str
    edition: str
    build: str
    revision: int
    source: Source
    generalization: str
    shutdown_state: str
    virtual_size_bytes: int
    firmware: dict[str, object]
    guest_access: dict[str, object]
    windows_update: dict[str, object]
    sha256: str
    detected_at: str
    first_boot: dict[str, object] | None = None
    machine_type: str | None = None
    kernel_release: str | None = None
    arch: str | None = "x86_64"
    architecture: str | None = None
    cloud_init_profile: str | None = None
    inject_network_config: bool | None = False
    user_edited_fields: list[str] = field(default_factory=list)
    inspect_error: str | None = None

    def __post_init__(self) -> None:
        if self.arch is None and self.architecture is not None:
            self.arch = self.architecture
        elif self.architecture is None and self.arch is not None:
            self.architecture = self.arch


@dataclass
class UnverifiedImageMetadata(ImageMetadata):
    """In-memory display status only; never a sidecar or provenance assertion."""

    platform: str | None = None


ImageMetadataLike = ImageMetadata | WindowsImageMetadata | UnverifiedImageMetadata


class ImagePreflight(TypedDict):
    bootable: bool
    isf_supported: bool
    reusable: bool
    needs_user_action: bool
    reasons: list[str]
    family: str


def _sidecar_path(image: Path) -> Path:
    return image.with_suffix(image.suffix + ".meta.json")


def save_sidecar(image: Path, meta: ImageMetadataLike) -> None:
    if isinstance(meta, UnverifiedImageMetadata):
        raise ValueError("Unverified image status must not be saved as provenance")
    path = _sidecar_path(image)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(meta), indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _invalidate_catalog_caches() -> None:
    try:
        from ..catalog import invalidate_catalog_caches
    except ImportError:
        return
    invalidate_catalog_caches()


def load_sidecar(image: Path) -> ImageMetadataLike | None:
    path = _sidecar_path(image)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("metadata must be an object")
        if raw.get("platform") == "windows":
            # Accept the descriptive JSON spelling used by the provenance
            # contract while emitting the catalog's existing ``arch`` field.
            if "arch" not in raw and "architecture" in raw:
                raw["arch"] = raw["architecture"]
            return WindowsImageMetadata(**raw)
        return ImageMetadata(**raw)
    except (TypeError, ValueError):
        logger.warning("Invalid image sidecar: %s", path)
        return None


def _windows_hint(*values: object) -> bool:
    return any(
        isinstance(value, str) and re.search(r"(?:^|[-_.])(?:windows|winref|win2k)(?:[-_.0-9]|$)", value.lower())
        for value in values
    )


def _unverified_metadata(*, windows: bool, reason: str) -> UnverifiedImageMetadata:
    return UnverifiedImageMetadata(
        schema_version=0,
        platform="windows" if windows else None,
        distro="unknown",
        version="unknown",
        family="windows" if windows else "other",
        default_username="",
        os_variant="generic",
        machine_type=None,
        kernel_release=None,
        arch=None,
        cloud_init_profile=None,
        inject_network_config=False,
        source="filename",
        detected_at=datetime.now(UTC).isoformat(timespec="seconds"),
        inspect_error=reason,
    )


def _protected_metadata(image: Path) -> WindowsImageMetadata | UnverifiedImageMetadata | None:
    """Prevent automatic inspection from replacing Windows or unreadable provenance."""
    path = _sidecar_path(image)
    windows = _windows_hint(image.stem)
    if path.is_symlink():
        return _unverified_metadata(windows=windows, reason="Symlinked provenance requires operator review")
    try:
        existing = load_sidecar(image)
    except OSError:
        return _unverified_metadata(windows=windows, reason="Unreadable provenance requires operator review")
    if isinstance(existing, WindowsImageMetadata):
        return existing
    if existing is not None:
        if _windows_hint(existing.distro, existing.family, existing.os_variant):
            return _unverified_metadata(windows=True, reason="Windows requires explicit schema-v2 operator provenance")
        return None
    if path.exists():
        return _unverified_metadata(
            windows=windows, reason="Invalid provenance requires operator review; original sidecar preserved"
        )
    if windows:
        return _unverified_metadata(windows=True, reason="Windows requires explicit schema-v2 operator provenance")
    return None


def _is_stale_inspector_option_error(meta: ImageMetadataLike) -> bool:
    return meta.source == "filename" and "unrecognized option '--xml'" in (meta.inspect_error or "")


def _is_stale_unknown_filename_fallback(image: Path, meta: ImageMetadataLike) -> bool:
    if meta.source != "filename" or meta.distro != "unknown" or not meta.inspect_error:
        return False
    from ..catalog import _infer_from_filename

    return _infer_from_filename(image.name)["distro"] != "unknown"


def _is_stale_sidecar(image: Path, meta: ImageMetadataLike) -> bool:
    return _is_stale_inspector_option_error(meta) or _is_stale_unknown_filename_fallback(image, meta)


_DISTRO_ALIASES = {
    "oraclelinux": "ol",
    "oracle": "ol",
    "redhat": "rhel",
    "red hat enterprise linux": "rhel",
    "opensuse leap": "opensuse-leap",
    "opensuse tumbleweed": "opensuse-tumbleweed",
    "arch linux": "arch",
}

_REDHAT_FAMILY = {"fedora", "centos", "rhel", "rocky", "almalinux", "redhat", "ol"}

_DISTRO_DEFAULT_USERNAME = {
    "ubuntu": "ubuntu",
    "debian": "debian",
    "fedora": "fedora",
    "centos": "centos",
    "rocky": "rocky",
    "almalinux": "almalinux",
    "rhel": "ec2-user",
    "ol": "opc",
    "kali": "kali",
    "parrot": "user",
    "opensuse": "opensuse",
    "opensuse-leap": "opensuse",
    "opensuse-tumbleweed": "opensuse",
    "sles": "sles",
    "suse": "suse",
    "arch": "arch",
    "archlinux": "arch",
    "manjaro": "manjaro",
}


def _normalize_distro(distro: str) -> str:
    normalized = (distro or "unknown").strip().lower().replace("_", "-")
    return _DISTRO_ALIASES.get(normalized, normalized)


def _family_for(distro: str) -> str:
    distro = _normalize_distro(distro)
    if distro in _REDHAT_FAMILY:
        return "redhat"
    if distro in {"ubuntu", "debian", "kali", "parrot"}:
        return "debian"
    return "other"


def _machine_type_for(distro: str, version: str) -> str | None:
    if distro == "debian" and version not in ("", "unknown"):
        try:
            if int(version.split(".")[0]) <= 12:
                return "pc"
        except (ValueError, AttributeError):
            pass
    return None


def inspect_image(image: Path, timeout: int = 60, binary: str = "virt-inspector") -> ImageMetadataLike:
    """Run virt-inspector against `image` and return parsed metadata.

    Raises FileNotFoundError if the binary is missing, subprocess.TimeoutExpired if it
    does not finish in time, subprocess.CalledProcessError on non-zero exit, ValueError
    if the XML cannot be parsed.
    """
    protected = _protected_metadata(image)
    if protected is not None:
        return protected
    result = subprocess.run(
        [binary, "-a", str(image)],
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    )
    root = ET.fromstring(result.stdout)  # noqa: S314  # parses virt-inspector XML output, not untrusted network data
    os_node = root.find("operatingsystem")
    if os_node is None:
        raise ValueError("virt-inspector returned no operatingsystem element")

    def _text(tag: str) -> str | None:
        node = os_node.find(tag)
        return node.text.strip() if node is not None and node.text else None

    distro = _normalize_distro(_text("distro") or "unknown")
    if _windows_hint(_text("type"), distro, _text("osinfo")):
        return _unverified_metadata(
            windows=True, reason="Inspector detected Windows; operator-prepared schema-v2 provenance is required"
        )
    major = _text("major_version") or ""
    minor = _text("minor_version") or ""
    version = f"{major}.{minor}" if major and minor else (major or "unknown")
    arch = _text("arch")
    osinfo = _text("osinfo") or "generic"

    return ImageMetadata(
        schema_version=SIDECAR_SCHEMA_VERSION,
        distro=distro,
        version=version,
        family=_family_for(distro),
        default_username=_DISTRO_DEFAULT_USERNAME.get(distro, "root"),
        os_variant=osinfo,
        machine_type=_machine_type_for(distro, version),
        kernel_release=None,
        arch=arch,
        cloud_init_profile=None,
        inject_network_config=None,
        source="inspected",
        detected_at=datetime.now(UTC).isoformat(timespec="seconds"),
        user_edited_fields=[],
        inspect_error=None,
    )


_WINDOWS_SIZE_BYTES = 40 * 1024**3
_WINDOWS_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _classify_windows_preflight(image: Path, meta: WindowsImageMetadata) -> ImagePreflight:
    reasons: list[str] = []
    suffix = image.suffix.lower()
    bootable = suffix in BOOTABLE_IMAGE_EXTENSIONS
    if not bootable:
        reasons.append(f"extension {suffix or '(none)'} is not auto-bootable")

    if type(meta.schema_version) is not int or meta.schema_version != WINDOWS_SIDECAR_SCHEMA_VERSION:
        reasons.append("Windows provenance schema version is not 3 (prepared first-boot bundle required)")
    try:
        parse_windows_bundle(meta.first_boot)
    except ValueError as exc:
        reasons.append(str(exc))
    if meta.platform != "windows":
        reasons.append("platform is not windows")
    if meta.distro != "windows-server" or meta.version != "2022":
        reasons.append("image is not Windows Server 2022")
    if meta.family != "windows":
        reasons.append("family is not windows")
    if not isinstance(meta.arch, str) or meta.arch.strip().lower() not in {"x86_64", "amd64"}:
        reasons.append("architecture is not x86_64")
    if not isinstance(meta.architecture, str) or meta.architecture.strip().lower() not in {"x86_64", "amd64"}:
        reasons.append("architecture alias is inconsistent with the x86_64 profile")
    if (
        not isinstance(meta.default_username, str)
        or not meta.default_username.isascii()
        or meta.default_username.lower() != "administrator"
    ):
        reasons.append("default username must be Administrator for QGA key bootstrap")
    if meta.os_variant != "win2k22":
        reasons.append("OS variant must be win2k22")
    if meta.machine_type is not None and meta.machine_type != "q35":
        reasons.append("machine type must agree with q35 firmware")
    if meta.cloud_init_profile is not None or meta.inject_network_config is not False:
        reasons.append("Windows must not request Linux cloud-init or network injection")
    if meta.kernel_release is not None:
        reasons.append("Windows provenance must not carry a Linux kernel release")
    if meta.source != "operator_prepared":
        reasons.append("source is not operator_prepared")
    if meta.generalization != "generalized":
        reasons.append("image is not generalized")
    if meta.shutdown_state != "shut_off":
        reasons.append("image was not cleanly shut off")
    if type(meta.virtual_size_bytes) is not int or meta.virtual_size_bytes != _WINDOWS_SIZE_BYTES:
        reasons.append("virtual size must be exactly 40 GiB")

    firmware = meta.firmware if isinstance(meta.firmware, dict) else {}
    if firmware.get("machine") != "q35":
        reasons.append("firmware machine must be q35")
    if firmware.get("firmware") != "ovmf":
        reasons.append("firmware must be ovmf")
    if firmware.get("secure_boot") is not False:
        reasons.append("Secure Boot must be disabled")
    if firmware.get("tpm") is not False:
        reasons.append("TPM must be disabled")
    if firmware.get("nvram") != "per_instance_copy":
        reasons.append("NVRAM must be per_instance_copy")

    guest_access = meta.guest_access if isinstance(meta.guest_access, dict) else {}
    if guest_access.get("method") != "openssh":
        reasons.append("guest access must use OpenSSH")
    if guest_access.get("bootstrap") != "qga":
        reasons.append("guest bootstrap must use QGA")
    if guest_access.get("key_in_image") is not False:
        reasons.append("image must not contain an authorized key")
    if guest_access.get("host_keys") != "regenerate_per_instance":
        reasons.append("SSH host keys must be regenerated per instance")
    if "password" in guest_access or guest_access.get("password_bootstrap"):
        reasons.append("password bootstrap is not permitted")

    windows_update = meta.windows_update if isinstance(meta.windows_update, dict) else {}
    if windows_update.get("policy") != "optional":
        reasons.append("Windows Update policy must be optional")
    pending_count = windows_update.get("pending_count")
    if type(pending_count) is not int or pending_count < 0:
        reasons.append("Windows Update pending_count must be a non-negative integer")
    if type(meta.revision) is not int or meta.revision < 0:
        reasons.append("revision must be a non-negative integer")
    if any(not isinstance(value, str) or not value.strip() for value in (meta.edition, meta.build)):
        reasons.append("edition and build are required")
    if not isinstance(meta.sha256, str) or not _WINDOWS_SHA256_RE.fullmatch(meta.sha256):
        reasons.append("sha256 must be 64 lowercase hexadecimal characters")

    return {
        "bootable": bootable,
        "isf_supported": False,
        "reusable": bootable and not reasons,
        "needs_user_action": bool(reasons),
        "reasons": reasons,
        "family": "windows",
    }


def classify_image_preflight(image: Path, meta: ImageMetadataLike) -> ImagePreflight:
    """Return conservative automation eligibility for a disk image."""
    if isinstance(meta, UnverifiedImageMetadata):
        return {
            "bootable": image.suffix.lower() in BOOTABLE_IMAGE_EXTENSIONS,
            "isf_supported": False,
            "reusable": False,
            "needs_user_action": True,
            "reasons": [meta.inspect_error or "Image provenance requires operator review"],
            "family": meta.family,
        }
    if isinstance(meta, WindowsImageMetadata) or getattr(meta, "platform", None) == "windows":
        if not isinstance(meta, WindowsImageMetadata):
            return {
                "bootable": image.suffix.lower() in BOOTABLE_IMAGE_EXTENSIONS,
                "isf_supported": False,
                "reusable": False,
                "needs_user_action": True,
                "reasons": ["Windows provenance metadata is invalid"],
                "family": "windows",
            }
        return _classify_windows_preflight(image, meta)

    reasons: list[str] = []
    suffix = image.suffix.lower()
    bootable = suffix in BOOTABLE_IMAGE_EXTENSIONS
    if not bootable:
        reasons.append(f"extension {suffix or '(none)'} is not auto-bootable")

    distro = _normalize_distro(meta.distro)
    if distro == "unknown":
        reasons.append("distro is unknown")

    isf_supported = distro in ISF_SUPPORTED_DISTROS
    if not isf_supported:
        reasons.append(f"distro {distro} is not supported for ISF automation")

    arch = (meta.arch or "x86_64").strip().lower()
    if arch not in {"x86_64", "amd64"}:
        isf_supported = False
        reasons.append(f"architecture {arch} is not supported for ISF automation")

    needs_user_action = bool(reasons)
    return {
        "bootable": bootable,
        "isf_supported": isf_supported,
        "reusable": False,
        "needs_user_action": needs_user_action,
        "reasons": reasons,
        "family": meta.family,
    }


def require_reusable_windows_image(image: Path, *, allowed_dir: Path) -> Path:
    """Validate the selected source's existing provenance without inspecting or writing the disk.

    This is a metadata gate, not evidence of generalization or a disk-content
    verification. Never use ensure_metadata here: missing provenance must fail
    without invoking an inspector or manufacturing a fallback sidecar.
    """
    try:
        selected = image.resolve(strict=True)
        selected.relative_to(allowed_dir.resolve())
        if not selected.is_file():
            raise ValueError("not a regular image file")
    except (OSError, RuntimeError, ValueError):
        raise ValueError(
            "Windows image preflight failed: source must be a file inside the permitted directory"
        ) from None

    try:
        sidecar = _sidecar_path(selected)
        if sidecar.is_symlink():
            raise ValueError("symlinked provenance")
        meta = load_sidecar(selected)
        if not isinstance(meta, WindowsImageMetadata):
            raise ValueError("missing Windows provenance")
        result = classify_image_preflight(selected, meta)
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
        raise ValueError("Windows image preflight failed: missing or invalid schema-v3 Windows provenance") from None
    if result["reusable"] is not True:
        raise ValueError("Windows image preflight failed: " + "; ".join(result["reasons"]))
    return selected


def _filename_fallback(image: Path, error: str | None) -> ImageMetadata:
    # Imported lazily to avoid circular import (catalog imports image_inspection).
    from ..catalog import _infer_from_filename

    raw = _infer_from_filename(image.name)
    return ImageMetadata(
        schema_version=SIDECAR_SCHEMA_VERSION,
        distro=raw["distro"],
        version=raw["version"],
        family=raw["family"],
        default_username=raw["default_username"],
        os_variant=raw["os_variant"],
        machine_type=raw["machine_type"],
        kernel_release=None,
        arch=None,
        cloud_init_profile=raw.get("cloud_init_profile"),
        inject_network_config=raw.get("inject_network_config"),
        source="filename",
        detected_at=datetime.now(UTC).isoformat(timespec="seconds"),
        user_edited_fields=[],
        inspect_error=error,
    )


def ensure_metadata(image: Path) -> ImageMetadataLike:
    """Return metadata for `image`, populating a sidecar if none exists.

    Precedence: existing sidecar > virt-inspector > filename regex fallback.
    """
    protected = _protected_metadata(image)
    if protected is not None:
        return protected
    existing = load_sidecar(image)
    if existing is not None and not _is_stale_sidecar(image, existing):
        return existing

    from ..core.config import settings

    meta: ImageMetadataLike
    if not settings.image_inspection_enabled:
        meta = _filename_fallback(image, error="image_inspection disabled")
        save_sidecar(image, meta)
        _invalidate_catalog_caches()
        return meta

    try:
        meta = inspect_image(
            image,
            timeout=settings.image_inspection_timeout_seconds,
            binary=settings.image_inspection_virt_inspector_bin,
        )
    except FileNotFoundError:
        meta = _filename_fallback(image, error="virt-inspector not installed")
    except subprocess.TimeoutExpired:
        meta = _filename_fallback(
            image,
            error=f"virt-inspector timeout after {settings.image_inspection_timeout_seconds}s",
        )
    except subprocess.CalledProcessError as exc:
        stderr_first = (exc.stderr or "").splitlines()[0] if exc.stderr else f"exit {exc.returncode}"
        meta = _filename_fallback(image, error=f"virt-inspector failed: {stderr_first}")
    except (ValueError, ET.ParseError) as exc:
        meta = _filename_fallback(image, error=f"virt-inspector parse error: {exc}")

    protected = _protected_metadata(image)
    if protected is not None:
        return protected
    if isinstance(meta, UnverifiedImageMetadata):
        return meta
    save_sidecar(image, meta)
    _invalidate_catalog_caches()
    return meta


IMAGE_INSPECTIONS: dict[str, dict] = {}
_INSPECTIONS_LOCK = threading.Lock()


def _run_inspection(image: Path, force: bool) -> None:
    name = image.name
    protected = _protected_metadata(image)
    if protected is not None:
        blocked = force or isinstance(protected, UnverifiedImageMetadata)
        with _INSPECTIONS_LOCK:
            IMAGE_INSPECTIONS[name] = {
                "status": "error" if blocked else "done",
                "meta": protected,
                "error": "Automatic inspection cannot replace protected provenance; operator review required"
                if blocked
                else None,
            }
        return
    if not force:
        existing = load_sidecar(image)
        if existing is not None and not _is_stale_sidecar(image, existing):
            with _INSPECTIONS_LOCK:
                IMAGE_INSPECTIONS[name] = {"status": "done", "meta": existing, "error": None}
            return

    from ..core.config import settings

    try:
        if settings.image_inspection_enabled:
            try:
                meta = inspect_image(
                    image,
                    timeout=settings.image_inspection_timeout_seconds,
                    binary=settings.image_inspection_virt_inspector_bin,
                )
            except FileNotFoundError:
                meta = _filename_fallback(image, error="virt-inspector not installed")
            except subprocess.TimeoutExpired:
                meta = _filename_fallback(
                    image,
                    error=f"virt-inspector timeout after {settings.image_inspection_timeout_seconds}s",
                )
            except subprocess.CalledProcessError as exc:
                stderr_first = (exc.stderr or "").splitlines()[0] if exc.stderr else f"exit {exc.returncode}"
                meta = _filename_fallback(image, error=f"virt-inspector failed: {stderr_first}")
            except (ValueError, ET.ParseError) as exc:
                meta = _filename_fallback(image, error=f"virt-inspector parse error: {exc}")
        else:
            meta = _filename_fallback(image, error="image_inspection disabled")

        protected = _protected_metadata(image)
        if protected is not None or isinstance(meta, UnverifiedImageMetadata):
            with _INSPECTIONS_LOCK:
                IMAGE_INSPECTIONS[name] = {
                    "status": "error",
                    "meta": protected or meta,
                    "error": "Automatic inspection cannot create or replace protected provenance; operator review required",
                }
            return

        # Preserve user-edited fields from existing sidecar on re-inspect
        previous = load_sidecar(image)
        if previous is not None and previous.user_edited_fields:
            for fname in previous.user_edited_fields:
                if hasattr(meta, fname) and hasattr(previous, fname):
                    setattr(meta, fname, getattr(previous, fname))
            meta.user_edited_fields = list(previous.user_edited_fields)

        save_sidecar(image, meta)
        _invalidate_catalog_caches()
        with _INSPECTIONS_LOCK:
            IMAGE_INSPECTIONS[name] = {"status": "done", "meta": meta, "error": None}
    except Exception as exc:
        logger.exception("Inspection thread failed for %s", image)
        with _INSPECTIONS_LOCK:
            IMAGE_INSPECTIONS[name] = {"status": "error", "meta": None, "error": str(exc)}


def inspect_image_bg(image: Path, *, join: bool = False, force: bool = False) -> threading.Thread:
    """Spawn a background thread that inspects `image` and writes its sidecar.

    `force=True` re-runs inspection even if a sidecar already exists. If a non-force
    inspection is already in flight, a `force=True` call waits for it to finish and
    then spawns a fresh inspection so the user's Re-inspect click is honoured.
    `join=True` waits for the thread to finish (used in tests).
    """
    name = image.name

    while True:
        with _INSPECTIONS_LOCK:
            running = IMAGE_INSPECTIONS.get(name)
            if running and running.get("status") == "running":
                existing_thread = running.get("thread")
            else:
                existing_thread = None

            if existing_thread is None or not existing_thread.is_alive():
                IMAGE_INSPECTIONS[name] = {"status": "running", "meta": None, "error": None}
                break

            if not force:
                # Non-force caller is happy to share the in-flight result.
                if join:
                    pass  # fall through to join below
                else:
                    return existing_thread

        if not force:
            # Non-force + join: wait outside the lock for the in-flight thread.
            existing_thread.join()
            return existing_thread

        # force=True with an in-flight job: wait for it, then loop and spawn a fresh one.
        existing_thread.join()

    thread = threading.Thread(target=_run_inspection, args=(image, force), daemon=True)
    with _INSPECTIONS_LOCK:
        IMAGE_INSPECTIONS[name]["thread"] = thread
    thread.start()
    if join:
        thread.join()
    return thread
