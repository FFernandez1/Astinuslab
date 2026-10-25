from __future__ import annotations

import base64
import json
import subprocess
import sys

import pytest

from astinuslab.virtualization import guest_wait, qga_exec


@pytest.mark.parametrize(
    "launch,status",
    [
        ({"return": {"pid": True}}, None),
        ({"return": {"pid": 0}}, None),
        ({"return": {"pid": 17}}, {"return": {"exited": "false", "exitcode": 0}}),
        ({"return": {"pid": 17}}, {"return": {"exited": True, "exitcode": False}}),
        ({"return": {"pid": 17}}, {"return": {"exited": True}}),
    ],
)
def test_malformed_process_identity_or_exit_proof_is_not_success(monkeypatch, launch, status):
    calls = []

    def rpc(*args, **kwargs):
        calls.append(args[2])
        return json.dumps(launch if len(calls) == 1 else status)

    monkeypatch.setattr(qga_exec, "_run_qga_command", rpc)
    with pytest.raises(qga_exec.GuestExecError):
        qga_exec.run_guest_exec("qemu:///system", "vm", "powershell.exe", [])
    assert len(calls) == (1 if status is None else 2)


def test_guest_exec_can_discard_stderr_without_decoding_it(monkeypatch):
    responses = iter(
        [
            json.dumps({"return": {"pid": 17}}),
            json.dumps(
                {
                    "return": {
                        "exited": True,
                        "exitcode": 0,
                        "out-data": base64.b64encode(b"OK").decode(),
                        "err-data": "intentionally-not-base64",
                    }
                }
            ),
        ]
    )
    monkeypatch.setattr(qga_exec, "_run_qga_command", lambda *a, **k: next(responses))
    result = qga_exec.run_guest_exec("qemu:///system", "test-vm", "powershell.exe", [], discard_stderr=True)
    assert result == qga_exec.GuestExecResult(0, "OK", "")


def test_truncated_stdout_cannot_pass_even_when_it_looks_like_complete_json(monkeypatch):
    responses = iter(
        [
            json.dumps({"return": {"pid": 17}}),
            json.dumps(
                {
                    "return": {
                        "exited": True,
                        "exitcode": 0,
                        "out-truncated": True,
                        "out-data": base64.b64encode(b'{"looks_complete":true}').decode(),
                    }
                }
            ),
        ]
    )
    monkeypatch.setattr(qga_exec, "_run_qga_command", lambda *a, **k: next(responses))
    with pytest.raises(qga_exec.GuestExecOutputLimitError):
        qga_exec.run_guest_exec("qemu:///system", "test-vm", "powershell.exe", [], discard_stderr=True)


def test_run_guest_exec_polls_until_exit_and_decodes_output(monkeypatch):
    responses = iter(
        [
            json.dumps({"return": {"pid": 17}}),
            json.dumps({"return": {"exited": False}}),
            json.dumps(
                {
                    "return": {
                        "exited": True,
                        "exitcode": 0,
                        "out-data": base64.b64encode(b"OK").decode(),
                    }
                }
            ),
        ]
    )
    monkeypatch.setattr(qga_exec, "_run_qga_command", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(qga_exec.time, "sleep", lambda _seconds: None)

    result = qga_exec.run_guest_exec("qemu:///system", "winref", "cmd.exe", ["/c", "ver"])

    assert result.exitcode == 0
    assert result.stdout == "OK"
    assert result.stderr == ""


def test_run_guest_exec_rejects_oversized_output(monkeypatch):
    oversized = base64.b64encode(b"x" * (qga_exec.MAX_QGA_RESPONSE_BYTES + 1)).decode()
    responses = iter(
        [
            json.dumps({"return": {"pid": 17}}),
            json.dumps({"return": {"exited": True, "exitcode": 0, "out-data": oversized}}),
        ]
    )
    monkeypatch.setattr(qga_exec, "_run_qga_command", lambda *args, **kwargs: next(responses))

    with pytest.raises(qga_exec.GuestExecOutputLimitError):
        qga_exec.run_guest_exec("qemu:///system", "winref", "powershell.exe", [])


def test_guest_exec_launch_and_poll_share_one_deadline(monkeypatch):
    now = [0.0]
    timeouts = []

    def execute(uri, vm, payload, *, timeout):
        timeouts.append(timeout)
        if payload["execute"] == "guest-exec":
            now[0] += 4
            return json.dumps({"return": {"pid": 17}})
        now[0] += timeout
        return json.dumps({"return": {"exited": False}})

    monkeypatch.setattr(qga_exec.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(qga_exec.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(qga_exec, "_run_qga_command", execute)

    with pytest.raises(qga_exec.GuestExecError, match="timeout"):
        qga_exec.run_guest_exec("qemu:///system", "test-vm", "powershell.exe", [], timeout=5)
    assert timeouts == [5, 1]
    assert now[0] == 5


@pytest.mark.parametrize("failure", ["timeout", "output_limit", "interrupt"])
def test_bounded_host_process_closes_pipes_and_reaps_child_on_failure(monkeypatch, failure):
    processes = []
    popen = subprocess.Popen

    def tracked_popen(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(guest_wait.subprocess, "Popen", tracked_popen)
    if failure == "interrupt":

        def interrupt(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(guest_wait.selectors.DefaultSelector, "select", interrupt)
    code = "import time; time.sleep(60)"
    expected: type[BaseException] = subprocess.TimeoutExpired
    if failure == "output_limit":
        code = "import sys,time; sys.stdout.write('x'*4096); sys.stdout.flush(); time.sleep(60)"
        expected = RuntimeError
    elif failure == "interrupt":
        expected = KeyboardInterrupt
    try:
        with pytest.raises(expected):
            guest_wait._run_bounded_subprocess(
                [sys.executable, "-c", code],
                max_output_bytes=128,
                timeout=0.2 if failure == "timeout" else 5,
                check=True,
                capture_output=True,
                text=True,
            )
        assert len(processes) == 1
        process = processes[0]
        assert process.poll() is not None, "cancelled host child is still running"
        assert process.stdout is not None
        assert process.stderr is not None
        assert process.stdout.closed
        assert process.stderr.closed
    finally:
        # Reap only this test's harmless child if the regression is present.
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
