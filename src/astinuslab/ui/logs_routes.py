from __future__ import annotations

import logging
import os
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

logger = logging.getLogger(__name__)

from ..core.log_layout import available_log_names, open_log
from ..core.logging_config import LOG_FILE_NAMES as _LOG_NAMES

_VALID_LOG_LEVELS = frozenset({"all", "debug", "info", "warning", "error", "critical"})
_MAX_LOG_SCAN_BYTES = 10 * 1024 * 1024  # 10 MB - stop scanning backward beyond this
_LOG_LINE_LIMITS = {"all": 1000, "filtered": 2000}

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from starlette.types import Receive, Scope, Send

from ..core.config import settings as _default_settings
from ..db import session_scope as _default_session_scope
from .jobs_helpers import (
    parse_line_level as _parse_line_level,
)

router = APIRouter()

# Test compatibility: route modules historically shared astinuslab.ui globals.


class _SettingsProxy:
    def __getattr__(self, name):
        import astinuslab.ui as ui_mod

        current = getattr(ui_mod, "settings", _default_settings)
        if current is self:
            current = _default_settings
        try:
            return getattr(current, name)
        except (AttributeError, TypeError):
            return getattr(_default_settings, name)

    def model_dump(self, *args, **kwargs):
        import astinuslab.ui as ui_mod

        current = getattr(ui_mod, "settings", _default_settings)
        if current is self:
            current = _default_settings
        return current.model_dump(*args, **kwargs)


class _SessionScopeProxy:
    def __call__(self, *args, **kwargs):
        import astinuslab.ui as ui_mod

        current = getattr(ui_mod, "session_scope", _default_session_scope)
        if current is self:
            current = _default_session_scope
        return current(*args, **kwargs)


settings = _SettingsProxy()
session_scope = _SessionScopeProxy()


from .job_context import (
    _base_ctx,
    templates,
)


def _available_logs() -> list[dict]:
    layout_error = None
    try:
        names = available_log_names(settings.storage_dir)
    except (OSError, ValueError):
        names = _LOG_NAMES
        layout_error = "Service log layout is unavailable; operator maintenance is required."
    result = []
    for name in names:
        info = None
        try:
            with open_log(settings.storage_dir, name) as stream:
                info = os.fstat(stream.fileno())
        except (OSError, ValueError):
            pass
        result.append(
            {
                "name": name,
                "layout_error": layout_error,
                "exists": info is not None,
                "size_kb": (info.st_size / 1024) if info else 0.0,
                "modified": datetime.fromtimestamp(info.st_mtime, tz=UTC) if info else None,
            }
        )
    return result


def _tail_filtered(
    path: Path | BinaryIO,
    line_count: int = 200,
    level: str = "all",
    q: str = "",
    chunk_size: int = 65536,
    max_scan_bytes: int = _MAX_LOG_SCAN_BYTES,
) -> list[dict]:
    """Return up to *line_count* lines from the tail of *path*, filtered by level and substring.

    Reads backward in *chunk_size* blocks until *line_count* matching lines are
    collected or *max_scan_bytes* have been read. Returns lines in chronological
    order as a list of {"level": str, "text": str} dicts, where level is lowercase
    (debug / info / warning / error / critical / other).
    When *level* is "all", OTHER lines (Celery banner, uvicorn startup) are included.
    When a specific level is requested, OTHER lines are excluded.
    """
    if isinstance(path, Path) and not path.exists():
        return []
    level_key = level.upper() if level != "all" else "ALL"
    q_lower = q.strip().lower()
    with path.open("rb") if isinstance(path, Path) else nullcontext(path) as fh:
        fh.seek(0, 2)
        file_size = fh.tell()
        if file_size == 0:
            return []
        buf = b""
        pos = file_size
        bytes_read = 0
        while pos > 0 and bytes_read < max_scan_bytes:
            read_size = min(chunk_size, pos, max_scan_bytes - bytes_read)
            pos -= read_size
            fh.seek(pos)
            buf = fh.read(read_size) + buf
            bytes_read += read_size
            # Early exit check: count matching lines from end of buffer so far.
            # Skip on the very first (possibly partial) chunk to avoid off-by-one.
            if bytes_read >= chunk_size:
                candidate_lines = buf.decode("utf-8", errors="replace").splitlines()
                if pos > 0 and candidate_lines:
                    candidate_lines = candidate_lines[1:]
                matched = 0
                for line in reversed(candidate_lines):
                    if not line:
                        continue
                    ll = _parse_line_level(line)
                    if level_key != "ALL" and ll != level_key:
                        continue
                    if q_lower and q_lower not in line.lower():
                        continue
                    matched += 1
                    if matched >= line_count:
                        break
                if matched >= line_count:
                    break
    all_lines = buf.decode("utf-8", errors="replace").splitlines()
    if pos > 0 and all_lines:
        all_lines = all_lines[1:]
    result: list[dict] = []
    for line in reversed(all_lines):
        if not line:
            continue
        ll = _parse_line_level(line)
        if level_key != "ALL" and ll != level_key:
            continue
        if q_lower and q_lower not in line.lower():
            continue
        result.append({"level": ll.lower(), "text": line})
        if len(result) >= line_count:
            break
    result.reverse()
    return result


def _tail_log(name: str, **kwargs: Any) -> list[dict[str, Any]]:
    try:
        with open_log(settings.storage_dir, name) as stream:
            return _tail_filtered(stream, **kwargs)
    except (OSError, ValueError):
        return []


class _OpenedLogResponse(FileResponse):
    """Retain FileResponse's range/HEAD contract while pinning its source inode."""

    def __init__(self, opened: AbstractContextManager[BinaryIO], stream: BinaryIO, filename: str) -> None:
        self.opened = opened
        super().__init__(
            path=f"/proc/self/fd/{stream.fileno()}",
            filename=filename,
            media_type="text/plain",
            stat_result=os.fstat(stream.fileno()),
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # A server's path-send extension may reopen after this response closes.
        scope = {
            **scope,
            "extensions": {k: v for k, v in scope.get("extensions", {}).items() if k != "http.response.pathsend"},
        }
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.opened.__exit__(None, None, None)


@router.get("/logs", response_class=HTMLResponse)
def admin_logs_page(
    request: Request,
    log: str = "api.log",
    lines: int = 200,
    level: str = "all",
    q: str = "",
    search: str = "",
):
    logs = _available_logs()
    valid_names = {entry["name"] for entry in logs}
    selected = log if log in valid_names else "api.log"
    selected_level = level if level in _VALID_LOG_LEVELS else "all"
    q = q[:200]  # cap query length
    max_lines = _LOG_LINE_LIMITS["all" if selected_level == "all" else "filtered"]
    line_count = min(max(lines, 50), max_lines)
    selected_entry = next((entry for entry in logs if entry["name"] == selected), logs[0])
    structured = (
        _tail_log(selected, line_count=line_count, level=selected_level, q=q) if selected_entry["exists"] else []
    )
    level_counts: dict[str, int] = {"debug": 0, "info": 0, "warning": 0, "error": 0, "critical": 0, "other": 0}
    for item in structured:
        lvl = item["level"]
        level_counts[lvl] = level_counts.get(lvl, 0) + 1
    return templates.TemplateResponse(
        request,
        "admin_logs.html",
        {
            "logs": logs,
            "log_metadata": selected_entry,
            "line_limits": _LOG_LINE_LIMITS,
            "selected_log": selected,
            "line_count": line_count,
            "selected_level": selected_level,
            "q": q,
            "search": search[:200],
            "max_lines": max_lines,
            "lines": structured,
            "level_counts": level_counts,
            **_base_ctx(request),
        },
    )


@router.get("/logs/{filename}/download")
def admin_logs_download(filename: str):
    opened = open_log(settings.storage_dir, filename)
    try:
        stream = opened.__enter__()
    except (OSError, ValueError) as exc:
        raise HTTPException(404, "Log not found") from exc
    try:
        return _OpenedLogResponse(opened, stream, filename)
    except BaseException:
        opened.__exit__(None, None, None)
        raise


@router.get("/logs/fragment", response_class=HTMLResponse)
def admin_logs_fragment(
    request: Request,
    log: str = "api.log",
    lines: int = 200,
    level: str = "all",
    q: str = "",
):
    """HTMX auto-refresh target - returns only the log content partial."""
    logs = _available_logs()
    valid_names = {entry["name"] for entry in logs}
    selected = log if log in valid_names else "api.log"
    selected_level = level if level in _VALID_LOG_LEVELS else "all"
    q = q[:200]
    max_lines = _LOG_LINE_LIMITS["all" if selected_level == "all" else "filtered"]
    line_count = min(max(lines, 50), max_lines)
    selected_entry = next((entry for entry in logs if entry["name"] == selected), logs[0])
    structured = (
        _tail_log(selected, line_count=line_count, level=selected_level, q=q) if selected_entry["exists"] else []
    )
    level_counts: dict[str, int] = {"debug": 0, "info": 0, "warning": 0, "error": 0, "critical": 0, "other": 0}
    for item in structured:
        lvl = item["level"]
        level_counts[lvl] = level_counts.get(lvl, 0) + 1
    return templates.TemplateResponse(
        request,
        "partials/log_content.html",
        {
            "lines": structured,
            "log_metadata": selected_entry,
            "refresh_metadata": True,
            "level_counts": level_counts,
            "line_count": line_count,
            "selected_log": selected,
            "selected_level": selected_level,
            "q": q,
            **_base_ctx(request),
        },
    )
