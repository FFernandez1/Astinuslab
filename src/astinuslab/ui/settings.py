"""Settings page: services / network / storage controls."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..app_settings import load_app_settings, save_app_settings
from ..core.config import settings
from ..core.system_status import (
    fake_mode_status,
    format_uptime,
    libvirt_leases,
    libvirt_network_info,
    service_status,
)
from ..csrf import csrf_token_for_request
from ..db import session_scope
from ..i18n import LANG_NAMES, get_translations
from ..storage.retention import run_retention
from ..storage.storage_admin_support import (
    clear_vol3_symbol_cache,
    reset_libvirt_network,
    schedule_stop_services,
    stop_pid_from_file,
    storage_stats,
)
from ..web.resources import TEMPLATES_DIR
from .asset_version import register_asset_version

logger = logging.getLogger(__name__)
settings_router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["format_uptime"] = format_uptime
register_asset_version(templates)


_SERVICE_DEFS = [
    {
        "id": "api",
        "label": "astinuslab-api",
        "unit": "astinuslab-api.service",
        "description": "Control plane HTTP API. Backs the console and CLI.",
        "pid_file_name": "api.pid",
    },
    {
        "id": "runner",
        "label": "astinuslab-runner",
        "unit": "astinuslab-runner.service",
        "description": "Host-side job runner. Drives libvirt + capture orchestration.",
        "pid_file_name": "runner.pid",
    },
]


def _base_ctx(request: Request) -> dict[str, Any]:
    from .jobs import _get_lang  # reuse the language cookie helper

    lang = _get_lang(request)
    return {
        "t": get_translations(lang),
        "lang": lang,
        "supported_langs": LANG_NAMES,
        "path": request.url.path,
        "csrf_token": csrf_token_for_request(request),
    }


def _collect_services() -> list[dict[str, Any]]:
    out = []
    for spec in _SERVICE_DEFS:
        pid_file = settings.storage_dir / spec["pid_file_name"]
        status = service_status(
            label=spec["label"],
            unit=spec["unit"],
            description=spec["description"],
            pid_file=pid_file,
        )
        status["id"] = spec["id"]
        out.append(status)
    return out


def _collect_network() -> dict[str, Any]:
    return libvirt_network_info(settings.libvirt_network, uri=settings.libvirt_uri)


def _collect_leases() -> list[dict[str, Any]]:
    return libvirt_leases(settings.libvirt_network, uri=settings.libvirt_uri)


def _collect_storage() -> dict[str, Any]:
    app = load_app_settings()
    stats = storage_stats(settings)
    min_gib = int(app.get("disk_guardrail", {}).get("min_free_gib", 0) or 0)
    return {
        "stats": stats,
        "retention": app["retention"],
        "guardrail": {
            "min_free_gib": min_gib,
            "under_pressure": bool(min_gib) and stats["free_gb"] < min_gib,
        },
    }


def _services_summary(services: list[dict[str, Any]]) -> dict[str, Any]:
    stopped = [s for s in services if s["state"] != "running"]
    return {
        "all_running": not stopped,
        "stopped_count": len(stopped),
    }


def _page_ctx(request: Request, toast: dict | None = None) -> dict[str, Any]:
    services = _collect_services()
    storage = _collect_storage()
    return {
        "request": request,
        "services": services,
        "services_summary": _services_summary(services),
        "network": _collect_network(),
        "leases": _collect_leases(),
        "storage": storage,
        "fake_mode": fake_mode_status(),
        "base_dir": str(settings.storage_dir),
        "toast": toast,
        **_base_ctx(request),
    }


@settings_router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return templates.TemplateResponse(request, "admin_settings.html", {**_page_ctx(request), **_activity_ctx(request)})


def _services_ctx(request: Request, toast: dict | None = None) -> dict[str, Any]:
    services = _collect_services()
    return {
        "request": request,
        "services": services,
        "services_summary": _services_summary(services),
        "toast": toast,
        **_base_ctx(request),
    }


def _network_ctx(request: Request, toast: dict | None = None) -> dict[str, Any]:
    return {
        "request": request,
        "network": _collect_network(),
        "leases": _collect_leases(),
        "toast": toast,
        **_base_ctx(request),
    }


def _activity_ctx(request: Request) -> dict[str, Any]:
    """Recent activity, read from the rows the platform already writes.

    Degrades to an empty report rather than breaking the page: the Settings page
    is where an operator goes when something is wrong, so a metrics query that
    fails must not take the service and storage panels down with it.
    """
    from ..core.operational_metrics import collect_metrics

    try:
        with session_scope() as session:
            metrics = collect_metrics(session)
    except Exception:
        logger.warning("settings activity panel unavailable", exc_info=True)
        metrics = {"actions": [], "jobs": {}, "queue": {"queued": 0, "running": 0, "oldest_queued_age_seconds": None}}
    return {"request": request, "metrics": metrics, **_base_ctx(request)}


def _storage_ctx(request: Request, toast: dict | None = None) -> dict[str, Any]:
    return {
        "request": request,
        "storage": _collect_storage(),
        "base_dir": str(settings.storage_dir),
        "toast": toast,
        **_base_ctx(request),
    }


@settings_router.get("/settings/fragment/services", response_class=HTMLResponse)
def settings_fragment_services(request: Request):
    return templates.TemplateResponse(request, "partials/settings_services.html", _services_ctx(request))


@settings_router.get("/settings/fragment/network", response_class=HTMLResponse)
def settings_fragment_network(request: Request):
    return templates.TemplateResponse(request, "partials/settings_network.html", _network_ctx(request))


@settings_router.get("/settings/fragment/storage", response_class=HTMLResponse)
def settings_fragment_storage(request: Request):
    return templates.TemplateResponse(request, "partials/settings_storage.html", _storage_ctx(request))


@settings_router.get("/settings/fragment/activity", response_class=HTMLResponse)
def settings_fragment_activity(request: Request):
    return templates.TemplateResponse(request, "partials/settings_activity.html", _activity_ctx(request))


def _save_retention_settings(dumps_days: str, logs_days: str, auto_enabled: str) -> None:
    """Persist retention windows. Raises ValueError on invalid input."""
    save_app_settings(
        {
            "retention": {
                "dumps_days": dumps_days,
                "logs_days": logs_days,
                "auto_enabled": bool(auto_enabled),
            }
        }
    )


def _retention_error_response(request: Request, exc: ValueError) -> HTMLResponse:
    toast = {"section": "storage", "ok": False, "message": str(exc)}
    return templates.TemplateResponse(request, "partials/settings_storage.html", _storage_ctx(request, toast=toast))


@settings_router.post("/settings/retention", response_class=HTMLResponse)
def settings_retention_save(
    request: Request,
    dumps_days: str = Form(...),
    logs_days: str = Form(...),
    auto_enabled: str = Form(""),
):
    try:
        _save_retention_settings(dumps_days, logs_days, auto_enabled)
    except ValueError as exc:
        return _retention_error_response(request, exc)
    toast = {"section": "storage", "ok": True, "message": "Retention saved."}
    return templates.TemplateResponse(request, "partials/settings_storage.html", _storage_ctx(request, toast=toast))


@settings_router.post("/settings/retention/run", response_class=HTMLResponse)
def settings_retention_run(
    request: Request,
    dumps_days: str = Form(...),
    logs_days: str = Form(...),
    auto_enabled: str = Form(""),
):
    try:
        _save_retention_settings(dumps_days, logs_days, auto_enabled)
    except ValueError as exc:
        return _retention_error_response(request, exc)
    result = run_retention()
    toast = {
        "section": "storage",
        "ok": True,
        "message": f"Retention ran. dumps_deleted: {result['dumps_deleted']}, logs_deleted: {result['logs_deleted']}",
    }
    return templates.TemplateResponse(request, "partials/settings_storage.html", _storage_ctx(request, toast=toast))


@settings_router.post("/settings/retention/preview", response_class=HTMLResponse)
def settings_retention_preview(
    request: Request,
    dumps_days: str = Form(...),
    logs_days: str = Form(...),
    auto_enabled: str = Form(""),
):
    from ..app_settings import _parse_retention_days

    try:
        candidate = (_parse_retention_days(dumps_days, "dumps_days"), _parse_retention_days(logs_days, "logs_days"))
    except ValueError as exc:
        return _retention_error_response(request, exc)
    result = run_retention(dry_run=True, retention_days=candidate)
    toast = {
        "section": "storage",
        "level": "info",
        "message": f"Dry run — next cleanup would delete {result['dumps_deleted']} dumps "
        f"and {result['logs_deleted']} logs (nothing deleted).",
    }
    context = _storage_ctx(request, toast=toast)
    context["storage"]["retention"] = {
        **context["storage"]["retention"],
        "dumps_days": candidate[0],
        "logs_days": candidate[1],
        "auto_enabled": bool(auto_enabled),
    }
    return templates.TemplateResponse(request, "partials/settings_storage.html", context)


@settings_router.post("/settings/symbols/clear", response_class=HTMLResponse)
def settings_symbols_clear(request: Request):
    result = clear_vol3_symbol_cache(settings)
    toast = {"section": "storage", "ok": bool(result.get("ok")), "message": result.get("message", "")}
    return templates.TemplateResponse(request, "partials/settings_storage.html", _storage_ctx(request, toast=toast))


@settings_router.post("/settings/guardrail", response_class=HTMLResponse)
def settings_guardrail_save(request: Request, min_free_gib: str = Form(...)):
    try:
        save_app_settings({"disk_guardrail": {"min_free_gib": min_free_gib}})
    except ValueError as exc:
        return _retention_error_response(request, exc)
    value = int(min_free_gib)
    message = f"Disk guardrail set: block new jobs under {value} GiB free." if value else "Disk guardrail disabled."
    toast = {"section": "storage", "ok": True, "message": message}
    return templates.TemplateResponse(request, "partials/settings_storage.html", _storage_ctx(request, toast=toast))


def _stop_pid_file(pid_file) -> bool:
    return stop_pid_from_file(pid_file)


def _service_def(service_id: str) -> dict:
    for spec in _SERVICE_DEFS:
        if spec["id"] == service_id:
            return spec
    raise HTTPException(status_code=404, detail=f"unknown service {service_id}")


@settings_router.post("/settings/services/stop-all", response_class=HTMLResponse)
def settings_services_stop_all(request: Request):
    result = schedule_stop_services(settings, stop_pid_from_file)
    toast = {"section": "services", "ok": bool(result.get("ok")), "message": result.get("message", "")}
    return templates.TemplateResponse(request, "partials/settings_services.html", _services_ctx(request, toast=toast))


@settings_router.post("/settings/services/{service_id}/stop", response_class=HTMLResponse)
def settings_service_stop(request: Request, service_id: str):
    spec = _service_def(service_id)
    pid_file = settings.storage_dir / spec["pid_file_name"]
    ok = _stop_pid_file(pid_file)
    toast = {
        "section": "services",
        "ok": ok,
        "message": f"{spec['label']} stop requested" if ok else f"{spec['label']} was not running",
    }
    return templates.TemplateResponse(request, "partials/settings_services.html", _services_ctx(request, toast=toast))


@settings_router.post("/settings/services/{service_id}/restart", response_class=HTMLResponse)
def settings_service_restart(request: Request, service_id: str):
    spec = _service_def(service_id)
    toast = {
        "section": "services",
        "level": "info",
        "message": f"{spec['label']} is managed by systemd — restart from the host: "
        f"`astinuslab.sh restart` (or `systemctl restart {spec['unit']}`).",
    }
    return templates.TemplateResponse(request, "partials/settings_services.html", _services_ctx(request, toast=toast))


@settings_router.post("/settings/network/restart", response_class=HTMLResponse)
def settings_network_restart(request: Request):
    result = reset_libvirt_network(settings)
    toast = {"section": "network", "ok": bool(result.get("ok")), "message": result.get("message", "")}
    return templates.TemplateResponse(request, "partials/settings_network.html", _network_ctx(request, toast=toast))


# Compatibility: importing astinuslab.ui.settings may replace package attribute
# `astinuslab.ui.settings`; expose config settings methods/attrs at module level.
def model_dump(*args, **kwargs):
    return settings.model_dump(*args, **kwargs)


def __getattr__(name: str):
    return getattr(settings, name)
