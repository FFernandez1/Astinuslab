from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ..catalog import target_by_id
from ..core.config import settings
from ..db import session_scope
from ..db.repositories import JobRepository
from ..images.image_inspection import ImageMetadata, load_sidecar
from ..storage.keystore import get_active_key_paths
from ..virtualization.host_helpers import storage_tmp_dir
from .symbols.base import FetchResult, IsfBuildOutcome, KernelMetadata, ProviderAttempt
from .symbols.build_id import build_id_from_guest
from .symbols.cache import SymbolCache
from .symbols.identity import SymbolIdentity
from .symbols.providers import DebuginfodProvider, PrebuiltIsfProvider, get_provider, is_supported
from .symbols.windows import resolve_windows_symbol_source, windows_symbol_cache_dir
from .vol3_tools import ToolDependencyError, require_dwarf2json_tool

logger = logging.getLogger(__name__)

_FETCH_SCRIPT = settings.base_dir / "vol3" / "scripts" / "fetch_vmlinux.sh"
_BUILD_SYMBOL_SCRIPT = settings.base_dir / "vol3" / "scripts" / "build_symbol_from_live_guest.sh"
_LIVE_SYMBOL_BUILD_TIMEOUT_SECONDS = 300


def is_vol3_enabled() -> bool:
    return settings.vol3_enabled


def os_family_for_distro(distro: str | None, *, platform: str | None = None) -> str:
    """Map a target distro slug to the Volatility 3 symbol family.

    Windows guests resolve PDB-based symbols; everything else uses the Linux
    ISF path.
    """
    if platform and platform.strip().lower() == "windows":
        return "windows"
    if distro and distro.strip().lower().startswith("win"):
        return "windows"
    return "linux"


def requires_isf_build(os_family: str) -> bool:
    """Whether a guest needs a per-kernel ISF built from kernel symbols.

    Linux does; Windows does not (it downloads PDB-derived symbols from the
    Microsoft Symbol Server or an offline cache).
    """
    return str(os_family).strip().lower() != "windows"


def _symbol_slug(kernel_release: str, build_id: str | None = None) -> str:
    """Return a filesystem-safe slug for the given kernel release.

    When a valid hex build_id is provided, a 16-char suffix is appended so
    that kernels with the same uname -r but different builds never collide on
    the same ISF file.  Non-hex build_ids (e.g. test fixtures) are ignored and
    the unqualified slug is returned, preserving backward compatibility.
    """
    s = kernel_release.replace("/", "_").replace(" ", "_")
    slug = re.sub(r"[^a-zA-Z0-9_.\-]", "", s)
    if build_id and re.fullmatch(r"[0-9a-fA-F]{16,64}", build_id):
        slug = f"{slug}_{build_id[:16].lower()}"
    return slug


def _symbol_path(kernel_release: str, build_id: str | None = None) -> Path | None:
    """Return the validated ISF path that exists on disk, or None.

    Prefers the build_id-qualified slug when a valid hex build_id is supplied;
    falls back to the unqualified slug for backward compatibility with ISFs
    written before build_id tracking was introduced.
    """
    linux_dir = settings.vol3_symbols_path / "linux"
    if build_id:
        p = linux_dir / f"{_symbol_slug(kernel_release, build_id)}.json"
        if _validate_isf(p)[0]:
            return p
    p = linux_dir / f"{_symbol_slug(kernel_release)}.json"
    if _validate_isf(p)[0]:
        return p
    return None


def _safe_cached_symbol_path(kernel_release: str, build_id: str | None, *, allow_legacy: bool = False) -> Path | None:
    if build_id:
        qualified = _qualified_symbol_path(kernel_release, build_id)
        if qualified is not None:
            return qualified
        return _symbol_path(kernel_release, build_id) if allow_legacy else None
    return _symbol_path(kernel_release) if allow_legacy else None


def _legacy_soft_hit_path(
    kernel_release: str,
    build_id: str | None,
    *,
    architecture: str = "x86_64",
) -> tuple[Path | None, bool]:
    """Resolve an unqualified (kernel-release-only) cached ISF.

    Returns (path, corroborated). corroborated is True only when the DB
    records a matching build_id for this kernel's most recent valid ISF —
    i.e. the filename match is independently confirmed. Otherwise the hit
    is a SOFT HIT: a bare `uname -r` filename match with no build_id
    evidence. The deliberate backstop is Volatility's Linux banner check
    (a stale ISF for a rebuilt kernel almost always fails the banner match
    hard rather than silently misinterpreting the dump); the residual risk
    is banner-identical rebuilds, which is why soft hits are recorded as
    source="cache_soft_hit" on the artifact row.
    """
    legacy_path = _symbol_path(kernel_release)
    if legacy_path is None:
        return None, False
    if build_id is None:
        return legacy_path, False
    with session_scope() as session:
        repo = JobRepository(session)
        artifact = repo.find_valid_isf_by_kernel(kernel_release, architecture=architecture)
        cached_build_id = artifact.build_id if artifact is not None else None
    if cached_build_id is not None and cached_build_id != build_id:
        return None, False
    return legacy_path, cached_build_id == build_id


def _cached_symbol_path_for_job(kernel_release: str, build_id: str | None) -> tuple[Path | None, bool]:
    """Return (path, soft_hit) for the job's cached ISF, or (None, False).

    soft_hit is True when the file was matched by kernel-release filename
    only, without build_id corroboration (see _legacy_soft_hit_path).
    """
    if build_id is not None:
        strict_path = _safe_cached_symbol_path(kernel_release, build_id, allow_legacy=False)
        if strict_path is not None:
            return strict_path, False
        legacy_path, corroborated = _legacy_soft_hit_path(kernel_release, build_id)
        return legacy_path, legacy_path is not None and not corroborated
    path = _safe_cached_symbol_path(kernel_release, None, allow_legacy=True)
    return path, path is not None


def _qualified_symbol_path(kernel_release: str, build_id: str | None) -> Path | None:
    """Return only a build-ID-qualified ISF path, without legacy fallback."""
    if not build_id:
        return None
    slug = _symbol_slug(kernel_release, build_id)
    if slug == _symbol_slug(kernel_release):
        return None
    p = settings.vol3_symbols_path / "linux" / f"{slug}.json"
    return p if _validate_isf(p)[0] else None


def _target_guest_user(target_id: str | None) -> str:
    if not target_id:
        return "ubuntu"
    target = target_by_id(target_id)
    if target is None:
        logger.warning("Vol3 symbol build: target %s not found; using default guest user", target_id)
        return "ubuntu"
    return str(target.get("default_username") or "ubuntu")


# Validation results keyed by (path, mtime_ns, size):
# ISF writes are atomic (os.replace), so any content change rotates the key.
# Bounded because temp-file validations (unique paths) would otherwise grow
# the dict for the worker's lifetime.
_ISF_VALIDATE_CACHE: dict[tuple[str, int, int], tuple[bool, str | None]] = {}
_ISF_VALIDATE_CACHE_MAX = 256


def _validate_isf(path: Path) -> tuple[bool, str | None]:
    """Lightweight validation of a dwarf2json ISF output file.

    Returns (True, None) for a valid ISF, or (False, reason) for an invalid one.
    Results are cached by (path, mtime_ns, size) so repeated cache probes do
    not re-parse multi-megabyte ISF JSON.
    """
    try:
        stat = path.stat()
    except OSError:
        return False, f"ISF file does not exist: {path}"
    if stat.st_size == 0:
        return False, f"ISF file is empty: {path}"
    cache_key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _ISF_VALIDATE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    try:
        data = json.loads(path.read_bytes())
    except json.JSONDecodeError as exc:
        result: tuple[bool, str | None] = (False, f"ISF file is not valid JSON: {exc}")
    except OSError as exc:
        return False, f"ISF file is not readable: {exc}"
    else:
        if not isinstance(data, dict) or not data:
            result = (False, "ISF file is not a JSON object or is empty")
        elif not isinstance(data.get("metadata"), dict) or not isinstance(data.get("metadata", {}).get("linux"), dict):
            result = (False, "ISF file missing metadata.linux object")
        elif not isinstance(data.get("symbols"), dict) or not data.get("symbols"):
            result = (False, "ISF file missing non-empty symbols object")
        else:
            result = (True, None)
    if len(_ISF_VALIDATE_CACHE) >= _ISF_VALIDATE_CACHE_MAX:
        _ISF_VALIDATE_CACHE.clear()
    _ISF_VALIDATE_CACHE[cache_key] = result
    return result


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _ensure_isf_directory(path: Path) -> None:
    """Create shared cache directories without changing existing ownership/modes."""
    try:
        path.mkdir(mode=0o2770)
    except FileNotFoundError:
        _ensure_isf_directory(path.parent)
        _ensure_isf_directory(path)
    except FileExistsError:
        if not path.is_dir():
            raise
    else:
        # mkdir's mode is filtered by umask and may drop setgid. Only repair
        # directories this process just created; installation repairs old ones.
        path.chmod(0o2770)


def _new_temp_isf_path(final_path: Path) -> Path:
    _ensure_isf_directory(final_path.parent)
    tmp = tempfile.NamedTemporaryFile(
        prefix=f".{final_path.name}.",
        suffix=".tmp",
        dir=final_path.parent,
        delete=False,
    )
    tmp_path = Path(tmp.name)
    tmp.close()
    return tmp_path


@contextmanager
def _isf_cache_lock(slug: str):
    """Cooperative per-slug flock on vol3/symbols/linux/.lock-<slug>.

    Blocks indefinitely until the lock is acquired. The caller should re-check
    _cached_symbol_path_for_job inside the with-block to short-circuit when a
    concurrent writer has already produced the ISF.
    """
    linux_dir = settings.vol3_symbols_path / "linux"
    _ensure_isf_directory(linux_dir)
    lock_path = linux_dir / f".lock-{slug}"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o660)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _replace_with_valid_isf(tmp_path: Path, final_path: Path) -> tuple[bool, str | None]:
    valid, reason = _validate_isf(tmp_path)
    if not valid:
        if tmp_path.exists():
            tmp_path.unlink()
        return False, reason
    # NamedTemporaryFile starts at 0600; publication must also permit the
    # analysis service's shared group to read and replace the validated ISF.
    tmp_path.chmod(0o660)
    os.replace(tmp_path, final_path)
    return _validate_isf(final_path)


def _copy_isf_atomically(source_path: Path, final_path: Path) -> tuple[bool, str | None]:
    tmp_path = _new_temp_isf_path(final_path)
    try:
        shutil.copy2(source_path, tmp_path)
        return _replace_with_valid_isf(tmp_path, final_path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def _parse_keyed_output(log_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return values
    # Last occurrence wins (dict overwrite): the trusted wrapper script prints
    # its keyed lines after all guest-relayed output in the merged job log, so
    # guest-injected duplicates earlier in the log lose.
    for line in lines:
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in {"isf_path", "kernel_release", "build_id"}:
            values[key] = value
    return values


def _extract_build_id(vmlinux_path: Path) -> str | None:
    """Return the GNU Build ID hex string for a vmlinux ELF, or None on failure.

    Used to disambiguate cached ISFs across kernel rebuilds with the same uname -r.
    Tolerant: returns None if readelf is missing, the file is unreadable, or the
    note section is absent.
    """
    try:
        result = subprocess.run(
            ["readelf", "-n", str(vmlinux_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if line.startswith("Build ID:"):
            value = line.split(":", 1)[1].strip()
            if value and all(c in "0123456789abcdefABCDEF" for c in value):
                return value.lower()
    return None


def _probe_guest_build_id(ip_address: str, ssh_key: Path, guest_user: str) -> str | None:
    """SSH into the live guest to extract the running kernel's GNU build ID.

    Runs ``readelf -n`` on the guest's ``/boot/vmlinuz-$(uname -r)``.  Returns
    the lowercase hex build ID string, or None on any failure (SSH timeout,
    readelf absent, build-ID note missing).  The caller treats None as "unknown"
    and falls back to the file-only cache check (soft-hit behaviour).
    """
    cmd = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=10",
        "-i",
        str(ssh_key),
        f"{guest_user}@{ip_address}",
        "kr=$(uname -r); readelf -n \"/boot/vmlinuz-$kr\" 2>/dev/null | awk '/Build ID:/{print $NF; exit}'",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError, TypeError, subprocess.SubprocessError):
        return None
    stdout = getattr(result, "stdout", "")
    if not isinstance(stdout, str):
        return None
    value = stdout.strip()
    if not isinstance(value, str):
        return None
    if value and all(c in "0123456789abcdefABCDEF" for c in value):
        return value.lower()
    return None


def _last_event_state_and_message(job_id: str) -> tuple[str | None, str | None]:
    """Return the (state, message) of the most recent JobEvent for job_id.

    Used by the live-guest fallback path to detect whether
    run_vol3_symbol_build's rc=2 branch already emitted the actionable
    vol3_unsupported_distro event, so the caller can avoid layering a
    generic failure summary on top of it.
    """
    with session_scope() as session:
        repo = JobRepository(session)
        events = repo.list_events(job_id)
        if not events:
            return None, None
        return events[-1].state, events[-1].message


def _scp_vmlinux_to_guest(local_path: str, ip_address: str, ssh_key: Path, guest_user: str) -> str | None:
    """scp a host-extracted vmlinux to the guest. Returns remote path on success."""
    remote_path = f"/var/tmp/astinuslab-vmlinux-{Path(local_path).name}"  # noqa: S108  # guest-side path, not a host temp file
    cmd = [
        "scp",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=15",
        "-i",
        str(ssh_key),
        local_path,
        f"{guest_user}@{ip_address}:{remote_path}",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        return None
    return remote_path if result.returncode == 0 else None


def run_vol3_symbol_build(
    job_id: str,
    *,
    log_name: str = "symbol-build-host.log",
    vmlinux_override: str | None = None,
) -> bool:
    """Run build_symbol_from_live_guest.sh for a job's live guest VM.

    Returns True on success (or if symbol already exists), False on failure.
    """
    with session_scope() as session:
        repo = JobRepository(session)
        runtime = repo.get_runtime(job_id, "target")
        ip_address = runtime.ip_address if runtime is not None else None
        job = repo.get_job(job_id)
        kernel_release = job.detected_kernel_release if job is not None else None
        distro = job.detected_distro if job is not None else None
        version = job.detected_version if job is not None else None
        guest_user = _target_guest_user(job.target_id if job is not None else None)
        capture_type = getattr(job, "capture_type", "memory") if job is not None else "memory"
        get_known_build_id = getattr(repo, "get_job_known_build_id", None)
        known_build_id = get_known_build_id(job_id) if get_known_build_id is not None else None

    if not ip_address:
        logger.warning("Vol3 symbol build %s: no target runtime or IP — skipping", job_id)
        return False

    if kernel_release is None:
        logger.warning("Vol3 symbol build %s: no kernel_release detected — skipping", job_id)
        return False

    # Obtain the SSH key early so we can use it for both the build-id probe and
    # the live script.  We intentionally soft-fail here: if the key is not yet
    # available, the cache check still runs as a file-only (soft-hit) check so
    # that missing credentials do not prevent a legitimate early return.  If we
    # reach the live-script path without a key we abort there.
    _ssh_key: Path | None = None
    try:
        _ssh_key, _ = get_active_key_paths()
    except RuntimeError:
        pass

    probed_build_id: str | None = None
    cache_build_id = known_build_id
    if _ssh_key is not None:
        probed_build_id = _probe_guest_build_id(ip_address, _ssh_key, guest_user)
        if probed_build_id is not None:
            cache_build_id = probed_build_id

    qualified_cache_path = _qualified_symbol_path(kernel_release, cache_build_id)
    cached_symbol_path = qualified_cache_path or _symbol_path(kernel_release)
    if cached_symbol_path is not None:
        # Build-id aware cache check.  Probe the guest's running kernel build_id
        # and compare against the build_id recorded for the cached ISF file.  If
        # they differ the cached file is stale (same uname -r, different build) so
        # we fall through to let the live script rebuild symbols.  If the probe
        # fails or either side is NULL (soft-hit) we return early.
        if cache_build_id is not None and qualified_cache_path is None:
            with session_scope() as session:
                repo = JobRepository(session)
                cached = repo.find_valid_isf_by_kernel(kernel_release)
                cached_build_id = cached.build_id if cached is not None else None
            if cached_build_id is not None and cached_build_id != cache_build_id:
                logger.info(
                    "Vol3 symbol build %s: cached ISF build_id %s != current %s for %s — treating as cache miss",
                    job_id,
                    cached_build_id,
                    cache_build_id,
                    kernel_release,
                )
                # Fall through to the live script to rebuild symbols.
            else:
                logger.info(
                    "Vol3 symbol build %s: symbol already exists for %s — skipping",
                    job_id,
                    kernel_release,
                )
                return True
        else:
            # No build_id from DB or probe — soft-hit.
            logger.info(
                "Vol3 symbol build %s: symbol already exists for %s — skipping",
                job_id,
                kernel_release,
            )
            return True

    # Past the cache check — need a valid SSH key to run the live script.
    if _ssh_key is None:
        try:
            ssh_key, _ = get_active_key_paths()
        except RuntimeError:
            logger.warning("Vol3 symbol build %s: no active SSH key — skipping", job_id)
            return False
    else:
        ssh_key = _ssh_key

    log_dir = settings.storage_dir / "dumps" / job_id / "vol3"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / log_name

    tmp_root = storage_tmp_dir(settings)
    tmp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="astinuslab-vol3-", dir=tmp_root) as tmp_dir:
        staging_symbols_dir = Path(tmp_dir) / "symbols"
        cmd = [
            "bash",
            str(_BUILD_SYMBOL_SCRIPT),
            "--guest-ip",
            ip_address,
            "--ssh-key",
            str(ssh_key),
            "--work-dir",
            tmp_dir,
            "--symbols-dir",
            str(staging_symbols_dir),
            "--guest-user",
            guest_user,
        ]
        logger.info("Vol3 symbol build %s: running %s", job_id, _BUILD_SYMBOL_SCRIPT.name)
        run_env = dict(os.environ)
        invasive_setting = bool(getattr(settings, "vol3_allow_invasive_guest_symbol_install", False))
        # Invasive guest package install mutates the evidence filesystem (apt lists,
        # package DB, mtimes). Permit it only for ISF-only jobs (capture_type "none");
        # any job that can produce a memory/disk artifact must keep the guest pristine.
        invasive_allowed = invasive_setting and capture_type == "none"
        run_env["VOL3_ALLOW_GUEST_PACKAGE_INSTALL"] = "1" if invasive_allowed else "0"
        if invasive_setting and not invasive_allowed:
            logger.warning(
                "Vol3 symbol build %s: invasive guest install suppressed (capture_type=%s)",
                job_id,
                capture_type,
            )
            with session_scope() as session:
                repo = JobRepository(session)
                if repo.get_job(job_id) is not None:
                    repo.add_event(
                        job_id,
                        "vol3_invasive_install_suppressed",
                        f"Invasive guest debug-package install is enabled in settings but was suppressed: "
                        f"job capture_type is '{capture_type}', and guest mutation would contaminate evidence.",
                    )
        elif invasive_allowed:
            with session_scope() as session:
                repo = JobRepository(session)
                if repo.get_job(job_id) is not None:
                    repo.add_event(
                        job_id,
                        "vol3_invasive_install_enabled",
                        "Invasive guest debug-package install is permitted for this ISF-only build; "
                        "the guest filesystem may be mutated (apt sources, debug packages).",
                    )
        if vmlinux_override:
            remote_vmlinux = _scp_vmlinux_to_guest(
                vmlinux_override,
                ip_address,
                ssh_key,
                guest_user,
            )
            if remote_vmlinux is None:
                logger.warning(
                    "Vol3 symbol build %s: scp of vmlinux override failed — proceeding without override",
                    job_id,
                )
            else:
                run_env["VMLINUX_OVERRIDE"] = remote_vmlinux
                logger.info(
                    "Vol3 symbol build %s: vmlinux override staged at %s",
                    job_id,
                    remote_vmlinux,
                )
        with log_path.open("wb") as lf:
            try:
                resolved_dwarf2json = require_dwarf2json_tool(settings)
            except ToolDependencyError as exc:
                lf.write(f"ERROR: dwarf2json dependency unavailable: {exc.code}\n".encode())
                logger.warning("Vol3 symbol build %s: dwarf2json dependency unavailable: %s", job_id, exc.code)
                return False
            run_env["DWARF2JSON_LOCAL"] = str(resolved_dwarf2json)
            run_env["DWARF2JSON_SHA256"] = settings.vol3_dwarf2json_sha256 or ""
            try:
                result = subprocess.run(
                    cmd,
                    stdout=lf,
                    stderr=lf,
                    timeout=_LIVE_SYMBOL_BUILD_TIMEOUT_SECONDS,
                    env=run_env,
                )
            except subprocess.TimeoutExpired:
                lf.write(
                    f"\nERROR: live Vol3 symbol build timed out after {_LIVE_SYMBOL_BUILD_TIMEOUT_SECONDS}s\n".encode()
                )
                logger.warning(
                    "Vol3 symbol build %s: timed out after %ss",
                    job_id,
                    _LIVE_SYMBOL_BUILD_TIMEOUT_SECONDS,
                )
                return False

        if result.returncode == 2:
            logger.warning(
                "Vol3 symbol build %s: live-guest build unsupported on this distro (script exit 2)",
                job_id,
            )
            with session_scope() as session:
                repo = JobRepository(session)
                if repo.get_job(job_id) is not None:
                    repo.add_event(
                        job_id,
                        "vol3_unsupported_distro",
                        "Vol3 live-guest build is not yet supported on this distro; "
                        "install kernel debug symbols manually inside the guest.",
                    )
            return False
        if result.returncode != 0:
            logger.warning("Vol3 symbol build %s: script exited with code %d", job_id, result.returncode)
            return False

        output = _parse_keyed_output(log_path)
        live_isf = output.get("isf_path")
        live_kernel_release = output.get("kernel_release") or kernel_release
        live_build_id = output.get("build_id") or probed_build_id
        if not live_kernel_release:
            logger.warning("Vol3 symbol build %s: script did not report kernel_release", job_id)
            return False
        if not live_isf:
            logger.warning("Vol3 symbol build %s: script did not report isf_path", job_id)
            return False
        live_isf_path = Path(live_isf)

        valid, reason = _validate_isf(live_isf_path)
        if not valid:
            logger.warning("Vol3 symbol build %s: live ISF validation failed: %s", job_id, reason)
            return False

        with _isf_cache_lock(_symbol_slug(live_kernel_release, live_build_id)):
            actual = (
                _qualified_symbol_path(live_kernel_release, live_build_id)
                if live_build_id
                else _symbol_path(live_kernel_release)
            )
            if actual is not None:
                with session_scope() as session:
                    repo = JobRepository(session)
                    repo.create_isf_artifact(
                        job_id,
                        live_kernel_release,
                        str(actual),
                        "cache",
                        distro=distro,
                        version=version,
                        sha256=_sha256_file(actual),
                        build_id=live_build_id,
                    )
                return True
            symbols_linux_dir = settings.vol3_symbols_path / "linux"
            symbols_linux_dir.mkdir(parents=True, exist_ok=True)
            isf_path = symbols_linux_dir / f"{_symbol_slug(live_kernel_release, live_build_id)}.json"
            valid, reason = _copy_isf_atomically(live_isf_path, isf_path)
            if not valid:
                logger.warning("Vol3 symbol build %s: cached ISF validation failed: %s", job_id, reason)
                return False

            isf_hash = _sha256_file(isf_path)

    with session_scope() as session:
        repo = JobRepository(session)
        repo.create_isf_artifact(
            job_id,
            live_kernel_release,
            str(isf_path),
            "live_guest",
            distro=distro,
            version=version,
            sha256=isf_hash,
            build_id=live_build_id,
        )

    logger.info("Vol3 symbol build %s: complete", job_id)
    return True


def _run_fetch_vmlinux(
    log_path: Path,
    work_dir: Path,
    distro: str | None,
    version: str | None,
    kernel_release: str | None,
    arch: str = "x86_64",
    build_id: str | None = None,
) -> tuple[str | None, str | None]:
    """Fetch vmlinux through the selected symbol provider."""
    metadata = KernelMetadata(
        distro=(distro or "").lower(),
        version=version or "",
        kernel_release=kernel_release or "",
        architecture=arch,
        build_id=build_id,
    )
    provider = get_provider(metadata)
    result = provider.fetch_vmlinux(metadata, work_dir)
    if result.error or result.exit_code is not None or result.phase or result.log_excerpt:
        with log_path.open("ab") as lf:
            details = []
            if result.phase:
                details.append(f"phase={result.phase}")
            if result.exit_code is not None:
                details.append(f"exit_code={result.exit_code}")
            if details:
                lf.write(("fetch_result " + " ".join(details) + "\n").encode("utf-8"))
            if result.log_excerpt:
                lf.write(("fetch_log_excerpt:\n" + result.log_excerpt + "\n").encode("utf-8", errors="replace"))
            if result.error:
                lf.write(result.error.encode("utf-8", errors="replace"))
    if result.unsupported:
        logger.warning("Vol3 fetch-vmlinux: distro '%s' not supported — skipping symbols", distro)
        return None, None
    if not result.success or result.vmlinux_path is None:
        logger.warning("Vol3 fetch-vmlinux: provider fetch failed — skipping symbols")
        return None, None

    return str(result.vmlinux_path), str(result.system_map_path) if result.system_map_path else None


_CACHE_SOURCES = ("prebuilt_isf", "debuginfod", "host_fetch", "host_fetch_unverified", "live_guest")


class LocalCacheProvider:
    name = "local_cache"

    def __init__(self, cache: SymbolCache, identity: SymbolIdentity) -> None:
        self.cache = cache
        self.identity = identity

    def supports(self, metadata: KernelMetadata) -> bool:
        return True

    def skip_reason(self, metadata: KernelMetadata) -> str | None:
        return None

    def fetch_vmlinux(self, metadata: KernelMetadata, work_dir: Path) -> FetchResult:
        identities = [self.identity]
        identities.extend(_symbol_identity(metadata, source=source) for source in _CACHE_SOURCES)
        seen: set[Path] = set()
        for identity in identities:
            path = self.cache.path_for(identity)
            if path in seen:
                continue
            seen.add(path)
            if path.exists():
                return FetchResult(success=True, isf_path=path)
        return FetchResult(success=False, error="cache miss")


class HostFetchProvider:
    name = "host_fetch"

    def supports(self, metadata: KernelMetadata) -> bool:
        return is_supported(metadata.distro)

    def skip_reason(self, metadata: KernelMetadata) -> str | None:
        return None if self.supports(metadata) else f"unsupported distro: {metadata.distro}"

    def fetch_vmlinux(self, metadata: KernelMetadata, work_dir: Path) -> FetchResult:
        guest_build_id_known = bool(metadata.build_id)
        log_path = work_dir / "host-fetch.log"
        vmlinux_path, system_map_path = _run_fetch_vmlinux(
            log_path,
            work_dir,
            metadata.distro,
            metadata.version,
            metadata.kernel_release,
            metadata.architecture,
            metadata.build_id,
        )
        if vmlinux_path is None:
            error = "host fetch failed"
            if log_path.exists():
                error = log_path.read_text(encoding="utf-8", errors="replace") or error
            return FetchResult(success=False, error=error, phase="fetch_vmlinux")
        host_build_id = _extract_build_id(Path(vmlinux_path))
        if metadata.build_id and host_build_id and metadata.build_id != host_build_id:
            return FetchResult(
                success=False,
                error=(
                    f"build_id mismatch for {metadata.kernel_release}: "
                    f"guest={metadata.build_id}, fetched={host_build_id}"
                ),
                phase="build_id_validation",
            )
        if host_build_id and not metadata.build_id:
            metadata.build_id = host_build_id
        build_id_verified = guest_build_id_known and host_build_id is not None
        return FetchResult(
            success=True,
            vmlinux_path=Path(vmlinux_path),
            system_map_path=Path(system_map_path) if system_map_path else None,
            build_id_verified=build_id_verified,
        )


def _symbol_identity(metadata: KernelMetadata, *, source: str) -> SymbolIdentity:
    return SymbolIdentity(
        metadata.architecture,
        metadata.kernel_release,
        metadata.build_id,
        source,
        distro=metadata.distro,
        version=metadata.version,
    )


def _resolve_build_id(
    metadata: KernelMetadata,
    *,
    guest_target=None,
    image_path: Path | None = None,
) -> None:
    if metadata.build_id:
        metadata.build_id = metadata.build_id.lower()
        return
    if image_path is not None:
        sidecar = load_sidecar(image_path)
        if isinstance(sidecar, ImageMetadata) and sidecar.build_id:
            metadata.build_id = sidecar.build_id.lower()
            return
    if guest_target is not None:
        ip_address = getattr(guest_target, "ip_address", None)
        ssh_key = getattr(guest_target, "ssh_key", None)
        guest_user = getattr(guest_target, "guest_user", "ubuntu")
        if ip_address and ssh_key:
            metadata.build_id = build_id_from_guest(ip_address, Path(ssh_key), guest_user, metadata.kernel_release)


def _ordered_providers(
    metadata: KernelMetadata,
    *,
    cache: SymbolCache | None = None,
    identity: SymbolIdentity | None = None,
):
    if cache is None or identity is None:
        cache = SymbolCache(settings.vol3_symbols_path.resolve(), max_bytes=10 * 1024 * 1024 * 1024)
        identity = _symbol_identity(metadata, source="pending")
    # Live-guest ISF builds are the separate run_vol3_symbol_build path, not a
    # host-chain provider, so the chain ends at host_fetch by design.
    return [
        LocalCacheProvider(cache, identity),
        PrebuiltIsfProvider(),
        DebuginfodProvider(),
        HostFetchProvider(),
    ]


def _finalize_ready_isf(result: FetchResult, identity: SymbolIdentity, work_dir: Path) -> Path | None:
    return result.isf_path


def _build_isf_from_vmlinux(result: FetchResult, identity: SymbolIdentity, work_dir: Path) -> Path | None:
    """Convert a fetched vmlinux into an ISF with dwarf2json.

    Returns None when the conversion itself fails (dwarf2json ran and errored, timed
    out, or produced an ISF that does not validate).  Raises ToolDependencyError when
    dwarf2json is not ready to run at all: that is a distinct, operator-fixable cause
    carrying a stable readiness code, and swallowing it here would strand the code in
    the server log while the caller reported a generic failure.
    """
    if result.vmlinux_path is None or result.source_kind != "elf":
        return None
    try:
        with result.vmlinux_path.open("rb") as vmlinux_file:
            if vmlinux_file.read(4) != b"\x7fELF":
                return None
    except OSError:
        return None
    work_dir.mkdir(parents=True, exist_ok=True)
    isf_path = work_dir / "built-isf.json"
    dwarf2json_path = require_dwarf2json_tool(settings)
    cmd = [str(dwarf2json_path), "linux", "--elf", str(result.vmlinux_path)]
    try:
        with isf_path.open("wb") as out_f:
            completed = subprocess.run(
                cmd,
                stdout=out_f,
                stderr=subprocess.PIPE,
                timeout=getattr(settings, "vol3_dwarf2json_timeout_seconds", None),
            )
    except subprocess.TimeoutExpired:
        isf_path.unlink(missing_ok=True)
        return None
    if completed.returncode != 0:
        isf_path.unlink(missing_ok=True)
        return None
    valid, _reason = _validate_isf(isf_path)
    return isf_path if valid else None


def _log_chain_finished(job_id: str, outcome: IsfBuildOutcome) -> None:
    with session_scope() as session:
        repo = JobRepository(session)
        if repo.get_job(job_id) is None:
            return
        repo.add_event(job_id, "isf_chain_finished", json.dumps(outcome.to_event_dict(), sort_keys=True))


def _run_symbol_provider_chain(
    metadata: KernelMetadata,
    *,
    work_dir: Path,
    cache: SymbolCache,
    job_id: str,
    guest_target=None,
    image_path: Path | None = None,
) -> IsfBuildOutcome:
    work_dir.mkdir(parents=True, exist_ok=True)
    _resolve_build_id(metadata, guest_target=guest_target, image_path=image_path)
    identity = _symbol_identity(metadata, source="pending")
    attempts: list[ProviderAttempt] = []

    with cache.lock_for_identity(identity):
        for provider in _ordered_providers(metadata, cache=cache, identity=identity):
            started = time.monotonic()
            attempt = ProviderAttempt(name=provider.name)
            if not provider.supports(metadata):
                attempt.skipped = True
                skip_reason = getattr(provider, "skip_reason", lambda _metadata: "unsupported")(metadata)
                attempt.skip_reason = skip_reason
                attempts.append(attempt)
                continue
            result = provider.fetch_vmlinux(metadata, work_dir)
            attempt.duration_ms = int((time.monotonic() - started) * 1000)
            attempt.urls = list(result.attempted_urls)
            attempt.log_excerpt = result.log_excerpt
            attempt.error = result.error
            attempt.vmlinux_path = str(result.vmlinux_path) if result.vmlinux_path else None
            if result.success:
                dependency_error: str | None = None
                if result.isf_path:
                    isf_path = _finalize_ready_isf(result, identity, work_dir)
                else:
                    # Only the local-cache and prebuilt providers hand back a ready ISF.
                    # debuginfod and host_fetch return a vmlinux, so reaching here means
                    # dwarf2json must run -- and its readiness code is what the operator
                    # needs to see, not a generic "something failed".
                    try:
                        isf_path = _build_isf_from_vmlinux(result, identity, work_dir)
                    except ToolDependencyError as exc:
                        logger.warning(
                            "Vol3 %s: %s returned a vmlinux but dwarf2json is unavailable: %s",
                            job_id,
                            provider.name,
                            exc.code,
                        )
                        isf_path = None
                        dependency_error = f"dwarf2json dependency unavailable: {exc.code}"
                if isf_path is None:
                    attempt.error = dependency_error or attempt.error or "ISF validation failed or dwarf2json failed"
                if isf_path is not None:
                    attempt.success = True
                    attempts.append(attempt)
                    if provider.name == "local_cache":
                        success_identity = identity
                        stored_path = isf_path
                    else:
                        source_name = provider.name if result.build_id_verified else f"{provider.name}_unverified"
                        success_identity = _symbol_identity(metadata, source=source_name)
                        stored_path = cache.store(
                            success_identity,
                            isf_path,
                            metadata.__dict__ | {"source": success_identity.source},
                        )
                    outcome = IsfBuildOutcome(True, stored_path, success_identity, metadata, attempts)
                    _log_chain_finished(job_id, outcome)
                    return outcome
            attempts.append(attempt)

    outcome = IsfBuildOutcome(False, None, identity, metadata, attempts)
    _log_chain_finished(job_id, outcome)
    return outcome


def _isf_job_log_path(job_id: str) -> Path:
    storage_dir = getattr(settings, "storage_dir", None)
    if storage_dir is None:
        storage_dir = settings.vol3_symbols_path.parent / "storage"
    log_dir = Path(storage_dir) / "dumps" / job_id / "vol3"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "symbol-build-host.log"


def _write_isf_diagnostics(job_id: str, outcome: IsfBuildOutcome) -> Path:
    path = _isf_job_log_path(job_id).with_name("symbol-diagnostics.json")
    path.write_text(json.dumps(outcome.to_event_dict(), indent=2, sort_keys=True), encoding="utf-8")
    return path


def run_vol3_isf_only(job_id: str) -> tuple[bool, str]:
    """Host action handler: build vol3 symbols for a completed job without memory capture.

    Skips immediately if a symbol for the job's kernel release already exists.
    Uses a temporary working directory; only the ISF in vol3/symbols persists.
    Returns (success, summary_message).
    """
    with session_scope() as session:
        repo = JobRepository(session)
        job = repo.get_job(job_id)
        if job is None:
            return False, f"Job {job_id} not found"
        kernel_release = job.detected_kernel_release
        distro = job.detected_distro
        version = job.detected_version
        target_id = job.target_id
        job_build_id = repo.get_job_known_build_id(job_id)

    # A validated catalog target is authoritative for the platform branch. This
    # keeps manually-created or stale detected-distro fields from routing an
    # explicit Windows target through the Linux ISF builder.
    target_platform = None
    try:
        target = target_by_id(target_id)
    except (LookupError, ValueError):
        target = None
    if target is not None and target.get("platform") == "windows" and target.get("reusable") is True:
        target_platform = "windows"
        distro = target.get("distro") or distro
        version = target.get("version") or version

    if not requires_isf_build(os_family_for_distro(distro, platform=target_platform)):
        # Windows resolves PDB-derived symbols from the Microsoft Symbol Server
        # (or an offline cache) at analysis time; there is no per-kernel Linux
        # ISF to build here.
        source = resolve_windows_symbol_source(offline_cache_dir=windows_symbol_cache_dir(settings.vol3_symbols_path))
        logger.info(
            "Vol3 %s: Windows target (%s) — skipping Linux ISF build; symbols via %s",
            job_id,
            distro,
            source["source"],
        )
        return True, f"Windows target uses PDB symbols ({source['source']}); no Linux ISF build required"

    if kernel_release is None:
        return False, f"Job {job_id} has no detected kernel_release — skipping"

    initial_symbol_path, initial_soft_hit = _cached_symbol_path_for_job(kernel_release, job_build_id)
    if initial_symbol_path is not None:
        logger.info("Vol3 %s: symbol already exists for %s — skipping", job_id, kernel_release)
        with session_scope() as session:
            repo = JobRepository(session)
            repo.create_isf_artifact(
                job_id,
                kernel_release,
                str(initial_symbol_path),
                "cache_soft_hit" if initial_soft_hit else "cache",
                distro=distro,
                version=version,
                sha256=_sha256_file(initial_symbol_path),
                build_id=job_build_id,
            )
        return True, f"Symbol already exists for {kernel_release} — skipped"

    lock_slug = _symbol_slug(kernel_release, job_build_id) if kernel_release else "linux-isf"
    with _isf_cache_lock(lock_slug):
        locked_symbol_path, locked_soft_hit = _cached_symbol_path_for_job(kernel_release, job_build_id)
        if locked_symbol_path is not None:
            logger.info("Vol3 %s: symbol already exists for %s — skipping", job_id, kernel_release)
            with session_scope() as session:
                repo = JobRepository(session)
                repo.create_isf_artifact(
                    job_id,
                    kernel_release,
                    str(locked_symbol_path),
                    "cache_soft_hit" if locked_soft_hit else "cache",
                    distro=distro,
                    version=version,
                    sha256=_sha256_file(locked_symbol_path),
                    build_id=job_build_id,
                )
            return True, f"Symbol already exists for {kernel_release} — skipped"

        tmp_root = Path(getattr(settings, "storage_dir", settings.vol3_symbols_path.parent / "storage")) / "tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="astinuslab-vol3-", dir=tmp_root) as tmp_dir:
            tmp_path = Path(tmp_dir)
            fetch_work_dir = tmp_path / "fetch"
            fetch_work_dir.mkdir()
            metadata = KernelMetadata(
                distro=(distro or "").lower(),
                version=version or "",
                kernel_release=kernel_release,
                architecture="x86_64",
                build_id=job_build_id,
            )
            cache = SymbolCache(settings.vol3_symbols_path.resolve(), max_bytes=10 * 1024 * 1024 * 1024)
            outcome = _run_symbol_provider_chain(
                metadata,
                work_dir=fetch_work_dir,
                cache=cache,
                job_id=job_id,
            )
            diagnostics_path = _write_isf_diagnostics(job_id, outcome)
            if outcome.success and outcome.isf_path is not None:
                valid, reason = _validate_isf(outcome.isf_path)
                if not valid:
                    msg = f"ISF validation failed: {reason}; diagnostics: {diagnostics_path}"
                    logger.warning("Vol3 %s: %s", job_id, msg)
                    return False, msg
                legacy_symbols_dir = settings.vol3_symbols_path / "linux"
                legacy_symbols_dir.mkdir(parents=True, exist_ok=True)
                legacy_isf_path = legacy_symbols_dir / f"{_symbol_slug(kernel_release, outcome.metadata.build_id)}.json"
                if outcome.isf_path != legacy_isf_path:
                    valid, reason = _copy_isf_atomically(outcome.isf_path, legacy_isf_path)
                    if not valid:
                        msg = f"ISF validation failed: {reason}; diagnostics: {diagnostics_path}"
                        logger.warning("Vol3 %s: %s", job_id, msg)
                        return False, msg
                    outcome_isf_path = legacy_isf_path
                else:
                    outcome_isf_path = outcome.isf_path
                isf_hash = _sha256_file(outcome_isf_path)
                with session_scope() as session:
                    repo = JobRepository(session)
                    repo.create_isf_artifact(
                        job_id,
                        kernel_release,
                        str(outcome_isf_path),
                        outcome.identity.source,
                        distro=distro,
                        version=version,
                        sha256=isf_hash,
                        build_id=outcome.metadata.build_id,
                    )
                return True, f"Symbol built: {outcome_isf_path}, sha256: {isf_hash}; diagnostics: {diagnostics_path}"

            log_path = _isf_job_log_path(job_id)
            host_attempt = next((attempt for attempt in outcome.attempts if attempt.name == "host_fetch"), None)
            if host_attempt is not None and host_attempt.error:
                log_path.write_text(host_attempt.error, encoding="utf-8", errors="replace")
            fallback_vmlinux_path = host_attempt.vmlinux_path if host_attempt is not None else None
            logger.info("Vol3 %s: trying live guest fallback after provider chain failure", job_id)
            fallback_kwargs = {"log_name": "symbol-build-live.log"}
            if fallback_vmlinux_path:
                fallback_kwargs["vmlinux_override"] = fallback_vmlinux_path
            if run_vol3_symbol_build(job_id, **fallback_kwargs):
                return (
                    True,
                    f"Symbol built using live guest fallback after provider chain failed; diagnostics: {diagnostics_path}",
                )
            last_state, last_msg = _last_event_state_and_message(job_id)
            if last_state == "vol3_unsupported_distro" and last_msg:
                return False, last_msg
            last_error = next((attempt.error for attempt in reversed(outcome.attempts) if attempt.error), None)
            error_suffix = f"; reason: {last_error}" if last_error else ""
            # A provider can hand back a vmlinux and still fail to yield an ISF --
            # debuginfod fetches one, then dwarf2json has to convert it.  Saying "no
            # vmlinux obtained" in that case blames the wrong half of the pipeline
            # and sends the operator hunting for a download problem that never was.
            fetched_vmlinux = any(attempt.vmlinux_path for attempt in outcome.attempts)
            lead = "no ISF built from the fetched vmlinux" if fetched_vmlinux else "no vmlinux obtained"
            msg = (
                f"ISF build failed: {lead} for distro={distro!r}, "
                f"version={version!r}, kernel_release={kernel_release!r}; see log: {log_path}{error_suffix}; diagnostics: {diagnostics_path}"
            )
            logger.warning("Vol3 %s: %s", job_id, msg)
            return False, msg


def _finalise(job_id: str, action_id: int | None, *, success: bool, summary: str) -> None:
    with session_scope() as session:
        repo = JobRepository(session)
        if repo.get_job(job_id) is None:
            logger.info("Vol3 _finalise %s: job no longer exists (deleted during analysis) — skipping", job_id)
            return
        if success:
            repo.add_event(job_id, "vol3_completed", f"Volatility 3 analysis completed. {summary}")
        else:
            repo.add_event(job_id, "vol3_failed", f"Volatility 3 analysis failed: {summary}")
        if action_id is not None:
            repo.update_action(
                action_id,
                status="completed" if success else "failed",
                error_summary=None if success else summary,
                finished_at=datetime.now(UTC),
                active_pid=None,
            )
