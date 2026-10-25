from __future__ import annotations

import hmac
import secrets

from fastapi import Request
from fastapi.responses import Response

from .api.errors import ApiError

CSRF_COOKIE_NAME = "astinuslab_csrf"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_FORM_MEDIA_TYPES = {"application/x-www-form-urlencoded", "multipart/form-data"}


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_token_for_request(request: Request) -> str:
    token = getattr(request.state, "csrf_token", None)
    if isinstance(token, str) and token:
        return token
    token = request.cookies.get(CSRF_COOKIE_NAME) or generate_csrf_token()
    request.state.csrf_token = token
    return token


def prepare_csrf_request(request: Request) -> None:
    csrf_token_for_request(request)


def apply_csrf_cookie(request: Request, response: Response) -> None:
    if getattr(request.state, "csrf_clear", False):
        return
    current = request.cookies.get(CSRF_COOKIE_NAME)
    token = csrf_token_for_request(request)
    if current == token and not getattr(request.state, "csrf_rotate", False):
        return
    is_html_response = "text/html" in response.headers.get("content-type", "")
    if not current and not is_html_response and not getattr(request.state, "csrf_rotate", False):
        return
    response.set_cookie(
        CSRF_COOKIE_NAME,
        token,
        path="/",
        httponly=False,
        samesite="strict",
        secure=request.url.scheme == "https",
    )


def rotate_csrf_token(request: Request) -> None:
    request.state.csrf_token = generate_csrf_token()
    request.state.csrf_rotate = True


def clear_csrf_cookie(request: Request, response: Response) -> None:
    request.state.csrf_clear = True
    response.delete_cookie(CSRF_COOKIE_NAME, path="/", httponly=False, samesite="strict")


async def require_csrf_request(request: Request) -> None:
    if request.method.upper() not in _UNSAFE_METHODS:
        return

    from .core.security import principal_from_bearer_token

    if principal_from_bearer_token(request.headers.get("Authorization")) is not None:
        return

    expected = request.cookies.get(CSRF_COOKIE_NAME)
    supplied = request.headers.get(CSRF_HEADER_NAME)
    if supplied is None:
        media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        if media_type in _FORM_MEDIA_TYPES:
            form = await request.form()
            form_value = form.get(CSRF_FORM_FIELD)
            supplied = form_value if isinstance(form_value, str) else None

    if not expected or not supplied:
        raise ApiError(403, "CSRF token required", "csrf_required")
    if not expected.isascii() or not supplied.isascii() or not hmac.compare_digest(expected, supplied):
        raise ApiError(403, "CSRF token invalid", "csrf_invalid")
