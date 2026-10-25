"""HTML route handlers for Volatility 3 symbol readiness and ISF build workflows."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..api.schemas import JobCreate, JobOut
from ..catalog import is_windows_family, load_attackers, load_network_nodes
from ..core.config import settings as _default_settings
from ..db import session_scope as _default_session_scope
from ..db.repositories import JobRepository
from ..forensics import api as volatility_api
from ..forensics.vol3_tools import get_vol3_readiness
from ..images.image_inspection import ISF_SUPPORTED_DISTROS
from ..jobs.job_services import (
    create_job_service,
    list_scenarios_service,
    list_targets_service,
)
from .job_context import _base_ctx, templates
from .jobs_helpers import redirect_with_message as _redirect_with_message
from .jobs_helpers import translate_scenarios as _translate_scenarios

logger = logging.getLogger(__name__)

router = APIRouter()


class _SettingsProxy:
    """Read-only test seam: resolves astinuslab.ui.settings on each access.

    Kept deliberately (B03 review): many tests monkeypatch
    astinuslab.ui.settings / .session_scope; the proxy is read-only
    indirection with no global mutation. The canonical /api/v1 routes in
    volatility_api use real module imports and are unaffected.
    """

    def __getattr__(self, name):
        import astinuslab.ui as ui_mod

        current = getattr(ui_mod, "settings", _default_settings)
        if current is self:
            current = _default_settings
        try:
            return getattr(current, name)
        except (AttributeError, TypeError):
            return getattr(_default_settings, name)


class _SessionScopeProxy:
    """Compatibility proxy for tests and callers that monkeypatch astinuslab.ui.session_scope."""

    def __call__(self, *args, **kwargs):
        import astinuslab.ui as ui_mod

        current = getattr(ui_mod, "session_scope", _default_session_scope)
        if current is self:
            current = _default_session_scope
        return current(*args, **kwargs)


settings = _SettingsProxy()
session_scope = _SessionScopeProxy()


def _non_symbol_image_refs() -> set[str]:
    refs: set[str] = set()
    for entry in [*load_attackers(), *load_network_nodes()]:
        image_ref = str(entry.get("image_ref") or "").strip()
        if image_ref:
            refs.add(image_ref)
    return refs


def _is_linux_symbol_target(target: dict, non_symbol_refs: set[str] | None = None) -> bool:
    if not target.get("enabled"):
        return False
    if is_windows_family(target.get("family")):
        return False
    role = str(target.get("role") or "target").strip().lower()
    if role not in {"target", "vm"}:
        return False
    cloud_init_profile = str(target.get("cloud_init_profile") or "").strip().lower()
    if cloud_init_profile in {"attacker", "network", "appliance", "router"}:
        return False
    image_ref = str(target.get("image_ref") or "").strip()
    if image_ref and image_ref in (non_symbol_refs or _non_symbol_image_refs()):
        return False
    distro = str(target.get("distro") or "").strip().lower()
    return distro in ISF_SUPPORTED_DISTROS


def _linux_symbol_targets() -> list[dict]:
    non_symbol_refs = _non_symbol_image_refs()
    return [target for target in list_targets_service() if _is_linux_symbol_target(target, non_symbol_refs)]


def _isf_scenarios() -> list[dict]:
    return [
        scenario
        for scenario in list_scenarios_service()
        if scenario.get("enabled") and scenario.get("mode") == "immediate"
    ]


def _default_isf_scenario_id(scenarios: list[dict]) -> str:
    immediate = next((scenario for scenario in scenarios if scenario.get("mode") == "immediate"), None)
    chosen = immediate or (scenarios[0] if scenarios else None)
    if chosen is None:
        raise HTTPException(status_code=400, detail="No ISF-compatible scenario is enabled")
    return str(chosen["id"])


def _build_history() -> list[dict]:
    with session_scope() as session:
        repo = JobRepository(session)
        jobs = repo.list_jobs()
        repo.prefetch_latest_artifacts(jobs)
        rows: list[dict] = []
        for job in jobs:
            if not job.build_vol3_isf:
                continue
            artifact = repo.get_latest_isf_artifact(job.id)
            rows.append(
                {
                    "job": JobOut.model_validate(job),
                    "artifact_path": artifact.path if artifact is not None else None,
                    "artifact_source": artifact.source if artifact is not None else None,
                    "artifact_validation_status": artifact.validation_status if artifact is not None else None,
                    "artifact_kernel_release": artifact.kernel_release
                    if artifact is not None
                    else job.detected_kernel_release,
                }
            )
        return rows


@router.get("/volatility", response_class=HTMLResponse)
def volatility_index(request: Request):
    """Render Volatility 3 symbol workbench separate from general Jobs UI."""
    base = _base_ctx(request)
    # Public view only: the dataclass carries `resolved_path`, which must never be
    # reachable from a template (invariant 11).
    vol3_readiness = get_vol3_readiness().public_dict()
    scenarios = _translate_scenarios(_isf_scenarios(), base["t"])
    history = _build_history()
    built_target_ids = {row["job"].target_id for row in history if row["artifact_path"]}
    return templates.TemplateResponse(
        request,
        "volatility.html",
        {
            "targets": _linux_symbol_targets(),
            "scenarios": scenarios,
            "default_scenario_id": _default_isf_scenario_id(scenarios) if scenarios else "",
            "history": history,
            "built_target_ids": built_target_ids,
            "message": request.query_params.get("message"),
            # Error-by-default: only a redirect this app emitted opts into the green
            # banner, so a crafted ?message= link cannot render as a trusted success.
            "message_type": "success" if request.query_params.get("message_type") == "success" else "error",
            "vol3_enabled": settings.vol3_enabled,
            "vol3_readiness": vol3_readiness,
            **base,
        },
    )


@router.post("/volatility/build")
def build_volatility_symbols(
    request: Request,
    target_id: str = Form(...),
    scenario_id: str = Form(""),
):
    """Create an ISF-only Job from the Volatility workbench."""
    wants_json = (
        "application/json" in request.headers.get("accept", "") or request.headers.get("x-requested-with") == "fetch"
    )
    # Only `vol3_enabled = false` blocks the queue.  An unready dwarf2json does not:
    # the build runs the provider chain first, and the local-cache and prebuilt
    # providers can deliver a ready ISF without it.  debuginfod and host_fetch return
    # a vmlinux and do require dwarf2json; those paths, and the live-guest transfer,
    # re-verify the pin immediately before use and fail there with the stable
    # dependency code.
    readiness = get_vol3_readiness()
    if not readiness.enabled:
        detail = "Volatility integration is disabled. Enable it in configuration and review docs/user/VOLATILITY3.md."
        if wants_json:
            return JSONResponse(
                {
                    "detail": detail,
                    "code": "dependency_unavailable",
                    "context": {"dependency_code": readiness.isf_build.code},
                },
                status_code=503,
            )
        return _redirect_with_message("/volatility", detail)
    scenarios = _isf_scenarios()
    if target_id not in {str(target["id"]) for target in _linux_symbol_targets()}:
        raise HTTPException(status_code=400, detail="Target is not available for Linux ISF builds")
    selected_scenario_id = scenario_id.strip() or _default_isf_scenario_id(scenarios)
    scenario = next((item for item in scenarios if item["id"] == selected_scenario_id), None)
    if scenario is None:
        raise HTTPException(status_code=400, detail="Scenario is not available for Volatility symbol builds")

    payload = JobCreate(
        target_id=target_id,
        scenario_id=selected_scenario_id,
        execution_mode=scenario["mode"],
        capture_type="none",
        build_vol3_isf=True,
    )
    try:
        with session_scope() as session:
            job = create_job_service(session, payload)
            job_id = job.id
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Volatility symbol build request failed: %s", exc)
        if wants_json:
            return JSONResponse({"detail": f"Symbol build request failed: {exc}"}, status_code=500)
        return _redirect_with_message("/volatility", f"Symbol build request failed: {exc}")
    if wants_json:
        return {"job_id": job_id, "status": "queued", "url": f"/jobs/{job_id}"}
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


# ── Deprecated workbench read API aliases ────────────────────────────────────


def _mark_deprecated(response: Response) -> None:
    response.headers["Deprecation"] = "true"


def _recent_runs(limit: int = 20) -> list[dict]:
    return volatility_api._recent_runs(limit)


@router.get("/volatility/api/dumps", deprecated=True)
def volatility_api_dumps(response: Response):
    _mark_deprecated(response)
    return volatility_api.volatility_api_dumps()


@router.get("/volatility/api/catalog", deprecated=True)
def volatility_api_catalog(response: Response, os: str = "linux"):
    _mark_deprecated(response)
    return volatility_api.volatility_api_catalog(os)


@router.get("/volatility/api/queue", deprecated=True)
def volatility_api_queue(response: Response, limit: int = 20):
    _mark_deprecated(response)
    return volatility_api.volatility_api_queue(limit)


@router.post("/volatility/api/runs", deprecated=True)
def volatility_api_run(response: Response, body: volatility_api.PluginRunRequest):
    _mark_deprecated(response)
    return volatility_api.volatility_api_run(body)


@router.get("/volatility/api/runs/latest", deprecated=True)
def volatility_api_run_latest(response: Response, job_id: str, plugin: str):
    _mark_deprecated(response)
    return volatility_api.volatility_api_run_latest(job_id, plugin)


@router.get("/volatility/api/runs/{job_id}/{run_id}", deprecated=True)
def volatility_api_run_result(response: Response, job_id: str, run_id: str):
    _mark_deprecated(response)
    return volatility_api.volatility_api_run_result(job_id, run_id)
