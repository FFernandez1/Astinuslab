import json
import os
import re
import subprocess
import textwrap
import time
from pathlib import Path

import pytest
import yaml


def _wait_for_file(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"{path} was not created")


def _write_fake_python(tmp_path: Path) -> Path:
    fake_python = tmp_path / "fake-python"
    fake_python.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail

            MARKER_DIR="${FAKE_MARKER_DIR:?}"

            if [[ "${1:-}" == "-m" && "${2:-}" == "astinuslab.dbcli" && "${3:-}" == "init" ]]; then
                printf "init\n" > "$MARKER_DIR/init.called"
                exit 0
            fi

            if [[ "${1:-}" == "-m" && "${2:-}" == "celery" ]]; then
                printf "%s\n" "$$" > "$MARKER_DIR/celery.started"
                on_term() {
                    printf "term\n" > "$MARKER_DIR/celery.term"
                    exit 0
                }
                trap on_term TERM INT
                while true; do
                    sleep 0.05
                done
            fi

            if [[ "${1:-}" == "-m" && "${2:-}" == "uvicorn" ]]; then
                printf "%s\n" "$$" > "$MARKER_DIR/uvicorn.started"
                on_term() {
                    printf "term\n" > "$MARKER_DIR/uvicorn.term"
                    exit 0
                }
                trap on_term TERM INT
                if [[ "${FAKE_UVICORN_MODE:-}" == "exit" ]]; then
                    sleep 0.2
                    printf "done\n" > "$MARKER_DIR/uvicorn.done"
                    exit 0
                fi
                while true; do
                    sleep 0.05
                done
            fi

            printf "unexpected args: %s\n" "$*" >&2
            exit 64
            """
        ),
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    return fake_python


def test_playwright_configs_use_repo_python_binary() -> None:
    config_paths = [
        Path("playwright.config.js"),
        Path("playwright.integration.config.js"),
    ]

    for config_path in config_paths:
        config_text = config_path.read_text(encoding="utf-8")
        assert "const pythonBin" in config_text
        assert "python -m " not in config_text
        assert "python -c " not in config_text
        assert "JSON.stringify(pythonBin)" not in config_text
        assert "const shellQuote" in config_text
        assert "shellQuote(pythonBin)" in config_text
        single_quote = chr(39)
        expected_escape = f'replace(/{single_quote}/g, "{single_quote}\\\\{single_quote}{single_quote}")'
        assert expected_escape in config_text
        # setup_backend_venv.sh creates .venv; a bare venv/ default resolves only
        # on hosts that happen to carry a stray one, and never on CI.
        assert '".venv", "bin", "python"' in config_text
        assert '"venv", "bin", "python"' not in config_text.replace('".venv", "bin", "python"', "")


def test_full_e2e_server_uses_configurable_python_binary() -> None:
    script_text = Path("tools/dev/e2e_full_server.sh").read_text(encoding="utf-8")

    assert 'PYTHON_BIN="${ASTINUSLAB_PYTHON:-$REPO_ROOT/.venv/bin/python}"' in script_text
    assert '"$PYTHON_BIN" -m astinuslab.dbcli init' in script_text
    assert '"$PYTHON_BIN" -m celery -A astinuslab.jobs.celery_main worker' in script_text
    assert '"$PYTHON_BIN" -m uvicorn astinuslab.main:app --host 127.0.0.1 --port 8000 &' in script_text
    assert "UVICORN_PID=$!" in script_text
    assert 'kill "$WORKER_PID" "$UVICORN_PID" 2>/dev/null || true' in script_text
    assert 'wait "$UVICORN_PID"' in script_text
    assert 'exec "$PYTHON_BIN"' not in script_text
    assert "python -m " not in script_text
    assert "python -c " not in script_text


def test_full_e2e_server_cleans_worker_when_uvicorn_exits(tmp_path: Path) -> None:
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    fake_python = _write_fake_python(tmp_path)
    env = {
        **os.environ,
        "ASTINUSLAB_PYTHON": str(fake_python),
        "FAKE_MARKER_DIR": str(marker_dir),
        "FAKE_UVICORN_MODE": "exit",
    }

    result = subprocess.run(
        [str(Path("tools/dev/e2e_full_server.sh").resolve())],
        cwd=Path.cwd(),
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (marker_dir / "init.called").exists()
    assert (marker_dir / "uvicorn.done").exists()
    _wait_for_file(marker_dir / "celery.term")


def test_full_e2e_server_terminates_worker_and_uvicorn_on_term(tmp_path: Path) -> None:
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    fake_python = _write_fake_python(tmp_path)
    env = {
        **os.environ,
        "ASTINUSLAB_PYTHON": str(fake_python),
        "FAKE_MARKER_DIR": str(marker_dir),
    }
    proc = subprocess.Popen(
        [str(Path("tools/dev/e2e_full_server.sh").resolve())],
        cwd=Path.cwd(),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        _wait_for_file(marker_dir / "celery.started")
        _wait_for_file(marker_dir / "uvicorn.started")
        proc.terminate()
        stdout, stderr = proc.communicate(timeout=5)
    except Exception:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)
        raise

    assert proc.returncode is not None, stderr
    _wait_for_file(marker_dir / "celery.term")
    _wait_for_file(marker_dir / "uvicorn.term")
    assert stdout == ""


def test_ci_supplies_explicit_python_to_playwright_web_server() -> None:
    """CI has no repo-local venv, so the pythonBin default cannot resolve there.

    Without an explicit ASTINUSLAB_PYTHON the Playwright webServer dies with
    exit 127 before a single spec runs.
    """
    workflow = yaml.safe_load(Path(".github/workflows/ci.yml").read_text(encoding="utf-8"))

    playwright_steps = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if any(
            command in step.get("run", "")
            for command in (
                "playwright test",
                "test:smoke",
                "test:integration",
                "test:remediation",
                "test:docs",
            )
        )
    ]
    assert playwright_steps, "no CI step runs the Playwright suite"

    for step in playwright_steps:
        assert step.get("env", {}).get("ASTINUSLAB_PYTHON"), (
            f"step {step.get('name')!r} runs Playwright without an explicit ASTINUSLAB_PYTHON"
        )


def _playwright_server_config(overrides: dict[str, str]) -> dict:
    result = subprocess.run(
        [
            "node",
            "-e",
            "const {use, webServer} = require('./playwright.config.js');"
            "console.log(JSON.stringify({baseURL: use.baseURL, url: webServer.url,"
            "command: webServer.command, apiPort: webServer.env.ASTINUSLAB_API_PORT,"
            "reuseExistingServer: webServer.reuseExistingServer}));",
        ],
        env={**os.environ, **overrides},
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_smoke_port_override_keeps_server_and_browser_origins_aligned() -> None:
    config = _playwright_server_config({"ASTINUSLAB_E2E_FULL": "0", "ASTINUSLAB_SMOKE_PORT": "18087"})
    assert config["baseURL"] == "http://127.0.0.1:18087"
    assert config["url"] == "http://127.0.0.1:18087/health"
    assert "--port 18087" in config["command"]
    assert config["apiPort"] == "18087"
    assert config["reuseExistingServer"] is False


@pytest.mark.parametrize("port", ["0", "65536", "1.5", "invalid"])
def test_smoke_port_override_rejects_invalid_ports(port: str) -> None:
    result = subprocess.run(
        ["node", "-e", "require('./playwright.config.js')"],
        env={**os.environ, "ASTINUSLAB_E2E_FULL": "0", "ASTINUSLAB_SMOKE_PORT": port},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "ASTINUSLAB_SMOKE_PORT" in result.stderr


def test_full_tier_retains_its_server_port_when_smoke_port_is_overridden() -> None:
    config = _playwright_server_config(
        {"ASTINUSLAB_E2E_FULL": "1", "ASTINUSLAB_SMOKE_PORT": "18087", "ASTINUSLAB_API_PORT": "9000"}
    )
    assert config["baseURL"] == "http://127.0.0.1:8000"
    assert config["url"] == "http://127.0.0.1:8000/health"
    assert "e2e_full_server.sh" in config["command"]
    assert config["apiPort"] == "8000"


def _run_commands(run: str) -> list[str]:
    """Split a workflow ``run`` block into logical commands, joining line continuations."""
    joined = run.replace("\\\n", " ")
    return [line.strip() for line in joined.splitlines() if line.strip()]


_GATE_PREFIXES = ("bash -n ", "shellcheck ", "python tools/quality/audit_error_fields.py")


def test_ci_runs_shell_and_error_boundary_gates() -> None:
    """Shell/error-field gates must run on every push and fail the build when they fail.

    Task 6 hardens four shell scripts and Task 5 changes the API error payloads, so
    ShellCheck, ``bash -n``, and the error-field audit have to be enforced by CI rather
    than by a local run someone can forget.
    """
    workflow = yaml.safe_load(Path(".github/workflows/ci.yml").read_text(encoding="utf-8"))
    required_scripts = (
        "ops/deploy/install.sh",
        "ops/deploy/uninstall.sh",
        "ops/host/build_vol3_symbol_remote.sh",
        "vol3/scripts/build_symbol_from_live_guest.sh",
        "guest/scripts/build_vol3_symbol.sh",
    )

    job = workflow["jobs"]["shell-boundary"]
    assert "if" not in job, "the shell-boundary job must run unconditionally"
    assert job.get("continue-on-error") is not True, "the shell-boundary job must fail the build"

    steps = job["steps"]
    # Match invocations, not mentions: the ShellCheck install step also names the tool.
    run_lines = [line for step in steps for line in _run_commands(step.get("run", ""))]
    bash_commands = [line for line in run_lines if line.startswith("bash -n ")]
    shellcheck_commands = [line for line in run_lines if line.startswith("shellcheck ")]
    audit_commands = [line for line in run_lines if line.startswith("python tools/quality/audit_error_fields.py")]

    assert len(bash_commands) == 1
    assert len(shellcheck_commands) == 1
    assert len(audit_commands) == 1
    bash_command = bash_commands[0]
    shellcheck_command = shellcheck_commands[0]
    for script in required_scripts:
        assert script in bash_command
        assert script in shellcheck_command

    # A gate that cannot turn the build red is not a gate.
    for command in (bash_command, shellcheck_command, *audit_commands):
        assert not re.search(r"\|\|\s*(true|:)|;\s*true\b|\bset \+e\b", command), (
            f"gate command must not swallow its exit status: {command}"
        )
    gate_steps = [
        step for step in steps if any(line.startswith(_GATE_PREFIXES) for line in _run_commands(step.get("run", "")))
    ]
    assert len(gate_steps) == 3
    for step in gate_steps:
        name = step.get("name", "<unnamed>")
        assert step.get("continue-on-error") is not True, f"gate step {name!r} must fail the build"
        assert "if" not in step, f"gate step {name!r} must run unconditionally"

    checkouts = [
        step
        for step in workflow["jobs"]["backend"]["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout")
    ]
    assert len(checkouts) == 1
    assert checkouts[0].get("with", {}).get("fetch-depth") == 0


def test_playwright_web_server_uses_tracked_example_catalogs() -> None:
    config_text = Path("playwright.config.js").read_text(encoding="utf-8")

    expected_catalogs = {
        "ASTINUSLAB_TARGETS_CATALOG_PATH": "targets",
        "ASTINUSLAB_SCENARIOS_CATALOG_PATH": "scenarios",
        "ASTINUSLAB_WORKLOADS_CATALOG_PATH": "workloads",
        "ASTINUSLAB_ATTACKERS_CATALOG_PATH": "attackers",
        "ASTINUSLAB_NETWORK_NODES_CATALOG_PATH": "network_nodes",
    }

    for env_var, catalog_name in expected_catalogs.items():
        assert env_var in config_text
        assert f"catalogPath('{catalog_name}')" in config_text or f'catalogPath("{catalog_name}")' in config_text

    assert "ASTINUSLAB_IMAGES_DIR" in config_text
    assert "smokeImagesDir" in config_text
    assert (
        "tests', 'browser', 'fixtures', 'images" in config_text
        or 'tests", "browser", "fixtures", "images' in config_text
    )
