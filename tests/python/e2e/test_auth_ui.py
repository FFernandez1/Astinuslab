"""Browser cookies, login, CSRF, mutation and logout against an auth-enabled server."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from playwright.sync_api import Page, expect


def _log_in(page: Page, server: dict[str, object]) -> str:
    base = str(server["base_url"])
    page.goto(f"{base}/login")
    page.get_by_label("Admin token").fill(str(server["admin_token"]))
    page.get_by_role("button", name="Log in").click()
    return base


def test_authenticated_browser_mutation_and_logout(authenticated_server: dict[str, object], page: Page):
    base_url = str(authenticated_server["base_url"])
    page.goto(f"{base_url}/jobs")
    expect(page).to_have_url(re.compile(r"/login\?next="))

    page.get_by_label("Admin token").fill("wrong-admin-token")
    with page.expect_response(
        lambda response: response.url.endswith("/login") and response.request.method == "POST"
    ) as bad:
        page.get_by_role("button", name="Log in").click()
    assert bad.value.status == 401
    expect(page.get_by_text("Invalid admin token")).to_be_visible()
    before_login = {cookie["name"]: cookie["value"] for cookie in page.context.cookies()}
    assert "astinuslab_admin_token" not in before_login

    page.get_by_label("Admin token").fill(str(authenticated_server["admin_token"]))
    page.get_by_role("button", name="Log in").click()
    expect(page).to_have_url(f"{base_url}/jobs")
    cookies = {cookie["name"]: cookie for cookie in page.context.cookies()}
    assert cookies["astinuslab_admin_token"]["httpOnly"] is True
    assert cookies["astinuslab_admin_token"]["sameSite"] == "Strict"
    assert cookies["astinuslab_csrf"]["value"] != before_login["astinuslab_csrf"]

    # Both requests use the real browser's session cookie. Only the second
    # echoes the CSRF token supplied by the server-rendered page.
    denied = page.evaluate("""async () => {
        const response = await fetch('/api/v1/playground/sessions', { method: 'POST' });
        return {status: response.status, body: await response.json()};
    }""")
    assert denied["status"] == 403
    assert denied["body"]["code"] == "csrf_required"
    created = page.evaluate("""async () => {
        const token = document.querySelector('meta[name="astinuslab-csrf-token"]').content;
        const response = await fetch('/api/v1/playground/sessions', {
            method: 'POST', headers: {'X-CSRF-Token': token}
        });
        return {status: response.status, body: await response.json()};
    }""")
    assert created["status"] == 202
    session_id = created["body"]["job_id"]
    current = page.evaluate("""async () => {
        const response = await fetch('/api/v1/playground/sessions/current');
        return {status: response.status, body: await response.json()};
    }""")
    assert current["status"] == 200
    assert current["body"]["job_id"] == session_id

    # Logout has a POST endpoint; exercise it with browser-origin headers and
    # cookie handling, then prove that the protected page requires login again.
    logged_out = page.evaluate("""async () => {
        const token = document.querySelector('meta[name="astinuslab-csrf-token"]').content;
        const response = await fetch('/logout', {
            method: 'POST', headers: {'X-CSRF-Token': token}
        });
        return {status: response.status, url: response.url};
    }""")
    assert logged_out == {"status": 200, "url": f"{base_url}/login"}
    assert all(cookie["name"] != "astinuslab_admin_token" for cookie in page.context.cookies())
    page.goto(f"{base_url}/jobs")
    expect(page).to_have_url(re.compile(r"/login\?next="))
    assert page.request.get(f"{base_url}/api/v1/jobs").status == 401


def test_a_deep_link_returns_to_its_destination_after_login(authenticated_server: dict[str, object], page: Page):
    """An operator's first move on an installed release is a link into a page."""
    base = str(authenticated_server["base_url"])

    page.goto(f"{base}/volatility")
    expect(page).to_have_url(f"{base}/login?next=/volatility")

    page.get_by_label("Admin token").fill(str(authenticated_server["admin_token"]))
    page.get_by_role("button", name="Log in").click()

    # Back at the requested page, actually rendered rather than a shell.
    expect(page).to_have_url(f"{base}/volatility")
    expect(page.locator("#vol-workbench")).to_be_visible()


def test_login_never_redirects_off_site(authenticated_server: dict[str, object], page: Page):
    """`next` is attacker-reachable: a login link can be sent to anyone."""
    base = str(authenticated_server["base_url"])

    for hostile in ("https://evil.example/", "//evil.example/", "/\\evil.example"):
        page.goto(f"{base}/login?next={hostile}")
        # The form must not carry the hostile value forward.
        assert page.locator('input[name="next"]').input_value() == "/"

    page.get_by_label("Admin token").fill(str(authenticated_server["admin_token"]))
    page.get_by_role("button", name="Log in").click()
    assert page.url.startswith(base), page.url


def test_an_expired_session_offers_a_way_back_without_discarding_the_page(
    authenticated_server: dict[str, object], page: Page
):
    """A lab left open overnight outlives its 12-hour session.

    The poll starts answering 401. Silently retrying forever is the failure mode
    to avoid, and so is navigating away: an operator mid-wizard would lose their
    input. The page must say it is signed out and offer the way back.
    """
    base = _log_in(page, authenticated_server)
    page.goto(f"{base}/playground")
    expect(page.locator("#playground-root")).to_be_visible()

    # Expire the session server-side. The browser still holds its cookie; the
    # server stops honouring it -- exactly what the TTL does overnight.
    db = Path(str(authenticated_server["db_path"]))
    connection = sqlite3.connect(db)
    try:
        connection.execute("DELETE FROM admin_browser_sessions")
        connection.commit()
    finally:
        connection.close()

    root = page.locator("#playground-root")
    expect(root).to_have_attribute("data-signed-out", "true", timeout=15_000)
    expect(root).to_have_attribute("data-stale", "true")

    # Still here, and the way back is one click -- structural assertions so this
    # holds in every locale.
    expect(page).to_have_url(f"{base}/playground")
    back = page.locator('#pg-alerts a[href="/login?next=/playground"]')
    expect(back).to_be_visible()

    back.click()
    page.get_by_label("Admin token").fill(str(authenticated_server["admin_token"]))
    page.get_by_role("button", name="Log in").click()
    expect(page).to_have_url(f"{base}/playground")
    expect(page.locator("#playground-root")).not_to_have_attribute("data-signed-out", "true")
