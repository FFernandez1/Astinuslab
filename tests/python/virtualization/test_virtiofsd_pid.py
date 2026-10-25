from __future__ import annotations

import logging

import pytest

# Capture the real start_virtiofsd at import time, before the autouse
# _stub_virtiofsd fixture monkeypatches it on the vm_support module.
from astinuslab.virtualization.vm_support import start_virtiofsd as real_start_virtiofsd
from tests.python._support.helpers import autospec_setattr

_logger = logging.getLogger(__name__)


class _FakePopen:
    def __init__(self, *args, **kwargs):
        self.pid = 7777
        self.terminate_calls = 0

    def terminate(self):
        self.terminate_calls += 1


def test_start_virtiofsd_terminates_process_when_pid_file_write_fails(tmp_path, monkeypatch):
    """Closes the F06 race: if the pid-file write fails, virtiofsd must be SIGTERMed.

    Before the fix, the caller wrote the pid file *after* start_virtiofsd returned,
    so a write failure left the daemon running with no pid file — invisible to teardown.
    Now the write happens inside start_virtiofsd; failure must terminate the process.
    """
    import astinuslab.virtualization.vm_support as vm_mod

    socket_path = tmp_path / "virtiofsd.sock"
    share_dir = tmp_path / "share"
    share_dir.mkdir()
    pid_file_path = tmp_path / "virtiofsd.pid"

    # Make pid_file_path a directory so write_text raises IsADirectoryError.
    pid_file_path.mkdir()

    # Pre-create the socket so the readiness loop sees it on the first iteration.
    socket_path.touch()

    fake = _FakePopen()
    autospec_setattr(monkeypatch, vm_mod.subprocess, "Popen", lambda *a, **k: fake)

    with pytest.raises(IsADirectoryError):
        real_start_virtiofsd(_logger, "job-test-pid", socket_path, share_dir, pid_file_path)

    assert fake.terminate_calls == 1, "virtiofsd process must be terminated on pid-file write failure"


def test_start_virtiofsd_writes_pid_file_on_success(tmp_path, monkeypatch):
    """On the happy path, the pid file is written before the function returns
    so teardown_vm can always find the daemon.
    """
    import astinuslab.virtualization.vm_support as vm_mod

    socket_path = tmp_path / "virtiofsd.sock"
    share_dir = tmp_path / "share"
    share_dir.mkdir()
    pid_file_path = tmp_path / "virtiofsd.pid"

    socket_path.touch()
    fake = _FakePopen()
    autospec_setattr(monkeypatch, vm_mod.subprocess, "Popen", lambda *a, **k: fake)

    pid = real_start_virtiofsd(_logger, "job-test-ok", socket_path, share_dir, pid_file_path)

    assert pid == 7777
    assert pid_file_path.read_text() == "7777"
    assert fake.terminate_calls == 0
