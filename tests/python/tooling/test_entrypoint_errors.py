from __future__ import annotations

import os
import subprocess
import sys


def _run_entrypoint(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=20,
    )


def _poisoned_env(tmp_path):
    env = os.environ.copy()
    env["ASTINUSLAB_DB_PATH"] = str(tmp_path / "entrypoint.db")
    env["ASTINUSLAB_API_HOST"] = "0.0.0.0"
    env["ASTINUSLAB_AUTH_ENABLED"] = "false"
    env["ASTINUSLAB_ALLOW_UNSAFE_NETWORK_BIND_WITHOUT_AUTH"] = "false"
    return env


def _assert_friendly_config_error(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 2
    assert "astinuslab: configuration error:" in result.stderr
    assert "api_host" in result.stderr or "non-loopback" in result.stderr
    assert "Traceback" not in result.stderr


def test_serve_reports_config_error_without_traceback(tmp_path):
    result = _run_entrypoint(["-m", "astinuslab.serve"], _poisoned_env(tmp_path))

    _assert_friendly_config_error(result)


def test_dbcli_reports_config_error_without_traceback(tmp_path):
    result = _run_entrypoint(["-m", "astinuslab.dbcli", "status"], _poisoned_env(tmp_path))

    _assert_friendly_config_error(result)


def test_celery_entrypoint_reports_config_error_without_traceback(tmp_path):
    result = _run_entrypoint(["-c", "import astinuslab.jobs.celery_main"], _poisoned_env(tmp_path))

    _assert_friendly_config_error(result)


def test_dbcli_valid_config_remains_usable(tmp_path):
    env = os.environ.copy()
    env["ASTINUSLAB_DB_PATH"] = str(tmp_path / "valid.db")
    env["ASTINUSLAB_API_HOST"] = "127.0.0.1"

    result = _run_entrypoint(["-m", "astinuslab.dbcli", "status"], env)

    assert result.returncode in {0, 1}
    assert "configuration error" not in result.stderr
