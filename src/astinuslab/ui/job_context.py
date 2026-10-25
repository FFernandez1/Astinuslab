from __future__ import annotations

import hashlib
import json
import logging

logger = logging.getLogger(__name__)

from fastapi import APIRouter, HTTPException, Request
from fastapi.templating import Jinja2Templates

from ..api.schemas import (
    HostActionOut,
    JobEventOut,
    JobOut,
    JobRuntimeOut,
    ReportOut,
)
from ..catalog import (
    attacker_by_id,
    load_workloads,
)
from ..core.config import settings as _default_settings
from ..csrf import csrf_token_for_request
from ..db import session_scope as _default_session_scope
from ..db.repositories import ArtifactRepository, JobRepository, ReportRepository
from ..forensics.vol3_tools import get_vol3_readiness
from ..i18n import DEFAULT_LANG, LANG_NAMES, SUPPORTED_LANGS, get_translations
from ..jobs.job_services import (
    list_targets_service,
)
from ..jobs.job_status import CANCELLABLE_STATUSES, RUNTIME_CONTROLLABLE_STATUSES
from ..storage.keystore import get_active_key_paths
from ..web.resources import TEMPLATES_DIR
from .asset_version import register_asset_version
from .jobs_helpers import (
    has_active_vol3 as _has_active_vol3,
)
from .jobs_helpers import (
    runtime_action_status as _runtime_action_status,
)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
register_asset_version(templates)

ui_router = APIRouter()

# Test compatibility: route modules historically shared astinuslab.ui globals.


class _SettingsProxy:
    def __getattr__(self, name):
        import astinuslab.ui as ui_mod

        current = getattr(ui_mod, "settings", _default_settings)
        if current is self:
            current = _default_settings
        try:
            return getattr(current, name)
        except (AttributeError, TypeError):
            return getattr(_default_settings, name)

    def model_dump(self, *args, **kwargs):
        import astinuslab.ui as ui_mod

        current = getattr(ui_mod, "settings", _default_settings)
        if current is self:
            current = _default_settings
        return current.model_dump(*args, **kwargs)


class _SessionScopeProxy:
    def __call__(self, *args, **kwargs):
        import astinuslab.ui as ui_mod

        current = getattr(ui_mod, "session_scope", _default_session_scope)
        if current is self:
            current = _default_session_scope
        return current(*args, **kwargs)


settings = _SettingsProxy()
session_scope = _SessionScopeProxy()


ACTIVE_STATUSES = {
    "queued",
    "waiting_for_capacity",
    "preparing_image",
    "waiting_for_guest_access",
    "mounting_output_share",
    "generating_isf",
    "running_scenario_script",
    "acquiring_memory",
    "acquiring_disk",
    "publishing_artifact",
    "cancel_requested",
    "waiting_for_capture",
    "capture_requested",
    "provisioning_workload",
}


def _get_lang(request: Request) -> str:
    lang = request.cookies.get("lang", DEFAULT_LANG)
    return lang if lang in SUPPORTED_LANGS else DEFAULT_LANG


def _base_ctx(request: Request) -> dict:
    lang = _get_lang(request)
    return {
        "t": get_translations(lang),
        "lang": lang,
        "supported_langs": LANG_NAMES,
        "csrf_token": csrf_token_for_request(request),
        "packages_enabled": settings.packages_enabled,
    }


def _find_target(target_id: str) -> dict | None:
    return next((t for t in list_targets_service() if t["id"] == target_id), None)


def _get_vol3_summary(job_id: str) -> dict | None:
    if not settings.vol3_enabled:
        return None
    try:
        with session_scope() as session:
            repo = JobRepository(session)
            job = repo.get_job(job_id)
            if job is None:
                return None
            vol3_actions = [action for action in repo.list_actions(job_id) if action.action_type == "run_vol3_analysis"]
            kernel_release = job.detected_kernel_release
            build_vol3_isf = job.build_vol3_isf

            # For ISF generation we look for vol3-specific states, 'generating_isf',
            # or the 'completed'/'failed' events if this is an ISF-only job (capture_type == "none" and build_vol3_isf)
            is_isf_only = build_vol3_isf and job.capture_type == "none"

            vol3_events = []
            for event in repo.list_events(job_id):
                if event.state in {"vol3_queued", "vol3_started", "vol3_completed", "vol3_failed", "generating_isf"}:
                    vol3_events.append(event)
                elif is_isf_only and event.state in {"completed", "failed"}:
                    vol3_events.append(event)
                elif event.state == "completed" and "ISF-only" in event.message:
                    vol3_events.append(event)

            latest_action = vol3_actions[-1] if vol3_actions else None
            latest_event = vol3_events[-1] if vol3_events else None
            analysis_status = latest_action.status if latest_action is not None else "queued"
            last_run_at = None
            if latest_action is not None:
                last_run_at = latest_action.finished_at or latest_action.started_at or latest_action.created_at
            elif latest_event is not None:
                last_run_at = latest_event.created_at
            summary_message = latest_event.message if latest_event is not None else None
            latest_event_state = latest_event.state if latest_event is not None else None
            isf_log_path = settings.storage_dir / "dumps" / job_id / "vol3" / "symbol-build-host.log"
            isf_log_path_str = str(isf_log_path) if isf_log_path.exists() else None
            _artifact = repo.get_latest_isf_artifact(job_id)
            artifact_path: str | None = None
            artifact_sha256: str | None = None
            artifact_kernel_release: str | None = None
            artifact_source: str | None = None
            artifact_validation_status: str | None = None
            if _artifact is not None:
                artifact_path = _artifact.path
                artifact_sha256 = _artifact.sha256
                artifact_kernel_release = _artifact.kernel_release
                artifact_source = _artifact.source
                artifact_validation_status = _artifact.validation_status

        if not (build_vol3_isf or vol3_actions or vol3_events or artifact_path):
            return None

        isf_path_str: str | None = None
        if artifact_path is not None:
            isf_path_str = artifact_path
            isf_hash = artifact_sha256
            symbols_extracted = True
            summary_generated_path = artifact_path
        else:
            symbols_extracted = False
            isf_path = None

            summary_generated_path = None
            isf_hash = None
            if summary_message:
                if "Symbol built:" in summary_message:
                    parts = summary_message.split("Symbol built:", 1)[1].strip()
                    if ", sha256:" in parts:
                        path_part, hash_part = parts.split(", sha256:", 1)
                        summary_generated_path = path_part.strip()
                        isf_hash = hash_part.strip()
                    else:
                        summary_generated_path = parts
                elif "Symbol generated:" in summary_message:
                    summary_generated_path = summary_message.split("Symbol generated:", 1)[1].strip()
            isf_path_str = str(isf_path) if symbols_extracted and isf_path is not None else summary_generated_path

        if symbols_extracted or summary_generated_path:
            isf_status = "Generated"
            isf_badge = "completed"
        elif latest_event_state in ("vol3_failed", "failed") or analysis_status == "failed":
            isf_status = "Failed"
            isf_badge = "failed"
        elif latest_event_state == "vol3_completed" or latest_event_state == "completed":
            isf_status = "Skipped"
            isf_badge = "completed"
        elif latest_event_state == "generating_isf":
            isf_status = "Generating"
            isf_badge = "running"
        elif build_vol3_isf or vol3_actions:
            isf_status = "Pending"
            isf_badge = "queued"
        else:
            isf_status = "Not requested"
            isf_badge = "queued"

        return {
            "isf_status": isf_status,
            "isf_badge": isf_badge,
            "isf_path": isf_path_str,
            "isf_hash": isf_hash,
            "last_run_at": last_run_at,
            "summary_message": summary_message,
            "requested": build_vol3_isf,
            "isf_kernel_release": artifact_kernel_release if artifact_kernel_release is not None else kernel_release,
            "isf_source": artifact_source,
            "isf_validation_status": artifact_validation_status,
            "isf_log_path": isf_log_path_str,
        }
    except Exception:
        logger.exception("_get_vol3_summary failed for job %s", job_id)
        return None


def _rank_workloads_by_usage(workloads: list[dict]) -> list[dict]:
    usage_counts: dict[str, int] = {}
    with session_scope() as session:
        repo = JobRepository(session)
        for job in repo.list_jobs():
            workload_id = getattr(job, "workload_id", None)
            if workload_id:
                usage_counts[workload_id] = usage_counts.get(workload_id, 0) + 1

    ranked = sorted(
        workloads,
        key=lambda workload: (
            -usage_counts.get(workload["id"], 0),
            workload.get("display_name", workload["id"]).lower(),
        ),
    )
    return [{**workload, "usage_count": usage_counts.get(workload["id"], 0)} for workload in ranked]


from typing import Any


def _job_detail_context(
    request: Request,
    job_id: str,
    *,
    include_vol3_readiness: bool = True,
) -> dict[str, Any]:
    with session_scope() as session:
        repo = JobRepository(session)
        job = repo.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        job_out = JobOut.model_validate(job)
        events = [JobEventOut.model_validate(e) for e in repo.list_events(job_id)]
        actions = [HostActionOut.model_validate(a) for a in repo.list_actions(job_id)]
        runtimes = [JobRuntimeOut.model_validate(r) for r in repo.list_runtimes(job_id)]
        raw_artifacts = ArtifactRepository(session).list_artifacts(job_id=job_id)
        artifacts = [
            {
                "id": a.id,
                "case_id": a.case_id,
                "job_id": a.job_id,
                "acquisition_id": a.acquisition_id,
                "producer": a.producer,
                "kind": a.kind,
                "filename": a.filename,
                "sha256": a.sha256,
                "size_bytes": a.size_bytes,
                "manifest_path": a.manifest_path,
                "sha256_sidecar_path": a.sha256_sidecar_path,
                "created_at": a.created_at,
            }
            for a in raw_artifacts
        ]
        reports = [ReportOut.model_validate(r) for r in ReportRepository(session).list_reports(job_id=job_id)]
    target = _find_target(job_out.target_id)
    # SSH key is optional; the UI degrades gracefully when none is configured.
    try:
        active_ssh_key_path, _ = get_active_key_paths()
        active_ssh_key = str(active_ssh_key_path)
    except RuntimeError:
        active_ssh_key = ""
    workload = (
        next((w for w in load_workloads() if w["id"] == job_out.workload_id), None) if job_out.workload_id else None
    )
    selected_attacker = attacker_by_id(job_out.attacker_vm_id) if job_out.attacker_vm_id else None
    legacy_attacker = (workload.get("attacker_vm") or {}) if workload else {}
    attacker_username = (selected_attacker or {}).get("username") or legacy_attacker.get("username") or "kali"
    runtime_action_status = _runtime_action_status(actions)
    vol3_summary = _get_vol3_summary(job_id)
    # Status/events poll cheaply. Refresh controls only when their underlying
    # state changes; timestamps and event messages alone must not replace forms.
    controls_state = {
        "job": {
            key: getattr(job_out, key)
            for key in (
                "status",
                "guest_ip",
                "lab_snapshot_name",
                "lab_url",
                "network_isolated",
                "error_summary",
            )
        },
        "runtimes": [r.model_dump(mode="json", exclude={"created_at", "updated_at"}) for r in runtimes],
        "runtime_actions": runtime_action_status,
        "attacker_username": attacker_username,
        "artifacts": artifacts,
        "reports": [r.model_dump(mode="json") for r in reports],
        "vol3_summary": vol3_summary,
    }
    live_revision = hashlib.sha256(json.dumps(controls_state, sort_keys=True, default=str).encode()).hexdigest()
    context: dict[str, Any] = {
        "live_revision": live_revision,
        "job": job_out,
        "events": events,
        "actions": actions,
        "runtimes": runtimes,
        "artifacts": artifacts,
        "reports": reports,
        "target": target,
        "workload": workload,
        "runtime_action_status": runtime_action_status,
        "is_active": job_out.status in ACTIVE_STATUSES or _has_active_vol3(actions),
        "can_cancel": job_out.status in CANCELLABLE_STATUSES,
        "can_control_runtime": job_out.status in RUNTIME_CONTROLLABLE_STATUSES,
        "active_ssh_key": active_ssh_key,
        "attacker_username": attacker_username,
        "vol3_summary": vol3_summary,
        "vol3_enabled": settings.vol3_enabled,
        # Mutation handlers redirect back here with ?message=...; without these the
        # failure is invisible.  Readiness is evaluated at render time but re-verified
        # at execution, so a tool removed between page load and click lands exactly
        # here and must be explained rather than silently swallowed.
        #
        # The default is `error`, not `success`: only a redirect this app emitted
        # labels itself `message_type=success`, so a hand-typed or attacker-crafted
        # /jobs/<id>?message=Account+suspended renders RED instead of borrowing the
        # authority of a green success banner.
        "message": request.query_params.get("message"),
        "message_type": "success" if request.query_params.get("message_type") == "success" else "error",
        **_base_ctx(request),
    }
    if include_vol3_readiness:
        # Templates get the public view only.  The dataclass carries `resolved_path`,
        # which must never reach a rendered page (invariant 11); handing Jinja the
        # safe dict keeps a future template edit from leaking a host path.
        context["vol3_readiness"] = get_vol3_readiness().public_dict()
    return context
