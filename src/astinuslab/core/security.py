from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from ..csrf import require_csrf_request as require_csrf_request
from . import browser_sessions
from .config import normalize_browser_origin, settings


@dataclass(frozen=True)
class Principal:
    role: str = "admin"


_PUBLIC_EXACT_PATHS = {"/health", "/ready", "/login", "/logout"}
_PUBLIC_PREFIXES = ("/static/",)
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def requires_admin_auth(path: str) -> bool:
    """Fail closed: when auth is enabled, everything except public allowlist requires admin token."""
    return path not in _PUBLIC_EXACT_PATHS and not path.startswith(_PUBLIC_PREFIXES)


def _normalize_sha256_hash(value: str) -> str:
    if value.startswith("sha256:"):
        return value.removeprefix("sha256:")
    return value


def _token_matches(token: str, token_hash: str | None) -> bool:
    if not token_hash:
        return False
    expected = _normalize_sha256_hash(token_hash).lower()
    if len(expected) != 64:
        return False
    actual = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return hmac.compare_digest(actual, expected)


def generate_admin_api_token() -> dict[str, str]:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return {"token": token, "token_hash": f"sha256:{token_hash}"}


def principal_from_bearer_token(authorization: str | None) -> Principal | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    if not _token_matches(token, settings.admin_api_token_hash):
        return None
    return Principal(role="admin")


def principal_from_session_cookie(request: Request) -> Principal | None:
    token = request.cookies.get(browser_sessions.COOKIE_NAME)
    if not browser_sessions.session_is_valid(token, settings.admin_api_token_hash):
        return None
    return Principal(role="admin")


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def allowed_browser_origins() -> frozenset[str]:
    configured = settings.browser_allowed_origins
    if configured:
        normalized = {normalize_browser_origin(origin) for origin in configured}
        return frozenset(origin for origin in normalized if origin is not None)
    port = settings.api_port
    return frozenset(
        {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
            f"http://[::1]:{port}",
        }
    )


def require_same_origin_request(request: Request) -> Response | None:
    if request.method.upper() not in _UNSAFE_METHODS:
        return None
    fetch_site = request.headers.get("Sec-Fetch-Site", "").lower()
    if fetch_site == "cross-site":
        return _cross_origin_response()
    origin = request.headers.get("Origin")
    if origin is None or (origin == "null" and fetch_site == "same-origin"):
        return None
    normalized = normalize_browser_origin(origin)
    if normalized is None or normalized not in allowed_browser_origins():
        return _cross_origin_response()
    return None


def _cross_origin_response() -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={"detail": "cross-origin browser mutation denied", "code": "cross_origin_forbidden"},
    )


def require_admin_request(request: Request) -> Response | None:
    if not settings.auth_enabled or not requires_admin_auth(request.url.path):
        return None

    principal = principal_from_bearer_token(request.headers.get("Authorization"))
    if principal is None:
        principal = principal_from_session_cookie(request)
    if principal is None:
        if request.method == "GET" and _wants_html(request):
            return RedirectResponse(url=f"/login?next={request.url.path}", status_code=303)
        return JSONResponse(
            status_code=401,
            content={"detail": "admin bearer token required"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    request.state.principal = principal
    return None
