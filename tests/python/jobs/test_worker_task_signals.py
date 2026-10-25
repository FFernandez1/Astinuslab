"""Worker task signals: a task that dies must leave a record naming the Job it owned.

``owned_action`` already fails the HostAction row when the task body raises.
These handlers are the *observability* half: they explain, in the log, what
Celery did to a task — including for tasks that ``owned_action`` never wraps
(beat tasks, ``run_vol3_plugin``) and for revocations that never enter a task
body at all. They deliberately do not write lifecycle state; HostAction has one
owner and two writers would race it.
"""

from __future__ import annotations

import logging
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def signals():
    """Import the worker-only module, then undo what importing it connected.

    The handlers register through ``@signal.connect`` at import time, which is
    what a worker process wants and what a test session must not keep: the
    receivers would stay connected for every later test in the process. The
    prerun handler mints a request id when the publisher did not supply one, so
    a leaked receiver drains any mock a later test installs on
    ``secrets.token_hex``. The cases below call the handlers directly, so
    nothing here needs them connected.
    """
    from astinuslab.jobs import celery_worker_signals

    yield celery_worker_signals

    for signal, receiver in (
        (celery_worker_signals._task_prerun_signal, celery_worker_signals._on_task_prerun),
        (celery_worker_signals._task_postrun_signal, celery_worker_signals._on_task_postrun),
        (celery_worker_signals._task_failure_signal, celery_worker_signals._on_task_failure),
        (celery_worker_signals._task_revoked_signal, celery_worker_signals._on_task_revoked),
        (celery_worker_signals._worker_shutting_down_signal, celery_worker_signals._on_worker_shutting_down),
        (celery_worker_signals._worker_ready_signal, celery_worker_signals._on_worker_ready),
    ):
        signal.disconnect(receiver)


class _Request:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class TestSignalRegistrationBoundary:
    def test_api_import_registers_no_task_signals(self):
        """The API process must not grow worker-only receivers by importing the app."""
        code = """
from celery.signals import task_failure, task_revoked, worker_shutting_down
import astinuslab.main  # noqa: F401
print(len(task_failure.receivers), len(task_revoked.receivers), len(worker_shutting_down.receivers))
"""
        result = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)

        assert result.stdout.strip() == "0 0 0"

    def test_worker_import_registers_the_task_signals(self):
        code = """
from celery.signals import task_failure, task_revoked, worker_shutting_down
import astinuslab.jobs.celery_worker_signals  # noqa: F401
print(
    len(task_failure.receivers) > 0,
    len(task_revoked.receivers) > 0,
    len(worker_shutting_down.receivers) > 0,
)
"""
        result = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)

        assert result.stdout.strip() == "True True True"


class TestTaskFailure:
    def test_failure_is_logged_at_error_with_job_and_action(self, signals, caplog):
        with caplog.at_level(logging.ERROR):
            signals._on_task_failure(
                sender=_Request(name="acquire_memory_dump"),
                task_id="celery-task-1",
                exception=RuntimeError("guest agent unreachable"),
                args=("job-abc",),
                kwargs={"action_id": 42},
                einfo=None,
            )

        record = caplog.records[-1]
        assert record.levelno == logging.ERROR
        assert "task_failed" in record.message
        assert "task=acquire_memory_dump" in record.message
        assert "job_id=job-abc" in record.message
        assert "action_id=42" in record.message
        assert "RuntimeError" in record.message
        assert "guest agent unreachable" in record.message

    def test_a_task_without_a_job_is_still_reported(self, signals, caplog):
        """Beat tasks carry no job id; they are exactly the ones nothing else records."""
        with caplog.at_level(logging.ERROR):
            signals._on_task_failure(
                sender=_Request(name="run_retention"),
                task_id="celery-task-2",
                exception=OSError("disk full"),
                args=(),
                kwargs={},
                einfo=None,
            )

        message = caplog.records[-1].message
        assert "task=run_retention" in message
        assert "job_id=-" in message
        assert "action_id=-" in message

    def test_positional_action_id_is_recorded(self, signals, caplog):
        """Tasks are dispatched positionally as (job_id, action_id) in some paths."""
        with caplog.at_level(logging.ERROR):
            signals._on_task_failure(
                sender=_Request(name="run_immediate_job"),
                task_id="celery-task-3",
                exception=ValueError("bad"),
                args=("job-xyz", 7),
                kwargs={},
                einfo=None,
            )

        message = caplog.records[-1].message
        assert "job_id=job-xyz" in message
        assert "action_id=7" in message

    def test_a_malformed_exception_does_not_break_the_handler(self, signals, caplog):
        class _Unprintable(Exception):
            def __str__(self):
                raise RuntimeError("cannot render")

        with caplog.at_level(logging.ERROR):
            signals._on_task_failure(
                sender=_Request(name="run_vol3_plugin"),
                task_id="celery-task-4",
                exception=_Unprintable(),
                args=("job-1",),
                kwargs={},
                einfo=None,
            )

        assert "task_failed" in caplog.records[-1].message


class TestTaskRevoked:
    def test_revocation_records_the_reason(self, signals, caplog):
        with caplog.at_level(logging.WARNING):
            signals._on_task_revoked(
                sender=None,
                request=_Request(
                    task="acquire_memory_dump",
                    id="celery-task-5",
                    args=("job-abc",),
                    kwargs={"action_id": 9},
                ),
                terminated=True,
                signum=15,
                expired=False,
            )

        message = caplog.records[-1].message
        assert "task_revoked" in message
        assert "task=acquire_memory_dump" in message
        assert "job_id=job-abc" in message
        assert "action_id=9" in message
        assert "terminated=True" in message
        assert "expired=False" in message

    def test_an_expired_revocation_is_reported_as_such(self, signals, caplog):
        with caplog.at_level(logging.WARNING):
            signals._on_task_revoked(
                sender=None,
                request=_Request(task="run_retention", id="celery-task-6", args=(), kwargs={}),
                terminated=False,
                signum=None,
                expired=True,
            )

        assert "expired=True" in caplog.records[-1].message


class TestWorkerShuttingDown:
    def test_shutdown_is_recorded_with_its_identity(self, signals, caplog):
        with caplog.at_level(logging.INFO):
            signals._on_worker_shutting_down(sig="SIGTERM", how="Warm", exitcode=0)

        message = caplog.records[-1].message
        assert "worker_shutting_down" in message
        assert "signal=SIGTERM" in message
        assert "how=Warm" in message
