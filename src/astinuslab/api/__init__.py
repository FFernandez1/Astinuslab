"""Stable API facade over package-owned route implementation."""

from __future__ import annotations

import sys
from importlib import import_module
from types import ModuleType
from typing import Any

_ROUTES_MODULE = f"{__name__}.routes"


def _routes() -> ModuleType:
    return import_module(_ROUTES_MODULE)


def __getattr__(name: str) -> Any:
    return getattr(_routes(), name)


def __dir__() -> list[str]:
    return sorted({*globals(), *dir(_routes())})


class _ApiFacadeModule(ModuleType):
    """Forward monkeypatches to the module that owns route function globals."""

    def __setattr__(self, name: str, value: Any) -> None:
        implementation = sys.modules.get(_ROUTES_MODULE)
        if implementation is not None and hasattr(implementation, name):
            setattr(implementation, name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _ApiFacadeModule
