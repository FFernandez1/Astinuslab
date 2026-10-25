"""HTML route handlers for job, lab, workload, storage, SSH-key, and Playground UI surfaces.

Routes should stay thin: gather form inputs, call services/orchestrators, and choose templates or redirects. Domain
validation and host-side effects belong in service and orchestration modules.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

logger = logging.getLogger(__name__)

import yaml
from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ..api.errors import ApiError
from ..api.schemas import (
    JobOut,
)
from ..catalog import (
    all_cve_ids,
    all_service_names,
    annotate_workloads_for_target,
    invalidate_catalog_caches,
    load_attackers,
    load_network_nodes,
    load_workloads,
    search_workloads,
    workload_by_id,
)
from ..core.config import settings as _default_settings
from ..core.security import generate_admin_api_token
from ..db import session_scope as _default_session_scope
from ..db.repositories import JobHistoryState, JobRepository
from ..i18n import SUPPORTED_LANGS
from ..jobs.job_services import (
    list_scenarios_service,
    list_targets_service,
)
from ..storage.keystore import delete_key, generate_key, list_keys, set_active
from ..storage.storage_admin_support import storage_stats
from ..virtualization.guest_script_support import exec_script_on_guest as _exec_script_on_guest
from ..workloads.workload_cache_support import workload_cache_dir
from .jobs_helpers import (
    default_workload_yaml as _default_workload_yaml,
)
from .jobs_helpers import (
    human_size as _human_size,
)
from .jobs_helpers import (
    redirect_with_message as _redirect_with_message,
)
from .jobs_helpers import (
    redirect_with_success as _redirect_with_success,
)
from .jobs_helpers import (
    safe_next as _safe_next,
)
from .jobs_helpers import (
    translate_scenarios as _translate_scenarios,
)

router = APIRouter()
JOBS_PAGE_SIZE = 50

# Test compatibility: route modules historically shared astinuslab.ui globals.


class _SettingsProxy:
    """Compatibility proxy that preserves old route monkeypatch seams while settings moved modules."""

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
    """Compatibility proxy for tests and callers that monkeypatch session_scope through this route module."""

    def __call__(self, *args, **kwargs):
        import astinuslab.ui as ui_mod

        current = getattr(ui_mod, "session_scope", _default_session_scope)
        if current is self:
            current = _default_session_scope
        return current(*args, **kwargs)


settings = _SettingsProxy()
session_scope = _SessionScopeProxy()


from .job_context import (
    ACTIVE_STATUSES,
    _base_ctx,
    _find_target,
    _job_detail_context,
    _rank_workloads_by_usage,
    templates,
)
from .job_mutations import (
    acquire_memory_dump_from_ui,
    bulk_delete_jobs_from_form,
    cancel_job_from_ui,
    control_runtime_from_ui,
    create_job_from_form,
    delete_job_from_ui,
    force_kill_job_from_ui,
    reset_lab_from_ui,
    retry_job_from_ui,
    run_vol3_analysis_from_ui,
)


def _non_target_image_refs() -> set[str]:
    """Return image refs reserved for non-target Playground roles."""
    refs: set[str] = set()
    for entry in [*load_attackers(), *load_network_nodes()]:
        image_ref = str(entry.get("image_ref") or "").strip()
        if image_ref:
            refs.add(image_ref)
    return refs


def _is_job_target_candidate(target: dict, non_target_refs: set[str] | None = None) -> bool:
    """Return True when an entry belongs in focused Jobs target pickers."""
    if not target.get("enabled"):
        return False
    role = str(target.get("role") or "target").strip().lower()
    if role not in {"target", "vm"}:
        return False
    cloud_init_profile = str(target.get("cloud_init_profile") or "").strip().lower()
    if cloud_init_profile in {"attacker", "network", "appliance", "router"}:
        return False
    image_ref = str(target.get("image_ref") or "").strip()
    return not image_ref or image_ref not in (non_target_refs or _non_target_image_refs())


def _job_target_options() -> list[dict]:
    non_target_refs = _non_target_image_refs()
    return [target for target in list_targets_service() if _is_job_target_candidate(target, non_target_refs)]


def job_target_options() -> list[dict]:
    """Testable public seam for focused Jobs target options."""
    return _job_target_options()


@router.post("/set-lang")
def set_lang(request: Request, lang: str = Form(...), next: str = Form("/")):
    response = RedirectResponse(url=_safe_next(next), status_code=303)
    if lang in SUPPORTED_LANGS:
        response.set_cookie("lang", lang, max_age=365 * 24 * 3600, samesite="lax", httponly=True)
    return response


@router.get("/", response_class=HTMLResponse)
def index() -> RedirectResponse:
    return RedirectResponse(url="/playground", status_code=303)


def _jobs_history_context(page: int, state: JobHistoryState) -> dict[str, Any]:
    with session_scope() as session:
        repo = JobRepository(session)
        total = repo.count_job_history(state=state)
        pages = max(1, (total + JOBS_PAGE_SIZE - 1) // JOBS_PAGE_SIZE)
        page = min(page, pages)
        raw_jobs = repo.list_job_history(limit=JOBS_PAGE_SIZE, offset=(page - 1) * JOBS_PAGE_SIZE, state=state)
        repo.prefetch_latest_artifacts(raw_jobs)
        jobs = [JobOut.model_validate(job) for job in raw_jobs]
    return {
        "jobs": jobs,
        "jobs_total": total,
        "jobs_page": page,
        "jobs_pages": pages,
        "jobs_state": state,
        "jobs_start": (page - 1) * JOBS_PAGE_SIZE + 1 if jobs else 0,
        "jobs_end": (page - 1) * JOBS_PAGE_SIZE + len(jobs) if jobs else 0,
        "active_statuses": ACTIVE_STATUSES,
    }


@router.get("/jobs", response_class=HTMLResponse)
def jobs_index(request: Request, page: Annotated[int, Query(ge=1)] = 1, state: JobHistoryState = "all"):
    """Render jobs workbench entry point with catalog data needed by create-job controls."""
    targets = _job_target_options()
    base = _base_ctx(request)
    scenarios = _translate_scenarios(list_scenarios_service(), base["t"])
    workloads = load_workloads()
    attackers = load_attackers()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "targets": targets,
            "scenarios": scenarios,
            "workloads": workloads,
            "attackers": attackers,
            **_jobs_history_context(page, state),
            "message": request.query_params.get("message"),
            # Error-by-default: only this app's own redirects opt into the green banner.
            "message_type": "success" if request.query_params.get("message_type") == "success" else "error",
            "vol3_enabled": settings.vol3_enabled,
            **base,
        },
    )


@router.get("/fragments/workload-panel", response_class=HTMLResponse)
def workload_panel_fragment(request: Request, scenario_id: str = "", target_id: str = ""):
    """Render workload picker fragment after target or scenario changes in the job form."""
    scenarios = list_scenarios_service()
    scenario = next((s for s in scenarios if s["id"] == scenario_id), None)
    mode = scenario["mode"] if scenario else ""
    workloads = (
        _rank_workloads_by_usage(annotate_workloads_for_target(target_id, only_compatible=True)) if target_id else []
    )
    attackers = load_attackers()
    return templates.TemplateResponse(
        request,
        "partials/workload_panel.html",
        {
            "mode": mode,
            "target_selected": bool(target_id),
            "workloads": workloads,
            "attackers": attackers,
            **_base_ctx(request),
        },
    )


@router.get("/jobs-table-fragment", response_class=HTMLResponse)
def jobs_table_fragment(request: Request, page: Annotated[int, Query(ge=1)] = 1, state: JobHistoryState = "all"):
    """Render the jobs table fragment used by HTMX polling."""
    return templates.TemplateResponse(
        request,
        "partials/jobs_table.html",
        {
            **_jobs_history_context(page, state),
            "message": request.query_params.get("message"),
            # Error-by-default: only this app's own redirects opt into the green banner.
            "message_type": "success" if request.query_params.get("message_type") == "success" else "error",
            **_base_ctx(request),
        },
    )


@router.get("/jobs/new", response_class=HTMLResponse)
def jobs_new(request: Request):
    """Render standalone create-job page for non-Playground acquisition workflows."""
    targets = _job_target_options()
    base = _base_ctx(request)
    scenarios = _translate_scenarios(list_scenarios_service(), base["t"])
    attackers = load_attackers()
    return templates.TemplateResponse(
        request,
        "job_new.html",
        {
            "targets": targets,
            "scenarios": scenarios,
            "attackers": attackers,
            "vol3_enabled": settings.vol3_enabled,
            **base,
        },
    )


@router.post("/jobs/new")
def create_job_form(
    request: Request,
    target_id: str = Form(...),
    scenario_id: str = Form(...),
    capture_type: str = Form("memory"),
    custom_script: str = Form(""),
    workload_id: str = Form(""),
    attacker_vm_id: str = Form(""),
    vm_vcpus: str = Form(""),
    vm_memory_mb: str = Form(""),
    vm_disk_gb: str = Form(""),
    attacker_vm_vcpus: str = Form(""),
    attacker_vm_memory_mb: str = Form(""),
    attacker_vm_disk_gb: str = Form(""),
    build_vol3_isf: str = Form(""),
):
    """Translate HTML form fields into a JobCreate payload and surface service validation as form errors."""
    values = dict(
        target_id=target_id,
        scenario_id=scenario_id,
        capture_type=capture_type,
        custom_script=custom_script,
        workload_id=workload_id,
        attacker_vm_id=attacker_vm_id,
        vm_vcpus=vm_vcpus,
        vm_memory_mb=vm_memory_mb,
        vm_disk_gb=vm_disk_gb,
        attacker_vm_vcpus=attacker_vm_vcpus,
        attacker_vm_memory_mb=attacker_vm_memory_mb,
        attacker_vm_disk_gb=attacker_vm_disk_gb,
        build_vol3_isf=build_vol3_isf,
    )
    try:
        job_id = create_job_from_form(**values, target_options=_job_target_options())
    except ValueError as exc:
        base = _base_ctx(request)
        return templates.TemplateResponse(
            request,
            "job_new.html",
            {
                "targets": _job_target_options(),
                "scenarios": _translate_scenarios(list_scenarios_service(), base["t"]),
                "attackers": load_attackers(),
                "vol3_enabled": settings.vol3_enabled,
                "form_values": values,
                "form_error": str(exc),
                **base,
            },
            status_code=422,
        )
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


# The jobs table (and its bulk-delete form) lives on /jobs.  Redirecting to "/"
# would 303 on to /playground and drop the query string, so the operator would
# never see the outcome banner at all.
_JOBS_TABLE_URL = "/jobs"


@router.get("/jobs/bulk-delete")
def bulk_delete_jobs_get():
    return _redirect_with_message(
        _JOBS_TABLE_URL,
        "Bulk delete is an action triggered from the jobs table. Please select jobs and click 'Delete Selected'.",
    )


@router.post("/jobs/bulk-delete")
async def bulk_delete_jobs(request: Request):
    """Delete selected jobs from the UI while preserving per-job error reporting for partial failures."""
    result = bulk_delete_jobs_from_form(await request.form())
    # Mixed severity: `result.message` is a success tally that may also carry a
    # per-job failure appended by bulk_delete_jobs_from_form, so severity comes from
    # `result.failed` rather than from the text.  A bulk delete that skipped or
    # errored on any job must not render green.
    if result.failed:
        return _redirect_with_message(_JOBS_TABLE_URL, result.message)
    return _redirect_with_success(_JOBS_TABLE_URL, result.message)


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: str):
    """Render job detail page with events, runtimes, artifacts, and action controls."""
    if job_id.startswith("pg-"):
        raise HTTPException(status_code=404, detail="job not found")
    return templates.TemplateResponse(
        request,
        "job_detail.html",
        _job_detail_context(request, job_id),
    )


@router.get("/jobs/{job_id}/live-fragment", response_class=HTMLResponse)
def job_live_fragment(request: Request, job_id: str):
    """Render the polling fragment used by job detail pages to show state changes without full reloads."""
    return templates.TemplateResponse(
        request,
        "partials/live_section.html",
        _job_detail_context(request, job_id),
    )


@router.get("/jobs/{job_id}/live-status-fragment", response_class=HTMLResponse)
def job_live_status_fragment(request: Request, job_id: str):
    """Poll status cheaply, refreshing controls when their state changes."""
    context = _job_detail_context(request, job_id, include_vol3_readiness=False)
    revision = request.query_params.get("revision")
    if revision is not None and revision != context["live_revision"]:
        # Use the same database snapshot for the status and controls. Probe the
        # optional analysis tool only when rendering new controls, never every tick.
        from ..forensics.vol3_tools import get_vol3_readiness

        context["vol3_readiness"] = get_vol3_readiness().public_dict()
        return templates.TemplateResponse(
            request,
            "partials/live_section.html",
            context,
            headers={"HX-Retarget": "#live-section", "HX-Reswap": "outerHTML"},
        )
    return templates.TemplateResponse(request, "partials/live_status.html", context)


@router.post("/jobs/{job_id}/capture")
@router.post("/jobs/{job_id}/acquire-memory-dump")
def acquire_memory_dump_ui(
    job_id: str,
    exploit_tags: str = Form(""),
    exploit_notes: str = Form(""),
    capture_type: str = Form(""),
):
    """Handle operator-triggered acquisition from the UI and redirect back to the live job view."""
    result = acquire_memory_dump_from_ui(job_id, exploit_tags, exploit_notes, capture_type)
    if result.error_message:
        return _redirect_with_message(result.redirect_url, result.error_message)
    return RedirectResponse(url=result.redirect_url, status_code=303)


@router.post("/jobs/{job_id}/vol3-analysis")
def run_vol3_analysis_ui(job_id: str):
    """Queue Volatility analysis from the UI and redirect back to the job detail page."""
    result = run_vol3_analysis_from_ui(job_id)
    if result.error_message:
        return _redirect_with_message(result.redirect_url, result.error_message)
    return RedirectResponse(url=result.redirect_url, status_code=303)


@router.post("/jobs/{job_id}/force-kill")
def force_kill_job(job_id: str):
    """Destroy the libvirt VMs for this job and mark it as cancelled."""
    result = force_kill_job_from_ui(job_id)
    if result.error_message:
        return _redirect_with_message(result.redirect_url, result.error_message)
    return RedirectResponse(url=result.redirect_url, status_code=303)


@router.post("/jobs/{job_id}/retry")
def retry_job_ui(job_id: str):
    """Retry a failed job from the UI using service-layer cloning rules."""
    result = retry_job_from_ui(job_id)
    if result.error_message:
        return _redirect_with_message(result.redirect_url, result.error_message)
    return RedirectResponse(url=result.redirect_url, status_code=303)


@router.post("/jobs/{job_id}/reset-lab")
def reset_lab_ui(request: Request, job_id: str):
    """Reset a manual-trigger lab to its saved snapshot and return to job detail."""
    result = reset_lab_from_ui(job_id)
    if result.error_message:
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"detail": result.error_message}, status_code=503)
        return _redirect_with_message(result.redirect_url, result.error_message)
    return RedirectResponse(url=result.redirect_url, status_code=303)


@router.post("/jobs/{job_id}/runtimes/{role}/{action}")
def control_runtime_ui(job_id: str, role: str, action: str):
    result = control_runtime_from_ui(job_id, role, action)
    if result.error_message:
        return _redirect_with_message(result.redirect_url, result.error_message)
    return RedirectResponse(url=result.redirect_url, status_code=303)


@router.post("/jobs/{job_id}/cancel")
def cancel_job_ui(job_id: str):
    result = cancel_job_from_ui(job_id)
    if result.error_message:
        return _redirect_with_message(result.redirect_url, result.error_message)
    return RedirectResponse(url=result.redirect_url, status_code=303)


@router.post("/jobs/{job_id}/delete")
def delete_job_ui(job_id: str, delete_files: bool = False, redirect: str | None = None):
    delete_job_from_ui(job_id, delete_files=delete_files)
    if redirect:
        return _redirect_with_success(redirect, "Job deleted successfully.")
    return Response(content="", status_code=200)


@router.get("/jobs/{job_id}/script-runner-fragment", response_class=HTMLResponse)
def script_runner_fragment(request: Request, job_id: str):
    """Polled by job_detail.html until guest_ip is available, then returns the static panel."""
    with session_scope() as session:
        repo = JobRepository(session)
        job = repo.get_job(job_id)
        if job is None:
            return Response(content="", status_code=200)
        job_out = JobOut.model_validate(job)

    is_active = job_out.status in ACTIVE_STATUSES
    target = _find_target(job_out.target_id)

    if job_out.guest_ip:
        # VM is up — return the full panel with no polling trigger (static from now on)
        return templates.TemplateResponse(
            request,
            "partials/script_runner_wrapper.html",
            {"job": job_out, "target": target, "poll": False, **_base_ctx(request)},
        )
    else:
        # Still booting — return wrapper that keeps polling
        return templates.TemplateResponse(
            request,
            "partials/script_runner_wrapper.html",
            {"job": job_out, "target": target, "poll": is_active, **_base_ctx(request)},
        )


@router.post("/jobs/{job_id}/run-scripts", response_class=HTMLResponse)
async def run_scripts_on_guest(
    request: Request,
    job_id: str,
    scripts: list[UploadFile] = File(...),
    timeout: int = Form(60),
):
    """Run selected helper scripts through SSH against the target guest and return structured output."""
    with session_scope() as session:
        repo = JobRepository(session)
        job = repo.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        guest_ip = job.guest_ip
        target_id = job.target_id
    if not guest_ip:
        raise ApiError(409, "The VM does not have an IP yet. Wait for it to finish booting", "conflict")
    target = _find_target(target_id)
    guest_user = target["default_username"] if target else "ubuntu"

    _SCRIPT_MAX_BYTES = 1024 * 1024  # 1 MB per script
    results: list[dict] = []
    for upload in scripts:
        content = await upload.read(_SCRIPT_MAX_BYTES + 1)
        if len(content) > _SCRIPT_MAX_BYTES:
            raise HTTPException(status_code=413, detail="Script exceeds the 1 MB per-file limit")
        fname = upload.filename or f"script_{len(results) + 1}.sh"
        res = _exec_script_on_guest(guest_ip, guest_user, content, fname, timeout)
        results.append(res)

    return templates.TemplateResponse(
        request,
        "partials/script_exec_results.html",
        {"results": results, "job_id": job_id, **_base_ctx(request)},
    )


# ── Workload Snapshot Cache ────────────────────────────────────────────────────

_WORKLOAD_CACHE_DIR = workload_cache_dir(settings)
_WORKLOAD_CACHE_INDEX = _WORKLOAD_CACHE_DIR / "index.json"


def _load_workload_cache_entries() -> list[dict]:
    if not _WORKLOAD_CACHE_INDEX.exists():
        return []
    try:
        index: dict = json.loads(_WORKLOAD_CACHE_INDEX.read_text())
    except Exception:
        logger.exception("Failed to parse workload cache index at %s", _WORKLOAD_CACHE_INDEX)
        return []
    entries = []
    for key, meta in index.items():
        cache_file = _WORKLOAD_CACHE_DIR / meta.get("cache_file", "")
        if not str(cache_file.resolve()).startswith(str(_WORKLOAD_CACHE_DIR) + "/"):
            continue
        size_bytes = meta.get("size_bytes", 0)
        if cache_file.exists():
            size_bytes = cache_file.stat().st_size
        created_raw = meta.get("created_at", "")
        try:
            created_at = datetime.fromisoformat(created_raw).strftime("%Y-%m-%d %H:%M")
        except Exception:
            created_at = created_raw
        entries.append(
            {
                "key": key,
                "target_id": meta.get("target_id", ""),
                "workload_id": meta.get("workload_id", ""),
                "base_image": meta.get("base_image", ""),
                "size_bytes": size_bytes,
                "size_human": _human_size(size_bytes),
                "created_at": created_at,
            }
        )
    return entries


def _remove_workload_cache_entry(key: str) -> bool:
    """Remove a single cache entry by key. Returns True if the entry existed."""
    if not _WORKLOAD_CACHE_INDEX.exists():
        return False
    try:
        index: dict = json.loads(_WORKLOAD_CACHE_INDEX.read_text())
    except Exception:
        return False
    meta = index.pop(key, None)
    if meta is None:
        return False
    cache_file = _WORKLOAD_CACHE_DIR / meta.get("cache_file", "")
    if str(cache_file.resolve()).startswith(str(_WORKLOAD_CACHE_DIR) + "/"):
        cache_file.unlink(missing_ok=True)
    _WORKLOAD_CACHE_INDEX.write_text(json.dumps(index, indent=2))
    return True


@router.get("/workload-cache", response_class=HTMLResponse)
def admin_workload_cache_page(request: Request):
    """Render cached workload provisioning artifacts for operator cleanup."""
    entries = _load_workload_cache_entries()
    total_size_human = _human_size(sum(e["size_bytes"] for e in entries))
    return templates.TemplateResponse(
        request,
        "admin_workload_cache.html",
        {
            "entries": entries,
            "total_size_human": total_size_human,
            "message": request.query_params.get("message"),
            # Error-by-default: only this app's own redirects opt into the green banner.
            "message_type": "success" if request.query_params.get("message_type") == "success" else "error",
            **_base_ctx(request),
        },
    )


@router.post("/workload-cache/delete")
def admin_workload_cache_delete(key: str = Form(...)):
    if not _remove_workload_cache_entry(key):
        return _redirect_with_message(
            "/workload-cache", "Cache entry was not deleted. It may be in use or unavailable."
        )
    return _redirect_with_success("/workload-cache", "Cache entry deleted.")


@router.post("/workload-cache/bulk-delete")
def admin_workload_cache_bulk_delete(keys: list[str] = Form(default=[])):
    deleted = sum(1 for k in keys if _remove_workload_cache_entry(k))
    return _redirect_with_success("/workload-cache", f"Deleted {deleted} cache entr{'y' if deleted == 1 else 'ies'}.")


@router.post("/workload-cache/delete-all")
def admin_workload_cache_delete_all():
    entries = _load_workload_cache_entries()
    deleted = sum(1 for e in entries if _remove_workload_cache_entry(e["key"]))
    return _redirect_with_success("/workload-cache", f"Deleted {deleted} cache entr{'y' if deleted == 1 else 'ies'}.")


@router.get("/storage", response_class=HTMLResponse)
def admin_storage_page(request: Request):
    return templates.TemplateResponse(
        request,
        "admin_storage.html",
        {
            "stats": storage_stats(settings),
            "base_dir": str(settings.storage_dir),
            **_base_ctx(request),
        },
    )


# ── Image Vault ────────────────────────────────────────────────────────────────


def _workloads_catalog_write_path() -> Path:
    path = settings.workloads_catalog_path
    if path.name.endswith(".example.yaml"):
        return path.with_name(path.name.replace(".example.yaml", ".yaml"))
    return path


def _editor_yaml_for_workload(workload_id: str) -> str:
    raw: dict = {}
    catalog_path = _workloads_catalog_write_path()
    if catalog_path.exists():
        raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8")) or {}
    elif settings.workloads_catalog_path.exists():
        raw = yaml.safe_load(settings.workloads_catalog_path.read_text(encoding="utf-8")) or {}
    for item in raw.get("workloads", []):
        if item.get("id") == workload_id:
            return yaml.safe_dump(item, sort_keys=False, allow_unicode=False).strip()
    workload = workload_by_id(workload_id)
    if workload is None:
        return _default_workload_yaml()
    item = {
        "id": workload["id"],
        "enabled": True,
        "display_name": workload["display_name"],
        "tags": workload.get("tags", []),
        "services": workload.get("services", []),
        "cve_ids": workload.get("cve_ids", []),
        "cve_context": workload.get("cve_context") or "",
        "network_isolation": workload.get("network_isolation", False),
    }
    if workload.get("is_stack"):
        item["type"] = "stack"
        item["components"] = workload.get("components", [])
    if workload.get("install_script"):
        item["install_script"] = workload["install_script"]
    return yaml.safe_dump(item, sort_keys=False, allow_unicode=False).strip()


@router.get("/workloads", response_class=HTMLResponse)
def admin_workloads_page(request: Request, q: str = "", cve: str = "", edit: str = ""):
    """Render workload catalog administration with search, CVE filters, and optional edit context."""
    search_query = q.strip() or cve.strip()
    workloads = search_workloads(search_query, load_workloads())
    selected_id = edit.strip()
    editing_workload = workload_by_id(selected_id) if selected_id and selected_id != "new" else None
    write_path = _workloads_catalog_write_path()
    try:
        catalog_write_path = str(write_path.relative_to(settings.base_dir))
    except ValueError:
        catalog_write_path = str(write_path)
    return templates.TemplateResponse(
        request,
        "admin_workloads.html",
        {
            "workloads": workloads,
            "all_cve_ids": all_cve_ids(),
            "all_service_names": all_service_names(),
            "search_query": search_query,
            "editing_workload": editing_workload,
            "editor_yaml": _editor_yaml_for_workload(selected_id) if selected_id else _default_workload_yaml(),
            "catalog_write_path": catalog_write_path,
            **_base_ctx(request),
        },
    )


@router.post("/workloads/save")
def save_workload_template(
    editor_yaml: str = Form(...),
    original_id: str = Form(""),
):
    """Validate and persist operator-edited workload YAML."""
    try:
        parsed = yaml.safe_load(editor_yaml) or {}
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {exc}") from exc

    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="Workload template must be a YAML object")

    workload_id = str(parsed.get("id", "")).strip()
    if not workload_id:
        raise HTTPException(status_code=400, detail="Workload template must include a non-empty id")

    write_path = _workloads_catalog_write_path()
    raw: dict = {"version": 1, "workloads": []}
    if write_path.exists():
        raw = yaml.safe_load(write_path.read_text(encoding="utf-8")) or raw
    elif settings.workloads_catalog_path.exists():
        raw = yaml.safe_load(settings.workloads_catalog_path.read_text(encoding="utf-8")) or raw

    items = list(raw.get("workloads", []))
    replaced = False
    for idx, item in enumerate(items):
        if item.get("id") in {original_id.strip(), workload_id}:
            items[idx] = parsed
            replaced = True
            break
    if not replaced:
        items.append(parsed)

    raw["version"] = raw.get("version", 1)
    raw["workloads"] = items
    write_path.parent.mkdir(parents=True, exist_ok=True)
    write_path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=False), encoding="utf-8")
    invalidate_catalog_caches()
    return _redirect_with_success(
        f"/workloads?edit={urllib.parse.quote(workload_id)}", f"Saved workload template: {workload_id}"
    )


@router.post("/workloads/delete")
def delete_workload_template(workload_id: str = Form(...)):
    """Delete an operator-managed workload YAML entry from the editable catalog."""
    if not workload_id.strip():
        raise HTTPException(status_code=400, detail="Missing workload id")
    write_path = _workloads_catalog_write_path()
    raw: dict = {"version": 1, "workloads": []}
    if write_path.exists():
        raw = yaml.safe_load(write_path.read_text(encoding="utf-8")) or raw
    elif settings.workloads_catalog_path.exists():
        raw = yaml.safe_load(settings.workloads_catalog_path.read_text(encoding="utf-8")) or raw
    items = [item for item in raw.get("workloads", []) if item.get("id") != workload_id.strip()]
    raw["workloads"] = items
    write_path.parent.mkdir(parents=True, exist_ok=True)
    write_path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=False), encoding="utf-8")
    invalidate_catalog_caches()
    return _redirect_with_success("/workloads", f"Deleted workload template: {workload_id}")


@router.get("/ssh-keys", response_class=HTMLResponse)
def admin_ssh_keys_page(request: Request):
    return templates.TemplateResponse(
        request,
        "admin_ssh_keys.html",
        {
            "keys": list_keys(),
            "message": request.query_params.get("message"),
            # Error-by-default: only this app's own redirects opt into the green banner.
            "message_type": "success" if request.query_params.get("message_type") == "success" else "error",
            "auth_enabled": settings.auth_enabled,
            **_base_ctx(request),
        },
    )


@router.post("/api-keys/generate", response_class=HTMLResponse)
def admin_api_keys_generate(request: Request):
    response = templates.TemplateResponse(
        request,
        "admin_ssh_keys.html",
        {
            "keys": list_keys(),
            "api_token": generate_admin_api_token(),
            "auth_enabled": settings.auth_enabled,
            **_base_ctx(request),
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/ssh-keys/generate")
def admin_ssh_keys_generate(name: str = Form(...)):
    name = name.strip()
    try:
        generate_key(name)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse(url="/ssh-keys", status_code=303)


@router.post("/ssh-keys/set-active")
def admin_ssh_keys_set_active(name: str = Form(...)):
    try:
        set_active(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse(url="/ssh-keys", status_code=303)


@router.post("/ssh-keys/delete")
def admin_ssh_keys_delete(name: str = Form(...)):
    try:
        delete_key(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse(url="/ssh-keys", status_code=303)


@router.post("/ssh-keys/bulk-delete")
def admin_ssh_keys_bulk_delete(names: list[str] = Form(default=[])):
    deleted = 0
    skipped = 0
    for name in names:
        try:
            delete_key(name)
            deleted += 1
        except ValueError:
            skipped += 1
    return _redirect_with_success("/ssh-keys", f"Claves eliminadas: {deleted}. Omitidas: {skipped}.")


@router.get("/playground", response_class=HTMLResponse)
def playground_page(request: Request) -> HTMLResponse:
    """Render Playground front door; orchestration work happens through dedicated Playground routes."""
    return templates.TemplateResponse(
        request,
        "playground.html",
        {**_base_ctx(request), "packages_enabled": settings.packages_enabled},
    )
