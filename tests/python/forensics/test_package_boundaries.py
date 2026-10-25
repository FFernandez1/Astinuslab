from __future__ import annotations

import importlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


FORENSICS_MODULES = ("api", "services", "case_services")
ROOT_FORENSIC_MODULES = ("volatility_api", "forensic_services", "case_services")


def test_forensic_implementations_are_owned_by_the_forensics_package() -> None:
    for name in FORENSICS_MODULES:
        module = importlib.import_module(f"astinuslab.forensics.{name}")
        assert module.__file__ is not None
        assert Path(module.__file__).parent.name == "forensics"


def test_root_forensic_imports_are_removed() -> None:
    for name in ROOT_FORENSIC_MODULES:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(f"astinuslab.{name}")
