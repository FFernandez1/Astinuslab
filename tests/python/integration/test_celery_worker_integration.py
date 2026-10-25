"""Real Celery worker subprocess integration: control_runtime end to end.

Runs the production ``astinuslab.jobs.celery_main`` worker in a solo-pool
subprocess against temporary SQLite broker/result/app databases and dispatches
the production ``control_runtime`` task by name. The seeded action asks for
``pause``, which ``runtime_control.run_control_runtime_action`` rejects with
RuntimeError before any host command, so the rejection path is exercised
without touching a host or VM.

Covered: task execution in a separate process, JSON argument and payload
serialization, the child's visibility of DB rows committed by the parent side,
the persisted running -> failed transition with error_summary, and failure
propagation back through the result backend.

Not covered here (deliberately out of scope for this bounded test): broker
queue-depth analysis and redelivery/ack semantics.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from celery import Celery

pytestmark = pytest.mark.slow

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = REPO_ROOT / "src"

WORKER_READY_TIMEOUT_S = 60.0
RESULT_TIMEOUT_S = 20

# Seeds one job plus its queued control_runtime action through the production
# repositories, then prints the ids past the fake-mode banner.
SEED_SNIPPET = """
import json
from astinuslab.api.schemas import JobCreate
from astinuslab.db import session_scope
from astinuslab.db.repositories import JobRepository

with session_scope() as session:
    repo = JobRepository(session)
    job = repo.create_job(
        JobCreate(target_id="target-int", scenario_id="scenario-int", execution_mode="immediate")
    )
    action = repo.enqueue_action(job.id, "control_runtime", {"role": "target", "action": "pause"})
    ids = {"job_id": job.id, "action_id": action.id}
print("SEEDED " + json.dumps(ids), flush=True)
"""


def _child_env(data_dir: Path, db_path: Path, broker_url: str, backend_url: str) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    env.update(
        PYTHONPATH=str(SRC_DIR),
        ASTINUSLAB_DATA_DIR=str(data_dir),
        ASTINUSLAB_STORAGE_DIR=str(data_dir / "storage"),
        ASTINUSLAB_SERIAL_LOG_DIR=str(data_dir / "storage" / "logs" / "serial"),
        ASTINUSLAB_PLAYGROUND_DISK_DIR=str(data_dir / "disks"),
        ASTINUSLAB_PLAYGROUND_CAPTURE_DIR=str(data_dir / "captures"),
        # Also isolate any virsh CLI invocation if the rejection under test
        # regresses: the Python fake-libvirt switch does not control virsh.
        ASTINUSLAB_LIBVIRT_URI=f"test://{data_dir / 'unavailable-hypervisor.xml'}",
        ASTINUSLAB_DB_PATH=str(db_path),
        ASTINUSLAB_CELERY_BROKER_URL=broker_url,
        ASTINUSLAB_CELERY_RESULT_BACKEND=backend_url,
        ASTINUSLAB_CELERY_RECONCILIATION_ENABLED="0",
        ASTINUSLAB_FAKE_LIBVIRT="1",
        ASTINUSLAB_FAKE_PLAYGROUND="1",
        CELERY_TASK_ALWAYS_EAGER="0",
    )
    return env


def _run_child(args: list[str], env: dict[str, str], log_tail: str) -> subprocess.CompletedProcess:
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, f"{args[-1]} failed:\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}\n{log_tail}"
    return result


def _wait_for_worker_ready(proc: subprocess.Popen, log_path: Path) -> None:
    deadline = time.monotonic() + WORKER_READY_TIMEOUT_S
    while time.monotonic() < deadline:
        # The sqlite broker has no remote-control transport, so ping/inspect is
        # unavailable; poll the worker's own readiness log line instead.
        if "ready." in log_path.read_text(errors="replace"):
            return
        if proc.poll() is not None:
            pytest.fail(f"worker exited early (rc={proc.returncode}):\n{log_path.read_text(errors='replace')[-3000:]}")
        time.sleep(0.25)
    pytest.fail(f"worker never became ready:\n{log_path.read_text(errors='replace')[-3000:]}")


@pytest.mark.timeout(180)
def test_control_runtime_task_fails_and_persists_through_real_worker(tmp_path):
    data_dir = tmp_path / "data"
    storage_dir = data_dir / "storage"
    storage_dir.mkdir(parents=True)
    db_path = storage_dir / "astinuslab.db"
    broker_url = f"sqla+sqlite:///{storage_dir / 'celery_broker.db'}"
    backend_url = f"db+sqlite:///{storage_dir / 'celery_results.db'}"
    env = _child_env(data_dir, db_path, broker_url, backend_url)
    log_path = tmp_path / "worker.log"

    _run_child([sys.executable, "-m", "astinuslab.db", "init"], env, "")

    worker_cmd = [
        sys.executable,
        "-m",
        "celery",
        "-A",
        "astinuslab.jobs.celery_main",
        "worker",
        "--pool=solo",
        "--concurrency=1",
        "-Q",
        "celery",
        "-n",
        "runner@%h",
        "--without-gossip",
        "--without-mingle",
        "--without-heartbeat",
        "--loglevel=INFO",
    ]
    with log_path.open("wb") as log_file:
        proc = subprocess.Popen(worker_cmd, env=env, stdout=log_file, stderr=subprocess.STDOUT, cwd=str(tmp_path))
    try:
        # Seed only once the worker is ready: startup recovery runs before that
        # and must not get a chance to rewrite the row under test.
        _wait_for_worker_ready(proc, log_path)
        seed = _run_child([sys.executable, "-c", SEED_SNIPPET], env, log_path.read_text(errors="replace")[-2000:])
        payload = next(line for line in seed.stdout.splitlines() if line.startswith("SEEDED "))
        ids = json.loads(payload.removeprefix("SEEDED "))

        with Celery("astinuslab-integration-client", broker=broker_url, backend=backend_url) as client:
            client.conf.update(task_serializer="json", accept_content=["json"], result_serializer="json")
            async_result = client.send_task("control_runtime", args=[ids["job_id"], ids["action_id"]], queue="celery")
            returned = async_result.get(timeout=RESULT_TIMEOUT_S, propagate=False)

            tail = log_path.read_text(errors="replace")[-3000:]
            assert async_result.state == "FAILURE", f"state={async_result.state} result={returned!r}\n{tail}"
            assert isinstance(returned, RuntimeError), f"result={returned!r}\n{tail}"
            assert "unsupported runtime control payload: role='target' action='pause'" in str(returned), (
                f"result={returned!r}\n{tail}"
            )

        connection = sqlite3.connect(str(db_path))
        try:
            row = connection.execute(
                "SELECT status, error_summary, started_at, finished_at FROM host_actions WHERE id = ?",
                (ids["action_id"],),
            ).fetchone()
        finally:
            connection.close()
        assert row is not None, f"action row missing\n{tail}"
        status, error_summary, started_at, finished_at = row
        assert status == "failed", f"row={row!r}\n{tail}"
        assert "unsupported runtime control payload: role='target' action='pause'" in (error_summary or ""), (
            f"row={row!r}\n{tail}"
        )
        assert started_at is not None and finished_at is not None, f"row={row!r}\n{tail}"
    except Exception as exc:
        raise AssertionError(
            f"Worker integration failed: {exc}\n{log_path.read_text(errors='replace')[-3000:]}"
        ) from exc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=15)
