"""Service-layer operations for forensic capture and Volatility 3 analysis.

Serializes manual capture requests and queues Volatility 3 work against completed
dumps. Shares ``_dispatch_action_after_commit`` with ``job_services`` (forensic
depends on job, never the reverse). See ``job_services`` for job lifecycle.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..api.errors import ApiError
from ..core.config import settings
from ..core.locks import CaptureLockBusy, capture_lock
from ..db.repositories import JobRepository
from ..db.transactions import reserve_sqlite_write
from ..jobs.job_services import _VALID_CAPTURE_TYPES, _dispatch_action_after_commit
from ..jobs.job_status import JobStatus
from .analysis_input import (
    SOURCE_SNAPSHOT_KEY,
    AnalysisInputError,
    SourceSnapshot,
    select_source_artifact,
)
from .vol3_tools import check_volatility_tool

logger = logging.getLogger(__name__)


def _require_volatility_readiness() -> None:
    status = check_volatility_tool(settings)
    if not status.ready:
        raise ApiError(
            503,
            status.detail,
            "dependency_unavailable",
            {"dependency_code": status.code},
        )


def _forensic_enqueue_lock_dir() -> Path:
    # One directory shared with jobs.job_services.runtime_intent_lock_dir, so a
    # capture request and a runtime-control request contend for the same
    # per-Job lock instead of only appearing to.
    from ..jobs.job_services import runtime_intent_lock_dir

    return runtime_intent_lock_dir()


def enqueue_acquire_memory_dump_service(
    session: Session,
    job_id: str,
    exploit_tags: list[str] | None = None,
    exploit_notes: str | None = None,
    capture_type: str | None = None,
):
    """Serialize manual capture requests so only one forensic acquisition mutates a live job at a time."""
    try:
        with capture_lock(job_id, base_dir=_forensic_enqueue_lock_dir()):
            return _enqueue_acquire_memory_dump_service_locked(
                session,
                job_id,
                exploit_tags=exploit_tags,
                exploit_notes=exploit_notes,
                capture_type=capture_type,
            )
    except CaptureLockBusy as exc:
        raise ApiError(409, "memory dump acquisition is already queued or running", "conflict") from exc


def _enqueue_acquire_memory_dump_service_locked(
    session: Session,
    job_id: str,
    exploit_tags: list[str] | None = None,
    exploit_notes: str | None = None,
    capture_type: str | None = None,
):
    """Perform capture validation and action enqueueing while the job-scoped acquisition lock is held."""
    repo = JobRepository(session)
    job = repo.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != JobStatus.WAITING_FOR_CAPTURE:
        raise ApiError(409, "job is not in waiting_for_capture state", "conflict")
    requested_capture_type = capture_type or job.capture_type or "memory"
    if requested_capture_type not in _VALID_CAPTURE_TYPES:
        raise HTTPException(status_code=400, detail="capture_type must be memory, disk, both, or none")
    target_runtime = repo.get_runtime(job_id, "target")
    if (
        target_runtime is not None
        and target_runtime.power_state == "off"
        and requested_capture_type in {"memory", "both"}
    ):
        raise ApiError(409, "target VM is powered off; memory capture is unavailable", "conflict")
    for action in repo.list_actions(job_id):
        if action.status in {"queued", "running"} and action.action_type == "acquire_memory_dump":
            raise ApiError(409, "memory dump acquisition is already queued or running", "conflict")

    extra_fields: dict = {
        "capture_type": requested_capture_type,
    }
    if exploit_tags:
        extra_fields["exploit_tags"] = ",".join(t.strip() for t in exploit_tags if t.strip())
    if exploit_notes and exploit_notes.strip():
        extra_fields["exploit_notes"] = exploit_notes.strip()

    repo.transition_job_status(job_id, JobStatus.CAPTURE_REQUESTED, **extra_fields)
    action_payload: dict = {"capture_type": requested_capture_type}
    action = repo.enqueue_action(
        job_id,
        "acquire_memory_dump",
        action_payload,
    )
    tag_info = f" [tags: {extra_fields.get('exploit_tags', '')}]" if "exploit_tags" in extra_fields else ""
    repo.add_event(job_id, "capture_requested", f"Manual capture request received{tag_info}")
    repo.add_event(job_id, "action_queued", "Host action queued: acquire_memory_dump")
    logger.info("Capture triggered for job %s%s", job_id, tag_info)

    _dispatch_action_after_commit(
        session,
        repo,
        job_id=job_id,
        action_id=action.id,
        action_type="acquire_memory_dump",
    )

    return repo.get_job(job_id)


def trigger_capture_service(
    session: Session,
    job_id: str,
    exploit_tags: list[str] | None = None,
    exploit_notes: str | None = None,
    capture_type: str | None = None,
):
    """Record operator capture intent for manual-trigger jobs and move waiting jobs into capture_requested."""
    return enqueue_acquire_memory_dump_service(
        session,
        job_id,
        exploit_tags=exploit_tags,
        exploit_notes=exploit_notes,
        capture_type=capture_type,
    )


def enqueue_vol3_analysis_service(session: Session, job_id: str):
    """Queue a Volatility 3 analysis action for an existing memory dump without mutating capture artifacts."""
    reserve_sqlite_write(session)
    repo = JobRepository(session)
    job = repo.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.execution_mode == "artifact_analysis":
        raise ApiError(409, "Use the interactive workbench for artifact analysis", "conflict")
    if job.status != JobStatus.COMPLETED:
        raise ApiError(409, "Vol3 analysis requires a completed job", "conflict")
    if not job.dump_path:
        raise ApiError(409, "Vol3 analysis requires a completed memory dump", "conflict")
    _require_volatility_readiness()
    from .vol3_analysis import effective_plugins_for_os
    from .vol3_plugins import validate_plugin_names
    from .vol3_support import os_family_for_distro

    os_family = os_family_for_distro(job.detected_distro)
    plugins = effective_plugins_for_os(os_family, settings.vol3_default_plugins)
    if not plugins:
        raise ApiError(
            400,
            "Vol3 analysis requires at least one configured plugin",
            "invalid_request",
        )
    unknown_plugins = validate_plugin_names(os_family, plugins)
    if unknown_plugins:
        raise ApiError(
            400,
            f"Unknown {os_family} Volatility plugins: {', '.join(unknown_plugins)}",
            "invalid_request",
            {"plugins": unknown_plugins},
        )
    for action in repo.list_actions(job_id):
        if action.action_type == "run_vol3_analysis" and action.status in {"queued", "running"}:
            raise ApiError(409, "Vol3 analysis is already queued or running", "conflict")

    try:
        source = select_source_artifact(session, job)
    except AnalysisInputError as exc:
        raise ApiError(409, exc.detail, "conflict") from exc
    snapshot = SourceSnapshot.of(source)
    action = repo.enqueue_action(
        job_id,
        "run_vol3_analysis",
        {SOURCE_SNAPSHOT_KEY: snapshot.to_payload()},
        source_artifact_id=source.id,
    )
    repo.add_event(job_id, "vol3_queued", "Volatility 3 plugin analysis queued")
    _dispatch_action_after_commit(
        session,
        repo,
        job_id=job_id,
        action_id=action.id,
        action_type="run_vol3_analysis",
    )
    return repo.get_action(action.id)


def enqueue_vol3_plugin_run_service(session: Session, job_id: str, plugin: str, yara_rules: str | None = None) -> dict:
    """Queue an on-demand single-plugin Volatility 3 run against a completed dump.

    Returns ``{run_id, action_id, status}``. The same plugin cannot be queued
    twice concurrently for one dump.
    """
    from .vol3_plugins import is_yara_plugin, validate_plugin_names
    from .vol3_support import os_family_for_distro

    reserve_sqlite_write(session)
    repo = JobRepository(session)
    job = repo.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != JobStatus.COMPLETED:
        raise ApiError(409, "Vol3 analysis requires a completed job", "conflict")
    if not job.dump_path:
        raise ApiError(409, "Vol3 analysis requires a completed memory dump", "conflict")
    if job.execution_mode == "artifact_analysis":
        from .artifact_context import validate_analysis_context

        validate_analysis_context(session, job, require_symbols=True)
    # Cheap request validation first.  Readiness probing spawns Volatility, which
    # loads the whole framework, so a malformed request must never pay for it.
    plugin = (plugin or "").strip()
    os_family = os_family_for_distro(job.detected_distro)
    unknown_plugins = validate_plugin_names(os_family, [plugin])
    if unknown_plugins:
        raise ApiError(
            400,
            f"Unknown {os_family} Volatility plugins: {', '.join(unknown_plugins)}",
            "invalid_request",
            {"plugins": unknown_plugins},
        )

    yara_rules = (yara_rules or "").strip()
    if is_yara_plugin(plugin):
        if not yara_rules:
            raise HTTPException(status_code=400, detail="yarascan requires YARA rules")
        if len(yara_rules) > 65536:
            raise HTTPException(status_code=400, detail="YARA rules too large (max 64 KiB)")
    elif yara_rules:
        raise HTTPException(status_code=400, detail=f"{plugin} does not accept YARA rules")

    _require_volatility_readiness()

    for action in repo.list_actions(job_id):
        if action.action_type == "run_vol3_plugin" and action.status in {"queued", "running"}:
            if json.loads(action.payload or "{}").get("plugin") == plugin:
                raise ApiError(409, "This plugin is already queued or running for this dump", "conflict")

    try:
        source = select_source_artifact(session, job)
    except AnalysisInputError as exc:
        raise ApiError(409, exc.detail, "conflict") from exc
    run_id = uuid4().hex[:8]
    payload: dict[str, Any] = {
        "plugin": plugin,
        "run_id": run_id,
        **({"yara_rules": yara_rules} if yara_rules else {}),
        SOURCE_SNAPSHOT_KEY: SourceSnapshot.of(source).to_payload(),
    }
    if job.execution_mode == "artifact_analysis":
        payload["source_artifact_id"] = job.source_artifact_id
    action = repo.enqueue_action(job_id, "run_vol3_plugin", payload=payload, source_artifact_id=source.id)
    repo.add_event(job_id, "vol3_plugin_queued", f"Volatility 3 plugin queued: {plugin}")
    _dispatch_action_after_commit(
        session,
        repo,
        job_id=job_id,
        action_id=action.id,
        action_type="run_vol3_plugin",
    )
    return {"run_id": run_id, "action_id": action.id, "status": "queued"}


def _vol3_os_label(distro: str | None, version: str | None) -> str:
    name = (distro or "").strip()
    label = name.capitalize() if name else "Unknown"
    version_str = (version or "").strip()
    return f"{label} {version_str}".strip()


def list_vol3_dumps_service(session: Session) -> list[dict]:
    """List completed memory-capture jobs available for Volatility 3 analysis.

    Size is returned as raw bytes (``size_bytes``) so the presentation layer can
    format it; records are sorted newest-first by capture time.
    """
    from .vol3_support import os_family_for_distro

    repo = JobRepository(session)
    jobs = repo.list_jobs()
    repo.prefetch_latest_artifacts(jobs)
    dumps: list[dict] = []
    for job in jobs:
        if job.status != JobStatus.COMPLETED or not job.dump_path:
            continue
        if not Path(job.dump_path).is_file():
            continue
        os_family = os_family_for_distro(job.detected_distro)
        kernel = job.detected_kernel_release or ""
        architecture = job.detected_architecture or "x86_64"
        if os_family == "windows":
            symbols, sym_label = "ready", "PDB auto"
        elif repo.find_valid_isf_by_kernel(kernel, architecture=architecture) is not None:
            symbols = "ready"
            sym_label = f"ISF {kernel.split('-')[0]}" if kernel else "ISF ready"
        else:
            symbols, sym_label = "missing", "ISF needed"
        dumps.append(
            {
                "job_id": job.id,
                "file": job.dump_filename or job.dump_path.rsplit("/", 1)[-1],
                "os_family": os_family,
                "os_label": _vol3_os_label(job.detected_distro, job.detected_version),
                "kernel": kernel or "—",
                "source": job.target_id,
                "size_bytes": job.dump_size_bytes,
                "captured": job.finished_at.isoformat() if job.finished_at else None,
                "symbols": symbols,
                "sym_label": sym_label,
            }
        )
    from .artifact_context import playground_dumps

    dumps.extend(playground_dumps(session))
    dumps.sort(key=lambda d: d["captured"] or "", reverse=True)
    return dumps
