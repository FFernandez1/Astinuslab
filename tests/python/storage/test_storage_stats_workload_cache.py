from __future__ import annotations

import types

import pytest

from astinuslab.storage.storage_admin_support import storage_stats
from tests.python._support.helpers import autospec_setattr

pytestmark = pytest.mark.unit


def test_storage_stats_includes_workload_cache(tmp_path):
    fake = types.SimpleNamespace(
        storage_dir=tmp_path,
        base_dir=tmp_path,
        data_dir=tmp_path,
    )
    (tmp_path / "dumps").mkdir()
    (tmp_path / "logs").mkdir()
    cache = tmp_path / "workload-cache"
    cache.mkdir()
    (cache / "snap1.qcow2").write_bytes(b"x" * (3 * 1024 * 1024))
    (cache / "snap2.qcow2").write_bytes(b"x" * (1 * 1024 * 1024))

    stats = storage_stats(fake)
    assert stats["workload_cache_count"] == 2
    assert 3.9 <= stats["workload_cache_mb"] <= 4.1


def test_storage_stats_handles_missing_dirs_and_disk_usage_failure(tmp_path, monkeypatch):
    from astinuslab.storage import storage_admin_support as support

    storage_dir = tmp_path / "missing-storage"
    fake = types.SimpleNamespace(storage_dir=storage_dir, base_dir=tmp_path, data_dir=tmp_path)

    autospec_setattr(
        monkeypatch, support.shutil, "disk_usage", lambda path: (_ for _ in ()).throw(OSError("no statvfs"))
    )

    stats = storage_stats(fake)

    assert stats == {
        "dumps_count": 0,
        "dumps_mb": 0.0,
        "logs_count": 0,
        "logs_mb": 0.0,
        "workload_cache_count": 0,
        "workload_cache_mb": 0.0,
        "symbols_count": 0,
        "symbols_mb": 0.0,
        "free_gb": 0.0,
    }
    assert storage_dir.exists()


def test_storage_stats_includes_symbol_cache(tmp_path):
    symbols = tmp_path / "vol3" / "symbols"
    symbols.mkdir(parents=True)
    (symbols / "linux.json").write_bytes(b"x" * (2 * 1024 * 1024))
    fake = types.SimpleNamespace(storage_dir=tmp_path, base_dir=tmp_path, data_dir=tmp_path, vol3_symbols_path=symbols)

    stats = storage_stats(fake)
    assert stats["symbols_count"] == 1
    assert 1.9 <= stats["symbols_mb"] <= 2.1
