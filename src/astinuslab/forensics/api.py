"""Versioned Volatility JSON API endpoints."""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..core.config import settings
from ..db import session_scope
from ..db.models import EvidenceTombstone
from ..db.repositories import ArtifactRepository, JobRepository
from . import vol3_plugins
from .services import enqueue_vol3_plugin_run_service, list_vol3_dumps_service
from .vol3_tools import get_vol3_readiness

router = APIRouter(prefix="/api/v1/volatility", tags=["volatility"])


class Vol3ToolReadinessResponse(BaseModel):
    ready: bool
    code: str
    detail: str
    version: str | None
    # True only for an analysis tool at or above the supported minimum but outside
    # the versions this release was tested against: it runs, with results unvalidated.
    untested: bool


class Vol3ReadinessResponse(BaseModel):
    enabled: bool
    analysis: Vol3ToolReadinessResponse
    isf_build: Vol3ToolReadinessResponse


@router.get("/readiness", response_model=Vol3ReadinessResponse)
def volatility_api_readiness():
    """Return the safe public readiness state for operator-supplied tools."""
    return get_vol3_readiness().public_dict()


def _human_size(size: int | None) -> str:
    if not size:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


@router.get("/dumps")
def volatility_api_dumps():
    """Return memory dumps available for analysis, with human-formatted sizes."""
    with session_scope() as session:
        dumps = list_vol3_dumps_service(session)
    for dump in dumps:
        size_bytes = dump.get("size_bytes")
        dump["size"] = _human_size(size_bytes) if size_bytes else "—"
    return {"dumps": dumps}


class AnalysisContextRequest(BaseModel):
    artifact_id: str = Field(max_length=64)
    kernel_release: str = Field(max_length=255)
    architecture: str = Field(max_length=64)
    verified: bool
    provenance: str = Field(max_length=2048)


@router.post("/analysis-contexts")
def volatility_api_analysis_context(body: AnalysisContextRequest):
    from .artifact_context import create_analysis_context, playground_dumps

    with session_scope() as session:
        create_analysis_context(session, **body.model_dump())
        row = next(row for row in playground_dumps(session) if row["artifact_id"] == body.artifact_id)
        row["size"] = _human_size(row["size_bytes"])
        return row


@router.get("/catalog")
def volatility_api_catalog(os: str = "linux"):
    """Return the categorized Volatility 3 plugin catalog for an OS family."""
    return vol3_plugins.catalog_for_os(os)


def _failure_overlay(runs_dir: Path, run_id: str, result: dict) -> dict:
    """Publication diagnostics alter presentation, never hashed report bytes."""
    failure_file = runs_dir / f"{run_id}.failure.json"
    if failure_file.exists() and result.get("status") != "deleted":
        try:
            failure = json.loads(failure_file.read_text(encoding="utf-8"))
            error = failure.get("error") or "Vol3 result publication failed"
        except (OSError, ValueError, AttributeError):
            error = "Vol3 result publication diagnostic is unreadable"
        return {**result, "status": "failed", "error": error}
    return result


def _deleted_run(session, runs_dir: Path, run_id: str, result: dict) -> dict | None:
    job_id = runs_dir.parent.parent.name
    tombstone = session.scalars(
        select(EvidenceTombstone)
        .where(
            EvidenceTombstone.job_id == job_id,
            EvidenceTombstone.path == str(runs_dir / f"{run_id}.json"),
            EvidenceTombstone.producer == "vol3_plugin_run",
        )
        .order_by(EvidenceTombstone.id.desc())
    ).first()
    if tombstone is None:
        return None
    return {
        "run_id": run_id,
        "job_id": job_id,
        "plugin": result.get("plugin"),
        "finished_at": result.get("finished_at"),
        "elapsed": result.get("elapsed"),
        "dump_file": result.get("dump_file"),
        "status": "deleted",
        "tombstone_id": tombstone.id,
        "message": tombstone.notes,
    }


def _owned_run_status(session, runs_dir: Path, run_id: str, result: dict) -> dict:
    result = _failure_overlay(runs_dir, run_id, result)
    if result.get("status") not in {"completed", "running", "queued"}:
        return result
    job_id = runs_dir.parent.parent.name
    repo = JobRepository(session)
    owner_id = result.get("action_id")
    candidates = [repo.get_action(owner_id)] if isinstance(owner_id, int) else repo.list_actions(job_id)
    owners = [
        action
        for action in candidates
        if action is not None
        and action.job_id == job_id
        and action.action_type == "run_vol3_plugin"
        and json.loads(action.payload or "{}").get("run_id") == run_id
    ]
    # Legacy results did not embed an owner. The earliest claim owns those
    # bytes; a rejected later run_id collision cannot relabel that history.
    owner = min(owners, key=lambda action: action.id) if owners else None
    if owner is None or owner.status == result.get("status"):
        return result
    if owner.status == "completed":
        return {**result, "status": "completed"}
    return {
        "run_id": run_id,
        "job_id": job_id,
        "action_id": owner.id,
        "plugin": result.get("plugin"),
        "dump_file": result.get("dump_file"),
        "finished_at": result.get("finished_at"),
        "elapsed": result.get("elapsed"),
        "status": owner.status,
        "error": owner.error_summary,
    }


def _recent_runs(limit: int = 20) -> list[dict]:
    """List recent plugin runs from header sidecars under storage/dumps/*/vol3/runs/.

    Reads <run_id>.meta.json when present; falls back to the full result file
    for runs recorded before meta sidecars existed.
    """
    runs_root = settings.storage_dir / "dumps"
    if not runs_root.exists():
        return []
    runs: list[dict] = []
    for run_file in runs_root.glob("*/vol3/runs/*.json"):
        name = run_file.name
        if name.endswith((".manifest.json", ".meta.json", ".config.json", ".stdout.json", ".failure.json")):
            continue
        if (run_file.parent / ".history-hidden" / run_file.stem).exists():
            continue
        meta_file = run_file.with_name(f"{run_file.stem}.meta.json")
        source = meta_file if meta_file.is_file() else run_file
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        runs.append(
            {
                "run_id": data.get("run_id"),
                "job_id": data.get("job_id"),
                "action_id": data.get("action_id"),
                "_runs_dir": run_file.parent,
                "plugin": data.get("plugin"),
                "dump_file": data.get("dump_file"),
                "status": data.get("status"),
                "elapsed": data.get("elapsed"),
                "finished_at": data.get("finished_at"),
            }
        )
    runs.sort(key=lambda run: run.get("finished_at") or "", reverse=True)
    projected = []
    with session_scope() as session:
        for run in runs[:limit]:
            runs_dir = run.pop("_runs_dir")
            run_id = run["run_id"]
            projected.append(
                _deleted_run(session, runs_dir, run_id, run) or _owned_run_status(session, runs_dir, run_id, run)
            )
    return projected


@router.get("/queue")
def volatility_api_queue(limit: int = 20):
    """Return recent Volatility 3 plugin runs across all dumps (newest first)."""
    return {"runs": _recent_runs(limit)}


@router.delete("/queue")
def volatility_api_queue_clear():
    """Hide finished activity without modifying evidence or provenance bytes.

    Mark individual results present now, never a timestamp cutoff: an in-flight
    publication that finishes later must remain visible.
    """
    runs_root = settings.storage_dir / "dumps"
    if not runs_root.exists():
        return {"deleted": 0}
    deleted = 0
    for run_file in runs_root.glob("*/vol3/runs/*.json"):
        if run_file.name.endswith((".manifest.json", ".meta.json", ".config.json", ".stdout.json", ".failure.json")):
            continue
        hidden = run_file.parent / ".history-hidden" / run_file.stem
        if hidden.exists():
            continue
        try:
            data = json.loads(run_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("status") not in {"completed", "failed", "cancelled"}:
            continue
        hidden.parent.mkdir(exist_ok=True)
        hidden.touch(exist_ok=True)
        deleted += 1
    return {"deleted": deleted}


_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PLUGIN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*){1,3}$")


class PluginRunRequest(BaseModel):
    job_id: str
    # Bounded at the schema boundary: `plugin` is reflected back into the 400
    # `detail` and into `context["plugins"]`, so an oversized or flag-shaped name
    # must be rejected by pydantic before anything echoes it.  The same pattern is
    # re-checked further in (other entry points reach the runner directly).
    plugin: str = Field(max_length=128, pattern=_PLUGIN_RE.pattern)
    yara_rules: str | None = None


@router.post("/runs")
def volatility_api_run(body: PluginRunRequest):
    """Enqueue an on-demand single-plugin run for a dump."""
    with session_scope() as session:
        return enqueue_vol3_plugin_run_service(session, body.job_id, body.plugin, yara_rules=body.yara_rules)


@router.get("/runs/latest")
def volatility_api_run_latest(job_id: str, plugin: str):
    """Return the newest attempt, including failure, pending and deletion states.

    Never fall back to an older success when the latest attempt failed or its
    evidence was deleted. Include pre-sidecar history and unmaterialized actions.
    """
    if not _PLUGIN_RE.fullmatch(plugin):
        raise HTTPException(status_code=400, detail="invalid plugin name")
    dumps_dir = (settings.storage_dir / "dumps").resolve()
    runs_dir = (dumps_dir / job_id / "vol3" / "runs").resolve()
    if runs_dir == dumps_dir or not runs_dir.is_relative_to(dumps_dir):
        raise HTTPException(status_code=404, detail="run not found")

    candidates: dict[str, str] = {}
    attempts: dict[str, dict] = {}
    for path in runs_dir.glob("*.json"):
        if not path.name.endswith(".meta.json") and path.with_name(f"{path.stem}.meta.json").exists():
            continue
        if path.name.endswith((".manifest.json", ".config.json", ".stdout.json", ".failure.json")):
            continue
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        run_id = meta.get("run_id")
        if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id) or meta.get("plugin") != plugin:
            continue
        candidates[run_id] = str(meta.get("started_at") or meta.get("finished_at") or "")
    with session_scope() as session:
        for action in JobRepository(session).list_actions(job_id):
            if action.action_type != "run_vol3_plugin":
                continue
            payload = json.loads(action.payload or "{}")
            run_id = payload.get("run_id")
            if payload.get("plugin") == plugin and isinstance(run_id, str) and _RUN_ID_RE.fullmatch(run_id):
                candidates[run_id] = action.created_at.isoformat()
                attempts[run_id] = {"action_id": action.id, "status": action.status, "error": action.error_summary}
    if not candidates:
        return {"status": "none"}
    best_run_id = max(candidates, key=lambda rid: (candidates[rid], rid))
    result = volatility_api_run_result(job_id, best_run_id)
    attempt = attempts.get(best_run_id)
    if attempt and attempt["status"] != "completed" and attempt["action_id"] != result.get("action_id"):
        # A later rejected run_id collision is a new failed attempt, not the
        # owner of the retained bytes. Latest can report that attempt while
        # direct GET continues to describe the original immutable result.
        result = {"run_id": best_run_id, "job_id": job_id, "plugin": plugin, **attempt}
    result["cached"] = True
    return result


@router.get("/runs/{job_id}/{run_id}")
def volatility_api_run_result(job_id: str, run_id: str):
    """Return a plugin run's parsed result, or its status while still in flight."""
    if not _RUN_ID_RE.fullmatch(run_id):
        raise HTTPException(status_code=404, detail="run not found")
    dumps_dir = (settings.storage_dir / "dumps").resolve()
    runs_dir = (dumps_dir / job_id / "vol3" / "runs").resolve()
    if not runs_dir.is_relative_to(dumps_dir):
        raise HTTPException(status_code=404, detail="run not found")

    with session_scope() as session:
        deleted = _deleted_run(session, runs_dir, run_id, {})
        if deleted is not None:
            return deleted

    run_file = runs_dir / f"{run_id}.json"
    if run_file.is_file():
        try:
            result = json.loads(run_file.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise HTTPException(status_code=500, detail="run result is unreadable") from exc
        with session_scope() as session:
            result = _owned_run_status(session, runs_dir, run_id, result)
            for artifact in ArtifactRepository(session).list_artifacts(job_id=job_id):
                if artifact.producer == "vol3_plugin_run" and artifact.filename == f"{run_id}.json":
                    result["artifact_id"] = artifact.id
                    break
        return result

    meta_file = runs_dir / f"{run_id}.meta.json"
    if meta_file.is_file():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        if meta.get("status") == "deleted":
            return meta

    with session_scope() as session:
        repo = JobRepository(session)
        for action in repo.list_actions(job_id):
            if action.action_type != "run_vol3_plugin":
                continue
            if json.loads(action.payload or "{}").get("run_id") == run_id:
                status = action.status if action.status in {"queued", "running"} else "failed"
                payload = json.loads(action.payload or "{}")
                return {
                    "run_id": run_id,
                    "job_id": job_id,
                    "plugin": payload.get("plugin"),
                    "status": status,
                    "error": action.error_summary if status == "failed" else None,
                }
    if meta_file.is_file():
        return {"run_id": run_id, "job_id": job_id, "status": "failed", "error": "Retained run result is missing"}
    raise HTTPException(status_code=404, detail="run not found")


def _delete_run_files(job_id: str, run_id: str, *, force: bool = False) -> bool:
    """Explicit report deletion uses the same custody checks as the Vault."""
    from ..storage.deletion import cleanup_deletions, delete_artifact

    if not _RUN_ID_RE.fullmatch(run_id):
        raise HTTPException(status_code=404, detail="run not found")
    dumps_dir = (settings.storage_dir / "dumps").resolve()
    runs_dir = (dumps_dir / job_id / "vol3" / "runs").resolve()
    if runs_dir == dumps_dir or not runs_dir.is_relative_to(dumps_dir):
        raise HTTPException(status_code=404, detail="run not found")
    run_file = runs_dir / f"{run_id}.json"
    with session_scope() as session:
        for artifact in ArtifactRepository(session).list_artifacts(job_id=job_id):
            if artifact.producer != "vol3_plugin_run" or artifact.path != str(run_file):
                continue
            outcome = delete_artifact(
                session,
                artifact,
                actor="api:volatility",
                reason="operator selected analysis report deletion",
                roots=[dumps_dir],
                force=force,
            )
            if not outcome.deleted:
                raise HTTPException(status_code=409, detail=outcome.protection_reason)
            if outcome.tombstone_id is None:
                raise RuntimeError("deleted artifact is missing its custody record")
            session.commit()
            cleanup = cleanup_deletions(session, [outcome.tombstone_id], roots=[dumps_dir])
            errors = [error for result in cleanup for error in result.errors]
            if errors:
                raise HTTPException(
                    status_code=500,
                    detail={
                        "message": "Evidence disposal recorded; file cleanup is incomplete",
                        "deleted": True,
                        "tombstone_id": outcome.tombstone_id,
                        "cleanup_errors": errors,
                    },
                )
            return True
    if run_file.exists():
        raise HTTPException(
            status_code=409, detail="Unindexed analysis output retained for reconciliation; clear history to hide it."
        )
    return False


@router.delete("/runs/{job_id}/{run_id}")
def volatility_api_run_delete(job_id: str, run_id: str, force: bool = False):
    """Delete selected report evidence with custody protection and a tombstone."""
    return {"deleted": _delete_run_files(job_id, run_id, force=force)}
