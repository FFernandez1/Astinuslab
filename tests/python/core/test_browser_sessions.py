from __future__ import annotations

import hashlib
from datetime import timedelta
from types import SimpleNamespace

import pytest

from astinuslab.core import security

ADMIN_TOKEN = "browser-session-test-admin-token"


@pytest.fixture
def browser(client, monkeypatch):
    monkeypatch.setattr(
        security,
        "settings",
        SimpleNamespace(
            auth_enabled=True,
            admin_api_token_hash=hashlib.sha256(ADMIN_TOKEN.encode()).hexdigest(),
            api_port=8000,
            browser_allowed_origins=[],
        ),
    )
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    return client


def login(browser):
    browser.get("/login")
    response = browser.post(
        "/login",
        data={"token": ADMIN_TOKEN, "csrf_token": browser.cookies.get("astinuslab_csrf")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return browser.cookies.get("astinuslab_admin_token")


def test_browser_session_is_separate_from_permanent_bearer(browser):
    cookie = login(browser)
    assert cookie != ADMIN_TOKEN
    assert browser.get("/api/v1/jobs").status_code == 200
    browser.cookies.clear()
    assert browser.get("/api/v1/jobs", headers={"Authorization": f"Bearer {cookie}"}).status_code == 401
    assert browser.get("/api/v1/jobs", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}).status_code == 200


def test_logout_revokes_saved_session_cookie(browser):
    cookie = login(browser)
    response = browser.post(
        "/logout",
        headers={"X-CSRF-Token": browser.cookies.get("astinuslab_csrf")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert browser.cookies.get("astinuslab_admin_token") is None
    browser.cookies.set("astinuslab_admin_token", cookie)
    assert browser.get("/api/v1/jobs").status_code == 401


def test_permanent_token_is_not_accepted_as_browser_cookie(browser):
    browser.cookies.set("astinuslab_admin_token", ADMIN_TOKEN)
    assert browser.get("/api/v1/jobs").status_code == 401


@pytest.mark.parametrize("cookie", ["", "unknown-session", "v1." + "a" * 43, "x" * 5000])
def test_unknown_session_cookies_are_unauthorized(browser, cookie):
    browser.cookies.set("astinuslab_admin_token", cookie)
    assert browser.get("/api/v1/jobs").status_code == 401


@pytest.mark.parametrize("seconds_before_expiry, status", [(1, 200), (0, 401), (-1, 401)])
def test_server_checks_absolute_expiry(browser, monkeypatch, in_memory_session_scope, seconds_before_expiry, status):
    from astinuslab.core import browser_sessions
    from astinuslab.db.models import AdminBrowserSession

    login(browser)
    with in_memory_session_scope() as session:
        row = session.query(AdminBrowserSession).one()
        now = row.expires_at - timedelta(seconds=seconds_before_expiry)
    monkeypatch.setattr(browser_sessions, "_now", lambda: now)
    assert browser.get("/api/v1/jobs").status_code == status


def test_session_store_contains_only_hashes(browser, in_memory_session_scope):
    from astinuslab.db.models import AdminBrowserSession

    cookie = login(browser)
    with in_memory_session_scope() as session:
        row = session.query(AdminBrowserSession).one()
        assert row.token_hash == hashlib.sha256(cookie.encode()).hexdigest()
        assert row.admin_token_hash == hashlib.sha256(ADMIN_TOKEN.encode()).hexdigest()
        assert (row.expires_at - row.created_at).total_seconds() == 12 * 60 * 60


def test_admin_token_rotation_invalidates_existing_browser_sessions(browser, monkeypatch):
    login(browser)
    monkeypatch.setattr(security.settings, "admin_api_token_hash", hashlib.sha256(b"rotated-test-token").hexdigest())
    assert browser.get("/api/v1/jobs").status_code == 401
    assert browser.get("/api/v1/jobs", headers={"Authorization": "Bearer rotated-test-token"}).status_code == 200


def test_relogin_revokes_previous_session(browser):
    old_cookie = login(browser)
    new_cookie = login(browser)
    assert new_cookie != old_cookie
    assert browser.get("/api/v1/jobs").status_code == 200
    browser.cookies.clear()
    browser.cookies.set("astinuslab_admin_token", old_cookie)
    assert browser.get("/api/v1/jobs").status_code == 401


def test_logout_leaves_other_browser_session_valid(browser):
    first = login(browser)
    browser.cookies.clear()
    second = login(browser)
    assert first != second
    browser.post("/logout", headers={"X-CSRF-Token": browser.cookies.get("astinuslab_csrf")}, follow_redirects=False)
    browser.cookies.set("astinuslab_admin_token", first)
    assert browser.get("/api/v1/jobs").status_code == 200
