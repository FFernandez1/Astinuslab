"""Job identifiers as log record fields, and the request id crossing into the worker.

``request_id`` already follows an HTTP request. Worker records carried their
job through prose (``[job-abc] ...``), which no filter can select on. These are
the same mechanism extended: context variables the logging filter reads.
"""

from __future__ import annotations

import logging
from typing import cast

import pytest

from astinuslab.core import request_context

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clean_context():
    yield
    request_context.reset_job_context(None)


def _record_through_filter(**_kwargs) -> request_context._RequestLogRecord:
    """A record that has been through the filter, typed as the filter leaves it.

    The filter sets ``request_id``/``job_id``/``action_id`` on a plain
    ``LogRecord``; the declared shape lives in ``_RequestLogRecord``, so reading
    those fields back needs the same type the production code casts to.
    """
    record = logging.LogRecord("astinuslab.test", logging.INFO, __file__, 1, "msg", None, None)
    request_context.RequestContextFilter().filter(record)
    return cast(request_context._RequestLogRecord, record)


class TestJobContextVars:
    def test_absent_context_reports_the_placeholder(self):
        record = _record_through_filter()

        assert record.job_id == "-"
        assert record.action_id == "-"

    def test_bound_context_reaches_the_record(self):
        request_context.bind_job_context("job-abc", 42)

        record = _record_through_filter()

        assert record.job_id == "job-abc"
        assert record.action_id == "42"

    def test_a_job_without_an_action_reports_only_the_job(self):
        request_context.bind_job_context("job-abc", None)

        record = _record_through_filter()

        assert record.job_id == "job-abc"
        assert record.action_id == "-"

    def test_reset_restores_the_previous_context(self):
        outer = request_context.bind_job_context("job-outer", 1)
        inner = request_context.bind_job_context("job-inner", 2)

        request_context.reset_job_context(inner)
        assert _record_through_filter().job_id == "job-outer"

        request_context.reset_job_context(outer)
        assert _record_through_filter().job_id == "-"

    def test_reset_with_none_clears_the_context(self):
        request_context.bind_job_context("job-abc", 7)

        request_context.reset_job_context(None)

        assert _record_through_filter().job_id == "-"

    def test_a_hostile_job_id_cannot_inject_into_a_log_line(self):
        """Job ids reach the formatter; a newline would forge a second record."""
        request_context.bind_job_context("job\nFAKE [ERROR] forged", 1)

        assert "\n" not in _record_through_filter().job_id


class TestTextFormat:
    def test_the_job_pairs_are_appended_only_when_set(self, tmp_path):
        from astinuslab.core import logging_config

        formatter = logging_config.build_formatter("text")
        record = logging.LogRecord("astinuslab.test", logging.INFO, __file__, 1, "capture started", None, None)
        request_context.RequestContextFilter().filter(record)

        assert "job_id=" not in formatter.format(record)

        request_context.bind_job_context("job-abc", 42)
        record = logging.LogRecord("astinuslab.test", logging.INFO, __file__, 1, "capture started", None, None)
        request_context.RequestContextFilter().filter(record)
        rendered = formatter.format(record)

        assert "job_id=job-abc" in rendered
        assert "action_id=42" in rendered
        # Appended after the message on purpose: the Logs page's line parser
        # captures the logger name up to the first ':' and the message after it,
        # so anything inserted before the message would be read as the name.
        assert rendered.endswith("capture started job_id=job-abc action_id=42")
        assert "astinuslab.test: capture started" in rendered


class TestTaskHeaderPropagation:
    def test_publishing_a_task_carries_the_current_request_id(self):
        from astinuslab.jobs import celery_app as celery_app_mod

        request_id, token = request_context.bind_request_id("ui-request-abc")
        try:
            headers: dict[str, object] = {}
            celery_app_mod._attach_request_id(headers=headers)
        finally:
            request_context.reset_request_id(token)

        assert headers[request_context.REQUEST_ID_HEADER] == request_id

    def test_publishing_without_a_request_adds_no_header(self):
        from astinuslab.jobs import celery_app as celery_app_mod

        headers: dict[str, object] = {}
        celery_app_mod._attach_request_id(headers=headers)

        assert request_context.REQUEST_ID_HEADER not in headers

    def test_a_missing_headers_mapping_is_tolerated(self):
        """Celery calls this signal for every publish; it must never raise."""
        from astinuslab.jobs import celery_app as celery_app_mod

        celery_app_mod._attach_request_id(headers=None)


class TestWorkerBinding:
    def test_nested_tasks_restore_each_parent_context(self, monkeypatch):
        import threading
        from types import SimpleNamespace

        from astinuslab.jobs import celery_worker_signals as signals

        monkeypatch.setattr(signals, "_saved_context", threading.local())
        _, request_token = request_context.bind_request_id("http-request")
        job_token = request_context.bind_job_context("http-job", 1)
        try:
            outer = SimpleNamespace(request={request_context.REQUEST_ID_HEADER: "outer-request"})
            inner = SimpleNamespace(request={request_context.REQUEST_ID_HEADER: "inner-request"})
            signals._on_task_prerun(task=outer, args=("outer-job", 2))
            signals._on_task_prerun(task=inner, args=("inner-job", 3))
            assert request_context.current_request_id() == "inner-request"
            assert request_context.current_job_context() == ("inner-job", "3")

            signals._on_task_postrun(task=inner)
            assert request_context.current_request_id() == "outer-request"
            assert request_context.current_job_context() == ("outer-job", "2")

            signals._on_task_postrun(task=outer)
            assert request_context.current_request_id() == "http-request"
            assert request_context.current_job_context() == ("http-job", "1")

            # A subsequent task must not inherit either completed task's state.
            signals._on_task_prerun(task=inner, args=("next-job", 4))
            signals._on_task_postrun(task=inner)
            assert request_context.current_request_id() == "http-request"
            assert request_context.current_job_context() == ("http-job", "1")
        finally:
            request_context.reset_request_id(request_token)
            request_context.reset_job_context(job_token)

    def test_prerun_binds_the_published_request_id_and_the_job(self):
        from astinuslab.jobs import celery_worker_signals as signals

        class _Request:
            id = "celery-1"
            _headers = {request_context.REQUEST_ID_HEADER: "ui-request-abc"}

            def get(self, key, default=None):
                return self._headers.get(key, default)

        class _Task:
            request = _Request()

        try:
            signals._on_task_prerun(task=_Task(), args=("job-abc", 42), kwargs={})

            assert request_context.current_request_id() == "ui-request-abc"
            record = _record_through_filter()
            assert record.job_id == "job-abc"
            assert record.action_id == "42"
        finally:
            signals._on_task_postrun(task=_Task())

    def test_postrun_clears_the_context_for_the_next_task(self):
        from astinuslab.jobs import celery_worker_signals as signals

        class _Task:
            request = None

        signals._on_task_prerun(task=_Task(), args=("job-abc", 42), kwargs={})
        signals._on_task_postrun(task=_Task())

        assert request_context.current_request_id() == "-"
        assert _record_through_filter().job_id == "-"

    def test_postrun_restores_a_surrounding_request(self):
        """Under task_always_eager the task runs inside the request that sent it."""
        from astinuslab.jobs import celery_worker_signals as signals

        class _Task:
            request = None

        outer_id, token = request_context.bind_request_id("ui-request-outer")
        try:
            signals._on_task_prerun(task=_Task(), args=("job-abc", 42), kwargs={})
            assert request_context.current_request_id() != outer_id

            signals._on_task_postrun(task=_Task())

            assert request_context.current_request_id() == outer_id
            assert _record_through_filter().job_id == "-"
        finally:
            request_context.reset_request_id(token)

    def test_the_request_completion_record_keeps_its_id_after_an_eager_task(self):
        """The middleware's own line is filtered too; it must not read back as '-'."""
        import logging as logging_module

        from astinuslab.jobs import celery_worker_signals as signals

        class _Task:
            request = None

        outer_id, token = request_context.bind_request_id("ui-request-outer")
        try:
            signals._on_task_prerun(task=_Task(), args=("job-abc", 42), kwargs={})
            signals._on_task_postrun(task=_Task())

            plain = logging_module.LogRecord("astinuslab.main", 20, __file__, 1, "http_request", None, None)
            request_context.RequestContextFilter().filter(plain)
            record = cast(request_context._RequestLogRecord, plain)

            assert record.request_id == outer_id
        finally:
            request_context.reset_request_id(token)

    def test_a_task_published_without_a_request_id_gets_a_generated_one(self):
        """Beat publishes outside any request; its records still need a key."""
        from astinuslab.jobs import celery_worker_signals as signals

        class _Task:
            request = None

        try:
            signals._on_task_prerun(task=_Task(), args=(), kwargs={})

            assert request_context.current_request_id() != "-"
        finally:
            signals._on_task_postrun(task=_Task())
