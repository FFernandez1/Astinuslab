"""Exercise the host helper with fake curl, virsh and sudo; never touch a VM."""

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def bake_env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    mock = bin_dir / "mock"
    mock.write_text("""#!/usr/bin/python3
import json, os, pathlib, sys
tool = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["BAKE_CALLS"], "a") as f: f.write(json.dumps([tool, args]) + "\\n")
if tool == "curl":
    url = next(a for a in args if a.startswith("http"))
    if "-c" in args:
        pathlib.Path(args[args.index("-c") + 1]).write_text("localhost\\tFALSE\\t/\\tFALSE\\t0\\tastinuslab_csrf\\tfixture-csrf\\n")
        sys.exit(0)
    expected = "Authorization: Bearer fixture-token" if os.environ["BAKE_AUTH"] == "1" else "X-CSRF-Token: fixture-csrf"
    if expected not in args: sys.exit(22)
    method = args[args.index("-X")+1] if "-X" in args else "GET"
    if os.environ.get("BAKE_FAIL") == method: sys.exit(22)
    if method == "DELETE":
        marker = pathlib.Path(os.environ["BAKE_CALLS"] + ".pending")
        if os.environ.get("BAKE_PENDING") and not marker.exists():
            marker.touch()
            print(json.dumps({"code":"cleanup_pending"}))
            sys.exit(22)
        sys.exit(0)
    print(json.dumps({"id":"fixture-job","status":"waiting_for_capture"}))
elif tool == "virsh":
    if "domstate" in args: print("shut off")
elif tool == "sudo" and os.environ.get("BAKE_FAIL") == "flatten": sys.exit(1)
""")
    mock.chmod(0o755)
    for name in ("curl", "virsh", "sudo", "sleep"):
        (bin_dir / name).symlink_to(mock)
    data = tmp_path / "data"
    data.mkdir()
    (data / "images/definitions").mkdir(parents=True)
    (data / "config").mkdir()
    (data / "config/astinuslab.env").write_text("ASTINUSLAB_API_PORT=18777\n")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "ASTINUSLAB_DATA_DIR": str(data),
        "ASTINUSLAB_LIBVIRT_IMAGE_BASE_DIR": str(tmp_path / "libvirt"),
        "BAKE_CALLS": str(tmp_path / "calls"),
        "PYTHONPATH": f"{ROOT}/src:{ROOT}",
        "BAKE_AUTH": "0",
    }
    env.pop("ASTINUSLAB_TOKEN", None)
    return env


def run_bake(env, output="new.qcow2"):
    return subprocess.run(
        ["bash", str(ROOT / "ops/host/bake_workload.sh"), "target", "workload", output],
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )


@pytest.mark.parametrize("auth", [False, True])
def test_bake_authentication_paths_and_data_root(bake_env, auth):
    if auth:
        bake_env.update(BAKE_AUTH="1", ASTINUSLAB_TOKEN="fixture-token")
    result = run_bake(bake_env)
    assert result.returncode == 0, result.stderr + result.stdout
    calls = [json.loads(line) for line in Path(bake_env["BAKE_CALLS"]).read_text().splitlines()]
    requests = [args for tool, args in calls if tool == "curl"]
    assert all("http://localhost:18777" in " ".join(args) for args in requests)
    flatten = next(args for tool, args in calls if tool == "sudo")
    assert flatten[-1] == str(Path(bake_env["ASTINUSLAB_DATA_DIR"]) / "images/definitions/new.qcow2")
    assert flatten[-2] == str(Path(bake_env["ASTINUSLAB_LIBVIRT_IMAGE_BASE_DIR"]) / "fixture-job/target-job.qcow2")
    assert "Baking complete" in result.stdout


@pytest.mark.parametrize("failure", ["POST", "GET", "DELETE", "flatten"])
def test_bake_failure_never_reports_success(bake_env, failure):
    bake_env["BAKE_FAIL"] = failure
    result = run_bake(bake_env)
    assert result.returncode != 0
    assert "Baking complete" not in result.stdout


def test_bake_rejects_destination_escape_before_any_mutation(bake_env):
    result = run_bake(bake_env, "../escape.qcow2")
    assert result.returncode != 0
    assert not Path(bake_env["BAKE_CALLS"]).exists()


def test_bake_waits_for_async_cleanup_before_success(bake_env):
    bake_env["BAKE_PENDING"] = "1"
    result = run_bake(bake_env)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in Path(bake_env["BAKE_CALLS"]).read_text().splitlines()]
    assert sum(tool == "curl" and "DELETE" in args for tool, args in calls) == 2
