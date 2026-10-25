from __future__ import annotations

import logging
import re

from astinuslab.core.request_context import (
    RequestContextFilter,
    bind_request_id,
    current_request_id,
    normalize_request_id,
    reset_request_id,
)


def test_normalize_request_id_reuses_valid_bounded_value():
    assert normalize_request_id("client.request_id-123") == "client.request_id-123"


def test_normalize_request_id_replaces_missing_short_and_unsafe_values():
    generated = {
        normalize_request_id(None),
        normalize_request_id("short"),
        normalize_request_id("unsafe value"),
        normalize_request_id("line\nbreak"),
        normalize_request_id("x" * 129),
    }

    assert len(generated) == 5
    assert all(re.fullmatch(r"[a-f0-9]{32}", value) for value in generated)


def test_bind_and_reset_request_id_do_not_leak_context():
    assert current_request_id() == "-"

    request_id, token = bind_request_id("client-request-123")
    try:
        assert request_id == "client-request-123"
        assert current_request_id() == "client-request-123"
    finally:
        reset_request_id(token)

    assert current_request_id() == "-"


def test_logging_filter_injects_current_request_id():
    request_id, token = bind_request_id("client-request-123")
    try:
        record = logging.LogRecord("test", logging.INFO, __file__, 1, "message", (), None)
        assert RequestContextFilter().filter(record) is True
        assert getattr(record, "request_id", None) == request_id
    finally:
        reset_request_id(token)


def test_configured_handler_formats_request_id():
    from astinuslab.core.logging_config import configure_logging

    configure_logging("INFO")
    root = logging.getLogger()
    handler = next(handler for handler in root.handlers if handler.name == "astinuslab")
    request_filter = next(log_filter for log_filter in handler.filters if isinstance(log_filter, RequestContextFilter))
    request_id, token = bind_request_id("client-request-123")
    try:
        record = logging.LogRecord("test", logging.INFO, __file__, 1, "message", (), None)
        assert request_filter.filter(record) is True
        rendered = handler.format(record)
    finally:
        reset_request_id(token)

    assert "request_id=client-request-123" in rendered
