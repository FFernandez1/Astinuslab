from __future__ import annotations

import hashlib
import logging
import re
import types

import pytest
from starlette.testclient import TestClient

from astinuslab.core.request_context import current_request_id


@pytest.fixture()
def auth_settings():
    token_hash = hashlib.sha256(b"admin-test-token").hexdigest()
    return types.SimpleNamespace(
        auth_enabled=True,
        admin_api_token_hash=f"sha256:{token_hash}",
        api_port=8000,
        browser_allowed_origins=[],
    )


def test_response_reuses_valid_request_id():
    from astinuslab.main import app

    with TestClient(app) as client:
        response = client.get("/health", headers={"X-Request-ID": "client-request-123"})

    assert response.headers["X-Request-ID"] == "client-request-123"


def test_response_replaces_invalid_request_id():
    from astinuslab.main import app

    with TestClient(app) as client:
        response = client.get("/health", headers={"X-Request-ID": "unsafe value"})

    assert re.fullmatch(r"[a-f0-9]{32}", response.headers["X-Request-ID"])


def test_auth_denial_receives_request_id(monkeypatch, auth_settings):
    import astinuslab.core.security as security_mod
    from astinuslab.main import app

    monkeypatch.setattr(security_mod, "settings", auth_settings)

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/targets",
            headers={"X-Request-ID": "denied-request-123"},
        )

    assert response.status_code == 401
    assert response.headers["X-Request-ID"] == "denied-request-123"


def test_request_completion_log_has_stable_fields(caplog):
    from astinuslab.main import app

    with caplog.at_level(logging.INFO, logger="astinuslab.main"):
        with TestClient(app) as client:
            client.get("/health", headers={"X-Request-ID": "logged-request-123"})

    record = next(record for record in caplog.records if record.message.startswith("http_request "))
    assert record.request_id == "logged-request-123"
    assert "method=GET" in record.message
    assert "path=/health" in record.message
    assert "status=200" in record.message
    assert re.search(r"duration_ms=\d+(?:\.\d+)?", record.message)


def test_request_context_is_reset_after_response():
    from astinuslab.main import app

    assert current_request_id() == "-"
    with TestClient(app) as client:
        client.get("/health", headers={"X-Request-ID": "cleanup-request-123"})
    assert current_request_id() == "-"


def test_unhandled_exception_response_keeps_request_id_and_hides_error(monkeypatch):
    from astinuslab.core import fake_mode
    from astinuslab.main import app

    def raise_unhandled_error():
        raise RuntimeError("sensitive internal detail")

    monkeypatch.setattr(fake_mode, "fake_mode_active", raise_unhandled_error)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/health",
            headers={"X-Request-ID": "failed-request-123"},
        )

    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "failed-request-123"
    assert "sensitive internal detail" not in response.text
