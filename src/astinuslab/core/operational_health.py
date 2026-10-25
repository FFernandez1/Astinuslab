from __future__ import annotations

import logging
import os
import pwd
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ..db import engine
from .config import settings

logger = logging.getLogger(__name__)

ReadinessStatus = Literal["ready", "degraded", "not_ready"]
CheckStatus = Literal["ok", "unavailable"]
_PROBE_TIMEOUT_SECONDS = 1.0
_COMMAND_DETAIL_LIMIT = 256


class ReadinessCheck(TypedDict):
    status: CheckStatus
    critical: bool


class ReadinessPayload(TypedDict):
    status: ReadinessStatus
    checks: dict[str, ReadinessCheck]


@dataclass(frozen=True)
class Probe:
    name: str
    critical: bool
    run: Callable[[], None]


@dataclass(frozen=True)
class ReadinessResult:
    payload: ReadinessPayload
    http_status: int


def probe_database(db_engine: Engine = engine) -> None:
    with db_engine.connect() as connection:
        connection.execute(text("SELECT 1"))


def probe_storage(storage_dir: Path = settings.storage_dir) -> None:
    tmp_dir = storage_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    probe_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".readiness-",
            dir=tmp_dir,
            delete=False,
        ) as handle:
            probe_path = Path(handle.name)
            handle.write(b"ready\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if probe_path is not None:
            probe_path.unlink(missing_ok=True)


def _pid_is_live(pid: int) -> bool:
    return pid > 0 and Path(f"/proc/{pid}").exists()


def _command_diagnostic(result: subprocess.CompletedProcess[str]) -> str:
    raw_detail = result.stderr or result.stdout
    detail = " ".join(raw_detail.split())
    if len(detail) > _COMMAND_DETAIL_LIMIT:
        detail = f"{detail[: _COMMAND_DETAIL_LIMIT - 3]}..."
    return f"returncode={result.returncode} detail={detail!r}"


def probe_service(pid_file: Path, unit: str) -> None:
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        pid = 0
    if _pid_is_live(pid):
        return
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", unit],
        capture_output=True,
        text=True,
        timeout=_PROBE_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"service unavailable {_command_diagnostic(result)}")


def probe_libvirt(uri: str) -> None:
    result = subprocess.run(
        ["virsh", "-c", uri, "uri"],
        capture_output=True,
        text=True,
        timeout=_PROBE_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0 or result.stdout.strip() != uri:
        raise RuntimeError(f"libvirt unavailable {_command_diagnostic(result)}")


def default_probes() -> tuple[Probe, ...]:
    username = pwd.getpwuid(os.geteuid()).pw_name
    return (
        Probe("database", critical=True, run=probe_database),
        Probe("storage", critical=True, run=probe_storage),
        Probe(
            "runner",
            critical=False,
            run=lambda: probe_service(
                settings.storage_dir / "runner.pid",
                f"astinuslab-runner@{username}.service",
            ),
        ),
        Probe(
            "analysis",
            critical=False,
            run=lambda: probe_service(
                settings.storage_dir / "analysis.pid",
                f"astinuslab-analysis@{username}.service",
            ),
        ),
        Probe("libvirt", critical=False, run=lambda: probe_libvirt(settings.libvirt_uri)),
    )


def collect_readiness(probes: Sequence[Probe] | None = None) -> ReadinessResult:
    selected = tuple(probes) if probes is not None else default_probes()
    checks: dict[str, ReadinessCheck] = {}
    critical_failed = False
    optional_failed = False

    for probe in selected:
        state: CheckStatus = "ok"
        try:
            probe.run()
        except Exception:
            state = "unavailable"
            critical_failed = critical_failed or probe.critical
            optional_failed = optional_failed or not probe.critical
            logger.exception(
                "readiness_probe_failed check=%s critical=%s",
                probe.name,
                probe.critical,
            )
        checks[probe.name] = {"status": state, "critical": probe.critical}

    status: ReadinessStatus
    http_status: int
    if critical_failed:
        status, http_status = "not_ready", 503
    elif optional_failed:
        status, http_status = "degraded", 200
    else:
        status, http_status = "ready", 200
    return ReadinessResult(
        payload={"status": status, "checks": checks},
        http_status=http_status,
    )
