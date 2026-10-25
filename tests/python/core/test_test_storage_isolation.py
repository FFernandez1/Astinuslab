"""The test bootstrap must not leave artifacts in the application's vault."""

import os
import subprocess
import sys
from pathlib import Path


def test_test_bootstrap_isolates_and_cleans_default_storage(tmp_path):
    root = Path(__file__).resolve().parents[3]
    live_data = tmp_path / "live"
    live_data.mkdir()
    env = os.environ.copy()
    env.pop("ASTINUSLAB_STORAGE_DIR", None)
    env.pop("ASTINUSLAB_DB_PATH", None)
    env["ASTINUSLAB_DATA_DIR"] = str(live_data)
    probe = """
import pathlib
import runpy
import sys
runpy.run_path('tests/python/conftest.py')
from astinuslab.core.config import settings
storage = settings.storage_dir.resolve()
assert not storage.is_relative_to(pathlib.Path(sys.argv[1]).resolve()), storage
assert not storage.is_relative_to(pathlib.Path.cwd() / 'storage'), storage
artifact = storage / 'dumps' / 'probe.dump'
artifact.parent.mkdir(parents=True, exist_ok=True)
artifact.write_bytes(b'synthetic dump')
print(storage)
"""
    result = subprocess.run(
        [sys.executable, "-c", probe, str(live_data)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert not (live_data / "storage").exists()
    assert not Path(result.stdout.strip().splitlines()[-1]).exists()
