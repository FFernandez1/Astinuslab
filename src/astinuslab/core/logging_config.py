from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import settings
from .log_layout import LOG_FILE_NAMES as LOG_FILE_NAMES
from .log_layout import SERVICE_LOG_FILES, log_path
from .request_context import RequestContextFilter

_HANDLER_MARKER = "astinuslab"
_FILE_HANDLER_MARKER = "astinuslab-file"

_TEXT_FORMAT = "%(asctime)s [%(levelname)s] request_id=%(request_id)s %(name)s: %(message)s"
_TEXT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
# Context that is absent on most records. Appending these pairs only when they
# carry a value keeps an ordinary API line the same length it has always been.
_CONTEXT_FIELDS = ("job_id", "action_id")

# The runtime services and the file each one owns under ``storage/logs``.
# astinuslab.sh redirects each process's stdout to the same names, so the
# development launcher and an installed deployment present one set of logs
# rather than two. This mapping is the single source for the Logs page, the
# download allowlist and the "clear active logs" control.
# Size bound per file and how many rotations to keep. systemd deployments send
# the same records to the journal, which has its own bound; this one exists
# because storage/logs shares a filesystem with acquired evidence, and a log
# that fills it during an acquisition costs evidence, not convenience.
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 5


class ContextFormatter(logging.Formatter):
    """The human format, with the job pairs appended when a record has them.

    ``request_id=`` is always present because every record has one, real or
    ``-``. ``job_id``/``action_id`` are the worker's, so most API records would
    carry two useless placeholders; they are appended only when set.
    """

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        pairs = " ".join(
            f"{field}={value}" for field in _CONTEXT_FIELDS if (value := getattr(record, field, "-")) not in ("-", None)
        )
        return f"{rendered} {pairs}" if pairs else rendered


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with the correlation fields named.

    The text format has to be parsed back out of prose. This one does not: an
    aggregator, or the Logs page, reads the field it wants by name.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": str(getattr(record, "request_id", "-")),
        }
        for field in _CONTEXT_FIELDS:
            value = getattr(record, field, "-")
            if value not in ("-", None):
                payload[field] = str(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        # A record must never be lost to its own serialisation: default=str
        # renders whatever a caller attached, and ensure_ascii keeps the line
        # safe for any consumer that is not UTF-8 clean.
        return json.dumps(payload, default=str, ensure_ascii=True)


def build_formatter(log_format: str) -> logging.Formatter:
    """The formatter for *log_format*, falling back to text for anything else.

    A typo in configuration must not leave a service with no usable logging, so
    an unknown value degrades to the human format rather than raising.
    """
    if log_format.strip().lower() == "json":
        return JsonFormatter()
    return ContextFormatter(fmt=_TEXT_FORMAT, datefmt=_TEXT_DATE_FORMAT)


def log_file_for(service_identity: str) -> Path | None:
    """The log file *service_identity* should write, or None when disabled.

    Returns None unless ``log_to_file`` is set, so the default deployment keeps
    writing only to stdout and nothing starts competing with the launcher's own
    redirect.
    """
    try:
        name = SERVICE_LOG_FILES[service_identity]
    except KeyError:
        raise ValueError(f"unknown service identity: {service_identity!r}") from None
    if not getattr(settings, "log_to_file", False):
        return None
    try:
        return log_path(Path(settings.storage_dir), name)
    except (OSError, ValueError) as exc:
        logging.getLogger(__name__).warning("file logging disabled: %s", exc)
        return None


def _build_file_handler(
    log_file: Path,
    formatter: logging.Formatter,
    max_bytes: int,
    backup_count: int,
) -> logging.Handler | None:
    """A size-bounded handler for *log_file*, or None when it cannot be opened.

    A log destination that cannot be created must not take the process down with
    it: the stdout handler is the one the service manager actually reads.
    """
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
    except OSError as exc:
        logging.getLogger(__name__).warning("file logging disabled for %s: %s", log_file, exc)
        return None
    handler.setFormatter(formatter)
    handler.addFilter(RequestContextFilter())
    handler.name = _FILE_HANDLER_MARKER
    return handler


def configure_logging(
    level: str = "INFO",
    *,
    log_file: Path | None = None,
    max_bytes: int | None = None,
    backup_count: int | None = None,
) -> None:
    """Configure root logger for the AstinusLab backend.

    Safe to call from both the FastAPI process and the Celery worker.
    Idempotent: a second call replaces only the AstinusLab-owned handlers,
    leaving any other handlers (e.g. Celery's) untouched.

    With *log_file*, records also go to a size-bounded rotating file. Omitting
    it removes any file handler a previous call installed, so the argument
    always describes the resulting state rather than adding to it.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    formatter = build_formatter(getattr(settings, "log_format", "text"))

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(RequestContextFilter())
    handler.name = _HANDLER_MARKER

    root = logging.getLogger()
    root.setLevel(numeric_level)

    # Remove any previously-installed AstinusLab handler so that calling
    # configure_logging() more than once (e.g. in tests, or after Celery has
    # already attached its own root handler) does not duplicate output.
    owned = {_HANDLER_MARKER, _FILE_HANDLER_MARKER}
    for existing in [h for h in root.handlers if getattr(h, "name", None) in owned]:
        root.removeHandler(existing)
        existing.close()
    root.addHandler(handler)

    if log_file is not None:
        file_handler = _build_file_handler(
            Path(log_file),
            formatter,
            DEFAULT_LOG_MAX_BYTES if max_bytes is None else max_bytes,
            DEFAULT_LOG_BACKUP_COUNT if backup_count is None else backup_count,
        )
        if file_handler is not None:
            root.addHandler(file_handler)

    # Quieten noisy third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
