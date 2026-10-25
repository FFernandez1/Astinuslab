from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.ui


@pytest.mark.parametrize(
    ("path", "destination"),
    [
        ("/help", "usage/getting-started/"),
        ("/help/usage/getting-started.md", "usage/getting-started/"),
        ("/help/user/VOLATILITY3.md", "user/VOLATILITY3/"),
        ("/help/index.md", ""),
    ],
)
def test_help_opens_documentation_website(client, path, destination):
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"http://localhost:9000/{destination}"
    assert "<article" not in response.text


def test_sidebar_has_no_help_button(client):
    response = client.get("/jobs")
    assert response.status_code == 200
    assert 'href="/help' not in response.text


@pytest.mark.parametrize(
    ("language", "filename", "destination"),
    [
        ("es", "topic.md", "es/user/topic/"),
        ("ca", "topic.md", "ca/user/topic/"),
        ("fr", "topic.es.md", "es/user/topic/"),
        ("es", "topic.en.md", "user/topic/"),
        ("unknown", "topic.md", "user/topic/"),
    ],
)
def test_help_maps_mkdocs_languages(client, monkeypatch, tmp_path, language, filename, destination):
    from astinuslab.ui import help_routes

    (tmp_path / "user").mkdir()
    (tmp_path / "user/topic.md").write_text("# English\n")
    (tmp_path / "user/topic.es.md").write_text("# Español\n")
    (tmp_path / "user/topic.en.md").write_text("# English\n")
    monkeypatch.setattr(help_routes, "docs_root", lambda: tmp_path)
    client.cookies.set("lang", language)
    response = client.get(f"/help/user/{filename}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"http://localhost:9000/{destination}"


def test_help_uses_configured_docs_port(client, monkeypatch):
    from astinuslab.core.config import load_settings
    from astinuslab.ui import help_routes

    monkeypatch.setenv("ASTINUSLAB_DOCS_PORT", "9123")
    monkeypatch.setattr(help_routes, "settings", load_settings())
    response = client.get("/help", follow_redirects=False)
    assert response.headers["location"] == "http://localhost:9123/usage/getting-started/"


def test_help_maps_translated_index(client):
    client.cookies.set("lang", "fr")
    response = client.get("/help/index.es.md", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "http://localhost:9000/es/"


@pytest.mark.parametrize(
    "path", ["audits/private.md", "user/%2e%2e/%2e%2e/pyproject.toml", "user/secret.md", "user/test.txt", "user/%00.md"]
)
def test_help_rejects_unpublished_paths_and_symlinks(client, monkeypatch, tmp_path, path):
    from astinuslab.ui import help_routes

    (tmp_path / "user").mkdir()
    (tmp_path / "user/secret.md").symlink_to(Path(__file__))
    monkeypatch.setattr(help_routes, "docs_root", lambda: tmp_path)
    assert client.get("/help/" + path).status_code == 404


def test_help_requires_authentication(client, monkeypatch):
    from astinuslab.core import security

    monkeypatch.setattr(security, "settings", SimpleNamespace(auth_enabled=True, admin_api_token_hash=None))
    response = client.get("/help/user/VOLATILITY3.md", headers={"Accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


def test_help_rejects_symlink_into_unpublished_docs(client, monkeypatch, tmp_path):
    from astinuslab.ui import help_routes

    (tmp_path / "user").mkdir()
    (tmp_path / "audits").mkdir()
    (tmp_path / "audits/private.md").write_text("# Unpublished audit\n")
    (tmp_path / "user/alias.md").symlink_to(tmp_path / "audits/private.md")
    monkeypatch.setattr(help_routes, "docs_root", lambda: tmp_path)
    response = client.get("/help/user/alias.md")
    assert response.status_code == 404
    assert "Unpublished audit" not in response.text
