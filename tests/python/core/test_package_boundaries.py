from __future__ import annotations

import importlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


CORE_MODULES = (
    "compatibility",
    "config",
    "fake_mode",
    "locks",
    "logging_config",
    "operational_health",
    "output_parsers",
    "request_context",
    "security",
    "system_status",
)


def test_core_implementations_are_owned_by_the_core_package() -> None:
    for name in CORE_MODULES:
        module = importlib.import_module(f"astinuslab.core.{name}")
        assert module.__file__ is not None
        assert Path(module.__file__).parent.name == "core"


def test_root_core_imports_are_removed() -> None:
    for name in CORE_MODULES:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(f"astinuslab.{name}")


def test_config_defaults_still_resolve_from_the_repository_data_root() -> None:
    config = importlib.import_module("astinuslab.core.config")
    assert config.settings.base_dir == Path(__file__).resolve().parents[3]
