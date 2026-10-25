from __future__ import annotations

import logging
import secrets
from time import perf_counter
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware

from .api import router
from .api.errors import ApiError, api_error_handler
from .core import fake_mode
from .core.config import settings
from .core.logging_config import configure_logging, log_file_for
from .core.operational_health import collect_readiness
from .core.request_context import REQUEST_ID_HEADER, bind_request_id, reset_request_id
from .core.security import require_admin_request, require_same_origin_request
from .csrf import apply_csrf_cookie, prepare_csrf_request, require_csrf_request
from .db.repositories import InvalidJobTransitionError
from .forensics.api import router as volatility_router
from .lab_packages.api import router as lab_packages_router
from .playground.api import router as playground_router
from .readiness import router as readiness_router
from .ui import ui_router
from .web.resources import STATIC_DIR
from .web.static_cache import StaticCacheFiles

configure_logging(
    settings.api_log_level,
    log_file=log_file_for("api"),
    max_bytes=settings.log_max_bytes,
    backup_count=settings.log_backup_count,
)

logger = logging.getLogger(__name__)


app = FastAPI(
    title="AstinusLab API",
    version="1.0.0",
    dependencies=[Depends(require_csrf_request)],
)


async def _typed_api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, ApiError):  # pragma: no cover - registered for ApiError only
        raise TypeError(f"handler registered for ApiError, got {type(exc).__name__}")
    return await api_error_handler(request, exc)


app.add_exception_handler(ApiError, _typed_api_error_handler)


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


@app.exception_handler(StarletteHTTPException)
async def not_found_page_or_json(request: Request, exc: StarletteHTTPException):
    """Answer a navigation with a page, and a client with JSON.

    A bookmarked link to a deleted Job used to hand the operator the API's error
    body -- `{"detail":"Job not found"}` on a blank page, with no navigation and
    no way back. The same split the auth gate already uses decides this: a
    browser navigation sends `Accept: text/html`, while fetch and XHR send
    `*/*`, so API callers keep the JSON they parse.
    """
    if exc.status_code == 404 and _wants_html(request):
        from .ui.job_context import _base_ctx, templates

        return templates.TemplateResponse(
            request,
            "error_not_found.html",
            {**_base_ctx(request), "detail": exc.detail},
            status_code=404,
        )
    # Same shape as FastAPI's own handler, headers included: the auth gate's
    # WWW-Authenticate on a 401 has to survive this.
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=getattr(exc, "headers", None),
    )


def _csp_header(nonce: str) -> str:
    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "object-src 'none'"
    )


def _apply_security_headers(request: Request, response: Response) -> None:
    nonce = getattr(request.state, "csp_nonce", None)
    if isinstance(nonce, str):
        response.headers["Content-Security-Policy"] = _csp_header(nonce)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> PlainTextResponse:
    response = PlainTextResponse("Internal Server Error", status_code=500)
    request_id = getattr(request.state, "request_id", None)
    if isinstance(request_id, str):
        response.headers[REQUEST_ID_HEADER] = request_id
    _apply_security_headers(request, response)
    return response


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    request_id, request_id_token = bind_request_id(request.headers.get(REQUEST_ID_HEADER))
    request.state.request_id = request_id
    started = perf_counter()
    status_code = 500
    try:
        request.state.csp_nonce = secrets.token_urlsafe(16)
        prepare_csrf_request(request)
        origin_response = require_same_origin_request(request)
        if origin_response is not None:
            response = origin_response
        else:
            auth_response = require_admin_request(request)
            if auth_response is not None:
                response = auth_response
            else:
                response = await call_next(request)

        apply_csrf_cookie(request, response)
        _apply_security_headers(request, response)
        response.headers[REQUEST_ID_HEADER] = request_id
        status_code = response.status_code
        return response
    except Exception:
        logger.exception(
            "http_request_failed method=%s path=%s",
            request.method,
            request.url.path,
            extra={"request_id": request_id},
        )
        raise
    finally:
        duration_ms = (perf_counter() - started) * 1000
        logger.info(
            "http_request method=%s path=%s status=%d duration_ms=%.3f",
            request.method,
            request.url.path,
            status_code,
            duration_ms,
            extra={"request_id": request_id},
        )
        reset_request_id(request_id_token)


# Registered last, so it wraps the security middleware above and compresses the
# finished response -- headers that middleware set survive into it. Nothing else
# can do this job: ops/ ships systemd units, not a reverse proxy, so the app is
# the only thing between the operator and ~64 KB of stylesheet per page load.
# Starlette excludes `text/event-stream` itself, which keeps the Playground's
# capture events out of a gzip window that would buffer them. Level 6 rather
# than the library default of 9: this host is also running the lab's VMs.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)


@app.exception_handler(InvalidJobTransitionError)
async def invalid_job_transition_handler(request: Request, exc: InvalidJobTransitionError):
    return JSONResponse(
        status_code=409,
        content={
            "detail": str(exc),
            "code": "invalid_job_transition",
            "context": {"from_status": exc.from_status, "to_status": exc.to_status},
            "from_status": exc.from_status,
            "to_status": exc.to_status,
        },
    )


app.mount(
    "/static",
    StaticCacheFiles(directory=str(STATIC_DIR)),
    name="static",
)

# Operator guides referenced by the readiness banners. ops/deploy/install.sh ships
# docs/user/ specifically so the path a banner names is a file the operator
# actually has; the rest of docs/ stays out of the installed tree. check_dir is
# off so a checkout without the directory still starts and simply 404s here, and
# the mount is deliberately absent from the auth allowlist in security.py -- it
# is served to signed-in operators, not anonymously.
app.mount(
    "/docs/user",
    StaticFiles(directory=str(settings.base_dir / "docs" / "user"), check_dir=False),
    name="user_docs",
)

app.include_router(ui_router)
from .ui.settings import settings_router  # noqa: E402

app.include_router(settings_router)
app.include_router(router)
if settings.packages_enabled:
    app.include_router(lab_packages_router)
app.include_router(playground_router)
app.include_router(volatility_router)
app.include_router(readiness_router)


@app.get("/health")
def health():
    payload: dict[str, Any] = {"status": "ok"}
    if fake_mode.fake_mode_active():
        payload["fake_mode"] = fake_mode.fake_mode_flags()
        payload["warning"] = fake_mode.fake_mode_banner()
    return payload


@app.get("/ready")
def ready() -> JSONResponse:
    result = collect_readiness()
    return JSONResponse(status_code=result.http_status, content=result.payload)
