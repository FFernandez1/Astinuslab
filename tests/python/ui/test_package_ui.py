from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.unit
def test_package_page_is_feature_gated(client):
    response = client.get("/packages")
    assert response.status_code == 404


@pytest.mark.unit
def test_package_activity_api_is_feature_gated(client):
    response = client.get("/api/v1/lab-packages/activity")

    assert response.status_code == 404


@pytest.mark.unit
def test_package_page_renders_workflow_when_enabled(client, monkeypatch):
    import astinuslab.ui.package_routes as package_routes

    monkeypatch.setattr(package_routes, "settings", SimpleNamespace(packages_enabled=True))
    response = client.get("/packages")

    assert response.status_code == 200
    assert "Import / inspect" in response.text
    assert "package-inspect-form" in response.text
    assert "packages/index.js" in response.text


def test_package_template_does_not_use_translation_fallback_bypass():
    root = Path(__file__).resolve().parents[3]
    template = (root / "src/astinuslab/web/templates/packages.html").read_text(encoding="utf-8")
    assert " or '" not in template
    assert ' or "' not in template


@pytest.mark.parametrize("lang", ["en", "es", "fr", "ca"])
def test_verification_clipboard_feedback_is_localized(lang):
    from astinuslab.i18n import TRANSLATIONS

    translations = TRANSLATIONS[lang]
    for key in ("pkg_copy_succeeded", "pkg_copy_failed", "pkg_copy_verification_id"):
        assert translations[key].strip()
    if lang != "en":
        assert translations["pkg_copy_succeeded"] != TRANSLATIONS["en"]["pkg_copy_succeeded"]
