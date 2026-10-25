"""Filesystem paths for web resources installed with the Python package."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def _resource_directory(name: str) -> Path:
    path = Path(str(files("astinuslab.web").joinpath(name)))
    if not path.is_dir():
        raise RuntimeError(f"packaged web resource directory is missing: {name}")
    return path


STATIC_DIR = _resource_directory("static")
TEMPLATES_DIR = _resource_directory("templates")
