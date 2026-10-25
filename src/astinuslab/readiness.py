"""Authenticated deep-readiness probes for starting operator work safely.

``/health`` intentionally remains process-only liveness.  The checks here may
open database, broker, libvirt, and filesystem resources, so callers opt into
them through the versioned ``/api/v1/readiness`` endpoint.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import tempfile
import threading
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .app_settings import load_app_settings
from .core import fake_mode
from .core.config import Settings, settings
from .db import engine
from .forensics.vol3_tools import Vol3Readiness, get_vol3_readiness
from .jobs.celery_app import celery_app
from .virtualization import host_script_manifest
from .virtualization.host_helpers import _ALLOWED_HELPERS, PRIVILEGED_HELPER_DIR

ReadinessStatus = Literal["passed", "failed", "not_applicable"]
_DEEP_READINESS_TIMEOUT_SECONDS = 10.0
_READINESS_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="astinuslab-readiness")
_READINESS_SLOT = threading.BoundedSemaphore(value=1)


@dataclass(frozen=True)
class ReadinessCheck:
    """One stable readiness result plus the services whose work depends on it."""

    name: str
    status: ReadinessStatus
    code: str
    applies_to: tuple[str, ...]
    detail: str

    def public_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "code": self.code,
            "applies_to": list(self.applies_to),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ReadinessReport:
    checks: tuple[ReadinessCheck, ...]

    @property
    def ready(self) -> bool:
        return all(check.status != "failed" for check in self.checks)

    def public_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "status": "ready" if self.ready else "not_ready",
            "checks": [check.public_dict() for check in self.checks],
        }


_OPTIONAL_CAPABILITY_CHECKS = frozenset({"volatility_analysis", "volatility_isf_build"})


def mandatory_runtime_ready(report: ReadinessReport) -> bool:
    """Return whether core service dependencies are ready for an install switch.

    Volatility and ISF tooling is supplied separately by the operator, so its
    absence remains visible in deep readiness without making a fresh base
    installation roll back. Every other failed check remains switch-blocking.
    """
    return all(check.status != "failed" or check.name in _OPTIONAL_CAPABILITY_CHECKS for check in report.checks)


def _passed(name: str, applies_to: tuple[str, ...], detail: str) -> ReadinessCheck:
    return ReadinessCheck(name=name, status="passed", code="ready", applies_to=applies_to, detail=detail)


def _failed(name: str, code: str, applies_to: tuple[str, ...], detail: str) -> ReadinessCheck:
    return ReadinessCheck(name=name, status="failed", code=code, applies_to=applies_to, detail=detail)


def _not_applicable(name: str, code: str, applies_to: tuple[str, ...], detail: str) -> ReadinessCheck:
    return ReadinessCheck(
        name=name,
        status="not_applicable",
        code=code,
        applies_to=applies_to,
        detail=detail,
    )


def check_database_transaction(db_engine: Any = engine) -> ReadinessCheck:
    """Acquire SQLite's write reservation and roll it back without changing data."""
    try:
        with db_engine.connect() as connection:
            try:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                connection.exec_driver_sql("SELECT 1")
            finally:
                connection.rollback()
    except Exception:  # noqa: BLE001 - readiness must convert dependency exceptions into stable codes
        return _failed(
            "database_transaction",
            "database_transaction_failed",
            ("api", "runner", "analysis"),
            "The application database could not complete a write-capable transaction.",
        )
    return _passed(
        "database_transaction",
        ("api", "runner", "analysis"),
        "The application database completed and rolled back a write-capable transaction.",
    )


def check_celery_broker(app: Any = celery_app) -> ReadinessCheck:
    try:
        with app.connection_for_read() as connection:
            connection.ensure_connection(max_retries=0, timeout=3.0)
    except Exception:  # noqa: BLE001 - Celery transports expose backend-specific exceptions
        return _failed(
            "celery_broker",
            "celery_broker_unavailable",
            ("api", "runner", "analysis"),
            "The Celery broker is unavailable.",
        )
    return _passed(
        "celery_broker",
        ("api", "runner", "analysis"),
        "The Celery broker accepted a connection.",
    )


def probe_worker_readiness(app: Any = celery_app) -> list[dict[str, object]]:
    """Request worker-owned readiness payloads; never run their probes in API."""
    try:
        replies = app.control.broadcast("astinuslab_readiness", reply=True, timeout=2.0)
    except Exception:  # noqa: BLE001 - missing replies are represented by heartbeat status
        return []
    return replies if isinstance(replies, list) else []


def remote_control_available(app: Any = celery_app) -> bool:
    """Whether the configured broker can carry Celery remote-control replies.

    Celery remote control is a fanout exchange. The shipped SQLite broker
    transport does not implement fanout, so no worker can ever answer a
    ``broadcast``. Treating that silence as a failure reports a permanent false
    negative and blocks an install switch that is otherwise healthy, so it is
    reported as not_applicable instead. When the capability cannot be
    determined, assume it exists and keep the strict reading.
    """
    try:
        with app.connection_for_read() as connection:
            return bool(getattr(connection.default_channel, "supports_fanout", True))
    except Exception:  # noqa: BLE001 - transports expose backend-specific exceptions
        return True


def collect_worker_readiness_checks(
    replies: Sequence[dict[str, object]],
    *,
    remote_control: bool = True,
) -> tuple[ReadinessCheck, ReadinessCheck, ReadinessCheck]:
    if not remote_control:
        return (
            _not_applicable(
                "runner_heartbeat",
                "remote_control_unsupported",
                ("runner",),
                "The configured broker cannot carry Celery remote-control replies.",
            ),
            _not_applicable(
                "analysis_heartbeat",
                "remote_control_unsupported",
                ("analysis",),
                "The configured broker cannot carry Celery remote-control replies.",
            ),
            _not_applicable(
                "privileged_helper_sudo_runner",
                "remote_control_unsupported",
                ("runner",),
                "Runner sudo coverage was not evaluated because the broker cannot carry control replies.",
            ),
        )
    payloads: dict[str, dict[str, object]] = {}
    for reply in replies:
        if not isinstance(reply, dict):
            continue
        for payload in reply.values():
            if not isinstance(payload, dict):
                continue
            service_identity = payload.get("service_identity")
            if service_identity in {"runner", "analysis"} and service_identity not in payloads:
                payloads[service_identity] = payload

    runner_payload = payloads.get("runner")
    analysis_heartbeat = (
        _passed("analysis_heartbeat", ("analysis",), "The analysis worker returned a readiness heartbeat.")
        if "analysis" in payloads
        else _failed(
            "analysis_heartbeat",
            "analysis_heartbeat_missing",
            ("analysis",),
            "No analysis-worker readiness reply was received.",
        )
    )

    if runner_payload is None:
        return (
            _failed(
                "runner_heartbeat",
                "runner_heartbeat_missing",
                ("runner",),
                "No runner-owned readiness reply was received.",
            ),
            analysis_heartbeat,
            _not_applicable(
                "privileged_helper_sudo_runner",
                "runner_report_unavailable",
                ("runner",),
                "Runner sudo coverage was not evaluated because no runner report was received.",
            ),
        )

    heartbeat = _passed("runner_heartbeat", ("runner",), "The runner returned a readiness heartbeat.")
    helper_payload = runner_payload.get("helper_sudo")
    if not isinstance(helper_payload, dict):
        return (
            heartbeat,
            analysis_heartbeat,
            _failed(
                "privileged_helper_sudo_runner",
                "runner_readiness_report_invalid",
                ("runner",),
                "The runner readiness report omitted privileged-helper sudo coverage.",
            ),
        )

    remote_status = helper_payload.get("status")
    remote_code = helper_payload.get("code")
    if remote_status == "passed" and remote_code == "ready":
        helper_check = _passed(
            "privileged_helper_sudo_runner",
            ("runner",),
            "The runner reported passwordless sudo coverage for every privileged helper.",
        )
    elif remote_status == "not_applicable" and remote_code == "fake_libvirt_active":
        helper_check = _not_applicable(
            "privileged_helper_sudo_runner",
            "fake_libvirt_active",
            ("runner",),
            "Runner helper sudo is not applicable while fake libvirt is active.",
        )
    elif remote_status == "failed" and remote_code == "privileged_helper_sudo_unavailable":
        helper_check = _failed(
            "privileged_helper_sudo_runner",
            "privileged_helper_sudo_unavailable",
            ("runner",),
            "The runner cannot invoke every privileged helper through passwordless sudo.",
        )
    elif remote_status == "failed" and remote_code == "runner_readiness_probe_in_progress":
        helper_check = _failed(
            "privileged_helper_sudo_runner",
            "runner_readiness_probe_in_progress",
            ("runner",),
            "The runner privileged-helper readiness probe is already in progress.",
        )
    else:
        helper_check = _failed(
            "privileged_helper_sudo_runner",
            "runner_readiness_report_invalid",
            ("runner",),
            "The runner returned an invalid privileged-helper sudo result.",
        )
    return heartbeat, analysis_heartbeat, helper_check


def map_runner_libvirt_check(replies: Sequence[dict[str, object]]) -> ReadinessCheck:
    runner_payload: dict[str, object] | None = None
    for reply in replies:
        if not isinstance(reply, dict):
            continue
        for payload in reply.values():
            if isinstance(payload, dict) and payload.get("service_identity") == "runner":
                runner_payload = payload
                break
        if runner_payload is not None:
            break
    if runner_payload is None:
        return _not_applicable(
            "libvirt_connection",
            "runner_report_unavailable",
            ("runner",),
            "Libvirt was not evaluated because no runner report was received.",
        )
    payload = runner_payload.get("libvirt_connection")
    if not isinstance(payload, dict):
        return _failed(
            "libvirt_connection",
            "runner_readiness_report_invalid",
            ("runner",),
            "The runner readiness report omitted the libvirt connection result.",
        )
    status = payload.get("status")
    code = payload.get("code")
    if status == "passed" and code == "ready":
        return _passed("libvirt_connection", ("runner",), "The runner opened the configured libvirt connection.")
    if status == "not_applicable" and code == "fake_libvirt_active":
        return _not_applicable(
            "libvirt_connection",
            "fake_libvirt_active",
            ("runner",),
            "Libvirt is not applicable while fake mode is active.",
        )
    if status == "failed" and code == "runner_readiness_probe_in_progress":
        return _failed(
            "libvirt_connection",
            "runner_readiness_probe_in_progress",
            ("runner",),
            "The runner libvirt readiness probe is already in progress.",
        )
    if status == "failed" and code == "libvirt_connection_unavailable":
        return _failed(
            "libvirt_connection",
            "libvirt_connection_unavailable",
            ("runner",),
            "The runner cannot open the configured libvirt connection.",
        )
    return _failed(
        "libvirt_connection",
        "runner_readiness_report_invalid",
        ("runner",),
        "The runner returned an invalid libvirt readiness result.",
    )


def check_libvirt_connection(
    uri: str,
    *,
    libvirt_module: Any | None = None,
    fake_libvirt: bool | None = None,
) -> ReadinessCheck:
    if fake_libvirt is None:
        fake_libvirt = fake_mode.FAKE_LIBVIRT
    if fake_libvirt:
        return _not_applicable(
            "libvirt_connection",
            "fake_libvirt_active",
            ("runner",),
            "A real libvirt connection is not applicable while fake libvirt is active.",
        )
    try:
        if libvirt_module is None:
            import libvirt

            active_libvirt_module = libvirt
        else:
            active_libvirt_module = libvirt_module
        connection = active_libvirt_module.open(uri)
        if connection is None:
            raise RuntimeError("libvirt returned no connection")
        connection.close()
    except Exception:  # noqa: BLE001 - libvirtError is optional in hermetic test environments
        return _failed(
            "libvirt_connection",
            "libvirt_connection_unavailable",
            ("runner",),
            "The configured libvirt connection is unavailable.",
        )
    return _passed("libvirt_connection", ("runner",), "The configured libvirt connection opened successfully.")


def check_storage_write(storage_dir: Path) -> ReadinessCheck:
    probe_path: Path | None = None
    try:
        storage_dir = Path(storage_dir)
        with tempfile.NamedTemporaryFile(prefix=".astinuslab-readiness-", dir=storage_dir, delete=False) as probe:
            probe_path = Path(probe.name)
            probe.write(b"readiness\n")
            probe.flush()
            os.fsync(probe.fileno())
        probe_path.unlink()
    except (OSError, ValueError):
        if probe_path is not None:
            try:
                probe_path.unlink(missing_ok=True)
            except OSError:
                pass
        return _failed(
            "storage_write",
            "storage_not_writable",
            ("api", "runner", "analysis"),
            "The configured storage directory cannot create, sync, and remove a probe file.",
        )
    return _passed(
        "storage_write",
        ("api", "runner", "analysis"),
        "The configured storage directory passed a create, sync, and remove probe.",
    )


def check_storage_free_space(
    storage_dir: Path,
    *,
    minimum_free_bytes: int,
    disk_usage_fn: Callable[[Path], Any] = shutil.disk_usage,
) -> ReadinessCheck:
    try:
        free_bytes = int(disk_usage_fn(Path(storage_dir)).free)
    except (OSError, TypeError, ValueError):
        return _failed(
            "storage_free_space",
            "storage_free_space_unavailable",
            ("runner", "analysis"),
            "Free space for the configured storage directory could not be inspected.",
        )
    if free_bytes < minimum_free_bytes:
        return _failed(
            "storage_free_space",
            "storage_free_space_below_guardrail",
            ("runner", "analysis"),
            "Free storage is below the configured job guardrail.",
        )
    return _passed(
        "storage_free_space",
        ("runner", "analysis"),
        "Free storage satisfies the configured job guardrail.",
    )


def _root_policy_status(settings_obj: Settings) -> dict[str, str]:
    """Storage first, then the Playground directories, which are separate settings."""
    status = host_script_manifest.check_acl_policy_storage_root(settings_obj)
    if status.get("code") != "ready":
        return status
    return host_script_manifest.check_acl_policy_playground_dirs(settings_obj)


def map_root_policy_check(policy: dict[str, str]) -> ReadinessCheck:
    code = policy.get("code", "acl_policy_unavailable")
    if code == "ready":
        return _passed("root_policy", ("runner",), "The installed root policy covers configured storage.")
    if code == "not_applicable":
        return _not_applicable(
            "root_policy",
            "fake_libvirt_active",
            ("runner",),
            "The privileged root policy is not applicable while fake libvirt is active.",
        )
    return _failed(
        "root_policy",
        code,
        ("runner",),
        "The installed privileged-root policy does not cover configured storage.",
    )


def check_privileged_helper_integrity(
    helper_dir: Path = PRIVILEGED_HELPER_DIR,
    helper_names: Iterable[str] = _ALLOWED_HELPERS,
    *,
    expected_uid: int = 0,
    expected_gid: int = 0,
) -> ReadinessCheck:
    for name in sorted(helper_names):
        path = Path(helper_dir) / name
        try:
            info = path.lstat()
        except OSError:
            return _failed(
                "privileged_helper_integrity",
                "privileged_helper_missing",
                ("runner",),
                "An installed privileged helper is missing.",
            )
        if not stat.S_ISREG(info.st_mode):
            return _failed(
                "privileged_helper_integrity",
                "privileged_helper_invalid_type",
                ("runner",),
                "An installed privileged helper is not a regular file.",
            )
        if info.st_uid != expected_uid or info.st_gid != expected_gid:
            return _failed(
                "privileged_helper_integrity",
                "privileged_helper_owner_mismatch",
                ("runner",),
                "An installed privileged helper is not owned by root:root.",
            )
        if stat.S_IMODE(info.st_mode) != 0o755:
            return _failed(
                "privileged_helper_integrity",
                "privileged_helper_mode_mismatch",
                ("runner",),
                "An installed privileged helper does not have mode 0755.",
            )
    return _passed(
        "privileged_helper_integrity",
        ("runner",),
        "Every installed privileged helper is a root-owned regular file with mode 0755.",
    )


def check_api_privileged_helper_sudo() -> ReadinessCheck:
    """The API has NoNewPrivileges=true, so sudo is categorically not probed."""
    return _not_applicable(
        "privileged_helper_sudo_api",
        "not_applicable",
        ("api",),
        "Privileged-helper sudo coverage does not apply to the API service identity.",
    )


def check_source_non_writability(
    source_paths: Iterable[Path],
    *,
    install_root: Path | None = None,
) -> ReadinessCheck:
    if install_root is not None:
        root = Path(install_root)
        try:
            if not root.exists():
                return _failed(
                    "source_non_writability",
                    "application_source_missing",
                    ("api", "runner", "analysis"),
                    "The application install root is missing.",
                )
            if os.access(root, os.W_OK):
                return _failed(
                    "source_non_writability",
                    "application_source_writable",
                    ("api", "runner", "analysis"),
                    "The application install root is writable by the runtime identity.",
                )
        except OSError:
            return _failed(
                "source_non_writability",
                "application_source_unreadable",
                ("api", "runner", "analysis"),
                "Application install-root permissions could not be inspected by the runtime identity.",
            )

    for configured_path in source_paths:
        path = Path(configured_path)
        try:
            if not path.exists():
                return _failed(
                    "source_non_writability",
                    "application_source_missing",
                    ("api", "runner", "analysis"),
                    "A required application source path is missing.",
                )
            candidates = path.rglob("*") if path.is_dir() else ()
            if os.access(path, os.W_OK) or any(os.access(candidate, os.W_OK) for candidate in candidates):
                return _failed(
                    "source_non_writability",
                    "application_source_writable",
                    ("api", "runner", "analysis"),
                    "Application source or its virtual environment is writable by the runtime identity.",
                )
        except OSError:
            return _failed(
                "source_non_writability",
                "application_source_unreadable",
                ("api", "runner", "analysis"),
                "Application source permissions could not be inspected by the runtime identity.",
            )
    return _passed(
        "source_non_writability",
        ("api", "runner", "analysis"),
        "Application source and its virtual environment are not writable by the runtime identity.",
    )


def map_volatility_checks(snapshot: Vol3Readiness) -> tuple[ReadinessCheck, ReadinessCheck]:
    if not snapshot.enabled:
        return (
            _not_applicable(
                "volatility_analysis",
                "feature_disabled",
                ("runner", "analysis"),
                "Volatility analysis is disabled by configuration.",
            ),
            _not_applicable(
                "volatility_isf_build",
                "feature_disabled",
                ("runner",),
                "Volatility ISF building is disabled by configuration.",
            ),
        )

    analysis = (
        _passed("volatility_analysis", ("runner", "analysis"), "Volatility analysis tooling is ready.")
        if snapshot.analysis.ready
        else _failed(
            "volatility_analysis",
            snapshot.analysis.code,
            ("runner", "analysis"),
            "Volatility analysis tooling is not ready.",
        )
    )
    isf_build = (
        _passed("volatility_isf_build", ("runner",), "Volatility ISF-build tooling is ready.")
        if snapshot.isf_build.ready
        else _failed(
            "volatility_isf_build",
            snapshot.isf_build.code,
            ("runner",),
            "Volatility ISF-build tooling is not ready.",
        )
    )
    return analysis, isf_build


def check_deep_volatility_readiness(settings_obj: Settings) -> tuple[ReadinessCheck, ReadinessCheck]:
    snapshot = get_vol3_readiness(settings_obj, volatility_timeout_seconds=3.0)
    return map_volatility_checks(snapshot)


def check_fake_mode() -> ReadinessCheck:
    if fake_mode.fake_mode_active():
        return _failed(
            "fake_mode",
            "fake_mode_active",
            ("api", "runner", "analysis"),
            "A virtualization test double is active; generated artifacts are synthetic.",
        )
    return _passed(
        "fake_mode",
        ("api", "runner", "analysis"),
        "No virtualization test double is active.",
    )


def _source_paths(settings_obj: Settings) -> tuple[Path, ...]:
    base_dir = Path(settings_obj.base_dir)
    paths = [
        *(base_dir / name for name in ("src", "ops", "guest", ".venv")),
        base_dir / "pyproject.toml",
        *sorted(base_dir.glob("requirements*.lock")),
    ]
    # Older layouts kept these scripts at the root; current releases ship them
    # under ops. Protect any retained legacy tree without requiring it to exist.
    paths.extend(path for name in ("tools", "deploy") if (path := base_dir / name).exists() or path.is_symlink())
    return tuple(paths)


def _storage_guardrail_bytes() -> int | None:
    try:
        app_settings = load_app_settings()
        minimum_gib = int(app_settings.get("disk_guardrail", {}).get("min_free_gib", 0) or 0)
    except Exception:  # noqa: BLE001 - DB/driver failures become a stable guardrail readiness code
        return None
    return minimum_gib * 1024**3


def collect_readiness(settings_obj: Settings = settings) -> ReadinessReport:
    """Run every deep probe in stable response order."""
    worker_replies = probe_worker_readiness()
    checks: list[ReadinessCheck] = [
        check_database_transaction(),
        check_celery_broker(),
    ]
    checks.extend(collect_worker_readiness_checks(worker_replies, remote_control=remote_control_available()))
    checks.append(map_runner_libvirt_check(worker_replies))
    checks.extend((check_storage_write(settings_obj.storage_dir),))

    minimum_free_bytes = _storage_guardrail_bytes()
    if minimum_free_bytes is None:
        checks.append(
            _failed(
                "storage_free_space",
                "storage_guardrail_unavailable",
                ("runner", "analysis"),
                "The configured storage guardrail could not be read.",
            )
        )
    else:
        checks.append(check_storage_free_space(settings_obj.storage_dir, minimum_free_bytes=minimum_free_bytes))

    checks.append(map_root_policy_check(_root_policy_status(settings_obj)))
    if fake_mode.FAKE_LIBVIRT:
        checks.append(
            _not_applicable(
                "privileged_helper_integrity",
                "fake_libvirt_active",
                ("runner",),
                "Privileged helpers are not applicable while fake libvirt is active.",
            )
        )
    else:
        checks.append(check_privileged_helper_integrity())
    checks.append(check_api_privileged_helper_sudo())
    checks.append(
        check_source_non_writability(
            _source_paths(settings_obj),
            install_root=Path(settings_obj.base_dir),
        )
    )
    checks.extend(check_deep_volatility_readiness(settings_obj))
    checks.append(check_fake_mode())
    return ReadinessReport(checks=tuple(checks))


async def collect_readiness_with_deadline(
    collector: Callable[[], ReadinessReport] | None = None,
    *,
    timeout_seconds: float = _DEEP_READINESS_TIMEOUT_SECONDS,
) -> ReadinessReport:
    """Bound the full serial probe set so the operator endpoint cannot hang."""
    active_collector = collector if collector is not None else collect_readiness

    def timeout_report() -> ReadinessReport:
        return ReadinessReport(
            checks=(
                _failed(
                    "deep_readiness_deadline",
                    "deep_readiness_timeout",
                    ("api", "runner", "analysis"),
                    "Deep readiness exceeded its response deadline.",
                ),
            )
        )

    if not _READINESS_SLOT.acquire(blocking=False):
        return timeout_report()

    def run_probe() -> ReadinessReport:
        try:
            return active_collector()
        finally:
            _READINESS_SLOT.release()

    try:
        loop = asyncio.get_running_loop()
        try:
            probe = loop.run_in_executor(_READINESS_EXECUTOR, run_probe)
        except Exception:
            _READINESS_SLOT.release()
            raise
        return await asyncio.wait_for(probe, timeout=timeout_seconds)
    except TimeoutError:
        return timeout_report()


router = APIRouter(prefix="/api/v1", tags=["readiness"])


@router.get("/readiness", responses={503: {"description": "One or more applicable deep-readiness checks failed."}})
async def deep_readiness() -> JSONResponse:
    report = await collect_readiness_with_deadline()
    return JSONResponse(status_code=200 if report.ready else 503, content=report.public_dict())
