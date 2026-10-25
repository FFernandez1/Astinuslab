from __future__ import annotations

import io
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest

from tests.python._support.helpers import autospec_setattr


@contextmanager
def _null_scope():
    """A session scope for runs that touch no job row."""
    yield None


class _FakeLogger:
    def debug(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass


class _FakeProc:
    """Simulates a process that exits immediately."""

    def __init__(self, pid: int, returncode: int, stdout: str, stderr: str):
        self.pid = pid
        self.returncode = returncode
        # Binary since Task 23: the tail is bounded in bytes and decoded once, so
        # a cut in the middle of a multibyte character cannot raise mid-capture.
        self.stdout = io.BytesIO(stdout.encode())
        self.stderr = io.BytesIO(stderr.encode())

    def poll(self):
        return self.returncode  # already finished

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        pass

    def communicate(self):
        return ("", "")  # threads already drained via .read()


class _BlockingFakeProc:
    """Simulates a long-running process that responds to terminate()."""

    def __init__(self, pid: int):
        self.pid = pid
        self.returncode: int | None = None
        self.stdout = io.BytesIO(b"")
        self.stderr = io.BytesIO(b"")
        self._terminated = False

    def poll(self):
        if self._terminated:
            self.returncode = -15
            return -15
        return None

    def terminate(self):
        self._terminated = True

    def wait(self, timeout=None):
        return self.returncode

    def communicate(self):
        return ("", "")


class _NoopLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _NoopRepo:
    def __init__(self, session):
        pass

    def update_action(self, action_id, *, active_pid):
        pass


@contextmanager
def _noop_session_scope():
    yield object()


class TestProcessSupport:
    def test_run_command_closes_subprocess_pipes(self, monkeypatch):
        import astinuslab.virtualization.process_support as proc_mod

        fake_proc = _FakeProc(4312, 0, "ok\n", "")
        autospec_setattr(monkeypatch, proc_mod.subprocess, "Popen", lambda *a, **k: fake_proc)

        result = proc_mod.run_command(
            _FakeLogger(),
            _null_scope,
            None,
            {},
            {},
            _NoopLock(),
            ["echo", "test"],
        )

        assert result == "ok"
        assert fake_proc.stdout.closed
        assert fake_proc.stderr.closed

    def test_run_command_sets_and_clears_active_pid_on_success(self, in_memory_session_scope, monkeypatch):
        import astinuslab.virtualization.process_support as proc_mod
        from astinuslab.api.schemas import JobCreate
        from astinuslab.db.repositories import JobRepository
        from astinuslab.jobs.job_services import create_job_service

        autospec_setattr(
            monkeypatch,
            proc_mod.subprocess,
            "Popen",
            lambda *a, **k: _FakeProc(4321, 0, "ok\n", ""),
        )

        action_by_job: dict[str, int] = {}
        job_processes: dict[str, set[subprocess.Popen[Any]]] = {}

        with in_memory_session_scope() as session:
            job = create_job_service(
                session,
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                ),
            )
            repo = JobRepository(session)
            for existing in repo.list_actions(job.id):
                repo.update_action(existing.id, status="completed")
            action = repo.enqueue_action(job.id, "acquire_memory_dump")
            action_by_job[job.id] = action.id
            job_id = job.id
            action_id = action.id

        result = proc_mod.run_command(
            _FakeLogger(),
            in_memory_session_scope,
            JobRepository,
            action_by_job,
            job_processes,
            _NoopLock(),
            ["echo", "test"],
            cwd=Path("."),
            job_id=job_id,
        )

        assert result == "ok"
        assert job_id not in job_processes

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            updated = repo.get_action(action_id)
            assert updated is not None
            assert updated.active_pid is None

    def test_run_command_clears_active_pid_on_failure(self, in_memory_session_scope, monkeypatch):
        import astinuslab.virtualization.process_support as proc_mod
        from astinuslab.api.schemas import JobCreate
        from astinuslab.db.repositories import JobRepository
        from astinuslab.jobs.job_services import create_job_service

        autospec_setattr(
            monkeypatch,
            proc_mod.subprocess,
            "Popen",
            lambda *a, **k: _FakeProc(5678, 7, "", "bad err"),
        )

        action_by_job: dict[str, int] = {}
        job_processes: dict[str, set[subprocess.Popen[Any]]] = {}

        with in_memory_session_scope() as session:
            job = create_job_service(
                session,
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                ),
            )
            repo = JobRepository(session)
            for existing in repo.list_actions(job.id):
                repo.update_action(existing.id, status="completed")
            action = repo.enqueue_action(job.id, "acquire_memory_dump")
            action_by_job[job.id] = action.id
            job_id = job.id
            action_id = action.id

        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            proc_mod.run_command(
                _FakeLogger(),
                in_memory_session_scope,
                JobRepository,
                action_by_job,
                job_processes,
                _NoopLock(),
                ["echo", "test"],
                cwd=Path("."),
                job_id=job_id,
            )

        assert exc_info.value.returncode == 7
        assert job_id not in job_processes

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            updated = repo.get_action(action_id)
            assert updated is not None
            assert updated.active_pid is None

    def test_run_command_cancel_terminates_subprocess(self, in_memory_session_scope, monkeypatch):
        import astinuslab.virtualization.process_support as proc_mod
        from astinuslab.api.schemas import JobCreate
        from astinuslab.db.repositories import JobRepository
        from astinuslab.jobs.job_services import create_job_service

        fake_proc = _BlockingFakeProc(pid=9999)
        autospec_setattr(monkeypatch, proc_mod.subprocess, "Popen", lambda *a, **k: fake_proc)
        monkeypatch.setattr(proc_mod.os, "killpg", lambda _pid, _sig: fake_proc.terminate())

        action_by_job: dict[str, int] = {}
        job_processes: dict[str, set[subprocess.Popen[Any]]] = {}

        with in_memory_session_scope() as session:
            job = create_job_service(
                session,
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                ),
            )
            job_id = job.id

        with pytest.raises(RuntimeError, match="cancel_requested"):
            proc_mod.run_command(
                _FakeLogger(),
                in_memory_session_scope,
                JobRepository,
                action_by_job,
                job_processes,
                _NoopLock(),
                ["sleep", "9999"],
                job_id=job_id,
                is_cancel_requested_fn=lambda _jid: True,
            )

        assert fake_proc._terminated
        assert job_id not in job_processes

    def test_run_command_registers_pidfile_while_running_and_removes_it_after_completion(self, tmp_path):
        import astinuslab.virtualization.process_support as proc_mod

        job_id = "job-pid-lifecycle"
        sentinel = tmp_path / "finish"
        action_by_job: dict[str, int] = {}
        job_processes: dict[str, set[subprocess.Popen[Any]]] = {}
        command = [
            sys.executable,
            "-c",
            (
                "import pathlib, sys, time\n"
                "sentinel = pathlib.Path(sys.argv[1])\n"
                "while not sentinel.exists():\n"
                "    time.sleep(0.05)\n"
                "print('done')\n"
            ),
            str(sentinel),
        ]

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                proc_mod.run_command,
                _FakeLogger(),
                _noop_session_scope,
                _NoopRepo,
                action_by_job,
                job_processes,
                _NoopLock(),
                command,
                job_id=job_id,
                storage_dir=tmp_path,
            )

            pid_dir = proc_mod.job_pid_dir(tmp_path, job_id)
            deadline = time.monotonic() + 5
            pidfiles = []
            while time.monotonic() < deadline:
                pidfiles = list(pid_dir.iterdir()) if pid_dir.exists() else []
                if pidfiles:
                    break
                time.sleep(0.05)

            assert len(pidfiles) == 1
            sentinel.touch()
            assert future.result(timeout=5) == "done"

        assert not pidfiles[0].exists()
        assert not pid_dir.exists()

    def test_kill_job_subprocesses_terminates_all_tracked_processes(self, monkeypatch):
        import signal

        import astinuslab.jobs.job_control as orch_mod

        class _TrackedProc:
            def __init__(self, pid: int):
                self.pid = pid

        job_id = "job-multi-process"
        first = _TrackedProc(1111)
        second = _TrackedProc(2222)
        killed: list[tuple[int, int]] = []

        with orch_mod.PROCESS_LOCK:
            original = dict(orch_mod.JOB_PROCESSES)
            orch_mod.JOB_PROCESSES.clear()
            orch_mod.JOB_PROCESSES[job_id] = {
                cast(subprocess.Popen[Any], first),
                cast(subprocess.Popen[Any], second),
            }

        autospec_setattr(monkeypatch, orch_mod.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

        try:
            orch_mod.kill_job_subprocesses(job_id)
        finally:
            with orch_mod.PROCESS_LOCK:
                orch_mod.JOB_PROCESSES.clear()
                orch_mod.JOB_PROCESSES.update(original)

        assert sorted(killed) == [(1111, signal.SIGKILL), (2222, signal.SIGKILL)]


class TestRequireNoninteractiveSudo:
    def test_error_text_includes_install_command_and_helper_path(self):
        """The error must guide the operator to the exact install command and the helper that failed.

        Operator UX contract: missing install command in this error is a regression.
        """
        import astinuslab.virtualization.process_support as proc_mod

        def failing_run(cmd):
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=cmd,
                output=b"",
                stderr=b"sudo: a password is required",
            )

        with pytest.raises(RuntimeError) as exc_info:
            proc_mod.require_noninteractive_sudo(failing_run, settings=None)

        message = str(exc_info.value)
        assert "HOST NOT PROVISIONED" in message, message
        assert "sudo: a password is required" in message, message
        assert "ops/deploy/install.sh" in message, message
        assert "install_sudoers_astinuslab.sh" in message, message
        assert "libvirt_storage_admin.sh" in message, message

    def test_returns_none_on_success(self):
        import astinuslab.virtualization.process_support as proc_mod

        calls: list[list[str]] = []

        def ok_run(cmd):
            calls.append(cmd)
            return ""

        proc_mod.require_noninteractive_sudo(ok_run, settings=None)
        assert len(calls) == 1
        assert calls[0][:2] == ["sudo", "-n"]
        assert calls[0][-1] == "noop"


@pytest.mark.parametrize("fault", ["bookkeeping", "callback", "reader_start"])
def test_post_spawn_fault_keeps_custody_and_original_exception(monkeypatch, fault):
    import signal

    import astinuslab.virtualization.process_support as proc_mod

    class SupervisedProc(_BlockingFakeProc):
        def __init__(self):
            super().__init__(123456)
            self.waits = []

        def wait(self, timeout=None):
            self.waits.append(timeout)
            assert self._terminated, "must terminate before reaping a live child"
            assert timeout is not None, "reaping must be bounded"
            self.returncode = -15
            return self.returncode

    child = SupervisedProc()
    signals = []
    primary = ValueError("injected primary failure")

    def fail(*args, **kwargs):
        raise primary

    def signal_child(pid, sig):
        assert pid == child.pid
        signals.append(sig)
        child.terminate()

    monkeypatch.setattr(proc_mod.subprocess, "Popen", lambda *a, **k: child)
    monkeypatch.setattr(proc_mod.os, "killpg", signal_child)
    if fault == "bookkeeping":
        monkeypatch.setattr(proc_mod, "set_action_pid", fail)
    elif fault == "reader_start":
        monkeypatch.setattr(proc_mod.threading.Thread, "start", fail)
    registry: dict[str, set[subprocess.Popen[Any]]] = {}
    with pytest.raises(ValueError) as caught:
        proc_mod.run_command(
            _FakeLogger(),
            _noop_session_scope,
            _NoopRepo,
            {},
            registry,
            _NoopLock(),
            ["fixture"],
            job_id="job",
            is_cancel_requested_fn=fail if fault == "callback" else None,
        )
    assert caught.value is primary
    assert signals == [signal.SIGTERM]
    assert child.waits == [5.0]
    assert child.stdout.closed and child.stderr.closed
    assert registry == {}


@pytest.mark.parametrize("returncode", [0, 7])
def test_pid_clear_failure_preserves_command_result_after_cleanup(monkeypatch, returncode):
    from unittest.mock import Mock

    import astinuslab.virtualization.process_support as proc_mod

    child = _FakeProc(123456, returncode, "out", "command error")
    failure = ValueError("PID clear failed")

    def bookkeeping(_scope, _repo, _actions, _job, pid):
        if pid is None:
            raise failure

    monkeypatch.setattr(proc_mod.subprocess, "Popen", lambda *a, **k: child)
    monkeypatch.setattr(proc_mod, "set_action_pid", bookkeeping)
    logger = Mock()
    registry: dict[str, set[subprocess.Popen[Any]]] = {}
    expected = ValueError if returncode == 0 else subprocess.CalledProcessError
    with pytest.raises(expected) as caught:
        proc_mod.run_command(
            logger, _noop_session_scope, _NoopRepo, {}, registry, _NoopLock(), ["fixture"], job_id="job"
        )
    if returncode == 0:
        assert caught.value is failure
    else:
        assert isinstance(caught.value, subprocess.CalledProcessError)
        assert caught.value.returncode == 7
        assert logger.warning.called
    assert child.stdout.closed and child.stderr.closed and not registry


def test_post_spawn_failure_reaps_real_owned_child(monkeypatch):
    import os
    import signal

    import astinuslab.virtualization.process_support as proc_mod

    original_popen = subprocess.Popen
    children = []

    def spawn(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        children.append(child)
        return child

    def bookkeeping(_scope, _repo, _actions, _job, pid):
        if pid is not None:
            raise ValueError("real child bookkeeping failure")

    monkeypatch.setattr(proc_mod.subprocess, "Popen", spawn)
    monkeypatch.setattr(proc_mod, "set_action_pid", bookkeeping)
    try:
        with pytest.raises(ValueError, match="real child bookkeeping failure"):
            proc_mod.run_command(
                _FakeLogger(),
                _noop_session_scope,
                _NoopRepo,
                {},
                {},
                _NoopLock(),
                [sys.executable, "-c", "import time; time.sleep(30)"],
                job_id="owned-child",
            )
        assert len(children) == 1
        assert children[0].poll() is not None
        assert children[0].stdout is not None and children[0].stderr is not None
        assert children[0].stdout.closed and children[0].stderr.closed
    finally:
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)
            assert child.stdout is not None and child.stderr is not None
            child.stdout.close()
            child.stderr.close()


@pytest.mark.parametrize("unkillable", [False, True])
def test_fault_escalates_and_never_reaps_without_a_deadline(monkeypatch, unkillable):
    import signal
    from unittest.mock import Mock

    import astinuslab.virtualization.process_support as proc_mod

    child = _BlockingFakeProc(123456)
    waits, signals = [], []
    primary = ValueError("callback failed")

    def wait(timeout=None):
        waits.append(timeout)
        if unkillable or len(waits) == 1:
            raise subprocess.TimeoutExpired("fixture", timeout)
        child.returncode = -9
        return -9

    def callback(_job):
        raise primary

    monkeypatch.setattr(child, "wait", wait)
    monkeypatch.setattr(proc_mod.subprocess, "Popen", lambda *a, **k: child)
    monkeypatch.setattr(proc_mod.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    logger = Mock()
    registry: dict[str, set[subprocess.Popen[Any]]] = {}
    with pytest.raises(ValueError) as caught:
        proc_mod.run_command(
            logger,
            _noop_session_scope,
            _NoopRepo,
            {},
            registry,
            _NoopLock(),
            ["fixture"],
            job_id="job",
            is_cancel_requested_fn=callback,
        )
    assert caught.value is primary
    assert waits == [5.0, 5.0]
    assert signals == [(child.pid, signal.SIGTERM), (child.pid, signal.SIGKILL)]
    assert child.stdout.closed and child.stderr.closed and not registry
    if unkillable:
        assert any("cleanup incomplete" in str(call) for call in logger.warning.call_args_list)


def test_retained_pipe_has_one_bounded_drain_budget(monkeypatch):
    import os
    from unittest.mock import Mock

    import astinuslab.virtualization.process_support as proc_mod

    child = _FakeProc(123456, 0, "", "")
    writers = []
    for attr in ("stdout", "stderr"):
        read_fd, write_fd = os.pipe()
        getattr(child, attr).close()
        setattr(child, attr, os.fdopen(read_fd, "rb"))
        writers.append(write_fd)
    monkeypatch.setattr(proc_mod.subprocess, "Popen", lambda *a, **k: child)
    monkeypatch.setattr(proc_mod, "_DRAIN_TIMEOUT_SECONDS", 0.3)
    logger = Mock()
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="pipe"):
            proc_mod.run_command(logger, _null_scope, None, {}, {}, _NoopLock(), ["fixture"])
        assert time.monotonic() - started < 1.5
        assert child.stdout.closed and child.stderr.closed
        assert logger.warning.called
    finally:
        for fd in writers:
            os.close(fd)


class TestBoundedSubprocess:
    """Task 23 — a guest can neither run forever nor fill the worker's memory.

    `timeout=None` was the default, and stdout/stderr were accumulated into
    unbounded lists. A workload that never exits, or one that prints in a loop,
    took the worker down with it.
    """

    def _run(self, cmd, **kwargs):
        import astinuslab.virtualization.process_support as proc_mod

        return proc_mod.run_command(
            _FakeLogger(),
            _null_scope,
            None,
            {},
            {},
            _NoopLock(),
            cmd,
            **kwargs,
        )

    def test_a_command_without_an_explicit_timeout_still_gets_one(self, monkeypatch):
        import astinuslab.virtualization.process_support as proc_mod

        monkeypatch.setattr(proc_mod, "DEFAULT_TIMEOUT_SECONDS", 1.0)

        started = time.monotonic()
        with pytest.raises(RuntimeError, match="timed out"):
            self._run([sys.executable, "-c", "import time; time.sleep(30)"])

        assert time.monotonic() - started < 15, "the default must actually bound the run"

    def test_timeout_defaults_are_per_command_class_not_one_hidden_value(self):
        import astinuslab.virtualization.process_support as proc_mod

        acl = proc_mod.timeout_for(["sudo", "-n", "/usr/local/libexec/astinuslab/fix_libvirt_path_acl.sh", "/x"])
        disk = proc_mod.timeout_for(["bash", "/opt/astinuslab/ops/host/acquire_disk_remote.sh", "job", "ip"])
        unknown = proc_mod.timeout_for(["some-tool", "--flag"])

        assert acl < unknown < disk, "an ACL call and a disk image copy cannot share a deadline"
        assert unknown == proc_mod.DEFAULT_TIMEOUT_SECONDS

    @pytest.mark.parametrize(
        "cmd",
        [
            [],
            ["/home/tester/candidate/.venv/bin/python", "-c", "import time; time.sleep(30)"],
            ["some-tool", "--job-id=123", "--status=ok", "evidence", "ssh-note"],
            ["python", "-c", "import uuid"],
            ["cat", "/opt/acquire_disk_remote.sh"],
            ["bash", "-c", "echo acquire_disk_remote"],
            ["bash", "-lc", "echo acquire_disk_remote"],
            ["sudo", "-n", "bash", "-c", "echo acquire_disk_remote"],
            ["/ssh/backups/some-tool", "qemu-img"],
            ["identity-tool"],
        ],
    )
    def test_timeout_ignores_incidental_text(self, monkeypatch, cmd):
        import astinuslab.virtualization.process_support as proc_mod

        monkeypatch.setattr(proc_mod, "DEFAULT_TIMEOUT_SECONDS", 1.0)
        assert proc_mod.timeout_for(cmd) == 1.0

    @pytest.mark.parametrize(
        ("cmd", "expected"),
        [
            (["/usr/bin/id"], 300.0),
            (["virsh", "-c", "qemu:///system", "dominfo", "acquire_disk_remote"], 300.0),
            (["ssh", "host", "qemu-img info disk"], 3600.0),
            (["/usr/bin/qemu-img", "info", "evidence.qcow2"], 7200.0),
            (["sudo", "-n", "/opt/fix_libvirt_path_acl.sh", "/acquire_disk_remote"], 120.0),
            (["sudo", "-u", "ssh", "--", "bash", "/opt/acquire_disk_host.sh"], 21600.0),
            (["bash", "--", "/opt/acquire_disk_remote.sh", "--status=ok"], 21600.0),
            (["/opt/rebuild_cloud_image_libvirt_storage.sh", "image"], 7200.0),
            (["bash", "/opt/rebuild_cloud_image_with_virtiofs.sh", "image"], 7200.0),
            (["bash", "/opt/run_guest_script_remote.sh", "job"], 3600.0),
        ],
    )
    def test_timeout_classifies_only_executable_or_wrapped_helper(self, cmd, expected):
        import astinuslab.virtualization.process_support as proc_mod

        assert proc_mod.timeout_for(cmd) == expected

    def test_an_explicit_timeout_still_wins(self, monkeypatch):
        import astinuslab.virtualization.process_support as proc_mod

        monkeypatch.setattr(proc_mod, "DEFAULT_TIMEOUT_SECONDS", 600.0)

        with pytest.raises(RuntimeError, match="timed out"):
            self._run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1.0)

    def test_noisy_output_is_bounded_to_a_tail(self, monkeypatch):
        import astinuslab.virtualization.process_support as proc_mod

        monkeypatch.setattr(proc_mod, "MAX_STREAM_TAIL_BYTES", 4096)

        result = self._run(
            [sys.executable, "-c", "print('x' * 200_000); print('END-MARKER')"],
            timeout=60,
        )

        assert len(result) <= 4096
        assert "END-MARKER" in result, "the tail is what diagnoses a failure, so keep the end"

    def test_output_past_the_hard_cap_kills_the_process(self, monkeypatch):
        import astinuslab.virtualization.process_support as proc_mod

        monkeypatch.setattr(proc_mod, "MAX_TOTAL_OUTPUT_BYTES", 64 * 1024)

        with pytest.raises(RuntimeError, match="output"):
            self._run(
                [sys.executable, "-c", "import sys\nwhile True: sys.stdout.write('y' * 4096)"],
                timeout=60,
            )

    def test_a_truncated_tail_decodes_across_a_multibyte_boundary(self, monkeypatch):
        """Cutting a byte tail can land mid-character; that must not raise."""
        import astinuslab.virtualization.process_support as proc_mod

        monkeypatch.setattr(proc_mod, "MAX_STREAM_TAIL_BYTES", 101)

        result = self._run(
            [sys.executable, "-c", "import sys; sys.stdout.write('\\u00e9' * 500)"],
            timeout=60,
        )

        assert isinstance(result, str)
        assert "é" in result

    def test_a_failing_command_still_raises_with_its_stderr(self):
        with pytest.raises(subprocess.CalledProcessError) as exc:
            self._run(
                [sys.executable, "-c", "import sys; sys.stderr.write('the real problem\\n'); sys.exit(3)"],
                timeout=60,
            )

        assert exc.value.returncode == 3
        assert "the real problem" in (exc.value.stderr or "")

    def test_timeout_kills_the_whole_process_group(self, tmp_path):
        """A child that outlives its parent keeps holding the lab's resources."""
        marker = tmp_path / "child-alive"
        script = (
            "import os, sys, time, subprocess\n"
            f"subprocess.Popen([sys.executable, '-c', \"import time,pathlib;"
            f" pathlib.Path(r'{marker}').touch(); time.sleep(30)\"])\n"
            "time.sleep(30)\n"
        )

        with pytest.raises(RuntimeError, match="timed out"):
            self._run([sys.executable, "-c", script], timeout=2)

        time.sleep(1)
        # The grandchild was in the killed group, so nothing is left running.
        remaining = subprocess.run(
            ["pgrep", "-f", str(marker)], capture_output=True, text=True, check=False
        ).stdout.strip()
        assert remaining == "", f"process group survived the timeout: {remaining}"
