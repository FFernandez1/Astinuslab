"""Structured log output.

The text format is a human contract the Logs page parses with a regular
expression. JSON is the machine contract: the same fields, named, so an
aggregate question ("p95 acquisition duration", "failures per stage") does not
need a bespoke parser written against prose.
"""

from __future__ import annotations

import json
import logging

import pytest

from astinuslab.core import logging_config, request_context

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clean_context():
    yield
    request_context.reset_job_context(None)


def _render(formatter, message="http_request method=GET path=/jobs status=200", **kwargs):
    record = logging.LogRecord("astinuslab.main", logging.INFO, __file__, 1, message, None, None, **kwargs)
    request_context.RequestContextFilter().filter(record)
    return formatter.format(record)


class TestFormatSelection:
    def test_text_is_the_default(self):
        assert isinstance(logging_config.build_formatter("text"), logging.Formatter)
        assert "{" not in _render(logging_config.build_formatter("text"))[:1]

    def test_an_unknown_format_falls_back_to_text_without_raising(self):
        """A typo in configuration must not leave the service with no logging."""
        rendered = _render(logging_config.build_formatter("yaml"))

        assert "[INFO]" in rendered

    def test_the_setting_selects_the_formatter(self, tmp_path, monkeypatch, restore_root_handlers):
        from types import SimpleNamespace

        monkeypatch.setattr(
            logging_config,
            "settings",
            SimpleNamespace(storage_dir=tmp_path, log_to_file=False, log_format="json"),
        )
        logging_config.configure_logging("info")

        handler = next(
            h for h in logging.getLogger().handlers if getattr(h, "name", None) == logging_config._HANDLER_MARKER
        )
        assert isinstance(handler.formatter, logging_config.JsonFormatter)


class TestJsonRecords:
    def test_a_record_is_one_json_object_per_line(self):
        rendered = _render(logging_config.build_formatter("json"))

        assert "\n" not in rendered
        payload = json.loads(rendered)
        assert payload["level"] == "INFO"
        assert payload["logger"] == "astinuslab.main"
        assert payload["message"] == "http_request method=GET path=/jobs status=200"

    def test_it_carries_the_correlation_fields(self):
        request_context.bind_job_context("job-abc", 42)
        request_id, token = request_context.bind_request_id("ui-request-abc")
        try:
            payload = json.loads(_render(logging_config.build_formatter("json")))
        finally:
            request_context.reset_request_id(token)

        assert payload["request_id"] == request_id
        assert payload["job_id"] == "job-abc"
        assert payload["action_id"] == "42"

    def test_absent_correlation_fields_are_omitted_rather_than_dashed(self):
        payload = json.loads(_render(logging_config.build_formatter("json")))

        assert "job_id" not in payload
        assert "action_id" not in payload

    def test_the_timestamp_is_iso_8601_utc(self):
        from datetime import datetime

        payload = json.loads(_render(logging_config.build_formatter("json")))

        parsed = datetime.fromisoformat(payload["timestamp"])
        assert parsed.tzinfo is not None

    def test_an_exception_is_included_as_a_field_not_extra_lines(self):
        try:
            raise RuntimeError("guest agent unreachable")
        except RuntimeError:
            import sys

            record = logging.LogRecord(
                "astinuslab.main", logging.ERROR, __file__, 1, "task_failed", None, sys.exc_info()
            )
        request_context.RequestContextFilter().filter(record)

        rendered = logging_config.build_formatter("json").format(record)

        assert "\n" not in rendered
        payload = json.loads(rendered)
        assert "RuntimeError: guest agent unreachable" in payload["exception"]

    def test_a_message_with_quotes_and_newlines_stays_one_valid_object(self):
        rendered = _render(logging_config.build_formatter("json"), message='say "hi"\nand a second line')

        assert "\n" not in rendered
        assert json.loads(rendered)["message"] == 'say "hi"\nand a second line'

    def test_unserialisable_extras_do_not_break_the_record(self):
        record = logging.LogRecord("astinuslab.main", logging.INFO, __file__, 1, "msg", None, None)
        request_context.RequestContextFilter().filter(record)
        record.request_id = object()

        payload = json.loads(logging_config.build_formatter("json").format(record))

        assert payload["message"] == "msg"


@pytest.fixture()
def restore_root_handlers():
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in saved_handlers:
            handler.close()
    root.handlers = saved_handlers
    root.setLevel(saved_level)


class TestLogsPageParsesBothFormats:
    """The Logs page filters by level server-side; both formats must be readable."""

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("2026-09-24 12:00:00 [WARNING] request_id=- astinuslab.jobs: retry", "WARNING"),
            ("INFO:     Started server process [1]", "INFO"),
            ("[2026-09-24 12:00:00,123: ERROR/MainProcess] boom", "ERROR"),
            ('{"timestamp":"2026-09-24T12:00:00+00:00","level":"ERROR","logger":"a","message":"b"}', "ERROR"),
            ('{"level": "DEBUG", "message": "spaced separators"}', "DEBUG"),
            ("a line from some other producer", "OTHER"),
        ],
    )
    def test_level_is_recovered_from_every_emitted_format(self, line, expected):
        from astinuslab.ui.jobs_helpers import parse_line_level

        assert parse_line_level(line) == expected

    def test_a_text_line_carrying_job_context_still_parses(self):
        from astinuslab.ui.jobs_helpers import parse_line_level

        line = "2026-09-24 12:00:00 [INFO] request_id=abc astinuslab.jobs: started job_id=job-1 action_id=4"

        assert parse_line_level(line) == "INFO"

    def test_the_browser_renderer_handles_json_lines(self):
        """logs.js parses each line for its timestamp and message; JSON needs a branch."""
        from astinuslab.web.resources import STATIC_DIR

        source = (STATIC_DIR / "logs.js").read_text(encoding="utf-8")

        assert "JSON.parse" in source
