#!/usr/bin/env python3
"""Bounded synthetic workflow measurements; never runs a VM or forensic tool.

The stdlib supervisor creates a new private cgroup before any scenario imports
AstinusLab. Existing processes/groups/controllers are never changed.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import resource
import selectors
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIB = 1024 * 1024
SCENARIOS = ("hash", "results", "history", "cancel", "ui")
OUTPUT_LIMIT = 64 * MIB
LOG_LIMIT = MIB


class BoundaryError(RuntimeError):
    """No work may start without the requested safety boundary."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise BoundaryError(message)


def write_json(path: Path, value: object) -> None:
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)


def tree_bytes(root: Path) -> int:
    total = count = 0
    for current, directories, files in os.walk(root, followlinks=False):
        if len(Path(current).relative_to(root).parts) > 32:
            raise BoundaryError("output tree exceeds depth limit")
        for name in (*directories, *files):
            path = Path(current) / name
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue  # browser scratch can disappear during sampling
            count += 1
            if count > 20000:
                raise BoundaryError("output tree exceeds file limit")
            # Chromium may create scratch symlinks. Count the links themselves,
            # never follow them or attribute unrelated target bytes to this task.
            if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                total += info.st_size
    return total


def environment_info(root: Path) -> dict:
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        name, value = line.split(":", 1)
        if name in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
            memory[name + "_bytes"] = int(value.split()[0]) * 1024
    model = next(
        (
            line.split(":", 1)[1].strip()
            for line in Path("/proc/cpuinfo").read_text().splitlines()
            if line.startswith("model name")
        ),
        "unknown",
    )

    def git(*args):
        return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()

    versions = {}
    for name in ("fastapi", "sqlalchemy", "celery", "uvicorn", "pydantic"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return {
        "platform": platform.platform(),
        "cpu_model": model,
        "logical_cpus": os.cpu_count(),
        "available_cpu_affinity": sorted(os.sched_getaffinity(0)),
        "memory": memory,
        "disk_available_bytes": shutil.disk_usage(root).free,
        "git_revision": git("rev-parse", "HEAD"),
        "git_status": git("status", "--porcelain"),
        "python": sys.version,
        "executable": sys.executable,
        "packages": versions,
        "loadavg": Path("/proc/loadavg").read_text().strip(),
        "pressure": {name: Path("/proc/pressure", name).read_text().strip() for name in ("cpu", "memory", "io")},
        "benchmark_code_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((ROOT / "tools/benchmarks").glob("*"))
            if path.is_file()
        },
    }


def validate_workload(args: argparse.Namespace, host: dict) -> int:
    bounds = {
        "repetitions": (1, 3),
        "dump_mib": (1, 32),
        "rows": (1, 10000),
        "depth": (1, 32),
        "item_kib": (1, 1024),
        "retained": (2, 1000),
        "seconds": (1, 30),
    }
    for name, (low, high) in bounds.items():
        value = getattr(args, name)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise BoundaryError(f"{name} must be an integer in [{low}, {high}]")
    selected = args.scenario or list(SCENARIOS)
    if len(set(selected)) != len(selected) or any(value not in SCENARIOS for value in selected):
        raise BoundaryError("scenarios must be distinct supported names")
    estimate = (
        sum(
            {
                "hash": args.dump_mib * MIB,
                "results": args.rows * 160 + args.depth * 128 + args.item_kib * 1024,
                "history": args.retained * (16384 + 1024) + 4 * MIB,
                "cancel": MIB,
                "ui": 20 * MIB,
            }[name]
            + LOG_LIMIT
            for name in selected
        )
        * args.repetitions
    )
    if estimate > OUTPUT_LIMIT:
        raise BoundaryError(f"estimated output {estimate} exceeds {OUTPUT_LIMIT}-byte task budget")
    if host["disk_available_bytes"] < estimate + OUTPUT_LIMIT:
        raise BoundaryError("insufficient available storage for workload plus safety margin")
    ceiling = (1024 if "ui" in selected else 512) * MIB
    if host["memory"]["MemAvailable_bytes"] < ceiling + 256 * MIB:
        raise BoundaryError("insufficient available RAM for the enforced ceiling plus supervisor margin")
    return estimate


def create_output(path: Path) -> Path:
    absolute = path.absolute()
    if ".." in absolute.parts or absolute.parent.resolve() != absolute.parent:
        raise BoundaryError("output parent must be canonical and contain no symlinks")
    # Exclusive mkdir also refuses an existing/symlink destination.
    absolute.mkdir(mode=0o700)
    return absolute


def isolated_environment(data: Path, python: str, node_modules: Path | None) -> dict[str, str]:
    paths = json.loads(
        subprocess.check_output(
            [
                python,
                "-I",
                "-c",
                "import json,sysconfig; print(json.dumps([sysconfig.get_path('purelib'),sysconfig.get_path('platlib')]))",
            ],
            text=True,
        )
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("ASTINUSLAB_") and key not in {"PYTHONPATH", "PYTHONHOME", "NODE_OPTIONS", "NODE_PATH"}
    }
    env["PYTHONPATH"] = os.pathsep.join(
        dict.fromkeys([str(ROOT / "src"), str(ROOT), *paths, "/usr/lib/python3/dist-packages"])
    )
    env.update(
        ASTINUSLAB_DATA_DIR=str(data),
        ASTINUSLAB_DB_PATH=str(data / "app.db"),
        ASTINUSLAB_STORAGE_DIR=str(data / "storage"),
        ASTINUSLAB_LIBVIRT_IMAGE_BASE_DIR=str(data / "images"),
        ASTINUSLAB_PLAYGROUND_DISK_DIR=str(data / "disks"),
        ASTINUSLAB_PLAYGROUND_CAPTURE_DIR=str(data / "captures"),
        ASTINUSLAB_FAKE_MODE="1",
        ASTINUSLAB_FAKE_LIBVIRT="1",
        ASTINUSLAB_FAKE_PLAYGROUND="1",
        ASTINUSLAB_REAL_HOST="0",
        ASTINUSLAB_VOL3_ENABLED="false",
        ASTINUSLAB_CELERY_RECONCILIATION_ENABLED="false",
        TMPDIR=str(data / "tmp"),
    )
    for name in ("targets", "scenarios", "workloads", "attackers", "network_nodes"):
        env[f"ASTINUSLAB_{name.upper()}_CATALOG_PATH"] = str(ROOT / f"config/catalog/{name}.example.yaml")
    if node_modules is not None:
        env["NODE_PATH"] = str(node_modules.resolve())
    return env


class ResourceGroup:
    """An exclusively created cgroup. All control writes use its held directory fd."""

    def __init__(self, memory_bytes: int):
        membership = Path("/proc/self/cgroup").read_text().strip().splitlines()
        entry = next((line.split(":", 2)[2] for line in membership if line.startswith("0::")), None)
        if entry is None or ".." in Path(entry).parts:
            raise BoundaryError("cgroup v2 membership unavailable")
        current = Path("/sys/fs/cgroup") / entry.lstrip("/")
        parent = current.parent
        if not {"memory", "pids"} <= set((parent / "cgroup.subtree_control").read_text().split()):
            raise BoundaryError("existing parent does not already enable memory+pids controllers")
        if not os.access(parent, os.W_OK) or not os.access(parent / "cgroup.procs", os.W_OK):
            raise BoundaryError(f"no delegated sibling cgroup creation/attachment access: {parent}")
        self.path = parent / ("astinuslab-benchmark-" + uuid.uuid4().hex)
        self.path.mkdir(mode=0o700)
        self.fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        self.identity = (os.fstat(self.fd).st_dev, os.fstat(self.fd).st_ino)
        try:
            self.write("memory.max", str(memory_bytes))
            self.write("memory.swap.max", "0")
            self.write("pids.max", "256")
            if self.read("memory.max").strip() != str(memory_bytes) or self.read("memory.swap.max").strip() != "0":
                raise BoundaryError("kernel did not retain requested cgroup limits")
            require(self.read("pids.max").strip() == "256", "kernel did not retain process limit")
            for name in ("cgroup.kill", "cgroup.procs"):
                fd = os.open(name, os.O_WRONLY | os.O_NOFOLLOW, dir_fd=self.fd)
                os.close(fd)
            for name in ("cgroup.kill", "cgroup.procs", "memory.peak", "memory.events"):
                if not (self.path / name).exists():
                    raise BoundaryError(f"required cgroup control unavailable: {name}")
        except BaseException:
            self.close()
            raise

    def write(self, name: str, value: str) -> None:
        fd = os.open(name, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.fd)
        try:
            os.write(fd, value.encode())
        finally:
            os.close(fd)

    def read(self, name: str) -> str:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.fd)
        with os.fdopen(fd) as stream:
            return stream.read(65536)

    def child_limits(self, seconds: int) -> None:
        # Called only in the newly forked scenario child before exec/imports.
        self.write("cgroup.procs", "0")
        resource.setrlimit(resource.RLIMIT_FSIZE, (OUTPUT_LIMIT, OUTPUT_LIMIT))
        resource.setrlimit(resource.RLIMIT_CPU, (seconds + 1, seconds + 1))

    def rss_bytes(self) -> int:
        total = 0
        for value in self.read("cgroup.procs").split():
            try:
                fields = Path(f"/proc/{int(value)}/statm").read_text().split()
                total += int(fields[1]) * os.sysconf("SC_PAGE_SIZE")
            except (FileNotFoundError, ProcessLookupError):
                pass
        return total

    def kill(self) -> None:
        self.write("cgroup.kill", "1")

    def close(self) -> None:
        try:
            if self.read("cgroup.procs").strip():
                self.kill()
            deadline = time.monotonic() + 2
            while "populated 1" in self.read("cgroup.events"):
                if time.monotonic() > deadline:
                    raise BoundaryError(f"task cgroup remains populated; retained for inspection: {self.path}")
                time.sleep(0.01)
            info = self.path.lstat()
            if (info.st_dev, info.st_ino) != self.identity:
                raise BoundaryError("task cgroup path changed identity; refusing removal")
            self.path.rmdir()
        finally:
            os.close(self.fd)


def supervise(
    command: list[str], env: dict[str, str], case: Path, output: Path, *, seconds: int, memory_bytes: int
) -> dict:
    started = time.monotonic()
    result = {
        "command": command,
        "cwd": str(ROOT),
        "pythonpath": env["PYTHONPATH"],
        "limits": {
            "wall_seconds": seconds,
            "cgroup_memory_bytes": memory_bytes,
            "pids": 256,
            "task_output_bytes": OUTPUT_LIMIT,
        },
        "status": "failed",
    }
    group = ResourceGroup(memory_bytes)  # no scenario starts if enforcement is absent
    process = None
    handoff_fd = None
    peak_rss = count = 0
    reason = None
    selector = selectors.DefaultSelector()
    try:
        handoff_fd = os.open(case / "input.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        child_env = {**env, "ASTINUSLAB_BENCH_HANDOFF_FD": str(handoff_fd), "ASTINUSLAB_BENCH_GROUP_FD": str(group.fd)}
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            preexec_fn=lambda: group.child_limits(seconds),
            pass_fds=(group.fd, handoff_fd),
        )
        os.close(handoff_fd)
        handoff_fd = None
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        with (case / "worker.log").open("xb") as log:
            while True:
                for key, _ in selector.select(timeout=0.025):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        room = max(0, LOG_LIMIT - count)
                        log.write(chunk[:room])
                        count += len(chunk)
                peak_rss = max(peak_rss, group.rss_bytes())
                if count > LOG_LIMIT:
                    reason = "worker diagnostic output ceiling exceeded"
                elif time.monotonic() - started > seconds:
                    reason = "scenario wall deadline exceeded"
                elif tree_bytes(output) > OUTPUT_LIMIT:
                    reason = "aggregate task output watchdog exceeded"
                if reason:
                    group.kill()
                    break
                if process.poll() is not None and not selector.get_map():
                    break
        process.wait(timeout=2)
        result.update(
            exit_code=process.returncode,
            process_tree_peak_rss_bytes=peak_rss,
            cgroup_memory_peak_bytes=int(group.read("memory.peak").strip()),
            cgroup_memory_events=group.read("memory.events"),
            cgroup_path=str(group.path),
            captured_log_bytes=min(count, LOG_LIMIT),
            log_truncated=count > LOG_LIMIT,
        )
        result_file = case / "outcome.json"
        if process.returncode == 0 and not reason and result_file.is_file() and result_file.stat().st_size < LOG_LIMIT:
            result["measurement"] = json.loads(result_file.read_text())
            result["status"] = "passed"
        else:
            result["error"] = reason or "scenario worker failed; see worker.log"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if handoff_fd is not None:
            os.close(handoff_fd)
        selector.close()
        try:
            # Keep failure metrics too, before deleting the kernel accounting.
            result.update(
                process_tree_peak_rss_bytes=peak_rss,
                cgroup_memory_peak_bytes=int(group.read("memory.peak").strip()),
                cgroup_memory_events=group.read("memory.events"),
                cgroup_path=str(group.path),
            )
            group.kill()  # only this new group, including any orphaned task children
            if process is not None:
                process.wait(timeout=2)
                process.stdout.close()
        except Exception as exc:
            result.update(status="failed", cleanup_error=str(exc))
        finally:
            try:
                group.close()
            except Exception as exc:
                result.update(status="failed", cleanup_error=str(exc))
        result["supervised_wall_seconds"] = time.monotonic() - started
        result["case_storage_bytes"] = tree_bytes(case)
    return result


def hash_scenario(case: Path, config: dict) -> dict:
    from astinuslab.forensics.vol3_support import _sha256_file

    path = case / "synthetic.dump"
    block = b"AstinusLab synthetic I/O only\n" * 32768
    size = config["dump_mib"] * MIB
    independent = hashlib.sha256()
    with path.open("xb") as stream:
        remaining = size
        while remaining:
            data = block[: min(remaining, len(block))]
            stream.write(data)
            independent.update(data)
            remaining -= len(data)
    before = path.stat()
    begin = time.perf_counter()
    digest = _sha256_file(path)
    elapsed = time.perf_counter() - begin
    after = path.stat()
    require(
        digest == independent.hexdigest()
        and (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        == (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns),
        "synthetic hash/source integrity mismatch",
    )
    return {
        "kind": "synthetic sequential hashing, not forensic throughput",
        "operation_seconds": elapsed,
        "input_bytes": size,
        "sha256": digest,
        "bytes_per_second": size / elapsed,
    }


def results_scenario(case: Path, config: dict) -> dict:
    from astinuslab.forensics.vol3_plugin_run import _parse_json_rows_file

    rows = [{"PID": index, "COMM": f"synthetic-{index}"} for index in range(config["rows"])]
    rows[0]["oversized"] = "x" * (config["item_kib"] * 1024)
    cursor = rows[0]
    for depth in range(1, config["depth"] + 1):
        child = {"PID": -depth, "COMM": "nested"}
        cursor["__children"] = [child]
        cursor = child
    path = case / "synthetic-results.json"
    write_json(path, rows)
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    begin = time.perf_counter()
    columns, retained, count, truncated = _parse_json_rows_file(path, max_rows=config["rows"])
    elapsed = time.perf_counter() - begin
    require(columns[0] == "Depth" and count == config["rows"] + config["depth"], "nested result shape/count mismatch")
    require(len(retained) == config["rows"] and truncated, "result row ceiling not respected")
    require(hashlib.sha256(path.read_bytes()).hexdigest() == expected, "result input changed")
    return {
        "kind": "synthetic oversized/nested Volatility JSON parsing",
        "operation_seconds": elapsed,
        "input_bytes": path.stat().st_size,
        "sha256": expected,
        "top_level_rows": config["rows"],
        "nested_depth": config["depth"],
        "oversized_item_bytes": config["item_kib"] * 1024,
        "total_rows": count,
        "retained_rows": len(retained),
        "truncated": truncated,
    }


def history_scenario(case: Path, config: dict) -> dict:
    from astinuslab.core.config import settings
    from astinuslab.db import initialize_database
    from astinuslab.forensics.api import _recent_runs

    initialize_database()
    runs = settings.storage_dir / "dumps" / "synthetic-job" / "vol3" / "runs"
    runs.mkdir(parents=True)
    digests = {}
    for index in range(config["retained"]):
        identifier = f"synthetic-{index:06d}"
        header = {
            "run_id": identifier,
            "job_id": "synthetic-job",
            "plugin": "linux.pslist.PsList",
            "status": "completed",
            "finished_at": (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=index)).isoformat(),
        }
        for suffix, value in ((".json", {**header, "rows": [[index, "x" * 16384]]}), (".meta.json", header)):
            path = runs / (identifier + suffix)
            write_json(path, value)
            digests[path] = hashlib.sha256(path.read_bytes()).hexdigest()
    hidden = runs / ".history-hidden"
    hidden.mkdir()
    (hidden / "synthetic-000000").touch()
    begin = time.perf_counter()
    recent = _recent_runs(limit=config["retained"])
    elapsed = time.perf_counter() - begin
    expected = [f"synthetic-{index:06d}" for index in range(config["retained"] - 1, 0, -1)]
    require([row["run_id"] for row in recent] == expected, "history count/order/hiding mismatch")
    require(
        all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in digests.items()),
        "retained history bytes changed",
    )
    return {
        "kind": "synthetic sidecar history with real disposable schema; no owned Job/action rows",
        "operation_seconds": elapsed,
        "retained_runs": config["retained"],
        "hidden_runs": 1,
        "visible_runs": len(recent),
        "source_bytes_unchanged": True,
        "source_bytes": sum(path.stat().st_size for path in digests),
    }


def cancel_scenario(case: Path, config: dict) -> dict:
    from astinuslab.core.config import settings
    from astinuslab.jobs.job_control import kill_job_subprocesses
    from astinuslab.virtualization.process_support import job_pid_dir, register_job_pid

    children = []
    try:
        for _ in range(2):
            children.append(
                subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
            )
        target, sentinel = children
        job = "benchmark-" + uuid.uuid4().hex
        register_job_pid(settings.storage_dir, job, target.pid)
        begin = time.perf_counter()
        kill_job_subprocesses(job)
        target.wait(timeout=2)
        elapsed = time.perf_counter() - begin
        require(
            target.returncode == -signal.SIGKILL and sentinel.poll() is None,
            "cancellation crossed process ownership or failed",
        )
        require(not job_pid_dir(settings.storage_dir, job).exists(), "cancellation registry survived")
        return {
            "kind": "synthetic registered subprocess cancellation, not VM/capture control",
            "operation_seconds": elapsed,
            "call_to_exit_ms": elapsed * 1000,
            "target_pid": target.pid,
            "target_exit_code": target.returncode,
            "unregistered_sentinel_survived": True,
            "registry_removed": True,
        }
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)


def ui_scenario(case: Path, config: dict) -> dict:
    from astinuslab.db import initialize_database

    initialize_database()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = None
    try:
        with (case / "uvicorn.log").open("xb") as log:
            server = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "astinuslab.main:app",
                    "--fd",
                    str(listener.fileno()),
                    "--no-access-log",
                ],
                pass_fds=(listener.fileno(),),
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            listener.close()
            import urllib.request

            deadline = time.monotonic() + 10
            while True:
                if server.poll() is not None:
                    raise BoundaryError("isolated UI server exited before readiness")
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.2) as response:
                        if response.status == 200:
                            break
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise BoundaryError("isolated UI server readiness deadline exceeded")
                time.sleep(0.05)
            completed = subprocess.run(
                ["node", str(ROOT / "tools/benchmarks/workbench.cjs"), str(port), str(config["rows"])],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if completed.returncode:
                raise BoundaryError("browser failed: " + completed.stderr[-4000:])
            return json.loads(completed.stdout)
    finally:
        listener.close()
        if server is not None:
            if server.poll() is None:
                server.terminate()
            try:
                server.wait(timeout=2)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=2)


WORKERS = {
    "hash": hash_scenario,
    "results": results_scenario,
    "history": history_scenario,
    "cancel": cancel_scenario,
    "ui": ui_scenario,
}


def worker_handoff(case: Path) -> dict:
    """Refuse standalone workers before app imports, subprocesses or file writes."""
    try:
        handoff_fd = int(os.environ["ASTINUSLAB_BENCH_HANDOFF_FD"])
        group_fd = int(os.environ["ASTINUSLAB_BENCH_GROUP_FD"])
    except (KeyError, ValueError) as exc:
        raise BoundaryError("internal worker requires supervisor handoff and enforced cgroup") from exc
    info = os.fstat(handoff_fd)
    require(
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and info.st_uid == os.getuid()
        and not stat.S_IMODE(info.st_mode) & 0o077
        and info.st_size <= 16384,
        "unsafe worker handoff file",
    )
    require(fcntl.fcntl(handoff_fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY, "worker handoff must be read-only")
    require(case.is_absolute() and case.resolve() == case, "unsafe worker case path")
    for directory in (case, case.parent):
        owner = directory.lstat()
        require(
            stat.S_ISDIR(owner.st_mode) and owner.st_uid == os.getuid() and not stat.S_IMODE(owner.st_mode) & 0o077,
            "worker directories must be private",
        )
    named = (case / "input.json").lstat()
    require((info.st_dev, info.st_ino) == (named.st_dev, named.st_ino), "worker handoff path changed")
    config = json.loads(os.pread(handoff_fd, 16384, 0))
    require(config.get("supervisor_pid") == os.getppid(), "worker is not a child of its supervisor")
    membership = next(
        (
            line.split(":", 2)[2]
            for line in Path("/proc/self/cgroup").read_text().splitlines()
            if line.startswith("0::")
        ),
        "",
    )
    current = Path("/sys/fs/cgroup") / membership.lstrip("/")
    require(
        bool(re.fullmatch(r"astinuslab-benchmark-[a-f0-9]{32}", current.name)),
        "worker is not inside a private benchmark cgroup",
    )
    current_info = current.lstat()
    held = os.fstat(group_fd)
    require(
        (held.st_dev, held.st_ino) == (current_info.st_dev, current_info.st_ino),
        "worker cgroup differs from supervisor handoff",
    )

    def control(name):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=group_fd)
        with os.fdopen(descriptor) as stream:
            return stream.read(64).strip()

    scenario = config.get("scenario")
    require(scenario in SCENARIOS, "unsupported worker scenario")
    memory = (1024 if scenario == "ui" else 512) * MIB
    require(
        control("memory.max") == str(memory) and control("memory.swap.max") == "0" and control("pids.max") == "256",
        "worker cgroup limits differ from required bounds",
    )
    require(0 <= resource.getrlimit(resource.RLIMIT_FSIZE)[0] <= OUTPUT_LIMIT, "worker file-size limit missing")
    memory_lines = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    available = {
        "disk_available_bytes": shutil.disk_usage(case).free,
        "memory": {"MemAvailable_bytes": int(memory_lines["MemAvailable"].split()[0]) * 1024},
    }
    requested = argparse.Namespace(
        **{key: config.get(key) for key in ("dump_mib", "rows", "depth", "item_kib", "retained", "seconds")},
        repetitions=1,
        scenario=[scenario],
    )
    validate_workload(requested, available)
    require(0 <= resource.getrlimit(resource.RLIMIT_CPU)[0] <= requested.seconds + 1, "worker CPU deadline missing")
    require(
        case.name == f"{scenario}-{config.get('repetition')}" and config.get("repetition") in (1, 2, 3),
        "worker case identity does not match workload",
    )
    data = case / "runtime"
    expected = {
        "ASTINUSLAB_DATA_DIR": str(data),
        "ASTINUSLAB_DB_PATH": str(data / "app.db"),
        "ASTINUSLAB_STORAGE_DIR": str(data / "storage"),
        "ASTINUSLAB_LIBVIRT_IMAGE_BASE_DIR": str(data / "images"),
        "ASTINUSLAB_PLAYGROUND_DISK_DIR": str(data / "disks"),
        "ASTINUSLAB_PLAYGROUND_CAPTURE_DIR": str(data / "captures"),
        "ASTINUSLAB_FAKE_LIBVIRT": "1",
        "ASTINUSLAB_FAKE_PLAYGROUND": "1",
        "ASTINUSLAB_REAL_HOST": "0",
        "ASTINUSLAB_VOL3_ENABLED": "false",
        "ASTINUSLAB_CELERY_RECONCILIATION_ENABLED": "false",
        "TMPDIR": str(data / "tmp"),
    }
    require(
        all(os.environ.get(key) == value for key, value in expected.items()),
        "worker environment escaped isolated paths/fake boundaries",
    )
    os.close(handoff_fd)
    os.close(group_fd)
    return config


def worker(case: Path) -> int:
    config = worker_handoff(case)
    import astinuslab

    actual_source = Path(astinuslab.__file__).resolve()
    if not actual_source.is_relative_to(ROOT / "src"):
        raise BoundaryError(f"benchmark imported a different checkout: {actual_source}")
    begin = time.perf_counter()
    result = WORKERS[config["scenario"]](case, config)
    result["worker_setup_and_operation_seconds"] = time.perf_counter() - begin
    result["source_module"] = str(actual_source)
    result["worker_executable"] = sys.executable
    result["worker_python_version"] = sys.version
    result["worker_package_versions"] = {
        name: importlib.metadata.version(name) for name in ("fastapi", "sqlalchemy", "celery", "uvicorn", "pydantic")
    }
    write_json(case / "outcome.json", result)
    return 0


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--output", type=Path, required=True, help="new exclusive output directory; parent must exist")
    command.add_argument(
        "--scenario",
        action="append",
        choices=SCENARIOS,
        help="default: all five; select only a failed case for a corrective attempt",
    )
    command.add_argument("--python", default=sys.executable)
    command.add_argument("--node-modules", type=Path)
    for name, default in (
        ("repetitions", 1),
        ("dump-mib", 8),
        ("rows", 1000),
        ("depth", 8),
        ("item-kib", 256),
        ("retained", 100),
        ("seconds", 30),
    ):
        command.add_argument("--" + name, type=int, default=default)
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    host = environment_info(args.output.absolute().parent)
    estimate = validate_workload(args, host)
    output = create_output(args.output)
    record = {
        "argv": [sys.executable, str(Path(__file__).resolve()), *(sys.argv[1:] if argv is None else argv)],
        "cwd": os.getcwd(),
        "started_at": datetime.now(UTC).isoformat(),
        "environment": host,
        "estimated_output_bytes": estimate,
        "output_budget_note": "64MiB conservative generation budget plus aggregate watchdog, not a filesystem quota",
        "rss_note": "sampled aggregate process-tree RSS; kernel cgroup memory.max also bounds charged cache/kernel memory",
        "forensic_throughput": {
            "status": "unverified",
            "missing": "operator-supplied valid memory capture, matching symbols, verified Volatility tool and independently known artifact",
        },
        "scenarios": [],
    }
    write_json(output / "preflight.json", record)
    for scenario in args.scenario or SCENARIOS:
        for repetition in range(1, args.repetitions + 1):
            case = output / f"{scenario}-{repetition}"
            case.mkdir(mode=0o700)
            data = case / "runtime"
            (data / "config/catalog").mkdir(parents=True)
            (data / "tmp").mkdir()
            shutil.copyfile(
                ROOT / "config/catalog/profiles.example.yaml", data / "config/catalog/profiles.example.yaml"
            )
            config = {name: getattr(args, name) for name in ("dump_mib", "rows", "depth", "item_kib", "retained")}
            config.update(scenario=scenario, repetition=repetition, seconds=args.seconds, supervisor_pid=os.getpid())
            write_json(case / "input.json", config)
            try:
                # Fresh available-resource validation before every case.
                validate_workload(args, environment_info(output))
                env = isolated_environment(data, args.python, args.node_modules)
                result = supervise(
                    [args.python, str(Path(__file__).resolve()), "--worker", str(case)],
                    env,
                    case,
                    output,
                    seconds=args.seconds,
                    memory_bytes=(1024 if scenario == "ui" else 512) * MIB,
                )
            except (OSError, BoundaryError, subprocess.SubprocessError) as exc:
                result = {"status": "blocked", "error": f"{type(exc).__name__}: {exc}", "measurement": None}
            result.update(scenario=scenario, repetition=repetition)
            record["scenarios"].append(result)
            write_json(case / "supervision.json", result)
            print(
                json.dumps({"scenario": scenario, "status": result["status"], "error": result.get("error")}), flush=True
            )
            if result["status"] == "blocked" or result.get("cleanup_error"):
                # Missing hard-enforcement prerequisite is shared; do not probe
                # more groups/accounts or launch the remaining workloads.
                break
        if record["scenarios"][-1]["status"] == "blocked" or record["scenarios"][-1].get("cleanup_error"):
            break
    record["completed_at"] = datetime.now(UTC).isoformat()
    record["storage_bytes_before_final_report"] = tree_bytes(output)
    record["status"] = (
        "passed" if all(row["status"] == "passed" for row in record["scenarios"]) else "failed_or_blocked"
    )
    write_json(output / "results.json", record)
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--worker":
            raise SystemExit(worker(Path(sys.argv[2])))
        raise SystemExit(main())
    except (OSError, BoundaryError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Benchmark refused/failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
