from __future__ import annotations

import types
from pathlib import Path

from astinuslab.virtualization.host_helpers import storage_tmp_dir


def _settings_with(storage_dir):
    s = types.SimpleNamespace()
    if storage_dir is not None:
        s.storage_dir = storage_dir
    return s


def test_storage_tmp_dir_with_str_storage(tmp_path: Path) -> None:
    s = _settings_with(str(tmp_path))
    assert storage_tmp_dir(s) == tmp_path / "tmp"


def test_storage_tmp_dir_with_path_storage(tmp_path: Path) -> None:
    s = _settings_with(tmp_path)
    assert storage_tmp_dir(s) == tmp_path / "tmp"


def test_storage_tmp_dir_returns_path_type(tmp_path: Path) -> None:
    s = _settings_with(tmp_path)
    assert isinstance(storage_tmp_dir(s), Path)


def test_storage_tmp_dir_missing_storage_dir_uses_base_dir(tmp_path: Path) -> None:
    s = types.SimpleNamespace(base_dir=str(tmp_path), data_dir=str(tmp_path))
    result = storage_tmp_dir(s)
    assert result == Path(tmp_path) / "storage" / "tmp"
