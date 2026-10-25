from __future__ import annotations

import hashlib
import types
from typing import Any

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient

from astinuslab.core.browser_sessions import create_session
from astinuslab.core.config import Settings
from astinuslab.core.security import generate_admin_api_token, requires_admin_auth


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest.fixture()
def auth_settings():
    return types.SimpleNamespace(
        auth_enabled=True,
        admin_api_token_hash=f"sha256:{_sha256('admin-test-token')}",
        api_port=8000,
        browser_allowed_origins=[],
    )


@pytest.fixture()
def no_auth_settings():
    return types.SimpleNamespace(
        auth_enabled=False,
        admin_api_token_hash=None,
        api_port=8000,
        browser_allowed_origins=[],
    )


def test_network_bind_requires_auth_or_explicit_unsafe_override():
    with pytest.raises(ValidationError, match="auth_enabled=true"):
        Settings(api_host="0.0.0.0")


def test_network_bind_allows_enabled_admin_auth():
    settings = Settings(
        api_host="0.0.0.0",
        auth_enabled=True,
        admin_api_token_hash=f"sha256:{_sha256('admin-test-token')}",
        browser_allowed_origins=["https://astinuslab.example"],
    )

    assert settings.auth_enabled is True


def test_non_loopback_bind_requires_explicit_https_browser_origin():
    token_hash = f"sha256:{_sha256('admin-test-token')}"

    with pytest.raises(ValidationError, match="browser_allowed_origins"):
        Settings(api_host="0.0.0.0", auth_enabled=True, admin_api_token_hash=token_hash)

    with pytest.raises(ValidationError, match="HTTPS"):
        Settings(
            api_host="0.0.0.0",
            auth_enabled=True,
            admin_api_token_hash=token_hash,
            browser_allowed_origins=["http://astinuslab.example"],
        )


@pytest.mark.parametrize(
    "origin",
    [
        "https://astinuslab.example/path",
        "https://astinuslab.example?",
        "https://astinuslab.example#",
        "https://astinuslab.example:0",
    ],
)
def test_invalid_configured_browser_origin_is_rejected(origin):
    with pytest.raises(ValidationError, match="browser_allowed_origins"):
        Settings(browser_allowed_origins=[origin])


def test_enabled_auth_rejects_malformed_admin_token_hash():
    with pytest.raises(ValidationError, match="admin_api_token_hash"):
        Settings(
            auth_enabled=True,
            admin_api_token_hash="sha256:not-a-valid-hash",
        )


def test_generate_admin_api_token_returns_url_safe_token_and_hash():
    generated = generate_admin_api_token()

    assert generated["token"]
    assert len(generated["token"]) >= 43
    assert generated["token_hash"] == f"sha256:{_sha256(generated['token'])}"


def test_generate_admin_api_token_returns_unique_tokens():
    first = generate_admin_api_token()
    second = generate_admin_api_token()

    assert first["token"] != second["token"]
    assert first["token_hash"] != second["token_hash"]


@pytest.mark.parametrize(
    ("path", "requires_auth"),
    [
        ("/health", False),
        ("/ready", False),
        ("/static/app.css", False),
        ("/api/v1/jobs", True),
        ("/jobs/abc/delete", True),
        ("/volatility/api/dumps", True),
        ("/settings/services/stop-all", True),
        ("/", True),
        ("/healthx", True),
        ("/health/", True),
        ("/readyx", True),
        ("/ready/", True),
        ("/static", True),
    ],
)
def test_requires_admin_auth_fails_closed_except_public_allowlist(path, requires_auth):
    assert requires_admin_auth(path) is requires_auth


def _assert_admin_auth_required(response):
    assert response.status_code == 401
    assert response.json() == {"detail": "admin bearer token required"}
    assert response.headers["www-authenticate"] == "Bearer"


def test_api_returns_401_when_auth_enabled_and_bearer_token_missing(monkeypatch, auth_settings):
    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", auth_settings)

    with TestClient(app) as client:
        response = client.get("/api/v1/targets")

    _assert_admin_auth_required(response)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/jobs/new"),
        ("get", "/volatility/api/dumps"),
    ],
)
def test_non_public_routes_return_401_when_auth_enabled_and_bearer_token_missing(
    monkeypatch,
    auth_settings,
    method,
    path,
):
    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", auth_settings)

    with TestClient(app) as client:
        response = getattr(client, method)(path)

    _assert_admin_auth_required(response)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/jobs/new"),
        ("get", "/volatility/api/dumps"),
    ],
)
def test_non_public_routes_accept_valid_admin_bearer_when_auth_enabled(
    monkeypatch,
    auth_settings,
    method,
    path,
):
    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", auth_settings)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = getattr(client, method)(
            path,
            headers={"Authorization": "Bearer admin-test-token"},
        )

    assert response.status_code != 401


def test_non_public_routes_reject_wrong_admin_bearer_when_auth_enabled(monkeypatch, auth_settings):
    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", auth_settings)

    with TestClient(app) as client:
        response = client.get(
            "/volatility/api/dumps",
            headers={"Authorization": "Bearer wrong-token"},
        )

    _assert_admin_auth_required(response)


def test_auth_rejection_includes_security_headers(monkeypatch, auth_settings):
    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", auth_settings)

    with TestClient(app) as client:
        response = client.get("/api/v1/targets")

    assert response.status_code == 401
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"
    assert "content-security-policy" in response.headers


def test_api_accepts_admin_bearer_token_when_auth_enabled(monkeypatch, auth_settings):
    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", auth_settings)

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/targets",
            headers={"Authorization": "Bearer admin-test-token"},
        )

    assert response.status_code == 200


def test_openapi_schema_requires_admin_bearer_token_when_auth_enabled(monkeypatch, auth_settings):
    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", auth_settings)

    with TestClient(app) as client:
        response = client.get("/openapi.json")

    assert response.status_code == 401


def test_health_remains_public_when_auth_enabled(monkeypatch, auth_settings):
    import astinuslab.core.security as security_mod
    from astinuslab.core import fake_mode
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", auth_settings)
    # Pin fake-mode off so the body is deterministic regardless of the ambient
    # ASTINUSLAB_FAKE_* env (CI runs this suite with ASTINUSLAB_FAKE_LIBVIRT=1).
    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    monkeypatch.setattr(fake_mode, "FAKE_PLAYGROUND", False)
    monkeypatch.setattr(fake_mode, "FAKE_ORCHESTRATION", False)

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_remains_public_when_auth_enabled(monkeypatch, auth_settings):
    import astinuslab.core.security as security_mod
    from astinuslab import main as main_mod
    from astinuslab.core.operational_health import ReadinessResult

    monkeypatch.setattr(security_mod, "settings", auth_settings)
    monkeypatch.setattr(
        main_mod,
        "collect_readiness",
        lambda: ReadinessResult(
            payload={"status": "degraded", "checks": {}},
            http_status=200,
        ),
    )

    with TestClient(main_mod.app) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"


def test_browser_get_redirects_to_login_when_auth_enabled(monkeypatch, auth_settings):
    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", auth_settings)

    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/playground", headers={"Accept": "text/html"})

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/playground"


@pytest.mark.parametrize(
    "hostile",
    [
        "https://evil.example/",
        "http://evil.example/",
        "//evil.example/",
        "/\\evil.example",
        "\\\\evil.example",
        "javascript:alert(1)",
        "",
        None,
    ],
)
def test_safe_next_refuses_anything_that_leaves_this_origin(hostile):
    """`next` reaches the login page from a link, so it is attacker-supplied.

    Enumerated here rather than in the browser: this is a pure function, and one
    end-to-end assertion that the route is wired to it lives in
    tests/python/e2e/test_auth_ui.py.
    """
    from astinuslab.ui.auth_routes import _safe_next

    assert _safe_next(hostile) == "/"


@pytest.mark.parametrize("allowed", ["/", "/jobs", "/playground", "/help/user/PLAYGROUND.md"])
def test_safe_next_keeps_local_paths(allowed):
    from astinuslab.ui.auth_routes import _safe_next

    assert _safe_next(allowed) == allowed


def test_login_sets_admin_session_cookie(monkeypatch, auth_settings):
    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", auth_settings)

    csrf_token = "csrf-token"
    with TestClient(app, follow_redirects=False) as client:
        client.cookies.set("astinuslab_csrf", csrf_token)
        response = client.post(
            "/login",
            data={"token": "admin-test-token", "next": "/jobs", "csrf_token": csrf_token},
            headers={"Accept": "text/html"},
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/jobs"
    cookie = response.headers["set-cookie"]
    assert "astinuslab_admin_token=" in cookie
    assert "HttpOnly" in cookie
    assert "samesite=strict" in cookie.lower()


def test_cookie_authenticates_ui_route_when_auth_enabled(monkeypatch, auth_settings):
    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", auth_settings)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/jobs/new",
            cookies={"astinuslab_admin_token": create_session(auth_settings.admin_api_token_hash)},
            headers={"Accept": "text/html"},
        )

    assert response.status_code != 401
    assert response.status_code != 303


def test_login_rejects_bad_token(monkeypatch, auth_settings):
    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", auth_settings)

    csrf_token = "csrf-token"
    with TestClient(app, follow_redirects=False) as client:
        client.cookies.set("astinuslab_csrf", csrf_token)
        response = client.post(
            "/login",
            data={"token": "wrong", "next": "/jobs", "csrf_token": csrf_token},
            headers={"Accept": "text/html"},
        )

    assert response.status_code == 401
    assert "astinuslab_admin_token" not in response.headers.get("set-cookie", "")


def test_logout_deletes_admin_session_cookie(monkeypatch, auth_settings):
    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", auth_settings)

    csrf_token = "csrf-token"
    with TestClient(app, follow_redirects=False) as client:
        client.cookies.set("astinuslab_csrf", csrf_token)
        response = client.post(
            "/logout",
            data={"csrf_token": csrf_token},
            headers={"Accept": "text/html"},
        )

    assert response.status_code == 303
    assert "astinuslab_admin_token" in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]


def test_cross_origin_post_is_rejected_when_auth_disabled(monkeypatch, no_auth_settings):
    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", no_auth_settings)

    with TestClient(app, follow_redirects=False) as client:
        response = client.post("/login", data={"token": "unused"}, headers={"Origin": "https://evil.example"})

    assert response.status_code == 403
    assert response.json()["code"] == "cross_origin_forbidden"


def test_cross_origin_post_is_rejected_with_valid_cookie(monkeypatch, auth_settings):
    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", auth_settings)

    with TestClient(app, follow_redirects=False) as client:
        response = client.post(
            "/logout",
            cookies={"astinuslab_admin_token": create_session(auth_settings.admin_api_token_hash)},
            headers={"Origin": "https://evil.example"},
        )

    assert response.status_code == 403
    assert response.json()["code"] == "cross_origin_forbidden"


def test_cross_site_fetch_metadata_is_rejected(monkeypatch, no_auth_settings):
    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", no_auth_settings)

    with TestClient(app, follow_redirects=False) as client:
        response = client.post(
            "/login",
            data={"token": "unused"},
            headers={"Origin": "http://127.0.0.1:8000", "Sec-Fetch-Site": "cross-site"},
        )

    assert response.status_code == 403
    assert response.json()["code"] == "cross_origin_forbidden"


def test_same_origin_post_reaches_route(monkeypatch, no_auth_settings):
    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", no_auth_settings)

    csrf_token = "csrf-token"
    with TestClient(app, follow_redirects=False) as client:
        client.cookies.set("astinuslab_csrf", csrf_token)
        response = client.post(
            "/login",
            data={"token": "unused", "csrf_token": csrf_token},
            headers={"Origin": "http://127.0.0.1:8000"},
        )

    assert response.status_code == 303


def test_bearer_api_post_without_browser_headers_reaches_route(monkeypatch, auth_settings):
    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", auth_settings)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/v1/jobs",
            headers={"Authorization": "Bearer admin-test-token"},
            json={},
        )

    assert response.status_code != 403


@pytest.mark.parametrize(
    "origin",
    [
        "not an origin",
        "http://127.0.0.1:8000.evil.example",
        "http://127.0.0.1:8001",
        "http://user@127.0.0.1:8000",
        "http://127.0.0.1:8000/path",
        "http://127.0.0.1:8000?",
        "http://127.0.0.1:8000#",
        "http://127.0.0.1:0",
    ],
)
def test_invalid_or_untrusted_origin_is_rejected(monkeypatch, no_auth_settings, origin):
    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", no_auth_settings)

    with TestClient(app, follow_redirects=False) as client:
        response = client.post("/login", data={"token": "unused"}, headers={"Origin": origin})

    assert response.status_code == 403
    assert response.json()["code"] == "cross_origin_forbidden"


def test_browser_form_without_csrf_token_is_rejected(monkeypatch, no_auth_settings):
    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", no_auth_settings)

    with TestClient(app, follow_redirects=False) as client:
        response = client.post(
            "/login",
            data={"token": "unused"},
            headers={"Origin": "http://127.0.0.1:8000"},
        )

    assert response.status_code == 403
    assert response.json()["code"] == "csrf_required"


def test_browser_form_without_origin_still_requires_csrf(monkeypatch, no_auth_settings):
    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", no_auth_settings)

    with TestClient(app, follow_redirects=False) as client:
        response = client.post(
            "/login",
            data={"token": "unused"},
            headers={"Sec-Fetch-Site": "same-origin", "Accept": "text/html"},
        )

    assert response.status_code == 403
    assert response.json()["code"] == "csrf_required"


def test_same_origin_browser_form_with_opaque_origin_reaches_route(monkeypatch, no_auth_settings):
    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", no_auth_settings)
    token = "csrf-token"

    with TestClient(app, follow_redirects=False) as client:
        response = client.post(
            "/login",
            data={"token": "unused", "csrf_token": token},
            cookies={"astinuslab_csrf": token},
            headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"},
        )

    assert response.status_code == 303


def test_opaque_origin_form_still_requires_csrf(monkeypatch, no_auth_settings):
    """Trusting Sec-Fetch-Site opens the origin gate, never the CSRF one.

    The origin check is middleware and the token check is a route dependency, so
    admitting an opaque same-origin form leans entirely on CSRF still running
    behind it. Without this, reordering those two would leave the suite green
    while the relaxation turned into a bypass: the positive case asserts only
    that the request reaches the route, which it would either way.
    """
    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", no_auth_settings)

    with TestClient(app, follow_redirects=False) as client:
        response = client.post(
            "/login",
            data={"token": "unused"},
            headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"},
        )

    assert response.status_code == 403
    assert response.json()["code"] == "csrf_required"


def test_cookie_authenticated_form_without_browser_headers_still_requires_csrf(monkeypatch, auth_settings):
    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", auth_settings)

    with TestClient(app, follow_redirects=False) as client:
        client.cookies.set("astinuslab_admin_token", create_session(auth_settings.admin_api_token_hash))
        response = client.post("/set-lang", data={"lang": "en", "next": "/jobs"})

    assert response.status_code == 403
    assert response.json()["code"] == "csrf_required"


def test_browser_form_with_mismatched_csrf_token_is_rejected(monkeypatch, no_auth_settings):
    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", no_auth_settings)

    with TestClient(app, follow_redirects=False) as client:
        response = client.post(
            "/login",
            data={"token": "unused", "csrf_token": "wrong-token"},
            cookies={"astinuslab_csrf": "expected-token"},
            headers={"Origin": "http://127.0.0.1:8000"},
        )

    assert response.status_code == 403
    assert response.json()["code"] == "csrf_invalid"


@pytest.mark.parametrize("source", ["form", "header", "cookie"])
def test_non_ascii_csrf_token_is_rejected_without_server_error(monkeypatch, no_auth_settings, source):
    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", no_auth_settings)
    headers = {
        b"Origin": b"http://127.0.0.1:8000",
        b"Cookie": b"astinuslab_csrf=expected-token",
    }
    data = {"token": "unused", "csrf_token": "expected-token"}
    if source == "form":
        data["csrf_token"] = "non-ascii-\N{LATIN SMALL LETTER E WITH ACUTE}"
    elif source == "header":
        headers[b"X-CSRF-Token"] = b"non-ascii-\xe9"
    else:
        headers[b"Cookie"] = b'astinuslab_csrf="non-ascii-\xe9"'

    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        response = client.post("/login", data=data, headers=headers)

    assert response.status_code == 403
    assert response.json()["code"] == "csrf_invalid"


def test_browser_form_with_valid_csrf_token_reaches_route(monkeypatch, no_auth_settings):
    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", no_auth_settings)
    token = "csrf-token"

    with TestClient(app, follow_redirects=False) as client:
        response = client.post(
            "/login",
            data={"token": "unused", "csrf_token": token},
            cookies={"astinuslab_csrf": token},
            headers={"Origin": "http://127.0.0.1:8000"},
        )

    assert response.status_code == 303


def test_safe_html_request_bootstraps_csrf_cookie(monkeypatch, no_auth_settings):
    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", no_auth_settings)

    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/jobs/new")

    cookie = response.headers["set-cookie"]
    assert "astinuslab_csrf=" in cookie
    assert "Path=/" in cookie
    assert "samesite=strict" in cookie.lower()
    assert "HttpOnly" not in cookie


def test_login_rotates_csrf_cookie(monkeypatch, auth_settings):
    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", auth_settings)
    old_token = "old-csrf-token"

    with TestClient(app, follow_redirects=False) as client:
        response = client.post(
            "/login",
            data={"token": "admin-test-token", "csrf_token": old_token},
            cookies={"astinuslab_csrf": old_token},
            headers={"Origin": "http://127.0.0.1:8000"},
        )

    cookie = response.headers["set-cookie"]
    assert response.status_code == 303
    assert "astinuslab_csrf=" in cookie
    assert f"astinuslab_csrf={old_token}" not in cookie


def test_logout_clears_admin_and_csrf_cookies(monkeypatch, auth_settings):
    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", auth_settings)
    token = "csrf-token"

    with TestClient(app, follow_redirects=False) as client:
        client.cookies.set("astinuslab_admin_token", create_session(auth_settings.admin_api_token_hash))
        client.cookies.set("astinuslab_csrf", token)
        response = client.post(
            "/logout",
            data={"csrf_token": token},
            headers={"Origin": "http://127.0.0.1:8000"},
        )

    cookie = response.headers["set-cookie"]
    assert response.status_code == 303
    assert "astinuslab_admin_token=" in cookie
    assert "astinuslab_csrf=" in cookie
    assert cookie.count("Max-Age=0") == 2


def test_multipart_csrf_guard_reuses_fastapi_form_parse(monkeypatch):
    from fastapi import Depends, FastAPI, File, Form
    from starlette.requests import Request

    import astinuslab.core.security as security_mod

    guard = getattr(security_mod, "require_csrf_request", None)
    assert guard is not None, "central CSRF route dependency is missing"

    stream_calls = 0
    original_stream = Request.stream

    async def counted_stream(request):
        nonlocal stream_calls
        stream_calls += 1
        async for chunk in original_stream(request):
            yield chunk

    monkeypatch.setattr(Request, "stream", counted_stream)
    probe = FastAPI(dependencies=[Depends(guard)])

    @probe.post("/upload")
    async def upload(file: Any = File(...), csrf_token: str = Form(...)):
        return {"body": (await file.read()).decode(), "csrf_token": csrf_token}

    token = "csrf-token"
    with TestClient(probe) as client:
        response = client.post(
            "/upload",
            data={"csrf_token": token},
            files={"file": ("image.qcow2", b"large-upload-stream", "application/octet-stream")},
            cookies={"astinuslab_csrf": token},
            headers={"Origin": "http://127.0.0.1:8000"},
        )

    assert response.status_code == 200
    assert response.json()["body"] == "large-upload-stream"
    assert stream_calls == 1
