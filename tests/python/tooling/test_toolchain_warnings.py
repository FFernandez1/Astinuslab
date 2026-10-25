from __future__ import annotations

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_pep517_build_frontend_is_exactly_pinned_and_locked() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev_dependencies = project["project"]["optional-dependencies"]["dev"]
    pinned = [dependency for dependency in dev_dependencies if dependency.startswith("build==")]

    assert len(pinned) == 1
    version = pinned[0].partition("==")[2]
    assert version

    requirements_lock = (ROOT / "requirements-dev.lock").read_text(encoding="utf-8")
    uv_lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert f"build=={version}" in requirements_lock
    assert 'name = "build"' in uv_lock
    assert f'{{ name = "build", marker = "extra == \'dev\'", specifier = "=={version}" }}' in uv_lock
    assert "setuptools>=77" in project["build-system"]["requires"]
    assert project["project"]["license-files"] == [
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "LICENSES/*",
    ]
    assert "license-files" not in project["tool"]["setuptools"]


def test_starlette_testclient_uses_the_supported_httpx2_backend() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev_dependencies = project["project"]["optional-dependencies"]["dev"]
    assert any(dependency.startswith("httpx2>=") for dependency in dev_dependencies)

    lock = (ROOT / "requirements-dev.lock").read_text(encoding="utf-8")
    assert "httpx2==" in lock


def test_frontend_sources_declare_their_es_module_scope() -> None:
    static = ROOT / "src/astinuslab/web/static"
    package = json.loads((static / "package.json").read_text(encoding="utf-8"))
    assert package == {"private": True, "type": "module"}

    javascript = set(static.rglob("*.js"))
    stylesheets = set(static.rglob("*.css"))
    assert javascript and stylesheets, "frontend lint/format globs must never pass by matching zero source files"

    scripts = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["scripts"]
    assert scripts["lint:js"] == "eslint src/astinuslab/web/static tests/browser"
    expected_format_glob = '"src/astinuslab/web/static/**/*.{js,css}"'
    assert expected_format_glob in scripts["format"]
    assert expected_format_glob in scripts["format:check"]

    eslint = (ROOT / "eslint.config.js").read_text(encoding="utf-8")
    assert "files: ['src/astinuslab/web/static/**/*.js']" in eslint
    assert "'src/astinuslab/web/static/htmx.min.js'" in eslint


def test_canonical_docs_gates_disable_the_upstream_mkdocs2_banner() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    matrix = (ROOT / "docs/development/verification-matrix.md").read_text(encoding="utf-8")

    assert 'NO_MKDOCS_2_WARNING: "1"' in workflow
    assert "NO_MKDOCS_2_WARNING=1 .venv/bin/python -m mkdocs build" in matrix
