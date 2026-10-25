"""Pure helpers extracted from ui/jobs.py.

Side-effect-free utilities only — no DB sessions, no filesystem reads, no
network calls. Each function takes primitives or DTOs and returns
primitives or DTOs. Keeps ui/jobs.py focused on FastAPI route handlers.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import TYPE_CHECKING

import yaml
from fastapi.responses import RedirectResponse

if TYPE_CHECKING:
    from ..api.schemas import HostActionOut


_LEVEL_PATTERNS = (
    re.compile(r"\[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\]"),  # AstinusLab text format
    re.compile(r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL):\s"),  # uvicorn bare
    re.compile(r": (DEBUG|INFO|WARNING|ERROR|CRITICAL)/"),  # Celery format
    # AstinusLab JSON format (log_format = "json"). Last, so a text line that
    # merely mentions a level name is still classified by its own bracket.
    re.compile(r'^\{.*"level"\s*:\s*"(DEBUG|INFO|WARNING|ERROR|CRITICAL)"'),
)


def has_active_vol3(actions: list[HostActionOut]) -> bool:
    return any(a.action_type == "run_vol3_analysis" and a.status in ("queued", "running") for a in actions)


def _redirect_with_banner(url: str, message: str, message_type: str) -> RedirectResponse:
    """Redirect to `url` carrying a banner message and its severity.

    Any pre-existing `message`/`message_type` pair in `url` is dropped so callers
    that build a URL with one already attached cannot produce a duplicated query
    key, where the renderer's `query_params.get()` would silently pick the first
    (stale) value and mislabel the banner.
    """
    parsed = urllib.parse.urlsplit(url)
    params = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if key not in {"message", "message_type"}
    ]
    params += [("message", message), ("message_type", message_type)]
    target = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(params), parsed.fragment)
    )
    return RedirectResponse(url=target, status_code=303)


def redirect_with_message(url: str, message: str) -> RedirectResponse:
    """Redirect back to `url` with a FAILURE banner.

    This helper is error-only on purpose: every mutation handler that reports a
    problem routes through it, and the renderer defaults an unlabelled `?message=`
    to `error`.  Use `redirect_with_success` for the happy path.
    """
    return _redirect_with_banner(url, message, "error")


def redirect_with_success(url: str, message: str) -> RedirectResponse:
    """Redirect back to `url` with a SUCCESS banner (green, `role="status"`)."""
    return _redirect_with_banner(url, message, "success")


def safe_next(next: str) -> str:
    """Return next only if it is a safe relative path; fall back to '/'.

    URL-decodes first to catch encoded bypass attempts (/%2F, /%5C), then
    rejects protocol-relative (//evil.com) and backslash-normalised (/\\evil.com)
    paths that some browsers interpret as external redirects.
    """
    decoded = urllib.parse.unquote(next)
    if decoded.startswith("/") and not decoded.startswith("//") and not decoded.startswith("/\\"):
        return decoded
    return "/"


def translate_scenarios(scenarios: list[dict], t: dict) -> list[dict]:
    result = []
    for s in scenarios:
        key = f"scenario_desc_{s['id']}"
        if key in t:
            s = {**s, "description": t[key]}
        result.append(s)
    return result


def runtime_action_status(actions: list[HostActionOut]) -> dict[str, str]:
    status_by_role: dict[str, str] = {}
    for action in reversed(actions):
        if action.action_type != "control_runtime" or action.status not in {"queued", "running"}:
            continue
        try:
            parsed_role = json.loads(action.payload or "{}").get("role")
        except (json.JSONDecodeError, TypeError):
            parsed_role = None
        if parsed_role == "target" and "target" not in status_by_role:
            status_by_role["target"] = action.status
        elif parsed_role == "attacker" and "attacker" not in status_by_role:
            status_by_role["attacker"] = action.status
    return status_by_role


def human_size(n: int) -> str:
    val: float = n
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024:
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024
    return f"{val:.1f} TB"


def parse_line_level(line: str) -> str:
    """Extract the log level from a single log line.

    Recognises three formats emitted by this codebase:
      AstinusLab - 2026-05-07 07:56:28 [DEBUG] astinuslab.foo: msg
      uvicorn bare - INFO:     Started server process [12345]
      Celery       - [2026-05-07 07:54:57,882: DEBUG/MainProcess] ...
    Returns one of DEBUG/INFO/WARNING/ERROR/CRITICAL or "OTHER".
    """
    for pattern in _LEVEL_PATTERNS:
        m = pattern.search(line)
        if m:
            return m.group(1)
    return "OTHER"


def default_workload_yaml() -> str:
    item = {
        "id": "new-workload",
        "enabled": True,
        "display_name": "New workload",
        "tags": ["web"],
        "install_script": "guest/workloads/new-workload/install.sh",
        "services": [
            {
                "name": "ServiceName",
                "version": "1.0",
                "port": 80,
                "url_path": "/",
            }
        ],
        "cve_ids": [],
        "cve_context": "",
        "network_isolation": True,
    }
    return yaml.safe_dump(item, sort_keys=False, allow_unicode=False).strip()
