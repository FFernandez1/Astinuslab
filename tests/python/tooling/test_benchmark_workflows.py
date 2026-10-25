"""Runner refusal, isolation and owned-child cleanup (no benchmark repetitions)."""

from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("benchmark_workflows", ROOT / "tools/benchmarks/benchmark_workflows.py")
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)


def host():
    return {"disk_available_bytes": 10**12, "memory": {"MemAvailable_bytes": 8 * 1024**3}}


def args(tmp_path):
    return bench.parser().parse_args(["--output", str(tmp_path / "new")])


@pytest.mark.parametrize(
    ("name", "value"),
    [("repetitions", 4), ("repetitions", True), ("rows", -1), ("depth", 33), ("dump_mib", 33), ("seconds", 31)],
)
def test_oversized_request_is_refused_before_output_or_worker(tmp_path, monkeypatch, name, value):
    requested = args(tmp_path)
    setattr(requested, name, value)
    monkeypatch.setattr(bench, "ResourceGroup", lambda *_: pytest.fail("must not create resource group"))
    with pytest.raises(bench.BoundaryError):
        bench.validate_workload(requested, host())
    assert not requested.output.exists()


def test_total_budget_and_available_resources_are_checked(tmp_path):
    requested = args(tmp_path)
    requested.repetitions = 3
    with pytest.raises(bench.BoundaryError, match="estimated output"):
        bench.validate_workload(requested, host())
    requested.repetitions = 1
    low_disk = host()
    low_disk["disk_available_bytes"] = 10
    with pytest.raises(bench.BoundaryError, match="storage"):
        bench.validate_workload(requested, low_disk)
    low_ram = host()
    low_ram["memory"]["MemAvailable_bytes"] = 10
    with pytest.raises(bench.BoundaryError, match="RAM"):
        bench.validate_workload(requested, low_ram)


def test_existing_and_symlink_outputs_are_never_overwritten(tmp_path):
    old = tmp_path / "old"
    old.mkdir()
    (old / "sentinel").write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        bench.create_output(old)
    link = tmp_path / "link"
    link.symlink_to(old)
    with pytest.raises(FileExistsError):
        bench.create_output(link)
    with pytest.raises(bench.BoundaryError):
        bench.create_output(link / "nested")
    assert (old / "sentinel").read_bytes() == b"keep"
    file_link = tmp_path / "result.json"
    file_link.symlink_to(old / "sentinel")
    with pytest.raises(FileExistsError):
        bench.write_json(file_link, {"overwrite": True})
    assert (old / "sentinel").read_bytes() == b"keep"


def test_environment_excludes_installed_data_and_uses_worktree_first(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTINUSLAB_DB_PATH", "/opt/astinuslab/storage/private.db")
    monkeypatch.setenv("ASTINUSLAB_VOL3_BINARY", "/operator/tool")
    monkeypatch.setenv("ASTINUSLAB_REAL_HOST", "1")
    monkeypatch.setenv("PYTHONPATH", "/wrong/checkout")
    monkeypatch.setenv("NODE_OPTIONS", "--require /unrelated/script")
    env = bench.isolated_environment(tmp_path, sys.executable, None)
    assert env["ASTINUSLAB_DB_PATH"] == str(tmp_path / "app.db")
    assert "ASTINUSLAB_VOL3_BINARY" not in env and "NODE_OPTIONS" not in env
    assert env["ASTINUSLAB_REAL_HOST"] == "0"
    assert env["ASTINUSLAB_CELERY_RECONCILIATION_ENABLED"] == "false"
    assert env["PYTHONPATH"].split(os.pathsep)[:2] == [str(ROOT / "src"), str(ROOT)]
    assert env["PYTHONPATH"].split(os.pathsep)[-1] == "/usr/lib/python3/dist-packages"


def test_resource_controls_bind_to_owned_descriptor_and_refuse_replaced_path(tmp_path):
    owned = tmp_path / "owned"
    owned.mkdir()
    (owned / "memory.max").write_text("")
    group = object.__new__(bench.ResourceGroup)
    group.path = owned
    group.fd = os.open(owned, os.O_RDONLY | os.O_DIRECTORY)
    info = os.fstat(group.fd)
    group.identity = (info.st_dev, info.st_ino)
    moved = tmp_path / "moved"
    owned.rename(moved)
    owned.mkdir()
    (owned / "memory.max").write_text("untouched")
    group.write("memory.max", "512")
    assert (moved / "memory.max").read_text() == "512"
    assert (owned / "memory.max").read_text() == "untouched"
    group.read = lambda name: "" if name == "cgroup.procs" else "populated 0"
    with pytest.raises(bench.BoundaryError, match="changed identity"):
        group.close()
    assert owned.exists() and moved.exists()


def test_child_attachment_uses_only_self_and_never_supervisor_pid(monkeypatch):
    group = object.__new__(bench.ResourceGroup)
    writes, limits = [], []
    group.write = lambda name, value: writes.append((name, value))
    monkeypatch.setattr(bench.resource, "setrlimit", lambda key, value: limits.append((key, value)))
    group.child_limits(30)
    assert writes == [("cgroup.procs", "0")]
    assert len(limits) == 2


@pytest.mark.parametrize("scenario", ["timeout", "output", "failure", "cleanup"])
def test_supervisor_failure_kills_only_owned_child_and_records_failure(tmp_path, monkeypatch, scenario):
    """Actual tiny children; cgroup counters/attachment explicitly simulated."""
    case = tmp_path / "case"
    case.mkdir()
    bench.write_json(case / "input.json", {})
    processes = []

    class FakeGroup:
        def __init__(self, _):
            self.path = tmp_path / "fake-group"
            self.fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)

        def child_limits(self, _):
            pass

        def rss_bytes(self):
            return 4096

        def read(self, name):
            return "8192" if name == "memory.peak" else "oom 0\noom_kill 0\n"

        def kill(self):
            for process in processes:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)

        def close(self):
            os.close(self.fd)
            if scenario == "cleanup":
                raise bench.BoundaryError("simulated cleanup refusal")

    real_popen = subprocess.Popen
    sentinel = real_popen([sys.executable, "-c", "import time; time.sleep(10)"], start_new_session=True)

    def owned_popen(*arguments, **keywords):
        process = real_popen(*arguments, **keywords)
        processes.append(process)
        return process

    monkeypatch.setattr(bench, "ResourceGroup", FakeGroup)
    monkeypatch.setattr(bench.subprocess, "Popen", owned_popen)
    monkeypatch.setattr(bench, "LOG_LIMIT", 1024)
    code = {
        "timeout": "import time; time.sleep(10)",
        "output": "print('x'*8192)",
        "failure": "raise SystemExit(7)",
        "cleanup": "raise SystemExit(2)",
    }[scenario]
    try:
        result = bench.supervise(
            [sys.executable, "-c", code],
            {**os.environ, "PYTHONPATH": ""},
            case,
            tmp_path,
            seconds=1,
            memory_bytes=512 * bench.MIB,
        )
        assert result["status"] == "failed"
        assert result["process_tree_peak_rss_bytes"] == 4096
        assert result["cgroup_memory_peak_bytes"] == 8192
        assert sentinel.poll() is None
        assert processes and all(process.poll() is not None for process in processes)
        assert (case / "worker.log").stat().st_size <= 1024
        if scenario == "cleanup":
            assert "cleanup_error" in result
    finally:
        sentinel.kill()
        sentinel.wait(timeout=2)


def test_unavailable_cgroup_launches_no_workload(tmp_path, monkeypatch):
    case = tmp_path / "case"
    case.mkdir()

    def refuse(_):
        raise PermissionError("no delegated resource controls")

    monkeypatch.setattr(bench, "ResourceGroup", refuse)
    monkeypatch.setattr(bench.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("worker must not start"))
    with pytest.raises(PermissionError):
        bench.supervise(["unused"], {"PYTHONPATH": ""}, case, tmp_path, seconds=1, memory_bytes=512 * bench.MIB)
    assert list(case.iterdir()) == []


def test_standalone_worker_cannot_bypass_resource_preflight(tmp_path):
    """A caller-controlled JSON is not permission to enter the internal worker."""
    case = tmp_path / "hash-1"
    case.mkdir()
    bench.write_json(case / "input.json", {"scenario": "hash", "dump_mib": 999999999})
    env = {key: value for key, value in os.environ.items() if not key.startswith("ASTINUSLAB_BENCH_")}
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/benchmarks/benchmark_workflows.py"), "--worker", str(case)],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode != 0
    assert "requires supervisor handoff" in result.stderr
    assert sorted(path.name for path in case.iterdir()) == ["input.json"]


def test_worker_refuses_current_unbounded_group_even_with_private_handoff(tmp_path, monkeypatch):
    case = tmp_path / "hash-1"
    case.mkdir(mode=0o700)
    tmp_path.chmod(0o700)
    bench.write_json(case / "input.json", {"supervisor_pid": os.getppid()})
    descriptor = os.open(case / "input.json", os.O_RDONLY)
    group_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        monkeypatch.setenv("ASTINUSLAB_BENCH_HANDOFF_FD", str(descriptor))
        monkeypatch.setenv("ASTINUSLAB_BENCH_GROUP_FD", str(group_fd))
        with pytest.raises(bench.BoundaryError, match="private benchmark cgroup"):
            bench.worker_handoff(case)
        assert sorted(path.name for path in case.iterdir()) == ["input.json"]
    finally:
        os.close(descriptor)
        os.close(group_fd)
