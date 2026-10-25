from __future__ import annotations

import importlib
from pathlib import Path
from typing import get_type_hints

import pytest

pytestmark = pytest.mark.unit


MODULE_ALIASES = {
    "boot": "astinuslab.boot",
    "context": "astinuslab.job_run_context",
    "deps": "astinuslab.job_run_deps",
    "lab_lifecycle": "astinuslab.lab_lifecycle",
    "support": "astinuslab.orchestration_support",
}


def test_orchestration_implementations_are_owned_by_the_package() -> None:
    for name in MODULE_ALIASES:
        module = importlib.import_module(f"astinuslab.orchestration.{name}")
        assert module.__file__ is not None
        assert Path(module.__file__).parent.name == "orchestration"


def test_root_orchestration_imports_are_removed() -> None:
    for name, root_name in MODULE_ALIASES.items():
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(root_name)


def test_runner_and_live_session_use_package_owned_dependency_types() -> None:
    deps = importlib.import_module("astinuslab.orchestration.deps").JobRunDeps
    context = importlib.import_module("astinuslab.orchestration.context").JobRunContext

    assert get_type_hints(context)["deps"] is deps
