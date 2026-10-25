from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "pyproject.toml").is_file())


ROOT_API_MODULES = ("schemas", "api_errors")


def test_root_api_imports_are_removed() -> None:
    """Mirrors test_root_db_imports_are_removed; see its docstring."""
    for name in ROOT_API_MODULES:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(f"astinuslab.{name}")


def test_api_implementation_is_package_owned() -> None:
    api = importlib.import_module("astinuslab.api")
    importlib.import_module("astinuslab.api.errors")
    importlib.import_module("astinuslab.api.schemas")

    assert api.__file__ is not None
    assert Path(api.__file__).parent.name == "api"


def test_api_router_keeps_the_stage_f_route_fingerprint() -> None:
    from astinuslab.api import router

    route_fingerprint = {(method, route.path) for route in router.routes for method in getattr(route, "methods", set())}

    # 42 through Stage F, +1 for the read-only GET /api/v1/metrics summary.
    assert len(route_fingerprint) == 43
    assert {
        ("GET", "/api/v1/targets"),
        ("POST", "/api/v1/jobs"),
        ("POST", "/api/v1/jobs/{job_id}/capture"),
        ("GET", "/api/v1/artifacts/{artifact_id}/manifest"),
        ("PATCH", "/api/v1/acquisitions/{acquisition_id}/status"),
        ("GET", "/api/v1/metrics"),
    } <= route_fingerprint


def test_api_module_remains_the_route_monkeypatch_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    import astinuslab.api as api

    sentinel = [{"id": "package-boundary"}]
    monkeypatch.setattr(api, "list_targets_service", lambda: sentinel)

    assert api.list_targets() is sentinel


def test_production_imports_use_package_owned_api_contracts() -> None:
    offenders = []
    for path in (ROOT / "src/astinuslab").rglob("*.py"):
        if path.name in {"schemas.py"}:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = re.match(r"from (\.+)(api_errors|schemas) import", line.lstrip())
            if match and not (path.parent.name == "api" and match.group(2) == "schemas"):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line}")

    assert not offenders, "\n".join(offenders)
