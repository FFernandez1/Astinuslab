from __future__ import annotations

import types
from pathlib import Path

import pytest

from astinuslab.core.locks import capture_lock_dir


def _settings_with(storage_dir: str | Path) -> types.SimpleNamespace:
    return types.SimpleNamespace(storage_dir=storage_dir)


@pytest.mark.parametrize(
    ("prefix", "expected_suffix"),
    [
        ("forensic", "forensic-capture-locks"),
        ("playground", "playground-capture-locks"),
    ],
)
def test_capture_lock_dir_matches_prior_storage_dir_path(
    tmp_path: Path,
    prefix: str,
    expected_suffix: str,
) -> None:
    settings = _settings_with(tmp_path)

    assert capture_lock_dir(settings, prefix) == tmp_path / "tmp" / expected_suffix


def test_helper_returns_path(tmp_path: Path) -> None:
    settings = _settings_with(tmp_path)

    assert isinstance(capture_lock_dir(settings, "forensic"), Path)


def test_forensic_missing_storage_dir_matches_prior_fallback(tmp_path: Path) -> None:
    settings = types.SimpleNamespace(base_dir=tmp_path, data_dir=tmp_path)

    assert capture_lock_dir(settings, "forensic") == tmp_path / "storage" / "tmp" / "forensic-capture-locks"


def test_playground_missing_storage_dir_matches_prior_fallback(tmp_path: Path) -> None:
    capture_dir = tmp_path / "captures" / "playground"
    settings = types.SimpleNamespace(playground_capture_dir=capture_dir)

    assert capture_lock_dir(settings, "playground") == tmp_path / "captures" / "tmp" / "playground-capture-locks"
