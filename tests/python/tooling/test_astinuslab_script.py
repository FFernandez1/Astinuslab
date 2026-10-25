import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "astinuslab.sh"
pytestmark = [pytest.mark.unit, pytest.mark.host]


def copy_launcher(tmp_path: Path) -> Path:
    dest = tmp_path / "astinuslab.sh"
    shutil.copy2(SCRIPT, dest)
    os.chmod(dest, 0o755)
    return dest


def launcher_env(env=None):
    # Each copied launcher owns its fixture's configuration. Do not inherit the
    # parent application's isolated DATA_DIR, catalogs, credentials or endpoints.
    # Keep fake-mode boundaries and permit explicit per-test overrides.
    fake_flags = {"ASTINUSLAB_FAKE_LIBVIRT", "ASTINUSLAB_FAKE_PLAYGROUND"}
    merged = {key: value for key, value in os.environ.items() if not key.startswith("ASTINUSLAB_") or key in fake_flags}
    if env:
        merged.update(env)
    return merged


def _seed_start_checkout(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """Exercise the real launcher with inert service commands and private state."""
    launcher = copy_launcher(tmp_path)
    venv_bin = tmp_path / ".venv/bin"
    venv_bin.mkdir(parents=True)
    write_helper(
        venv_bin,
        "python",
        'case "$*" in\n'
        '  "-c import mkdocs")\n'
        '    echo docs-check >> "$START_STEPS"\n'
        '    exit "${DOCS_IMPORT_EXIT:-0}" ;;\n'
        '  "-m astinuslab.dbcli init")\n'
        '    echo migration >> "$START_STEPS"\n'
        '    exit "${MIGRATION_EXIT:-0}" ;;\n'
        "esac\n"
        "exit 0\n",
    )
    write_helper(venv_bin, "celery", "exit 0\n")
    write_helper(
        venv_bin,
        "pip",
        'echo docs-install >> "$START_STEPS"\nexit "${DOCS_INSTALL_EXIT:-0}"\n',
    )
    helpers = tmp_path / "helpers"
    helpers.mkdir()
    write_helper(helpers, "systemctl", "exit 1\n")
    write_helper(helpers, "setfacl", "exit 0\n")
    return launcher, {
        "PATH": str(helpers) + os.pathsep + os.environ["PATH"],
        "START_STEPS": str(tmp_path / "start-steps"),
        "ASTINUSLAB_RUNTIME_IDENTITIES_FILE": str(tmp_path / "absent-runtime-identities"),
    }


@pytest.mark.parametrize("existing_api", [False, True])
def test_start_docs_dependency_failure_leaves_service_pids_untouched(tmp_path: Path, existing_api: bool):
    launcher, env = _seed_start_checkout(tmp_path)
    storage = tmp_path / "storage"
    storage.mkdir()
    if existing_api:
        (storage / "api.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    original_pids = {p.name: p.read_bytes() for p in storage.glob("*.pid")}

    result = run(launcher, "start", "--docs", env={**env, "DOCS_IMPORT_EXIT": "1", "DOCS_INSTALL_EXIT": "23"})

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Failed to install docs dependencies" in result.stderr
    assert {p.name: p.read_bytes() for p in storage.glob("*.pid")} == original_pids
    assert (tmp_path / "start-steps").read_text().splitlines() == ["docs-check", "docs-install"]


@pytest.mark.parametrize("docs_installed", [False, True])
def test_start_prepares_docs_before_migrations_and_launches(tmp_path: Path, docs_installed: bool):
    launcher, env = _seed_start_checkout(tmp_path)

    result = run(launcher, "start", "--docs", env={**env, "DOCS_IMPORT_EXIT": "0" if docs_installed else "1"})

    assert result.returncode == 0, result.stdout + result.stderr
    expected_steps = ["docs-check", "migration"] if docs_installed else ["docs-check", "docs-install", "migration"]
    assert (tmp_path / "start-steps").read_text().splitlines() == expected_steps
    assert sorted(p.name for p in (tmp_path / "storage").glob("*.pid")) == [
        "analysis.pid",
        "api.pid",
        "docs.pid",
        "runner.pid",
    ]


def test_start_without_docs_does_not_prepare_docs(tmp_path: Path):
    launcher, env = _seed_start_checkout(tmp_path)

    result = run(launcher, "start", env={**env, "DOCS_IMPORT_EXIT": "1", "DOCS_INSTALL_EXIT": "23"})

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "start-steps").read_text().splitlines() == ["migration"]
    assert sorted(p.name for p in (tmp_path / "storage").glob("*.pid")) == ["analysis.pid", "api.pid", "runner.pid"]


@pytest.mark.parametrize("with_docs", [False, True])
def test_start_migration_failure_does_not_launch_services(tmp_path: Path, with_docs: bool):
    launcher, env = _seed_start_checkout(tmp_path)

    result = run(launcher, "start", *(["--docs"] if with_docs else []), env={**env, "MIGRATION_EXIT": "31"})

    assert result.returncode == 1, result.stdout + result.stderr
    assert "database migration failed; refusing to start" in result.stderr
    assert not list((tmp_path / "storage").glob("*.pid"))


def test_split_logs_tails_resolved_role_files_and_refuses_resolver_failure(tmp_path):
    body = SCRIPT.read_text().split("cmd_logs() {", 1)[1].split("\n}\n", 1)[0]
    helpers = tmp_path / "helpers"
    helpers.mkdir()
    output = tmp_path / "tailed"
    write_helper(helpers, "tail", 'printf "%s\\n" "$@" > "$CAPTURE"\n')
    python = write_helper(
        helpers,
        "python",
        'cat >/dev/null\nprintf "%s\\n" /private/logs/api/api.log /private/logs/runner/runner.log /private/logs/analysis/analysis-worker.log\n',
    )
    venv = tmp_path / "venv/bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(python)
    command = (
        "info() { :; }; warn() { :; }; _venv_ok() { return 0; }; SPLIT_USERS=true; API_LOG=flat-api; RUNNER_LOG=flat-runner; ANALYSIS_LOG=flat-analysis; cmd_logs() {"
        + body
        + "\n}; cmd_logs"
    )
    env = {
        **os.environ,
        "PATH": str(helpers) + os.pathsep + os.environ["PATH"],
        "VENV": str(venv.parent),
        "CAPTURE": str(output),
    }
    result = subprocess.run(["bash", "-c", command], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert output.read_text().splitlines() == [
        "-f",
        "/private/logs/api/api.log",
        "/private/logs/runner/runner.log",
        "/private/logs/analysis/analysis-worker.log",
    ]
    output.unlink()
    python.write_text("#!/bin/sh\nexit 23\n")
    result = subprocess.run(["bash", "-c", command], env=env, capture_output=True, text=True)
    assert result.returncode != 0 and not output.exists()


def run(script: Path, *args, env=None):
    return subprocess.run(
        [str(script), *args], cwd=script.parent, env=launcher_env(env), capture_output=True, text=True
    )


def seed_smoke_checkout(tmp_path: Path) -> None:
    (tmp_path / ".venv/bin").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src/astinuslab").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src/astinuslab/main.py").write_text("app = object()\n", encoding="utf-8")
    (tmp_path / "config/catalog").mkdir(parents=True, exist_ok=True)
    (tmp_path / "astinuslab.toml").write_text("[app]\n", encoding="utf-8")
    (tmp_path / "config/astinuslab.env").write_text("", encoding="utf-8")
    for name in ["targets", "workloads", "scenarios", "attackers", "profiles", "network_nodes"]:
        (tmp_path / f"config/catalog/{name}.yaml").write_text("{}\n", encoding="utf-8")
    (tmp_path / "images/definitions").mkdir(parents=True, exist_ok=True)
    (tmp_path / "images/definitions/test.qcow2").write_text("image\n", encoding="utf-8")


def write_helper(helper_dir: Path, name: str, body: str) -> Path:
    helper = helper_dir / name
    helper.write_text(f"#!/usr/bin/env bash\n{body}", encoding="utf-8")
    os.chmod(helper, 0o755)
    return helper


def write_smoke_helpers(helper_dir: Path, sudo_body: str = "exit 0\n", *, curl_body: str | None = None) -> None:
    helper_dir.mkdir()
    write_helper(
        helper_dir,
        "virsh",
        'if [[ $# -eq 3 && "${1:-}" == "-c" && "${2:-}" == "qemu:///system" && "${3:-}" == "version" ]]; then\n'
        "  exit 0\n"
        "fi\n"
        'if [[ $# -eq 4 && "${1:-}" == "-c" && "${2:-}" == "qemu:///system" && "${3:-}" == "net-info" && "${4:-}" == "default" ]]; then\n'
        "  printf '%s\\n' 'Name: default' 'Active: yes'\n"
        "  exit 0\n"
        "fi\n"
        "exit 2\n",
    )
    write_helper(helper_dir, "qemu-img", "exit 0\n")
    write_helper(helper_dir, "sudo", sudo_body)
    if curl_body is not None:
        write_helper(helper_dir, "curl", curl_body)


# Smoke probes /ready as well as /health, so every curl stub has to answer it.
READY_PAYLOADS = {
    "ready": (
        200,
        '{"status":"ready","checks":{"database":{"status":"ok","critical":true},'
        '"storage":{"status":"ok","critical":true},"runner":{"status":"ok","critical":false},'
        '"analysis":{"status":"ok","critical":false},"libvirt":{"status":"ok","critical":false}}}',
    ),
    "degraded": (
        200,
        '{"status":"degraded","checks":{"database":{"status":"ok","critical":true},'
        '"storage":{"status":"ok","critical":true},"runner":{"status":"unavailable","critical":false},'
        '"analysis":{"status":"unavailable","critical":false},"libvirt":{"status":"ok","critical":false}}}',
    ),
    "not_ready": (
        503,
        '{"status":"not_ready","checks":{"database":{"status":"unavailable","critical":true},'
        '"storage":{"status":"ok","critical":true},"runner":{"status":"ok","critical":false},'
        '"analysis":{"status":"ok","critical":false},"libvirt":{"status":"ok","critical":false}}}',
    ),
}


def _readiness_curl_body(outcome: str = "ready", *, then: str = "exit 0\n") -> str:
    """A curl stub that answers /ready with a fixed payload and runs *then* otherwise."""
    code, payload = READY_PAYLOADS[outcome]
    return (
        'for arg in "$@"; do\n'
        '  case "$arg" in\n'
        "    */ready)\n"
        f"      printf '%s\\n{code}' '{payload}'\n"
        "      exit 0\n"
        "      ;;\n"
        "  esac\n"
        "done\n"
    ) + then


def smoke_host_env(tmp_path: Path, helper_dir: Path) -> dict[str, str]:
    kvm_device = tmp_path / "dev/kvm"
    kvm_device.parent.mkdir(parents=True, exist_ok=True)
    kvm_device.write_text("kvm\n", encoding="utf-8")
    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text("flags\t: vmx\n", encoding="utf-8")
    return {
        "PATH": f"{helper_dir}:{os.environ['PATH']}",
        "ASTINUSLAB_SMOKE_KVM_DEVICE": str(kvm_device),
        "ASTINUSLAB_SMOKE_CPUINFO_PATH": str(cpuinfo),
    }


def test_usage_includes_setup_host_and_smoke():
    result = subprocess.run([str(SCRIPT)], cwd=ROOT, capture_output=True, text=True)

    assert result.returncode != 0
    assert "setup-host" in result.stdout
    assert "smoke" in result.stdout


def test_docs_extra_uses_unambiguous_braced_base_dir():
    launcher = SCRIPT.read_text(encoding="utf-8")

    assert '"${BASE_DIR}[docs]"' in launcher
    assert '"$BASE_DIR[docs]"' not in launcher


@pytest.mark.parametrize("flag", ["help", "--help", "-h"])
def test_explicit_help_exits_zero(flag):
    result = subprocess.run([str(SCRIPT), flag], cwd=ROOT, capture_output=True, text=True)

    assert result.returncode == 0
    assert "guide-volatility" in result.stdout


def test_unknown_command_still_exits_nonzero():
    result = subprocess.run([str(SCRIPT), "not-a-command"], cwd=ROOT, capture_output=True, text=True)

    assert result.returncode != 0


def test_seed_local_config_creates_env_once_without_overwrite(tmp_path):
    launcher = copy_launcher(tmp_path)
    (tmp_path / "config/catalog").mkdir(parents=True)
    (tmp_path / "config/astinuslab.env.example").write_text("ASTINUSLAB_API_PORT=8000\n", encoding="utf-8")
    (tmp_path / "astinuslab.toml.example").write_text("[app]\n", encoding="utf-8")
    (tmp_path / "config/catalog/targets.example.yaml").write_text("targets: []\n", encoding="utf-8")
    lines = launcher.read_text(encoding="utf-8").splitlines()
    dispatch = next(index for index, line in enumerate(lines) if line.startswith("# ── Dispatch"))
    harness = tmp_path / "seed-config.sh"
    harness.write_text("\n".join(lines[:dispatch]) + "\n_seed_local_config\n", encoding="utf-8")
    marker = tmp_path / "missing-runtime-identities"
    env = launcher_env({"ASTINUSLAB_RUNTIME_IDENTITIES_FILE": str(marker)})

    first = subprocess.run(["bash", str(harness)], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert first.returncode == 0, first.stdout + first.stderr
    env_file = tmp_path / "config/astinuslab.env"
    assert env_file.read_text(encoding="utf-8") == "ASTINUSLAB_API_PORT=8000\n"

    env_file.write_text("ASTINUSLAB_API_PORT=9000\n", encoding="utf-8")
    second = subprocess.run(["bash", str(harness)], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert second.returncode == 0, second.stdout + second.stderr
    assert env_file.read_text(encoding="utf-8") == "ASTINUSLAB_API_PORT=9000\n"


def test_guide_volatility_passes_context_without_mutating_host(tmp_path):
    launcher = copy_launcher(tmp_path)
    (tmp_path / ".venv/bin").mkdir(parents=True)
    args_log = tmp_path / "python-args.log"
    python = tmp_path / ".venv/bin/python"
    python.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$PYTHON_ARGS_LOG"\n', encoding="utf-8")
    os.chmod(python, 0o755)
    marker = tmp_path / "runtime-identities"
    marker.write_text(
        "mode=split\napi=astinuslab-api\nrunner=astinuslab-runner\nanalysis=astinuslab-analysis\ngroup=astinuslab\n",
        encoding="utf-8",
    )
    helper_dir = tmp_path / "helpers"
    helper_dir.mkdir()
    mutation_marker = tmp_path / "mutation-command-ran"
    forbidden = 'touch "$MUTATION_MARKER"\nexit 99\n'
    for command in ("sudo", "apt", "dnf", "pacman", "zypper", "curl", "wget", "systemctl"):
        write_helper(helper_dir, command, forbidden)

    result = run(
        launcher,
        "guide-volatility",
        env={
            "PATH": f"{helper_dir}:{os.environ['PATH']}",
            "ASTINUSLAB_RUNTIME_IDENTITIES_FILE": str(marker),
            "PYTHON_ARGS_LOG": str(args_log),
            "MUTATION_MARKER": str(mutation_marker),
            "ASTINUSLAB_API_HOST": "0.0.0.0",
            "ASTINUSLAB_API_PORT": "8123",
            "ASTINUSLAB_SERVICE_USER": "operator",
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert args_log.read_text(encoding="utf-8").splitlines() == [
        "-m",
        "astinuslab.forensics.vol3_guide",
        "--base-dir",
        str(tmp_path),
        "--data-dir",
        str(tmp_path),
        "--service-user",
        "operator",
        "--api-url",
        "http://127.0.0.1:8123",
        "--split-users",
    ]
    assert not mutation_marker.exists()


def test_launcher_detects_explicit_split_runtime_units(tmp_path):
    launcher = copy_launcher(tmp_path)
    marker = tmp_path / "runtime-identities"
    marker.write_text(
        "mode=split\napi=astinuslab-api\nrunner=astinuslab-runner\nanalysis=astinuslab-analysis\ngroup=astinuslab\n",
        encoding="utf-8",
    )

    helper_dir = tmp_path / "helpers"
    helper_dir.mkdir()
    write_helper(
        helper_dir,
        "systemctl",
        '[[ "${1:-}" == is-active && "${3:-}" != *@* ]] && exit 0\nexit 1\n',
    )
    result = run(
        launcher,
        "status",
        env={
            "PATH": f"{helper_dir}:{os.environ['PATH']}",
            "ASTINUSLAB_RUNTIME_IDENTITIES_FILE": str(marker),
        },
    )

    assert result.returncode == 0, result.stderr
    assert "Active runner mode: systemd" in result.stdout
    assert "astinuslab-api.service" in result.stdout
    assert "astinuslab-runner.service" in result.stdout
    assert "astinuslab-analysis.service" in result.stdout
    assert "astinuslab-api@" not in result.stdout


def test_launcher_rejects_partial_split_identity_marker(tmp_path):
    launcher = copy_launcher(tmp_path)
    marker = tmp_path / "runtime-identities"
    marker.write_text("mode=split\napi=astinuslab-api\n", encoding="utf-8")

    result = run(
        launcher,
        "status",
        env={"ASTINUSLAB_RUNTIME_IDENTITIES_FILE": str(marker)},
    )

    assert result.returncode != 0
    assert "incomplete or malformed split-user state" in result.stderr


def test_launcher_rejects_explicit_split_units_without_marker(tmp_path):
    launcher = copy_launcher(tmp_path)
    marker = tmp_path / "missing-runtime-identities"
    helper_dir = tmp_path / "helpers"
    helper_dir.mkdir()
    write_helper(
        helper_dir,
        "systemctl",
        '[[ "${1:-}" == cat && "${2:-}" == astinuslab-api.service ]] && exit 0\nexit 1\n',
    )

    result = run(
        launcher,
        "status",
        env={
            "PATH": f"{helper_dir}:{os.environ['PATH']}",
            "ASTINUSLAB_RUNTIME_IDENTITIES_FILE": str(marker),
        },
    )

    assert result.returncode != 0
    assert "incomplete or malformed split-user state" in result.stderr


def test_split_smoke_does_not_probe_runner_sudo_as_operator(tmp_path):
    launcher = copy_launcher(tmp_path)
    seed_smoke_checkout(tmp_path)
    marker = tmp_path / "runtime-identities"
    marker.write_text(
        "mode=split\napi=astinuslab-api\nrunner=astinuslab-runner\nanalysis=astinuslab-analysis\ngroup=astinuslab\n",
        encoding="utf-8",
    )
    python = tmp_path / ".venv/bin/python"
    python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    os.chmod(python, 0o755)
    (tmp_path / "storage").mkdir(exist_ok=True)
    (tmp_path / "storage/astinuslab.db").write_text("db\n", encoding="utf-8")
    helper_dir = tmp_path / "helpers"
    write_smoke_helpers(helper_dir, sudo_body="echo operator-sudo-probe >&2\nexit 1\n")
    env = smoke_host_env(tmp_path, helper_dir)
    env["ASTINUSLAB_RUNTIME_IDENTITIES_FILE"] = str(marker)

    result = run(launcher, "smoke", env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "runner-owned in split mode" in result.stdout
    assert "operator-sudo-probe" not in result.stderr


def test_split_doctor_does_not_read_or_write_service_owned_state(tmp_path):
    launcher = copy_launcher(tmp_path)
    seed_smoke_checkout(tmp_path)
    marker = tmp_path / "runtime-identities"
    marker.write_text(
        "mode=split\napi=astinuslab-api\nrunner=astinuslab-runner\nanalysis=astinuslab-analysis\ngroup=astinuslab\n",
        encoding="utf-8",
    )
    python = tmp_path / ".venv/bin/python"
    python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    os.chmod(python, 0o755)
    storage = tmp_path / "storage"
    storage.mkdir(exist_ok=True)
    (storage / "astinuslab.db").write_text("db\n", encoding="utf-8")
    os.chmod(tmp_path / "config", 0o000)
    os.chmod(storage, 0o000)
    helper_dir = tmp_path / "helpers"
    write_smoke_helpers(helper_dir)
    env = smoke_host_env(tmp_path, helper_dir)
    env["ASTINUSLAB_RUNTIME_IDENTITIES_FILE"] = str(marker)

    try:
        result = run(launcher, "doctor", env=env)
    finally:
        os.chmod(tmp_path / "config", 0o700)
        os.chmod(storage, 0o700)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "service-owned config and database checks delegated to authenticated deep readiness" in result.stdout
    assert "Permission denied" not in result.stderr


def test_split_start_refuses_manual_single_uid_launch_when_units_are_stopped(tmp_path):
    launcher = copy_launcher(tmp_path)
    seed_smoke_checkout(tmp_path)
    marker = tmp_path / "runtime-identities"
    marker.write_text(
        "mode=split\napi=astinuslab-api\nrunner=astinuslab-runner\nanalysis=astinuslab-analysis\ngroup=astinuslab\n",
        encoding="utf-8",
    )
    python = tmp_path / ".venv/bin/python"
    python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    os.chmod(python, 0o755)
    celery = tmp_path / ".venv/bin/celery"
    celery.write_text("#!/usr/bin/env bash\ntouch manual-worker-started\nexit 0\n", encoding="utf-8")
    os.chmod(celery, 0o755)

    result = run(
        launcher,
        "start",
        env={"ASTINUSLAB_RUNTIME_IDENTITIES_FILE": str(marker)},
    )

    assert result.returncode != 0
    assert "split-user install must be controlled through its explicit systemd units" in result.stderr
    assert not (tmp_path / "manual-worker-started").exists()


def test_split_smoke_uses_token_for_authenticated_deep_readiness(tmp_path):
    launcher = copy_launcher(tmp_path)
    seed_smoke_checkout(tmp_path)
    marker = tmp_path / "runtime-identities"
    marker.write_text(
        "mode=split\napi=astinuslab-api\nrunner=astinuslab-runner\nanalysis=astinuslab-analysis\ngroup=astinuslab\n",
        encoding="utf-8",
    )
    python = tmp_path / ".venv/bin/python"
    python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    os.chmod(python, 0o755)
    (tmp_path / "storage").mkdir(exist_ok=True)
    helper_dir = tmp_path / "helpers"
    write_smoke_helpers(
        helper_dir,
        curl_body=_readiness_curl_body(then='printf "%s\\n" "$*" >> "$CURL_ARGS_LOG"\nexit 0\n'),
    )
    write_helper(
        helper_dir,
        "systemctl",
        '[[ "${1:-}" == is-active && "${3:-}" != *@* ]] && exit 0\nexit 1\n',
    )
    args_log = tmp_path / "curl-args.log"
    env = smoke_host_env(tmp_path, helper_dir)
    env.update(
        {
            "ASTINUSLAB_RUNTIME_IDENTITIES_FILE": str(marker),
            "ASTINUSLAB_ADMIN_TOKEN": "task26-secret",
            "CURL_ARGS_LOG": str(args_log),
        }
    )

    result = run(launcher, "smoke", "--require-running", env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    curl_args = args_log.read_text(encoding="utf-8")
    assert "Authorization: Bearer task26-secret" in curl_args
    assert "/api/v1/readiness" in curl_args
    assert "authenticated deep readiness" in result.stdout


def test_split_smoke_fails_when_running_api_has_no_readiness_token(tmp_path):
    launcher = copy_launcher(tmp_path)
    seed_smoke_checkout(tmp_path)
    marker = tmp_path / "runtime-identities"
    marker.write_text(
        "mode=split\napi=astinuslab-api\nrunner=astinuslab-runner\nanalysis=astinuslab-analysis\ngroup=astinuslab\n",
        encoding="utf-8",
    )
    python = tmp_path / ".venv/bin/python"
    python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    os.chmod(python, 0o755)
    helper_dir = tmp_path / "helpers"
    write_smoke_helpers(helper_dir)
    write_helper(
        helper_dir,
        "systemctl",
        '[[ "${1:-}" == is-active && "${3:-}" != *@* ]] && exit 0\nexit 1\n',
    )
    env = smoke_host_env(tmp_path, helper_dir)
    env["ASTINUSLAB_RUNTIME_IDENTITIES_FILE"] = str(marker)

    result = run(launcher, "doctor", "--require-running", env=env)

    assert result.returncode != 0
    assert "ASTINUSLAB_ADMIN_TOKEN" in result.stdout


def test_split_smoke_delegates_operator_libvirt_access_to_deep_readiness(tmp_path):
    launcher = copy_launcher(tmp_path)
    seed_smoke_checkout(tmp_path)
    marker = tmp_path / "runtime-identities"
    marker.write_text(
        "mode=split\napi=astinuslab-api\nrunner=astinuslab-runner\nanalysis=astinuslab-analysis\ngroup=astinuslab\n",
        encoding="utf-8",
    )
    python = tmp_path / ".venv/bin/python"
    python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    os.chmod(python, 0o755)
    helper_dir = tmp_path / "helpers"
    write_smoke_helpers(helper_dir)
    write_helper(helper_dir, "virsh", "exit 1\n")
    write_helper(helper_dir, "curl", _readiness_curl_body())
    write_helper(
        helper_dir,
        "systemctl",
        '[[ "${1:-}" == is-active && "${3:-}" != *@* ]] && exit 0\nexit 1\n',
    )
    env = smoke_host_env(tmp_path, helper_dir)
    env.update(
        {
            "ASTINUSLAB_RUNTIME_IDENTITIES_FILE": str(marker),
            "ASTINUSLAB_ADMIN_TOKEN": "task26-secret",
            "ASTINUSLAB_SMOKE_KVM_DEVICE": str(tmp_path / "operator-inaccessible-kvm"),
        }
    )

    result = run(launcher, "smoke", "--require-running", env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "runner-owned" in result.stdout
    assert "cannot connect to libvirt" not in result.stdout


@pytest.mark.parametrize("command", ["cleanup", "uninstall"])
def test_split_launcher_refuses_destructive_legacy_workflows(tmp_path, command):
    launcher = copy_launcher(tmp_path)
    marker = tmp_path / "runtime-identities"
    marker.write_text(
        "mode=split\napi=astinuslab-api\nrunner=astinuslab-runner\nanalysis=astinuslab-analysis\ngroup=astinuslab\n",
        encoding="utf-8",
    )
    helper_dir = tmp_path / "helpers"
    helper_dir.mkdir()
    destructive_marker = tmp_path / "destructive-helper-ran"
    write_helper(helper_dir, "virsh", 'touch "$DESTRUCTIVE_MARKER"\nexit 1\n')
    env = launcher_env()
    env.update(
        {
            "PATH": f"{helper_dir}:{env['PATH']}",
            "ASTINUSLAB_RUNTIME_IDENTITIES_FILE": str(marker),
            "DESTRUCTIVE_MARKER": str(destructive_marker),
        }
    )

    result = subprocess.run(
        [str(launcher), command],
        cwd=tmp_path,
        env=env,
        input="yes\nn\nn\n",
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "split-user" in (result.stdout + result.stderr)
    assert not destructive_marker.exists()


def test_setup_host_help_documents_options(tmp_path):
    launcher = copy_launcher(tmp_path)

    result = run(launcher, "setup-host", "--help")

    assert result.returncode == 0
    assert "--with-mac" in result.stdout
    assert "--run-user USER" in result.stdout


def test_split_setup_host_preserves_evidence_read_policy():
    launcher = SCRIPT.read_text(encoding="utf-8")
    setup_host = launcher[launcher.index("setup_host()") : launcher.index("cmd_start()")]

    assert "--evidence-read-group" in setup_host
    assert "astinuslab" in setup_host


def test_setup_host_refuses_root_without_run_user_when_no_sudo_user(tmp_path):
    launcher = copy_launcher(tmp_path)

    result = run(launcher, "setup-host", env={"SUDO_USER": ""})

    if os.geteuid() == 0:
        assert result.returncode != 0
        assert "--run-user USER" in result.stderr
    else:
        assert "sudo" in (result.stdout + result.stderr)


def test_smoke_missing_venv_exits_nonzero_with_clear_message(tmp_path):
    launcher = copy_launcher(tmp_path)

    result = run(launcher, "smoke")

    assert result.returncode != 0
    assert "venv missing" in (result.stdout + result.stderr)
    assert "./astinuslab.sh setup" in (result.stdout + result.stderr)


def test_smoke_uses_kvm_device_override_for_host_probe(tmp_path):
    launcher = copy_launcher(tmp_path)
    seed_smoke_checkout(tmp_path)
    python = tmp_path / ".venv/bin/python"
    python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    os.chmod(python, 0o755)
    (tmp_path / "storage").mkdir(exist_ok=True)
    (tmp_path / "storage/astinuslab.db").write_text("db\n", encoding="utf-8")
    helper_dir = tmp_path / "helpers"
    write_smoke_helpers(helper_dir)
    env = smoke_host_env(tmp_path, helper_dir)
    missing_kvm = tmp_path / "missing-kvm"
    env["ASTINUSLAB_SMOKE_KVM_DEVICE"] = str(missing_kvm)

    result = run(launcher, "smoke", env=env)

    assert result.returncode != 0
    assert f"KVM not available: {missing_kvm} missing" in result.stdout


def test_smoke_success_path_uses_db_status_and_passes(tmp_path):
    launcher = copy_launcher(tmp_path)
    seed_smoke_checkout(tmp_path)
    python = tmp_path / ".venv/bin/python"
    python.write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> python-args.log\nexit 0\n",
        encoding="utf-8",
    )
    os.chmod(python, 0o755)
    helper_dir = tmp_path / "helpers"
    write_smoke_helpers(helper_dir)
    (tmp_path / "storage").mkdir(exist_ok=True)
    (tmp_path / "storage/astinuslab.db").write_text("db\n", encoding="utf-8")

    result = run(launcher, "smoke", env=smoke_host_env(tmp_path, helper_dir))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Smoke checks passed" in result.stdout
    args_log = (tmp_path / "python-args.log").read_text(encoding="utf-8")
    assert "-m astinuslab.dbcli status" in args_log
    assert "-m astinuslab.dbcli init" not in args_log


def test_smoke_helper_sudo_failure_prints_setup_host_command(tmp_path):
    launcher = copy_launcher(tmp_path)
    seed_smoke_checkout(tmp_path)
    python = tmp_path / ".venv/bin/python"
    python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    os.chmod(python, 0o755)
    helper_dir = tmp_path / "helpers"
    write_smoke_helpers(helper_dir, sudo_body="exit 1\n")

    result = run(launcher, "smoke", env=smoke_host_env(tmp_path, helper_dir))

    assert result.returncode != 0
    assert "privileged helper failed" in result.stdout
    assert "sudo ./astinuslab.sh setup-host" in result.stdout


def test_smoke_python_version_failure_is_aggregated(tmp_path):
    launcher = copy_launcher(tmp_path)
    seed_smoke_checkout(tmp_path)
    python = tmp_path / ".venv/bin/python"
    python.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "${1:-}" == "-" && ! -f python-version-failed ]]; then\n'
        "  touch python-version-failed\n"
        "  exit 1\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    os.chmod(python, 0o755)
    (tmp_path / "storage").mkdir(exist_ok=True)
    (tmp_path / "storage/astinuslab.db").write_text("db\n", encoding="utf-8")
    helper_dir = tmp_path / "helpers"
    write_smoke_helpers(helper_dir)

    result = run(launcher, "smoke", env=smoke_host_env(tmp_path, helper_dir))

    assert result.returncode != 0
    assert "Python 3.12+ required" in result.stdout
    assert "config OK" in result.stdout
    assert "privileged helper OK" in result.stdout
    assert "Smoke checks failed" in result.stderr


def test_smoke_require_running_fails_when_runner_is_stopped(tmp_path):
    launcher = copy_launcher(tmp_path)
    seed_smoke_checkout(tmp_path)
    python = tmp_path / ".venv/bin/python"
    python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    os.chmod(python, 0o755)
    (tmp_path / "storage").mkdir(exist_ok=True)
    (tmp_path / "storage/astinuslab.db").write_text("db\n", encoding="utf-8")
    (tmp_path / "storage/api.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    helper_dir = tmp_path / "helpers"
    write_smoke_helpers(helper_dir, curl_body=_readiness_curl_body())

    result = run(launcher, "smoke", "--require-running", env=smoke_host_env(tmp_path, helper_dir))

    assert result.returncode != 0
    assert "API health" in result.stdout
    assert "runner is stopped" in result.stdout


def test_smoke_ipv6_host_uses_bracketed_health_url(tmp_path):
    script = SCRIPT.read_text(encoding="utf-8")

    assert "_probe_host_for_url" in script
    assert "http://[${probe_host}]:${API_PORT}" in script


def _run_readiness_smoke(tmp_path, outcome: str):
    launcher = copy_launcher(tmp_path)
    seed_smoke_checkout(tmp_path)
    python = tmp_path / ".venv/bin/python"
    python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    os.chmod(python, 0o755)
    (tmp_path / "storage").mkdir(exist_ok=True)
    (tmp_path / "storage/astinuslab.db").write_text("db\n", encoding="utf-8")
    (tmp_path / "storage/api.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    (tmp_path / "storage/runner.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    (tmp_path / "storage/analysis.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    helper_dir = tmp_path / "helpers"
    write_smoke_helpers(helper_dir, curl_body=_readiness_curl_body(outcome))

    return run(launcher, "smoke", env=smoke_host_env(tmp_path, helper_dir))


def test_smoke_reports_readiness_not_only_liveness(tmp_path):
    """/health answers whether the process is alive; only /ready answers whether it can work."""
    result = _run_readiness_smoke(tmp_path, "ready")

    assert "API readiness" in result.stdout
    assert "/ready" in result.stdout


def test_smoke_names_the_unavailable_checks_when_degraded(tmp_path):
    result = _run_readiness_smoke(tmp_path, "degraded")

    assert "degraded" in result.stdout
    assert "runner" in result.stdout
    assert "analysis" in result.stdout


def test_smoke_fails_when_a_critical_readiness_check_is_unavailable(tmp_path):
    result = _run_readiness_smoke(tmp_path, "not_ready")

    assert result.returncode != 0
    assert "not_ready" in result.stdout
    assert "database" in result.stdout


def test_smoke_readiness_probe_is_unauthenticated(tmp_path):
    """/ready is on the public allowlist; the probe must not require a token."""
    script = SCRIPT.read_text(encoding="utf-8")
    readiness_block = script.split("API readiness")[0].rsplit("_smoke_readiness()", 1)[-1]

    assert "Authorization" not in readiness_block


def _launcher_roots(script: Path, *, env=None) -> dict[str, str]:
    """Evaluate only the variable prologue: sourcing the whole launcher would
    run its command dispatch."""
    lines = script.read_text(encoding="utf-8").splitlines()
    cut = next(index for index, line in enumerate(lines) if re.match(r"^[a-z_]+\(\)\s*\{", line))
    # Written beside the launcher so BASH_SOURCE resolves to the same directory.
    probe_file = script.parent / ".roots-probe.sh"
    probe_file.write_text(
        "\n".join(lines[:cut])
        + '\necho "DATA=$DATA_DIR"\necho "CODE=$BASE_DIR"\necho "EXPORTED=${ASTINUSLAB_DATA_DIR:-}"\n',
        encoding="utf-8",
    )
    probe = subprocess.run(["bash", str(probe_file)], env=launcher_env(env), capture_output=True, text=True)
    assert probe.returncode == 0, probe.stderr
    return dict(line.split("=", 1) for line in probe.stdout.strip().splitlines())


def test_launcher_resolves_mutable_paths_at_the_install_root_from_a_release(tmp_path):
    """The launcher ships inside the release, but storage, config and the TOML
    live at the stable install root. Resolving them from the script's own
    directory would look inside releases/<id>/ and find nothing."""
    install = tmp_path / "install"
    release = install / "releases/1.0.0-test00000000"
    release.mkdir(parents=True)
    (install / "current").symlink_to(release)

    roots = _launcher_roots(copy_launcher(release))

    assert roots["DATA"] == str(install)
    assert roots["CODE"] == str(release)


def test_launcher_keeps_one_root_in_a_development_checkout(tmp_path):
    roots = _launcher_roots(copy_launcher(tmp_path))

    assert roots["DATA"] == str(tmp_path)
    assert roots["CODE"] == str(tmp_path)


def test_launcher_explicit_data_root_overrides_inferred_root(tmp_path, monkeypatch):
    parent_root = tmp_path / "parent-data"
    fixture_root = tmp_path / "fixture-data"
    monkeypatch.setenv("ASTINUSLAB_DATA_DIR", str(parent_root))

    roots = _launcher_roots(copy_launcher(tmp_path), env={"ASTINUSLAB_DATA_DIR": str(fixture_root)})

    assert roots["DATA"] == roots["EXPORTED"] == str(fixture_root)
    assert roots["CODE"] == str(tmp_path)
    assert os.environ["ASTINUSLAB_DATA_DIR"] == str(parent_root)


def test_launcher_exports_the_data_root_to_every_child(tmp_path):
    """The application resolves its own data root from ASTINUSLAB_DATA_DIR. A
    child python started from the release without it would look for the
    database and catalogs inside releases/<id>/."""
    install = tmp_path / "install"
    release = install / "releases/1.0.0-test00000000"
    release.mkdir(parents=True)

    roots = _launcher_roots(copy_launcher(release))

    assert roots["EXPORTED"] == str(install)


def seed_setup_checkout(tmp_path: Path) -> None:
    (tmp_path / "config/catalog").mkdir(parents=True, exist_ok=True)
    (tmp_path / "astinuslab.toml.example").write_text("[app]\n", encoding="utf-8")
    (tmp_path / "config/astinuslab.env.example").write_text("ASTINUSLAB_API_PORT=8000\n", encoding="utf-8")
    for name in ["targets", "workloads", "scenarios", "attackers", "profiles", "network_nodes"]:
        (tmp_path / f"config/catalog/{name}.example.yaml").write_text("{}\n", encoding="utf-8")
    (tmp_path / "requirements-dev.lock").write_text("# dev lock\n", encoding="utf-8")
    (tmp_path / "requirements.lock").write_text("# prod lock\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'astinuslab'\n", encoding="utf-8")


def setup_mock_python3(bin_dir: Path, mock_log: Path) -> dict[str, str]:
    bin_dir.mkdir(parents=True, exist_ok=True)
    # Keep setup tests independent of installed build tools and active services.
    for command, body in (("pkg-config", "exit 0\n"), ("cc", "exit 0\n"), ("systemctl", "exit 1\n")):
        write_helper(bin_dir, command, body)
    python3 = bin_dir / "python3"
    python3.write_text(
        f"""#!/usr/bin/env bash
if [[ "${{1:-}}" == "-c" ]]; then
  exit 0
fi
if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "venv" ]]; then
  target_venv="${{3}}"
  mkdir -p "$target_venv/bin"
  cat <<'EOF' > "$target_venv/bin/python"
#!/usr/bin/env bash
echo "python: $*" >> "{mock_log}"
if [[ "$*" == *"-r "*requirements-dev.lock* || "$*" == *"-r "*requirements.lock* ]]; then
  if [[ "${{FAIL_LOCK_INSTALL:-0}}" == "1" ]]; then
    echo "mock lock install failed" >&2
    exit 1
  fi
fi
exit 0
EOF
  chmod 0755 "$target_venv/bin/python"
  cat <<'EOF' > "$target_venv/bin/pip"
#!/usr/bin/env bash
echo "pip: $*" >> "{mock_log}"
exit 0
EOF
  chmod 0755 "$target_venv/bin/pip"
  exit 0
fi
exec /usr/bin/python3 "$@"
""",
        encoding="utf-8",
    )
    python3.chmod(0o755)

    return {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "ASTINUSLAB_RUNTIME_IDENTITIES_FILE": str(bin_dir / "absent-identities"),
    }


def test_setup_installs_dev_lock_before_editable_no_deps(tmp_path):
    launcher = copy_launcher(tmp_path)
    seed_setup_checkout(tmp_path)
    mock_log = tmp_path / "mock.log"
    env = setup_mock_python3(tmp_path / "bin", mock_log)

    result = run(launcher, "setup", env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    log_lines = mock_log.read_text(encoding="utf-8").splitlines()
    lock_expected = f"python: -m pip install -r {tmp_path}/requirements-dev.lock"
    editable_expected = f"python: -m pip install -e {tmp_path} --no-deps"
    assert lock_expected in log_lines, f"Missing {lock_expected!r} in {log_lines}"
    assert editable_expected in log_lines, f"Missing {editable_expected!r} in {log_lines}"
    assert log_lines.index(lock_expected) < log_lines.index(editable_expected)
    assert "[dev]" not in "\n".join(log_lines)


def test_setup_stops_on_dev_lock_install_failure(tmp_path):
    launcher = copy_launcher(tmp_path)
    seed_setup_checkout(tmp_path)
    mock_log = tmp_path / "mock.log"
    env = setup_mock_python3(tmp_path / "bin", mock_log)
    env["FAIL_LOCK_INSTALL"] = "1"

    result = run(launcher, "setup", env=env)

    assert result.returncode != 0
    log_lines = mock_log.read_text(encoding="utf-8").splitlines()
    lock_expected = f"python: -m pip install -r {tmp_path}/requirements-dev.lock"
    editable_expected = f"python: -m pip install -e {tmp_path} --no-deps"
    assert lock_expected in log_lines, f"Missing {lock_expected!r} in {log_lines}"
    assert editable_expected not in log_lines
    assert not (tmp_path / "storage/ssh_keys/astinuslab-default").exists()


def test_setup_prints_start_and_smoke_require_running(tmp_path):
    launcher = copy_launcher(tmp_path)
    seed_setup_checkout(tmp_path)
    mock_log = tmp_path / "mock.log"
    env = setup_mock_python3(tmp_path / "bin", mock_log)

    result = run(launcher, "setup", env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "./astinuslab.sh start && ./astinuslab.sh smoke --require-running" in result.stdout
    assert "./astinuslab.sh smoke && ./astinuslab.sh start" not in result.stdout


def test_backend_venv_helper_installs_prod_lock_before_editable_no_deps(tmp_path):
    repo = tmp_path / "repo"
    tools_dev = repo / "tools/dev"
    tools_dev.mkdir(parents=True)
    helper = tools_dev / "setup_backend_venv.sh"
    shutil.copy2(ROOT / "tools/dev/setup_backend_venv.sh", helper)
    helper.chmod(0o755)
    (repo / "requirements.lock").write_text("# prod lock\n", encoding="utf-8")
    mock_log = tmp_path / "mock.log"
    env = setup_mock_python3(tmp_path / "bin", mock_log)

    result = subprocess.run([str(helper)], cwd=repo, env={**os.environ, **env}, capture_output=True, text=True)

    assert result.returncode == 0, result.stdout + result.stderr
    log_lines = mock_log.read_text(encoding="utf-8").splitlines()
    lock_expected = f"python: -m pip install -r {repo}/requirements.lock"
    editable_expected = f"python: -m pip install -e {repo} --no-deps"
    assert lock_expected in log_lines, f"Missing {lock_expected!r} in {log_lines}"
    assert editable_expected in log_lines, f"Missing {editable_expected!r} in {log_lines}"
    assert log_lines.index(lock_expected) < log_lines.index(editable_expected)


def test_backend_venv_helper_stops_on_lock_install_failure(tmp_path):
    repo = tmp_path / "repo"
    tools_dev = repo / "tools/dev"
    tools_dev.mkdir(parents=True)
    helper = tools_dev / "setup_backend_venv.sh"
    shutil.copy2(ROOT / "tools/dev/setup_backend_venv.sh", helper)
    helper.chmod(0o755)
    (repo / "requirements.lock").write_text("# prod lock\n", encoding="utf-8")
    mock_log = tmp_path / "mock.log"
    env = setup_mock_python3(tmp_path / "bin", mock_log)
    env["FAIL_LOCK_INSTALL"] = "1"

    result = subprocess.run([str(helper)], cwd=repo, env={**os.environ, **env}, capture_output=True, text=True)

    assert result.returncode != 0
    log_lines = mock_log.read_text(encoding="utf-8").splitlines()
    lock_expected = f"python: -m pip install -r {repo}/requirements.lock"
    editable_expected = f"python: -m pip install -e {repo} --no-deps"
    assert lock_expected in log_lines, f"Expected {lock_expected!r} in {log_lines}"
    assert editable_expected not in log_lines
