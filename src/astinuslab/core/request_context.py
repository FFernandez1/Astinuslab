from __future__ import annotations

import logging
import re
import secrets
from contextvars import ContextVar, Token
from typing import cast

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._-]{8,128}\Z")
_REQUEST_ID: ContextVar[str] = ContextVar("astinuslab_request_id", default="-")

# The worker's half of the same correlation. A Job and its HostAction are what
# an operator actually follows across a capture, and prose (``[job-abc] ...``)
# inside a message cannot be filtered on. Same mechanism, one level down.
_JOB_ID: ContextVar[str] = ContextVar("astinuslab_job_id", default="-")
_ACTION_ID: ContextVar[str] = ContextVar("astinuslab_action_id", default="-")

# Identifiers reach the formatter and are written to a log line. A newline in
# one would forge a second record, so everything is flattened to a safe token.
_UNSAFE_CONTEXT_RE = re.compile(r"[^A-Za-z0-9._:-]")
_MAX_CONTEXT_LENGTH = 128

JobContextToken = tuple[Token[str], Token[str]]


class _RequestLogRecord(logging.LogRecord):
    request_id: str
    job_id: str
    action_id: str


def normalize_request_id(value: str | None) -> str:
    if value is not None and _REQUEST_ID_RE.fullmatch(value):
        return value
    return secrets.token_hex(16)


def bind_request_id(value: str | None) -> tuple[str, Token[str]]:
    request_id = normalize_request_id(value)
    return request_id, _REQUEST_ID.set(request_id)


def reset_request_id(token: Token[str]) -> None:
    _REQUEST_ID.reset(token)


def set_request_id(value: str) -> None:
    """Put back a previously observed id, without a token.

    A Celery worker thread is reused across tasks, so a token taken in one task
    is not valid in the next one's context; a value always is. HTTP keeps using
    ``bind_request_id``/``reset_request_id``, which nest properly.
    """
    _REQUEST_ID.set(value or "-")


def current_request_id() -> str:
    return _REQUEST_ID.get()


def _safe_context_value(value: object) -> str:
    if value is None:
        return "-"
    text = _UNSAFE_CONTEXT_RE.sub("_", str(value))[:_MAX_CONTEXT_LENGTH]
    return text or "-"


def bind_job_context(job_id: object, action_id: object = None) -> JobContextToken:
    """Attach a Job (and optionally its action) to every record from here on."""
    return _JOB_ID.set(_safe_context_value(job_id)), _ACTION_ID.set(_safe_context_value(action_id))


def reset_job_context(token: JobContextToken | None) -> None:
    """Restore the previous job context, or clear it when *token* is None."""
    if token is None:
        _JOB_ID.set("-")
        _ACTION_ID.set("-")
        return
    job_token, action_token = token
    _JOB_ID.reset(job_token)
    _ACTION_ID.reset(action_token)


def current_job_context() -> tuple[str, str]:
    return _JOB_ID.get(), _ACTION_ID.get()


class RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        typed = cast(_RequestLogRecord, record)
        typed.request_id = current_request_id()
        typed.job_id, typed.action_id = current_job_context()
        return True
