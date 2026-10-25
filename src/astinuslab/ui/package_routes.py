"""Server-rendered entry point for the local portable-lab package workflow."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..core.config import settings as _default_settings
from .job_context import _base_ctx, templates

router = APIRouter()


class _SettingsProxy:
    """Resolve application settings at request time for UI test seams."""

    def __getattr__(self, name):
        import astinuslab.ui as ui_mod

        current = getattr(ui_mod, "settings", _default_settings)
        if current is self:
            current = _default_settings
        try:
            return getattr(current, name)
        except AttributeError:
            return getattr(_default_settings, name)


settings = _SettingsProxy()


@router.get("/packages", response_class=HTMLResponse)
def packages_page(request: Request) -> HTMLResponse:
    if not settings.packages_enabled:
        raise HTTPException(status_code=404, detail="portable lab packages are disabled")
    return templates.TemplateResponse(
        request,
        "packages.html",
        {**_base_ctx(request), "packages_enabled": True},
    )
