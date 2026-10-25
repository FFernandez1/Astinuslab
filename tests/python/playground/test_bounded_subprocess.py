"""Bounded host queries for guest agent and SSH probes.

A wedged or chatty ``virsh`` must not be able to freeze an authored session or
buffer an unbounded reply into host memory. Every guest query therefore runs
under both a wall-clock deadline and an output ceiling, and a process that hits
either is killed by process group rather than left behind. These tests drive
real subprocesses so the deadline, the ceiling, and the kill are exercised the
way they behave in production.
"""

from __future__ import annotations

import subprocess

import pytest

from astinuslab.virtualization.guest_wait import _run_bounded_subprocess, _run_subprocess

pytestmark = pytest.mark.unit


def _sh(script: str) -> list[str]:
    return ["/bin/sh", "-c", script]


def _run(script: str, *, max_output_bytes: int = 4096, timeout: float = 10.0, check: bool = False):
    return _run_bounded_subprocess(
        _sh(script),
        max_output_bytes=max_output_bytes,
        timeout=timeout,
        check=check,
        capture_output=True,
        text=True,
    )


# --- argument contract ---------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"capture_output": False, "text": True},
        {"capture_output": True, "text": False},
        {},
    ],
)
def test_a_bounded_run_requires_captured_text_output(kwargs):
    with pytest.raises(ValueError, match="capture_output=True and text=True"):
        _run_bounded_subprocess(_sh("echo hello"), max_output_bytes=64, timeout=5, check=False, **kwargs)


def test_the_wrapper_routes_to_the_bounded_runner_when_a_ceiling_is_given():
    result = _run_subprocess(
        _sh("echo hello"),
        _max_output_bytes=64,
        timeout=5,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "hello"


def test_the_wrapper_runs_plainly_without_a_ceiling():
    result = _run_subprocess(_sh("echo hello"), capture_output=True, text=True, timeout=5)

    assert result.stdout.strip() == "hello"


# --- capture -------------------------------------------------------------------


def test_a_small_command_returns_its_output():
    result = _run("echo hello")

    assert result.returncode == 0
    assert result.stdout.strip() == "hello"
    assert result.stderr == ""


def test_standard_error_is_captured_separately():
    result = _run("echo out; echo err 1>&2")

    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"


def test_a_command_that_writes_nothing_returns_empty_streams():
    result = _run("true")

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_undecodable_output_is_replaced_rather_than_raising():
    result = _run("printf '\\377\\376'")

    assert result.returncode == 0
    assert result.stdout


def test_a_nonzero_exit_is_reported_without_check():
    result = _run("exit 3")

    assert result.returncode == 3


def test_a_nonzero_exit_raises_under_check():
    with pytest.raises(subprocess.CalledProcessError) as caught:
        _run("echo out; echo err 1>&2; exit 4", check=True)

    assert caught.value.returncode == 4
    assert caught.value.output.strip() == "out"
    assert caught.value.stderr.strip() == "err"


# --- the output ceiling --------------------------------------------------------


def test_output_past_the_ceiling_is_refused():
    with pytest.raises(RuntimeError, match="exceeded its output limit"):
        _run("head -c 100000 /dev/zero | tr '\\0' 'x'", max_output_bytes=64)


def test_output_exactly_at_the_ceiling_is_allowed():
    result = _run("printf 'xxxxxxxx'", max_output_bytes=8)

    assert result.stdout == "xxxxxxxx"


def test_the_ceiling_counts_both_streams_together():
    with pytest.raises(RuntimeError, match="exceeded its output limit"):
        _run("printf 'aaaa'; printf 'bbbb' 1>&2; printf 'cccc'", max_output_bytes=8)


# --- the deadline --------------------------------------------------------------


def test_a_command_past_its_deadline_is_timed_out():
    with pytest.raises(subprocess.TimeoutExpired):
        _run("sleep 5", timeout=0.3)


def test_a_command_that_holds_its_pipes_open_is_timed_out():
    with pytest.raises(subprocess.TimeoutExpired):
        _run("echo partial; sleep 5", timeout=0.3)


def test_a_timed_out_command_leaves_no_running_process(monkeypatch):
    """The runner kills the process group, so nothing survives the deadline.

    Watch the actual child rather than scanning the machine: a global process
    search would also see unrelated processes and other tests.
    """

    from astinuslab.virtualization import guest_wait

    started = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(guest_wait.subprocess, "Popen", recording_popen)

    with pytest.raises(subprocess.TimeoutExpired):
        _run("sleep 5", timeout=0.3)

    assert len(started) == 1
    assert started[0].poll() is not None


def test_a_command_holding_its_pipes_open_is_reaped_too(monkeypatch):
    from astinuslab.virtualization import guest_wait

    started = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(guest_wait.subprocess, "Popen", recording_popen)

    with pytest.raises(subprocess.TimeoutExpired):
        _run("echo partial; sleep 5", timeout=0.3)

    assert started[0].poll() is not None
