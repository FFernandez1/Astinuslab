import importlib
from pathlib import Path

import pytest

UI_DIR = Path(__file__).parents[3] / "src" / "astinuslab" / "ui"
REMOVED = ["ssh_keys", "storage", "workload_cache", "workloads", "logs", "images"]


@pytest.mark.parametrize("name", REMOVED)
def test_placeholder_ui_module_is_gone(name):
    assert not (UI_DIR / f"{name}.py").exists(), f"placeholder ui/{name}.py should be removed"


def test_single_ui_router_is_mounted():
    importlib.import_module("astinuslab.main")
    from fastapi import APIRouter

    from astinuslab.ui import ui_router

    assert isinstance(ui_router, APIRouter)
