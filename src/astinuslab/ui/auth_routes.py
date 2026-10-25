from __future__ import annotations

import html

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..core import security as security_mod
from ..core.browser_sessions import COOKIE_NAME, SESSION_TTL_SECONDS, create_session, revoke_session
from ..csrf import clear_csrf_cookie, csrf_token_for_request, rotate_csrf_token

router = APIRouter()


def _safe_next(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def _login_page(*, next_path: str, csrf_token: str, error: str | None = None) -> HTMLResponse:
    escaped_next = html.escape(next_path, quote=True)
    error_html = f'<p class="form-error">{html.escape(error)}</p>' if error else ""
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AstinusLab Login</title>
  <link rel="stylesheet" href="/static/css/tokens.css">
  <link rel="stylesheet" href="/static/css/layout.css">
  <link rel="stylesheet" href="/static/css/components.css">
</head>
<body class="al-root">
  <main class="content" style="max-width: 420px; margin: 12vh auto;">
    <h1>AstinusLab</h1>
    <form method="post" action="/login" class="panel" style="display: grid; gap: 1rem;">
      <input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}">
      <input type="hidden" name="next" value="{escaped_next}">
      <label>Admin token
        <input name="token" type="password" autocomplete="current-password" autofocus required>
      </label>
      {error_html}
      <button type="submit">Log in</button>
    </form>
  </main>
</body>
</html>"""
    )


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    next_path = _safe_next(next)
    if not security_mod.settings.auth_enabled:
        return RedirectResponse(url=next_path, status_code=303)
    if security_mod.principal_from_session_cookie(request) is not None:
        return RedirectResponse(url=next_path, status_code=303)
    return _login_page(next_path=next_path, csrf_token=csrf_token_for_request(request))


@router.post("/login")
def login(request: Request, token: str = Form(...), next: str = Form("/")):
    next_path = _safe_next(next)
    if not security_mod.settings.auth_enabled:
        return RedirectResponse(url=next_path, status_code=303)
    token_hash = security_mod.settings.admin_api_token_hash
    if token_hash is None or not security_mod._token_matches(token, token_hash):
        response = _login_page(
            next_path=next_path,
            csrf_token=csrf_token_for_request(request),
            error="Invalid admin token",
        )
        response.status_code = 401
        return response

    session_token = create_session(
        token_hash,
        previous_token=request.cookies.get(COOKIE_NAME),
    )
    redirect_response = RedirectResponse(url=next_path, status_code=303)
    redirect_response.set_cookie(
        COOKIE_NAME,
        session_token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
    )
    rotate_csrf_token(request)
    return redirect_response


@router.post("/logout")
def logout(request: Request):
    revoke_session(request.cookies.get(COOKIE_NAME))
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, httponly=True, samesite="strict")
    clear_csrf_cookie(request, response)
    return response
