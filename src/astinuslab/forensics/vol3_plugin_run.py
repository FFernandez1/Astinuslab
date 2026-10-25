"""On-demand single-plugin Volatility 3 runs for the analysis workbench.

Runs exactly one plugin against a completed job's memory dump using the JSON
renderer, parses the output into a column/row table, and writes a per-run result
file under ``storage/dumps/<job_id>/vol3/runs/<run_id>.json`` that the workbench
UI polls. Unlike :func:`vol3_analysis.run_vol3_analysis` (which runs a fixed
plugin set as part of the job lifecycle), this is dump-centric and interactive.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..core.config import settings
from ..core.locks import _safe_lock_name, blocking_file_lock
from ..db import session_scope
from ..db.repositories import ArtifactRepository, JobRepository, ReportRepository
from ..db.transactions import reserve_sqlite_write
from ..jobs.action_lifecycle import owned_action
from ..storage import publication
from ..storage.artifacts import (
    build_artifact_manifest,
)
from .analysis_input import (
    AnalysisInputError,
    ResolvedInput,
    pinned_source_id,
    require_unchanged_input,
    resolve_analysis_input,
    verify_source_bytes,
)
from .symbols.windows import windows_symbol_cache_dir
from .vol3_analysis import _select_isf_artifact, _stage_selected_isf, _verify_pinned_isf
from .vol3_plugins import apply_severity, is_yara_plugin, validate_plugin_names
from .vol3_provenance import execution_files, file_identity, isf_trust, tool_identity
from .vol3_support import _sha256_file, os_family_for_distro, requires_isf_build
from .vol3_tools import ToolDependencyError, require_verified_volatility

logger = logging.getLogger(__name__)

_VOL3_TIMEOUT_SECONDS = 900
# Strict shapes guard against path traversal (run_id used in a filename) and
# argv flag-smuggling (plugin passed to the vol3 CLI). The enqueue service
# already validates these, but the runner is a separate Celery entry point.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Header fields served by the /queue endpoint (see volatility_api._recent_runs).
_RUN_META_KEYS = (
    "run_id",
    "job_id",
    "action_id",
    "plugin",
    "dump_file",
    "source_artifact_id",
    "status",
    "elapsed",
    "started_at",
    "finished_at",
)
_PLUGIN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*){1,3}$")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _safe_runs_dir(job_id: str) -> Path | None:
    dumps_dir = (settings.storage_dir / "dumps").resolve()
    runs_dir = (dumps_dir / job_id / "vol3" / "runs").resolve()
    if runs_dir == dumps_dir or not runs_dir.is_relative_to(dumps_dir):
        return None
    return runs_dir


def _vol3_run_lock_path(job_id: str) -> Path:
    """Per-job lock file serializing ISF staging + vol3 execution for one job."""
    return settings.storage_dir / "tmp" / "vol3-run-locks" / f"{_safe_lock_name(job_id)}.lock"


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (str, int, float)):
        return value
    return json.dumps(value)


def _walk_item(row: Any, depth: int = 0) -> Iterator[tuple[int, dict[str, Any]]]:
    """Yield (depth, row) pairs depth-first from one vol3 JSON row."""
    if not isinstance(row, dict):
        return
    yield depth, row
    children = row.get("__children")
    if isinstance(children, list):
        for child in children:
            yield from _walk_item(child, depth + 1)


def _iter_json_array_file(path: Path) -> Iterator[Any]:
    """Stream items from a top-level JSON array without retaining full stdout."""

    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    decoder = json.JSONDecoder(parse_constant=reject_constant)
    buffer = ""
    pos = 0
    eof = False
    state = "start"
    with path.open("r", encoding="utf-8") as handle:
        while True:
            # Drop consumed bytes; memory is bounded by one item plus a chunk.
            buffer = buffer[pos:]
            pos = 0
            if not eof:
                chunk = handle.read(65536)
                buffer += chunk
                eof = not chunk
            while True:
                while pos < len(buffer) and buffer[pos] in " \t\r\n":
                    pos += 1
                if pos == len(buffer):
                    break
                char = buffer[pos]
                if state == "end":
                    raise ValueError("trailing data after JSON array")
                if state == "start":
                    if char != "[":
                        raise ValueError("expected top-level JSON array")
                    pos += 1
                    state = "first"
                    continue
                if state in {"first", "separator"} and char == "]":
                    pos += 1
                    state = "end"
                    continue
                if state == "separator":
                    if char != ",":
                        raise ValueError("expected comma between JSON array items")
                    pos += 1
                    state = "item"
                    continue
                try:
                    item, end = decoder.raw_decode(buffer, pos)
                except json.JSONDecodeError as exc:
                    if eof:
                        raise ValueError(str(exc)) from exc
                    break
                # A scalar token may be incomplete at a chunk boundary (1e3,
                # for example); require its delimiter before accepting it.
                if end == len(buffer) and not eof:
                    break
                if buffer[end : end + 1] and buffer[end] not in " \t\r\n,]":
                    if not eof and buffer[pos] in "-0123456789":
                        break
                    raise ValueError("invalid JSON value delimiter")
                yield item
                pos = end
                state = "separator"
            if eof:
                if state == "end":
                    return
                raise ValueError("empty or unterminated JSON array")


def _rows_from_items(
    items: Iterator[Any], *, max_rows: int | None = None
) -> tuple[list[str], list[list[Any]], int, bool]:
    columns: list[str] = []
    seen: set[str] = set()
    stored: list[tuple[int, dict[str, Any]]] = []
    total_rows = 0
    has_nested = False
    for item in items:
        for depth, row in _walk_item(item):
            total_rows += 1
            if depth > 0:
                has_nested = True
            for key in row:
                if key.startswith("__"):
                    continue
                if key not in seen:
                    seen.add(key)
                    columns.append(key)
            if max_rows is None or len(stored) < max_rows:
                stored.append((depth, row))
    rows = [[_cell(row.get(col)) for col in columns] for _depth, row in stored]
    if has_nested:
        rows = [[depth, *row] for (depth, _source), row in zip(stored, rows, strict=True)]
        columns = ["Depth", *columns]
    truncated = max_rows is not None and total_rows > max_rows
    return columns, rows, total_rows, truncated


def _parse_json_rows_file(path: Path, *, max_rows: int | None) -> tuple[list[str], list[list[Any]], int, bool]:
    """Parse Volatility JSON output from disk while retaining only displayed rows."""
    return _rows_from_items(_iter_json_array_file(path), max_rows=max_rows)


def _count_findings_from_file(plugin: str, columns: list[str], path: Path) -> dict[str, int]:
    findings = {"crit": 0, "warn": 0}
    data_columns = columns[1:] if columns[:1] == ["Depth"] else columns
    include_depth = data_columns is not columns
    for item in _iter_json_array_file(path):
        for depth, row in _walk_item(item):
            cells = [_cell(row.get(col)) for col in data_columns]
            if include_depth:
                cells = [depth, *cells]
            _flags, row_findings = apply_severity(plugin, columns, [cells])
            findings["crit"] += row_findings["crit"]
            findings["warn"] += row_findings["warn"]
    return findings


def _read_text_prefix(path: Path, limit: int = 4096) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit).strip()
    except OSError:
        return ""


def _finalise(job_id: str, action_id: int | None, *, success: bool, error: str | None = None) -> None:
    with session_scope() as session:
        repo = JobRepository(session)
        if repo.get_job(job_id) is None:
            logger.info("[%s] Vol3 plugin run finalise skipped; job no longer exists", job_id)
            return
        repo.add_event(
            job_id,
            "vol3_plugin_completed" if success else "vol3_plugin_failed",
            "Volatility 3 plugin run completed" if success else f"Volatility 3 plugin run failed: {error}",
        )
        if action_id is not None:
            repo.update_action(
                action_id,
                status="completed" if success else "failed",
                error_summary=None if success else str(error or "Vol3 plugin run failed"),
                finished_at=_utc_now(),
                active_pid=None,
            )


def _write_run_meta(runs_dir: Path, result: dict[str, Any], *, status: str | None = None) -> None:
    """Replace mutable presentation headers without truncating the prior one."""
    target = runs_dir / f"{result['run_id']}.meta.json"
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    meta = {key: result.get(key) for key in _RUN_META_KEYS}
    if status is not None:
        meta["status"] = status
    try:
        temporary.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(target)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            logger.warning("could not remove unfinished run header %s", temporary, exc_info=True)


def _write_result(runs_dir: Path, result: dict[str, Any]) -> None:
    runs_dir.mkdir(parents=True, exist_ok=True)
    target = (runs_dir / f"{result['run_id']}.json").resolve()
    if not target.is_relative_to(runs_dir.resolve()):
        raise ValueError("unsafe run_id path")
    target.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    # Header sidecar: lets /queue list runs without loading full row payloads.
    _write_run_meta(runs_dir, result, status="running" if result["status"] == "completed" else None)


def _record_run_report_artifact(
    *,
    job_id: str,
    run_file: Path,
    plugin: str,
    run_id: str,
    row_count: int,
    findings: dict[str, int],
    dump_path: str | None,
    dump_sha256: str | None,
    dump_sha256_at_use: str | None,
    isf_path: str | None,
    isf_sha256: str | None,
    isf_source: str | None,
    isf_pinned: bool | None = None,
    execution: dict[str, Any] | None = None,
    source_input: ResolvedInput,
    action_id: int | None,
) -> str | None:
    """Publish a completed run's result file as a first-class report artifact.

    Mirrors :func:`vol3_analysis._record_success_report_artifact` with
    ``producer="vol3_plugin_run"``. Returns the created artifact id.
    """

    source_identity: dict[str, Any] = {"artifact_id": source_input.artifact_id}
    with session_scope() as session:
        repo = JobRepository(session)
        job = repo.get_job(job_id)
        action = repo.get_action(action_id) if action_id is not None else None
        require_unchanged_input(session, job, action, source_input)
        if job is not None and job.execution_mode == "artifact_analysis":
            from .artifact_context import validate_analysis_context

            source, context = validate_analysis_context(session, job)
            if source.path != dump_path or source.sha256 != dump_sha256:
                raise ValueError("Analysis source identity changed before publication")
            acquisition = job.acquisition
            if acquisition is None:
                raise ValueError("Analysis source acquisition is missing before publication")
            source_identity = {
                "artifact_id": source.id,
                "session_id": source.session_id,
                "acquisition_id": source.acquisition_id,
                "case_id": source.case_id or acquisition.case_id,
                "analysis_context": context,
            }

    def _build_run_manifest(sha256: str, size_bytes: int) -> dict[str, Any]:
        manifest = build_artifact_manifest(
            artifact_path=run_file,
            kind="report",
            producer="vol3_plugin_run",
            sha256=sha256,
            size_bytes=size_bytes,
            job_id=job_id,
            inputs=[
                {
                    "kind": "memory",
                    **source_identity,
                    "path": dump_path,
                    "sha256": dump_sha256,
                    "sha256_at_use": dump_sha256_at_use,
                    "verified": bool(dump_sha256),
                },
                {"kind": "isf", "path": isf_path, "sha256": isf_sha256, "source": isf_source, "pinned": isf_pinned},
            ],
        )

        manifest["executions"] = [execution] if execution is not None else []
        manifest["inputs"][1]["trust"] = isf_trust(isf_source)
        return manifest

    # Volatility wrote the run file itself; the attempt adopts it so the manifest
    # and sidecar still appear as a pair and the publication is journalled.
    with publication.attempt(
        session_scope,
        producer="vol3_plugin_run",
        kind="report",
        final_path=run_file,
        staging_parent=run_file.parent,
        job_id=job_id,
    ) as attempt:
        triad = attempt.adopt(run_file, _build_run_manifest)
        published = attempt.publish_sidecars()
        sha256, size_bytes = triad.sha256, triad.size_bytes
        manifest_path, sidecar_path = published.manifest_path, published.sidecar_path

        with session_scope() as session:
            reserve_sqlite_write(session)
            artifact_repo = ArtifactRepository(session)
            report_repo = ReportRepository(session)
            repo = JobRepository(session)
            job = repo.get_job(job_id)
            action = repo.get_action(action_id) if action_id is not None else None
            require_unchanged_input(session, job, action, source_input)
            case_id = job.acquisition.case_id if job is not None and job.acquisition is not None else None
            acquisition_id = job.acquisition_id if job is not None else None
            input_artifact_id = source_identity.get("artifact_id")
            if "case_id" in source_identity:
                case_id = source_identity["case_id"]
                acquisition_id = source_identity["acquisition_id"]
            report_artifact = artifact_repo.create_artifact(
                kind="report",
                producer="vol3_plugin_run",
                path=str(run_file),
                filename=run_file.name,
                sha256=sha256,
                size_bytes=size_bytes,
                manifest_path=str(manifest_path),
                sha256_sidecar_path=str(sidecar_path),
                metadata_json=json.dumps(
                    {
                        "plugin": plugin,
                        "run_id": run_id,
                        "rows": row_count,
                        "findings": findings,
                        "isf_source": isf_source,
                    },
                    sort_keys=True,
                ),
                case_id=case_id,
                job_id=job_id,
                acquisition_id=acquisition_id,
            )
            report_repo.create_report(
                report_type="volatility3_plugin",
                status="completed",
                path=str(run_file),
                title=f"Volatility 3 — {plugin}",
                summary=f"{row_count} rows",
                metadata_json=json.dumps({"plugin": plugin, "run_id": run_id, "findings": findings}, sort_keys=True),
                case_id=report_artifact.case_id,
                job_id=job_id,
                acquisition_id=report_artifact.acquisition_id,
                input_artifact_id=input_artifact_id,
                output_artifact_id=report_artifact.id,
            )
            artifact_id = report_artifact.id
            attempt.complete(session, artifact_id=artifact_id)
        attempt.finish()
        return artifact_id


def _result(
    *,
    run_id: str,
    job_id: str,
    plugin: str,
    status: str,
    started_at: datetime,
    dump_file: str | None = None,
    input_verified: bool = False,
    isf_source: str | None = None,
    isf_pinned: bool | None = None,
    include_isf_provenance: bool = False,
    columns: list[str] | None = None,
    rows: list[list[Any]] | None = None,
    flags: list[str | None] | None = None,
    findings: dict[str, int] | None = None,
    truncated: bool = False,
    total_rows: int | None = None,
    error: str | None = None,
    execution: dict[str, Any] | None = None,
    action_id: int | None = None,
) -> dict[str, Any]:
    finished = _utc_now()
    elapsed_s = max((finished - started_at).total_seconds(), 0.0)
    result: dict[str, Any] = {
        "source_artifact_id": (execution or {}).get("inputs", {}).get("memory", {}).get("artifact_id"),
        "run_id": run_id,
        "job_id": job_id,
        "action_id": action_id,
        "plugin": plugin,
        "status": status,
        "dump_file": dump_file,
        "input_verified": input_verified,
        "columns": columns or [],
        "rows": rows or [],
        "flags": flags or [],
        "findings": findings or {"crit": 0, "warn": 0},
        "truncated": truncated,
        "total_rows": total_rows if total_rows is not None else len(rows or []),
        "summary": (
            f"{total_rows} rows (showing first {len(rows or [])})"
            if truncated and total_rows is not None
            else f"{len(rows or [])} rows"
        ),
        "started_at": started_at.isoformat(),
        "finished_at": finished.isoformat(),
        "elapsed": f"{elapsed_s:.1f}s",
    }
    if execution is not None:
        result["execution"] = execution
        result["vol3_version"] = execution.get("tool", {}).get("version")
    if error is not None:
        result["error"] = error
    if include_isf_provenance:
        result["isf_source"] = isf_source
        result["isf_trust"] = isf_trust(isf_source)
        result["isf_low_trust"] = result["isf_trust"] == "unverified"
        result["isf_pinned"] = isf_pinned
    return result


@owned_action
def run_vol3_plugin(job_id: str, action_id: int | None = None) -> tuple[bool, dict[str, Any]]:
    """Run a single Volatility 3 plugin (named in the action payload) against a dump."""
    started_at = _utc_now()
    runs_dir = _safe_runs_dir(job_id)

    # Resolve the requested plugin + run id from the action payload.
    expected_source_id = None
    plugin = ""
    yara_rules = ""
    run_id = uuid4().hex[:8]
    with session_scope() as session:
        repo = JobRepository(session)
        action = repo.get_action(action_id) if action_id is not None else None
        if action is not None:
            payload = json.loads(action.payload or "{}")
            expected_source_id = pinned_source_id(action, payload)
            plugin = str(payload.get("plugin") or "").strip()
            yara_rules = str(payload.get("yara_rules") or "")
            raw_run_id = str(payload.get("run_id") or "").strip()
            if _RUN_ID_RE.fullmatch(raw_run_id):
                run_id = raw_run_id
            repo.update_action(action.id, status="running", started_at=started_at, error_summary=None)

    execution: dict[str, Any] = (
        {"inputs": {"memory": {"artifact_id": expected_source_id}}} if expected_source_id else {}
    )
    # What the run is known to be about, filled in as each part becomes known.
    # `fail` closes over it so a failure names the evidence it was analysing:
    # without it every failed run's history row showed an em dash where the
    # dump belongs, which in a workbench about evidence identity makes a
    # session's failures indistinguishable from one another.
    identified: dict[str, Any] = {}

    def fail(error: str) -> tuple[bool, dict[str, Any]]:
        result = _result(
            run_id=run_id,
            job_id=job_id,
            plugin=plugin,
            status="failed",
            started_at=started_at,
            dump_file=identified.get("dump_file"),
            error=error,
            execution=execution,
            action_id=action_id,
        )
        if runs_dir is not None:
            _write_result(runs_dir, result)
        _finalise(job_id, action_id, success=False, error=error)
        return False, result

    if runs_dir is None:
        # Cannot write a result file safely; still finalise the action.
        _finalise(job_id, action_id, success=False, error="invalid job_id path")
        return False, _result(
            run_id=run_id,
            job_id=job_id,
            plugin=plugin,
            status="failed",
            started_at=started_at,
            error="Vol3 plugin run received an invalid job_id path",
        )

    # Reserve all paths before diagnostics, rules or raw streams are written.
    # The action claim rejects duplicate delivery; this also fences a reused
    # run_id on a different action and legacy files predating action claims.
    runs_dir.mkdir(parents=True, exist_ok=True)
    if any(runs_dir.glob(f"{run_id}.*")):
        error = "Vol3 run_id already has retained output"
        _finalise(job_id, action_id, success=False, error=error)
        return False, _result(
            run_id=run_id, job_id=job_id, plugin=plugin, status="failed", started_at=started_at, error=error
        )
    try:
        (runs_dir / f"{run_id}.work").mkdir(exist_ok=False)
    except FileExistsError:
        _finalise(job_id, action_id, success=False, error="Vol3 run_id is already reserved")
        return False, {"run_id": run_id, "status": "failed", "error": "Vol3 run_id is already reserved"}

    if not plugin:
        return fail("Vol3 plugin run requires a plugin name")
    if not _PLUGIN_RE.fullmatch(plugin):
        return fail(f"Vol3 plugin run rejected malformed plugin name: {plugin}")
    if is_yara_plugin(plugin):
        if not yara_rules.strip():
            return fail("yarascan requires YARA rules")
        if len(yara_rules) > 65536:
            return fail("YARA rules too large")
    source_identity = {}
    # Load job + dump + symbol context.
    with session_scope() as session:
        repo = JobRepository(session)
        job = repo.get_job(job_id)
        if job is None:
            return fail(f"Job {job_id} not found")
        if job.status != "completed":
            return fail("Vol3 plugin run requires a completed memory dump job")
        try:
            source_input = resolve_analysis_input(
                session, job, repo.get_action(action_id) if action_id is not None else None
            )
        except AnalysisInputError as exc:
            return fail(str(exc))
        source_identity = {"artifact_id": source_input.artifact_id}
        if job.execution_mode == "artifact_analysis":
            from fastapi import HTTPException

            from .artifact_context import validate_analysis_context

            try:
                source, context = validate_analysis_context(session, job, require_symbols=True)
            except HTTPException as exc:
                return fail(str(exc.detail))
            if not expected_source_id or source.id != expected_source_id:
                return fail("Queued source artifact identity no longer matches the analysis context")
            source_identity = {
                "artifact_id": source.id,
                "session_id": source.session_id,
                "acquisition_id": source.acquisition_id,
                "analysis_context": context,
            }
        dump_path = source_input.path
        dump_file = source_input.filename or Path(dump_path).name
        identified["dump_file"] = dump_file
        kernel_release = job.detected_kernel_release or ""
        architecture = job.detected_architecture or "x86_64"
        os_family = os_family_for_distro(job.detected_distro)
        # Same selection rules as run_vol3_analysis (build_id-first, then the
        # job's own ISF, then any valid ISF for this kernel) so batch reports
        # and interactive runs always resolve the same symbol table.
        latest_artifact = repo.get_latest_isf_artifact(job_id)
        latest_artifact_build_id = latest_artifact.build_id if latest_artifact is not None else None
        artifact = _select_isf_artifact(
            repo,
            job_id,
            kernel_release,
            architecture,
            build_id=latest_artifact_build_id,
        )
        if job.execution_mode == "artifact_analysis":
            from .artifact_context import validate_selected_isf

            try:
                validate_selected_isf(artifact, kernel_release, architecture)
            except HTTPException as exc:
                return fail(str(exc.detail))
        artifact_path = artifact.path if artifact is not None else None
        dump_sha256 = source_input.sha256
        isf_sha256 = artifact.sha256 if artifact is not None else None
        isf_source = repo.resolve_isf_origin_source(artifact) if artifact is not None else None

    unknown_plugins = validate_plugin_names(os_family, [plugin])
    if unknown_plugins:
        return fail(f"Unknown {os_family} Volatility plugin: {plugin}")
    if architecture != "x86_64":
        return fail(f"Vol3 plugin run supports x86_64 only, got {architecture}")
    if not dump_path or not Path(dump_path).is_file():
        return fail("Vol3 plugin run requires an existing completed memory dump")
    if requires_isf_build(os_family) and (artifact_path is None or not Path(artifact_path).is_file()):
        return fail("Vol3 plugin run requires a valid ISF for the job kernel")

    # Re-verify evidence hashes at use time: the recorded capture-time checksum
    # must match the bytes vol3 is about to read (fail closed on mismatch).
    try:
        dump_sha256_at_use = verify_source_bytes(source_input)
    except (AnalysisInputError, OSError) as exc:
        return fail(str(exc))
    input_verified = bool(dump_sha256)
    isf_sha256_at_use = None
    if requires_isf_build(os_family) and artifact_path:
        isf_sha256_at_use = _sha256_file(Path(artifact_path))
        if isf_sha256 and isf_sha256_at_use != isf_sha256:
            return fail(
                f"ISF hash mismatch: recorded {isf_sha256}, "
                f"computed {isf_sha256_at_use} — the symbol file changed since it was built"
            )

    # Stage symbols and run the single plugin with the JSON renderer, serialized
    # per job: staging rmtree's the shared symbols dir, so it must never overlap
    # another vol3 run for the same job.
    output_dir = runs_dir / f"{run_id}.work"
    output_dir.mkdir(parents=True, exist_ok=True)
    with blocking_file_lock(_vol3_run_lock_path(job_id)):
        # Fresh, job-local vol3 cache (shared path with run_vol3_analysis;
        # both wipe it only while holding the same per-job run lock).
        vol3_cache_dir = output_dir / "vol3-cache"
        shutil.rmtree(vol3_cache_dir, ignore_errors=True)
        vol3_cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            if requires_isf_build(os_family):
                assert artifact_path is not None  # noqa: S101  # mypy narrowing; unreachable by construction
                symbols_path = _stage_selected_isf(output_dir, Path(artifact_path))
            else:
                symbols_path = windows_symbol_cache_dir(settings.vol3_symbols_path)
                symbols_path.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001 - surface staging failures as a run error
            return fail(f"Vol3 plugin run failed to stage symbols: {exc}")

        staged_identity = None
        if requires_isf_build(os_family) and artifact_path is not None:
            staged_identity = file_identity(symbols_path / "linux" / Path(artifact_path).name)
            if staged_identity["sha256"] != isf_sha256_at_use:
                return fail("ISF changed between verification and symbol staging")

        rules_path = None
        if is_yara_plugin(plugin):
            rules_path = runs_dir / f"{run_id}.yar"
            runs_dir.mkdir(parents=True, exist_ok=True)
            rules_path.write_text(yara_rules, encoding="utf-8")

        runs_dir.mkdir(parents=True, exist_ok=True)
        config_path = runs_dir / f"{run_id}.config.json"
        config_path.unlink(missing_ok=True)  # vol3 --save-config refuses to overwrite
        try:
            # In the statement before the spawn it guards, one fresh probe per run.
            # See require_verified_volatility.
            verified = require_verified_volatility(settings)
            volatility_path = verified.path
            execution["tool"] = tool_identity(verified)
        except ToolDependencyError as exc:
            return fail(exc.detail)
        except OSError as exc:
            return fail(f"Volatility executable identity could not be read: {exc}")
        cmd = [
            str(volatility_path),
            "-r",
            "json",
            "-f",
            str(dump_path),
            "--symbol-dirs",
            str(symbols_path),
            "--cache-path",
            str(vol3_cache_dir),
            "--save-config",
            str(config_path),
        ]
        if requires_isf_build(os_family):
            assert artifact_path is not None  # noqa: S101  # narrowed before staging
            staged_isf = symbols_path / "linux" / Path(artifact_path).name
            plugin_class = plugin.rsplit(".", maxsplit=1)[-1]
            config_prefix = f"plugins.{plugin_class}.kernel.symbol_table_name"
            cmd += [
                "-e",
                f"{config_prefix}.class={json.dumps('volatility3.framework.symbols.linux.LinuxKernelIntermedSymbols')}",
                "-e",
                f"{config_prefix}.isf_url={json.dumps(staged_isf.resolve().as_uri())}",
            ]
        cmd.append(plugin)
        if rules_path is not None:
            cmd += ["--yara-file", str(rules_path)]
        execution.update(
            schema_version=1,
            argv=cmd,
            parameters={"plugin": plugin, "renderer": "json"},
            settings={
                "os_family": os_family,
                "architecture": architecture,
                "kernel_release": kernel_release,
                "max_rows": int(getattr(settings, "vol3_plugin_run_max_rows", 50000)),
                "timeout_seconds": _VOL3_TIMEOUT_SECONDS,
            },
            inputs={
                "memory": {
                    **source_identity,
                    "path": dump_path,
                    "sha256": dump_sha256,
                    "sha256_at_use": dump_sha256_at_use,
                    "integrity_verified": input_verified,
                    "acquisition_validity": "not_assessed",
                },
                "isf": {
                    "path": artifact_path,
                    "sha256": isf_sha256,
                    "sha256_at_use": isf_sha256_at_use,
                    "source": isf_source,
                    "trust": isf_trust(isf_source),
                    "staged": staged_identity,
                },
            },
        )
        stdout_path = runs_dir / f"{run_id}.stdout.json"
        stderr_path = runs_dir / f"{run_id}.stderr.log"
        stdout_path.unlink(missing_ok=True)
        stderr_path.unlink(missing_ok=True)
        try:
            with (
                stdout_path.open("w", encoding="utf-8") as stdout_file,
                stderr_path.open("w", encoding="utf-8") as stderr_file,
            ):
                proc = subprocess.run(
                    cmd,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    text=True,
                    timeout=_VOL3_TIMEOUT_SECONDS,
                )
        except subprocess.TimeoutExpired:
            execution["files"] = execution_files(stdout_path, stderr_path, config_path, rules_path)
            return fail(f"{plugin} timed out after {_VOL3_TIMEOUT_SECONDS}s")
        except Exception as exc:  # noqa: BLE001 - subprocess launch failure
            execution["files"] = execution_files(stdout_path, stderr_path, config_path, rules_path)
            return fail(f"{plugin} failed to start: {exc}")

        execution["files"] = execution_files(stdout_path, stderr_path, config_path, rules_path)
        execution["returncode"] = proc.returncode
        if proc.returncode != 0:
            detail = (_read_text_prefix(stderr_path) or _read_text_prefix(stdout_path)).strip()
            return fail(f"{plugin} failed with exit code {proc.returncode}: {detail}".rstrip(": "))

        isf_pinned: bool | None = None
        if requires_isf_build(os_family):
            isf_pinned, pin_error = _verify_pinned_isf(config_path, symbols_path)
            if pin_error is not None:
                return fail(f"{plugin}: {pin_error}")
        execution["inputs"]["isf"]["pinned"] = isf_pinned

    max_rows = int(getattr(settings, "vol3_plugin_run_max_rows", 50000))
    row_cap = max_rows if max_rows > 0 else None
    try:
        columns, rows, total_rows, truncated = _parse_json_rows_file(stdout_path, max_rows=row_cap)
    except ValueError as exc:
        return fail(f"{plugin} produced unparseable JSON output: {exc}")

    flags, findings = apply_severity(plugin, columns, rows)
    if truncated:
        findings = _count_findings_from_file(plugin, columns, stdout_path)
    result = _result(
        run_id=run_id,
        job_id=job_id,
        plugin=plugin,
        status="completed",
        started_at=started_at,
        dump_file=dump_file,
        input_verified=input_verified,
        isf_source=isf_source,
        isf_pinned=isf_pinned,
        include_isf_provenance=True,
        columns=columns,
        rows=rows,
        flags=flags,
        findings=findings,
        truncated=truncated,
        total_rows=total_rows,
        execution=execution,
        action_id=action_id,
    )
    _write_result(runs_dir, result)
    try:
        _record_run_report_artifact(
            job_id=job_id,
            run_file=runs_dir / f"{run_id}.json",
            plugin=plugin,
            run_id=run_id,
            row_count=total_rows,
            findings=findings,
            dump_path=dump_path,
            dump_sha256=dump_sha256,
            dump_sha256_at_use=dump_sha256_at_use,
            isf_path=artifact_path,
            isf_sha256=isf_sha256,
            isf_source=isf_source,
            isf_pinned=isf_pinned,
            execution=execution,
            source_input=source_input,
            action_id=action_id,
        )
        _finalise(job_id, action_id, success=True)
    except Exception as exc:  # Publication is part of successful analysis.
        error = f"Vol3 result publication failed: {exc}"
        # result.json may already be hashed/adopted. Only mutable presentation
        # status changes; retain the evidence and a separate failure record.
        failure = {
            **result,
            "status": "failed",
            "error": error,
            "action_id": action_id,
            "recovery": {"action_finalization": "pending"},
        }
        (runs_dir / f"{run_id}.failure.json").write_text(json.dumps(failure, indent=2), encoding="utf-8")
        _write_run_meta(runs_dir, failure)
        try:
            _finalise(job_id, action_id, success=False, error=error)
            failure["recovery"]["action_finalization"] = "completed"
        except Exception as finalization_error:
            failure["recovery"]["error"] = str(finalization_error)
            logger.exception("[%s] plugin action finalization requires recovery", job_id)
        (runs_dir / f"{run_id}.failure.json").write_text(json.dumps(failure, indent=2), encoding="utf-8")
        return False, failure
    try:
        _write_run_meta(runs_dir, result)
    except OSError:
        # Publication and its owning action are already durable. A stale
        # presentation header must neither fail them nor hide their completion;
        # API history projects the durable owner onto this bounded header.
        logger.exception("[%s] completed run header requires refresh: %s", job_id, run_id)
    return True, result
