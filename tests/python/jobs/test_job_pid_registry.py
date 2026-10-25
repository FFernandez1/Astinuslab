from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from types import SimpleNamespace


def test_register_unregister_job_pid_are_idempotent(tmp_path):
    from astinuslab.virtualization.process_support import job_pid_dir, register_job_pid, unregister_job_pid

    job_id = "job-registry"
    pid = 12345
    pid_path = job_pid_dir(tmp_path, job_id) / str(pid)

    register_job_pid(tmp_path, job_id, pid)
    register_job_pid(tmp_path, job_id, pid)

    assert pid_path.exists()
    # Since Task 24 the entry carries identity, not just a filename. A PID that
    # is not running records nulls, which is exactly what fails verification.
    record = json.loads(pid_path.read_text())
    assert record["schema_version"] == "job-process/v1"
    assert record["pid"] == pid
    assert record["start_time_ticks"] is None

    unregister_job_pid(tmp_path, job_id, pid)
    unregister_job_pid(tmp_path, job_id, pid)

    assert not pid_path.exists()
    assert not job_pid_dir(tmp_path, job_id).exists()


def test_kill_job_subprocesses_kills_registry_process_with_empty_memory_registry(tmp_path, monkeypatch):
    import astinuslab.jobs.job_control as jc_mod
    from astinuslab.virtualization.process_support import job_pid_dir, register_job_pid

    job_id = "job-cross-process"
    process = subprocess.Popen(
        ["sleep", "60"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    register_job_pid(tmp_path, job_id, process.pid)
    monkeypatch.setattr(jc_mod, "settings", SimpleNamespace(storage_dir=tmp_path))

    with jc_mod.PROCESS_LOCK:
        original = dict(jc_mod.JOB_PROCESSES)
        jc_mod.JOB_PROCESSES.clear()

    try:
        jc_mod.kill_job_subprocesses(job_id)

        deadline = time.monotonic() + 5
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)

        assert process.poll() is not None
        assert not (job_pid_dir(tmp_path, job_id) / str(process.pid)).exists()
        assert not job_pid_dir(tmp_path, job_id).exists()
    finally:
        with jc_mod.PROCESS_LOCK:
            jc_mod.JOB_PROCESSES.clear()
            jc_mod.JOB_PROCESSES.update(original)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def test_kill_job_subprocesses_removes_dead_pid_registry_entry(tmp_path, monkeypatch):
    import astinuslab.jobs.job_control as jc_mod
    from astinuslab.virtualization.process_support import job_pid_dir, register_job_pid

    job_id = "job-dead-pid"
    process = subprocess.Popen(
        ["true"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    process.wait(timeout=5)
    register_job_pid(tmp_path, job_id, process.pid)
    monkeypatch.setattr(jc_mod, "settings", SimpleNamespace(storage_dir=tmp_path))

    with jc_mod.PROCESS_LOCK:
        original = dict(jc_mod.JOB_PROCESSES)
        jc_mod.JOB_PROCESSES.clear()

    try:
        jc_mod.kill_job_subprocesses(job_id)

        assert not (job_pid_dir(tmp_path, job_id) / str(process.pid)).exists()
        assert not job_pid_dir(tmp_path, job_id).exists()
    finally:
        with jc_mod.PROCESS_LOCK:
            jc_mod.JOB_PROCESSES.clear()
            jc_mod.JOB_PROCESSES.update(original)


def test_kill_job_subprocesses_ignores_missing_registry_dir(tmp_path, monkeypatch):
    import astinuslab.jobs.job_control as jc_mod

    monkeypatch.setattr(jc_mod, "settings", SimpleNamespace(storage_dir=tmp_path))
    jc_mod.kill_job_subprocesses("job-without-registry")


# ---------------------------------------------------------------------------
# Task 24 — a numeric PID is not an identity
#
# The registry held empty files named by PID, and cancellation sent SIGKILL to
# whatever process group happened to hold that number. PIDs are recycled; after
# a worker crash the number in the file may belong to something else entirely.
# ---------------------------------------------------------------------------


def test_registry_records_the_identity_not_just_the_number(tmp_path):
    import json

    from astinuslab.virtualization.process_support import job_pid_dir, register_job_pid

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        register_job_pid(tmp_path, "job-1", child.pid, job_id_action=7)
        record = json.loads((job_pid_dir(tmp_path, "job-1") / str(child.pid)).read_text())

        assert record["schema_version"] == "job-process/v1"
        assert record["pid"] == child.pid
        assert record["pgid"] == os.getpgid(child.pid)
        assert record["start_time_ticks"] > 0
        assert "python" in record["expected_executable"].lower()
        assert record["job_id"] == "job-1"
        assert record["action_id"] == 7
    finally:
        child.kill()
        child.wait()


def test_a_recycled_pid_is_never_signalled(tmp_path, monkeypatch):
    """The registry names a PID whose process has since been replaced. The
    replacement must not receive SIGKILL."""
    from astinuslab.jobs import job_control
    from astinuslab.virtualization.process_support import job_pid_dir, register_job_pid

    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        register_job_pid(tmp_path, "job-1", victim.pid, job_id_action=None)

        # Rewrite the record's identity so it describes a different process that
        # once held this PID: same number, older start time.
        record_path = job_pid_dir(tmp_path, "job-1") / str(victim.pid)
        record = json.loads(record_path.read_text())
        record["start_time_ticks"] = max(1, record["start_time_ticks"] - 5000)
        record_path.write_text(json.dumps(record))

        monkeypatch.setattr(job_control, "settings", SimpleNamespace(storage_dir=tmp_path))
        job_control.kill_job_subprocesses("job-1")

        time.sleep(0.3)
        assert victim.poll() is None, "an unverified record must never produce a signal"
        assert not record_path.exists(), "the stale record is cleared without signalling"
    finally:
        victim.kill()
        victim.wait()


def test_a_verified_process_is_still_killed(tmp_path, monkeypatch):
    from astinuslab.jobs import job_control
    from astinuslab.virtualization.process_support import register_job_pid

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        register_job_pid(tmp_path, "job-1", child.pid, job_id_action=None)

        monkeypatch.setattr(job_control, "settings", SimpleNamespace(storage_dir=tmp_path))
        job_control.kill_job_subprocesses("job-1")

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and child.poll() is None:
            time.sleep(0.1)
        assert child.poll() is not None, "a record that verifies must still be killed"
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


def test_a_legacy_empty_record_is_cleared_without_signalling(tmp_path, monkeypatch):
    """Files written by the previous release carry no identity, so they cannot be
    verified. Removing them is the safe direction; signalling them is not."""
    from astinuslab.jobs import job_control
    from astinuslab.virtualization.process_support import job_pid_dir

    survivor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        pid_dir = job_pid_dir(tmp_path, "job-1")
        pid_dir.mkdir(parents=True, exist_ok=True)
        legacy = pid_dir / str(survivor.pid)
        legacy.touch()

        monkeypatch.setattr(job_control, "settings", SimpleNamespace(storage_dir=tmp_path))
        job_control.kill_job_subprocesses("job-1")

        time.sleep(0.3)
        assert survivor.poll() is None
        assert not legacy.exists()
    finally:
        survivor.kill()
        survivor.wait()


def test_identity_of_a_dead_pid_does_not_verify(tmp_path):
    from astinuslab.virtualization import process_support

    child = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    child.wait()

    assert process_support.read_process_identity(child.pid) is None
