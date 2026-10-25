from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from ..core.config import settings
from ..core.locks import _safe_lock_name, blocking_file_lock
from ..db import session_scope
from ..db.repositories import ArtifactRepository, JobRepository, ReportRepository
from ..db.transactions import reserve_sqlite_write
from ..storage import publication
from ..storage.artifacts import (
    build_artifact_manifest,
)
from .analysis_input import (
    AnalysisInputError,
    ResolvedInput,
    require_unchanged_input,
    resolve_analysis_input,
    verify_source_bytes,
)
from .symbols.windows import windows_symbol_cache_dir
from .vol3_plugins import validate_plugin_names
from .vol3_provenance import execution_files, file_identity, has_output, isf_trust, tool_identity
from .vol3_support import _sha256_file, os_family_for_distro, requires_isf_build
from .vol3_tools import ToolDependencyError, require_verified_volatility

logger = logging.getLogger(__name__)

_PLUGIN_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_VOL3_TIMEOUT_SECONDS = 900
_WINDOWS_DEFAULT_PLUGINS = ("windows.info.Info", "windows.pslist.PsList", "windows.pstree.PsTree")


def _windows_plugins() -> list[str]:
    return list(_WINDOWS_DEFAULT_PLUGINS)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _plugins_from_settings(raw: object) -> list[str]:
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def effective_plugins_for_os(os_family: str, configured_defaults: object) -> list[str]:
    if not requires_isf_build(os_family):
        return _windows_plugins()
    return _plugins_from_settings(configured_defaults)


def _plugin_output_name(plugin: str) -> str:
    safe = _PLUGIN_NAME_RE.sub("_", plugin).strip("._")
    return f"{safe or 'plugin'}.txt"


def _plugin_config_name(plugin: str) -> str:
    safe = _PLUGIN_NAME_RE.sub("_", plugin).strip("._")
    return f"{safe or 'plugin'}.config.json"


def _plugin_stderr_name(plugin: str) -> str:
    safe = _PLUGIN_NAME_RE.sub("_", plugin).strip("._")
    return f"{safe or 'plugin'}.stderr.log"


def _isf_urls_from_saved_config(config_path: Path) -> list[Path] | None:
    """Return local paths for every ``*.isf_url`` in a vol3 saved config."""
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    paths: list[Path] = []
    for key, value in data.items():
        if not isinstance(key, str) or not key.endswith(".isf_url"):
            continue
        if not isinstance(value, str):
            return None
        parsed = urlparse(value)
        if parsed.scheme == "file" and parsed.netloc in {"", "localhost"}:
            paths.append(Path(url2pathname(parsed.path)))
        elif not parsed.scheme and Path(value).is_absolute():
            paths.append(Path(value))
        else:
            return None
    return paths


def _verify_pinned_isf(config_path: Path, staged_symbols_path: Path) -> tuple[bool, str | None]:
    """Check that vol3 resolved its symbol table(s) from the staged dir only."""
    isf_paths = _isf_urls_from_saved_config(config_path)
    if isf_paths is None:
        return False, "Volatility saved ISF config is missing, unreadable or invalid"
    if not isf_paths:
        return False, "Volatility saved config contains no ISF reference"
    staged_root = staged_symbols_path.resolve()
    foreign = sorted(str(p) for p in isf_paths if not p.resolve().is_relative_to(staged_root))
    if foreign:
        return False, (
            "Volatility 3 resolved a symbol table outside the staged "
            f"symbols directory ({staged_root}): {', '.join(foreign)}"
        )
    for path in isf_paths:
        try:
            with path.open("rb") as handle:
                handle.read(1)
        except OSError as exc:
            return False, f"Volatility saved config references an unreadable ISF: {exc}"
    return True, None


def _write_summary(output_dir: Path, summary: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary.setdefault("run_id", output_dir.name)
    summary.setdefault("output_dir", str(output_dir))
    (output_dir / "analysis-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _record_success_report_artifact(
    *,
    job_id: str,
    output_dir: Path,
    summary: dict[str, Any],
    dump_path: str | None,
    plugin_count: int,
    attempt: publication.PublicationAttempt,
    source_input: ResolvedInput,
    action_id: int | None,
) -> None:
    summary_path = output_dir / "analysis-summary.json"

    with session_scope() as session:
        repo = JobRepository(session)
        require_unchanged_input(
            session, repo.get_job(job_id), repo.get_action(action_id) if action_id is not None else None, source_input
        )

    def _build_report_manifest(sha256: str, size_bytes: int) -> dict[str, Any]:
        manifest = build_artifact_manifest(
            artifact_path=summary_path,
            kind="report",
            producer="vol3_analysis",
            sha256=sha256,
            size_bytes=size_bytes,
            job_id=job_id,
            inputs=[
                {
                    "kind": "memory",
                    "artifact_id": source_input.artifact_id,
                    "path": dump_path,
                    "sha256": summary.get("dump_sha256"),
                    "sha256_at_use": summary.get("dump_sha256_at_use"),
                    "verified": summary.get("input_verified"),
                },
                {
                    "kind": "isf",
                    "path": summary.get("isf_path"),
                    "sha256": summary.get("isf_sha256"),
                    "source": summary.get("isf_source"),
                    "pinned": summary.get("isf_pinned"),
                },
            ],
        )

        manifest["executions"] = [r["execution"] for r in summary["plugins"] if "execution" in r]
        manifest["inputs"][1]["trust"] = isf_trust(summary.get("isf_source"))
        return manifest

    triad = attempt.adopt(summary_path, _build_report_manifest)
    published = attempt.publish_sidecars()
    sha256, size_bytes = triad.sha256, triad.size_bytes
    manifest_path, sidecar_path = published.manifest_path, published.sidecar_path

    with session_scope() as session:
        reserve_sqlite_write(session)
        artifact_repo = ArtifactRepository(session)
        report_repo = ReportRepository(session)
        repo = JobRepository(session)
        job = repo.get_job(job_id)
        require_unchanged_input(
            session, job, repo.get_action(action_id) if action_id is not None else None, source_input
        )
        input_artifact_id = source_input.artifact_id
        report_artifact = artifact_repo.create_artifact(
            kind="report",
            producer="vol3_analysis",
            path=str(summary_path),
            filename=summary_path.name,
            sha256=sha256,
            size_bytes=size_bytes,
            manifest_path=str(manifest_path),
            sha256_sidecar_path=str(sidecar_path),
            case_id=job.acquisition.case_id if job is not None and job.acquisition is not None else None,
            job_id=job_id,
            acquisition_id=job.acquisition_id if job is not None else None,
        )
        report_repo.create_report(
            report_type="volatility3",
            status=str(summary["status"]),
            path=str(summary_path),
            title="Volatility 3 analysis",
            summary=f"{plugin_count} plugins completed",
            metadata_json=json.dumps({"plugins": summary.get("plugins", [])}, sort_keys=True),
            case_id=report_artifact.case_id,
            job_id=job_id,
            acquisition_id=report_artifact.acquisition_id,
            input_artifact_id=input_artifact_id,
            output_artifact_id=report_artifact.id,
        )
        attempt.complete(session, artifact_id=report_artifact.id)
    attempt.finish()


def _stage_selected_isf(output_dir: Path, artifact_path: Path) -> Path:
    symbols_path = output_dir / "symbols"
    linux_symbols_path = symbols_path / "linux"
    if linux_symbols_path.exists():
        shutil.rmtree(linux_symbols_path)
    linux_symbols_path.mkdir(parents=True, exist_ok=True)
    shutil.copy2(artifact_path, linux_symbols_path / artifact_path.name)
    return symbols_path


def _safe_output_dir(job_id: str) -> Path | None:
    dumps_dir = (settings.storage_dir / "dumps").resolve()
    output_dir = (dumps_dir / job_id / "vol3").resolve()
    if output_dir == dumps_dir or not output_dir.is_relative_to(dumps_dir):
        return None
    return output_dir


def _vol3_run_lock_path(job_id: str) -> Path:
    """Per-job lock file serializing ISF staging + vol3 execution for one job.

    Defined per runner module (not shared) so each module's monkeypatched
    test settings resolve the lock under the test storage tree.
    """
    return settings.storage_dir / "tmp" / "vol3-run-locks" / f"{_safe_lock_name(job_id)}.lock"


def _select_isf_artifact(
    repo: JobRepository,
    job_id: str,
    kernel_release: str | None,
    architecture: str,
    *,
    build_id: str | None = None,
):
    if build_id:
        artifact = repo.find_isf_by_build_id(build_id, architecture)
        if artifact is not None:
            return artifact
    # An ISF is keyed by kernel build, not by VM, so fall back to any valid ISF
    # for this kernel when the dump's own job has none (e.g. built separately).
    return repo.get_valid_isf_artifact(
        job_id, kernel_release or "", architecture=architecture
    ) or repo.find_valid_isf_by_kernel(kernel_release or "", architecture=architecture)


def _finalise_action(
    job_id: str,
    action_id: int | None,
    *,
    success: bool,
    summary: dict[str, Any],
) -> None:
    error = summary.get("error")
    with session_scope() as session:
        repo = JobRepository(session)
        if repo.get_job(job_id) is None:
            logger.info("[%s] Vol3 analysis finalise skipped; job no longer exists", job_id)
            return
        repo.add_event(
            job_id,
            "vol3_completed" if success else "vol3_failed",
            "Volatility 3 plugin analysis completed" if success else f"Volatility 3 plugin analysis failed: {error}",
        )
        if action_id is not None:
            repo.update_action(
                action_id,
                status="completed" if success else "failed",
                error_summary=None if success else str(error or "Vol3 analysis failed"),
                finished_at=datetime.now(UTC),
                active_pid=None,
            )


def _fail(
    job_id: str,
    action_id: int | None,
    output_dir: Path | None,
    error: str,
    *,
    plugins: list[dict[str, Any]] | None = None,
    dump_path: str | None = None,
    dump_sha256: str | None = None,
    isf_path: str | None = None,
    isf_sha256: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    summary: dict[str, Any] = {
        "job_id": job_id,
        "status": "failed",
        "error": error,
        "dump_path": dump_path,
        "dump_sha256": dump_sha256,
        "isf_path": isf_path,
        "isf_sha256": isf_sha256,
        "plugins": plugins or [],
        "finished_at": _utc_now_iso(),
    }
    if output_dir is not None:
        _write_summary(output_dir, summary)
    _finalise_action(job_id, action_id, success=False, summary=summary)
    return False, summary


def run_vol3_analysis(job_id: str, action_id: int | None = None) -> tuple[bool, dict[str, Any]]:
    """Reserve an immutable batch identity before writing or executing.

    Prior evidence, including legacy fixed-path reports, is never rewritten.
    A failed publication leaves diagnostics under this attempt only.
    """
    root = _safe_output_dir(job_id)
    if root is None:
        return _fail(job_id, action_id, None, "Vol3 analysis received an invalid job_id path")
    output_dir = root / "batches" / uuid.uuid4().hex
    try:
        output_dir.mkdir(parents=True, exist_ok=False)
        with publication.attempt(
            session_scope,
            producer="vol3_analysis",
            kind="report",
            final_path=output_dir / "analysis-summary.json",
            staging_parent=output_dir,
            job_id=job_id,
        ) as attempt:
            return _run_reserved_analysis(job_id, action_id, output_dir, attempt)
    except Exception as exc:
        # The summary may already have been hashed or linked. Never rewrite it
        # after a publication/DB error; retain a separate failure diagnostic.
        failure: dict[str, Any] = {
            "job_id": job_id,
            "action_id": action_id,
            "status": "failed",
            "error": f"Vol3 analysis failed: {exc}",
            "finished_at": _utc_now_iso(),
            "run_id": output_dir.name,
            "output_dir": str(output_dir),
            "recovery": {"action_finalization": "pending"},
        }
        # Disk diagnostics must precede any DB operation: publication and
        # finalization can fail together when SQLite is locked/unavailable.
        failure_path = output_dir / "failure.json"
        try:
            failure_path.write_text(json.dumps(failure, indent=2), encoding="utf-8")
        except OSError as diagnostic_error:
            failure["diagnostic_error"] = str(diagnostic_error)
            logger.exception("[%s] could not persist batch failure diagnostic", job_id)
        try:
            _finalise_action(job_id, action_id, success=False, summary=failure)
            failure["recovery"]["action_finalization"] = "completed"
        except Exception as finalization_error:
            failure["recovery"]["error"] = str(finalization_error)
            logger.exception("[%s] batch action finalization requires recovery", job_id)
        try:
            failure_path.write_text(json.dumps(failure, indent=2), encoding="utf-8")
        except OSError:
            logger.exception("[%s] could not update batch recovery diagnostic", job_id)
        return False, failure


def _run_reserved_analysis(
    job_id: str, action_id: int | None, output_dir: Path, attempt: publication.PublicationAttempt
) -> tuple[bool, dict[str, Any]]:
    precondition_error: str | None = None
    dump_path: str | None = None
    dump_sha256: str | None = None
    kernel_release: str | None = None
    architecture = "x86_64"
    os_family = "linux"
    latest_artifact_build_id: str | None = None
    artifact_path: str | None = None
    isf_sha256: str | None = None
    isf_source: str | None = None
    source_input: ResolvedInput | None = None
    with session_scope() as session:
        repo = JobRepository(session)
        job = repo.get_job(job_id)
        if job is None:
            precondition_error = f"Job {job_id} not found"
        else:
            if action_id is not None:
                repo.update_action(
                    action_id,
                    status="running",
                    started_at=datetime.now(UTC),
                    error_summary=None,
                )
            if job.status != "completed":
                precondition_error = "Vol3 analysis requires a completed memory dump job"
            else:
                try:
                    source_input = resolve_analysis_input(
                        session, job, repo.get_action(action_id) if action_id is not None else None
                    )
                    dump_path = source_input.path
                    dump_sha256 = source_input.sha256
                except AnalysisInputError as exc:
                    precondition_error = str(exc)
            kernel_release = job.detected_kernel_release
            architecture = job.detected_architecture or "x86_64"
            os_family = os_family_for_distro(job.detected_distro)
            latest_artifact = repo.get_latest_isf_artifact(job_id)
            latest_artifact_build_id = latest_artifact.build_id if latest_artifact is not None else None

    if precondition_error is not None:
        return _fail(
            job_id,
            action_id,
            output_dir,
            precondition_error,
            dump_path=dump_path,
            dump_sha256=dump_sha256,
            isf_path=artifact_path,
            isf_sha256=isf_sha256,
        )

    assert source_input is not None  # noqa: S101 - resolved above or returned with a precondition error.
    if architecture != "x86_64":
        return _fail(
            job_id,
            action_id,
            output_dir,
            f"Vol3 analysis supports x86_64 only, got {architecture}",
            dump_path=dump_path,
            dump_sha256=dump_sha256,
            isf_path=artifact_path,
            isf_sha256=isf_sha256,
        )

    with session_scope() as session:
        repo = JobRepository(session)
        artifact = _select_isf_artifact(
            repo,
            job_id,
            kernel_release,
            architecture,
            build_id=latest_artifact_build_id,
        )
        artifact_path = artifact.path if artifact is not None else None
        isf_sha256 = artifact.sha256 if artifact is not None else None
        isf_source = repo.resolve_isf_origin_source(artifact) if artifact is not None else None

    if not dump_path or not Path(dump_path).is_file():
        return _fail(
            job_id,
            action_id,
            output_dir,
            "Vol3 analysis requires an existing completed memory dump",
            dump_path=dump_path,
            dump_sha256=dump_sha256,
            isf_path=artifact_path,
            isf_sha256=isf_sha256,
        )
    if requires_isf_build(os_family) and (artifact_path is None or not Path(artifact_path).is_file()):
        return _fail(
            job_id,
            action_id,
            output_dir,
            "Vol3 analysis requires a valid ISF for the job kernel",
            dump_path=dump_path,
            dump_sha256=dump_sha256,
            isf_path=artifact_path,
            isf_sha256=isf_sha256,
        )
    # Re-verify evidence hashes at use time before Volatility 3 reads them.
    try:
        dump_sha256_at_use = verify_source_bytes(source_input)
    except (AnalysisInputError, OSError) as exc:
        return _fail(
            job_id,
            action_id,
            output_dir,
            str(exc),
            dump_path=dump_path,
            dump_sha256=dump_sha256,
            isf_path=artifact_path,
            isf_sha256=isf_sha256,
        )
    isf_sha256_at_use = None
    if requires_isf_build(os_family) and artifact_path:
        isf_sha256_at_use = _sha256_file(Path(artifact_path))
        if isf_sha256 and isf_sha256_at_use != isf_sha256:
            return _fail(
                job_id,
                action_id,
                output_dir,
                f"ISF hash mismatch: recorded {isf_sha256}, "
                f"computed {isf_sha256_at_use} — the symbol file changed since it was built",
                dump_path=dump_path,
                dump_sha256=dump_sha256,
                isf_path=artifact_path,
                isf_sha256=isf_sha256,
            )

    plugins = effective_plugins_for_os(os_family, settings.vol3_default_plugins)
    if not plugins:
        return _fail(
            job_id,
            action_id,
            output_dir,
            "Vol3 analysis requires at least one configured plugin",
            dump_path=dump_path,
            dump_sha256=dump_sha256,
            isf_path=artifact_path,
            isf_sha256=isf_sha256,
        )
    unknown_plugins = validate_plugin_names(os_family, plugins)
    if unknown_plugins:
        return _fail(
            job_id,
            action_id,
            output_dir,
            f"Unknown {os_family} Volatility plugins: {', '.join(unknown_plugins)}",
            dump_path=dump_path,
            dump_sha256=dump_sha256,
            isf_path=artifact_path,
            isf_sha256=isf_sha256,
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    plugin_results: list[dict[str, Any]] = []
    started_at = _utc_now_iso()
    error: str | None = None
    vol3_version: str | None = None
    batch_volatility_path: Path | None = None

    with blocking_file_lock(_vol3_run_lock_path(job_id)):
        # Fresh, job-local vol3 cache: a stale banner cache could otherwise
        # resolve the kernel banner to an ISF outside the staged directory.
        # Safe to wipe here: the per-job lock is held for staging + all runs.
        vol3_cache_dir = output_dir / "vol3-cache"
        shutil.rmtree(vol3_cache_dir, ignore_errors=True)
        vol3_cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            if requires_isf_build(os_family):
                assert artifact_path is not None  # noqa: S101  # mypy narrowing; unreachable by construction
                job_symbols_path = _stage_selected_isf(output_dir, Path(artifact_path))
            else:
                # Windows: point Volatility 3 at the windows PDB/ISF symbol cache;
                # symbols are fetched from the Microsoft Symbol Server or offline cache.
                job_symbols_path = windows_symbol_cache_dir(settings.vol3_symbols_path)
                job_symbols_path.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return _fail(
                job_id,
                action_id,
                output_dir,
                f"Vol3 analysis failed to stage selected ISF: {exc}",
                dump_path=dump_path,
                dump_sha256=dump_sha256,
                isf_path=artifact_path,
                isf_sha256=isf_sha256,
            )

        staged_identity = None
        if requires_isf_build(os_family) and artifact_path is not None:
            staged_identity = file_identity(job_symbols_path / "linux" / Path(artifact_path).name)
            if staged_identity["sha256"] != isf_sha256_at_use:
                return _fail(job_id, action_id, output_dir, "ISF changed between verification and symbol staging")

        for plugin in plugins:
            output_path = output_dir / _plugin_output_name(plugin)
            stderr_path = output_dir / _plugin_stderr_name(plugin)
            config_path = output_dir / _plugin_config_name(plugin)
            config_path.unlink(missing_ok=True)  # vol3 --save-config refuses to overwrite
            result_info: dict[str, Any] = {
                "plugin": plugin,
                "status": "running",
                "output_path": str(output_path),
                "stderr_path": str(stderr_path),
                "started_at": _utc_now_iso(),
            }
            try:
                # Inside the loop on purpose: one fresh probe per plugin, in the
                # statement before the spawn it guards.  See require_verified_volatility.
                verified = require_verified_volatility(settings)
                volatility_path, readiness_version = verified.path, verified.version
            except ToolDependencyError as exc:
                result_info.update(
                    status="failed",
                    finished_at=_utc_now_iso(),
                    error=exc.detail,
                )
                plugin_results.append(result_info)
                error = exc.detail
                break
            if batch_volatility_path is None:
                batch_volatility_path = volatility_path
                vol3_version = readiness_version
            elif volatility_path != batch_volatility_path or readiness_version != vol3_version:
                error = "The configured Volatility executable changed during batch execution."
                result_info.update(
                    status="failed",
                    finished_at=_utc_now_iso(),
                    error=error,
                )
                plugin_results.append(result_info)
                break
            cmd = [
                str(volatility_path),
                "-f",
                str(dump_path),
                "--symbol-dirs",
                str(job_symbols_path),
                "--cache-path",
                str(vol3_cache_dir),
                "--save-config",
                str(config_path),
            ]
            if requires_isf_build(os_family):
                assert artifact_path is not None  # noqa: S101  # narrowed before staging
                staged_isf = job_symbols_path / "linux" / Path(artifact_path).name
                plugin_class = plugin.rsplit(".", maxsplit=1)[-1]
                config_prefix = f"plugins.{plugin_class}.kernel.symbol_table_name"
                cmd += [
                    "-e",
                    f"{config_prefix}.class={json.dumps('volatility3.framework.symbols.linux.LinuxKernelIntermedSymbols')}",
                    "-e",
                    f"{config_prefix}.isf_url={json.dumps(staged_isf.resolve().as_uri())}",
                ]
            cmd.append(plugin)
            result_info["command"] = cmd
            try:
                tool = tool_identity(verified)
            except OSError as exc:
                error = f"Volatility executable identity could not be read: {exc}"
                result_info.update(status="failed", finished_at=_utc_now_iso(), error=error)
                plugin_results.append(result_info)
                break
            execution: dict[str, Any] = {
                "schema_version": 1,
                "tool": tool,
                "argv": cmd,
                "parameters": {"plugin": plugin, "renderer": "text"},
                "settings": {
                    "os_family": os_family,
                    "kernel_release": kernel_release,
                    "architecture": architecture,
                    "timeout_seconds": _VOL3_TIMEOUT_SECONDS,
                },
                "inputs": {
                    "memory": {
                        "artifact_id": source_input.artifact_id,
                        "path": dump_path,
                        "sha256": dump_sha256,
                        "sha256_at_use": dump_sha256_at_use,
                        "integrity_verified": bool(dump_sha256),
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
            }
            result_info["execution"] = execution
            try:
                # stdout is the published evidence file; stderr (vol3 progress noise
                # and framework warnings) is segregated so two runs of the same dump
                # diff clean and the .txt stays machine-parseable.
                with output_path.open("w", encoding="utf-8") as out, stderr_path.open("w", encoding="utf-8") as err:
                    proc = subprocess.run(
                        cmd,
                        stdout=out,
                        stderr=err,
                        text=True,
                        timeout=_VOL3_TIMEOUT_SECONDS,
                    )
            except subprocess.TimeoutExpired:
                execution["files"] = execution_files(output_path, stderr_path, config_path)
                result_info.update(
                    status="failed",
                    finished_at=_utc_now_iso(),
                    error=f"{plugin} timed out after {_VOL3_TIMEOUT_SECONDS}s",
                )
                plugin_results.append(result_info)
                error = result_info["error"]
                break
            except Exception as exc:
                execution["files"] = execution_files(output_path, stderr_path, config_path)
                result_info.update(
                    status="failed",
                    finished_at=_utc_now_iso(),
                    error=f"{plugin} failed to start: {exc}",
                )
                plugin_results.append(result_info)
                error = result_info["error"]
                break

            execution["files"] = execution_files(output_path, stderr_path, config_path)
            execution["returncode"] = proc.returncode
            if proc.returncode != 0:
                result_info.update(
                    status="failed",
                    returncode=proc.returncode,
                    finished_at=_utc_now_iso(),
                    error=f"{plugin} failed with exit code {proc.returncode}",
                )
                plugin_results.append(result_info)
                error = result_info["error"]
                break

            if not has_output(output_path):
                error = f"{plugin} produced empty stdout"
                result_info.update(status="failed", returncode=proc.returncode, finished_at=_utc_now_iso(), error=error)
                plugin_results.append(result_info)
                break

            isf_pinned = False
            if requires_isf_build(os_family):
                isf_pinned, pin_error = _verify_pinned_isf(config_path, job_symbols_path)
                if pin_error is not None:
                    result_info.update(
                        status="failed",
                        returncode=proc.returncode,
                        finished_at=_utc_now_iso(),
                        error=f"{plugin}: {pin_error}",
                    )
                    plugin_results.append(result_info)
                    error = result_info["error"]
                    break
            result_info["isf_pinned"] = isf_pinned
            execution["inputs"]["isf"]["pinned"] = isf_pinned

            result_info.update(
                status="completed",
                returncode=proc.returncode,
                finished_at=_utc_now_iso(),
            )
            plugin_results.append(result_info)

    success = error is None
    summary: dict[str, Any] = {
        "job_id": job_id,
        "status": "completed" if success else "failed",
        "dump_path": str(dump_path),
        "source_artifact_id": source_input.artifact_id,
        "dump_sha256": dump_sha256,
        "dump_sha256_at_use": dump_sha256_at_use,
        "input_verified": bool(dump_sha256),
        "isf_path": artifact_path,
        "isf_sha256": isf_sha256,
        "isf_source": isf_source,
        "isf_trust": isf_trust(isf_source),
        "isf_low_trust": isf_trust(isf_source) == "unverified",
        "isf_pinned": (
            all(bool(r.get("isf_pinned")) for r in plugin_results)
            if requires_isf_build(os_family) and plugin_results
            else None
        ),
        "symbols_path": str(job_symbols_path),
        "vol3_version": vol3_version,
        "plugins": plugin_results,
        "started_at": started_at,
        "finished_at": _utc_now_iso(),
    }
    if error is not None:
        summary["error"] = error

    _write_summary(output_dir, summary)
    if success:
        _record_success_report_artifact(
            job_id=job_id,
            output_dir=output_dir,
            summary=summary,
            dump_path=dump_path,
            plugin_count=len(plugin_results),
            attempt=attempt,
            source_input=source_input,
            action_id=action_id,
        )
    _finalise_action(job_id, action_id, success=success, summary=summary)
    return success, summary
