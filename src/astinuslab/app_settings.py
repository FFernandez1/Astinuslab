"""Database-backed application settings (currently: retention windows)."""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from typing import Any

from sqlalchemy.orm import Session

from .db import session_scope
from .db.repositories import AppSettingsRepository
from .db.transactions import reserve_sqlite_write
from .storage.retention import DUMPS_RETENTION_DAYS, LOGS_RETENTION_DAYS

logger = logging.getLogger(__name__)

_SETTINGS_KEY = "app_settings"
_RETENTION_RANGE = range(1, 3651)
# Free-space floor (GiB) that blocks new jobs. 0 disables the guardrail.
_MIN_FREE_GIB_RANGE = range(0, 1_000_001)


def _defaults() -> dict[str, Any]:
    return {
        "retention": {
            "dumps_days": DUMPS_RETENTION_DAYS,
            "logs_days": LOGS_RETENTION_DAYS,
            "auto_enabled": False,
        },
        "disk_guardrail": {
            "min_free_gib": 0,
        },
    }


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _parse_retention_days(value: Any, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if parsed not in _RETENTION_RANGE:
        raise ValueError(f"{field} out of range (1..3650)")
    return parsed


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _normalize_retention(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "dumps_days": _parse_retention_days(raw.get("dumps_days", DUMPS_RETENTION_DAYS), "dumps_days"),
        "logs_days": _parse_retention_days(raw.get("logs_days", LOGS_RETENTION_DAYS), "logs_days"),
        "auto_enabled": _parse_bool(raw.get("auto_enabled", False)),
    }
    # Optional last-run audit metadata, written by the retention runner.
    if raw.get("last_run_at"):
        out["last_run_at"] = str(raw["last_run_at"])
        out["last_dumps_deleted"] = int(raw.get("last_dumps_deleted") or 0)
        out["last_logs_deleted"] = int(raw.get("last_logs_deleted") or 0)
    return out


def _parse_min_free_gib(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("min_free_gib must be an integer") from exc
    if parsed not in _MIN_FREE_GIB_RANGE:
        raise ValueError("min_free_gib out of range (0..1000000)")
    return parsed


def _normalize_disk_guardrail(raw: dict[str, Any]) -> dict[str, Any]:
    return {"min_free_gib": _parse_min_free_gib(raw.get("min_free_gib", 0))}


def _normalize_settings(raw: dict[str, Any]) -> dict[str, Any]:
    merged = _deep_merge(_defaults(), raw)
    retention = merged.get("retention")
    if not isinstance(retention, dict):
        raise ValueError("retention must be an object")
    merged["retention"] = _normalize_retention(retention)
    guardrail = merged.get("disk_guardrail")
    if not isinstance(guardrail, dict):
        raise ValueError("disk_guardrail must be an object")
    merged["disk_guardrail"] = _normalize_disk_guardrail(guardrail)
    return merged


def _load_app_settings(session: Session) -> dict[str, Any]:
    value = AppSettingsRepository(session).get(_SETTINGS_KEY)
    if value is None:
        return _defaults()
    try:
        loaded = json.loads(value)
        if not isinstance(loaded, dict):
            raise ValueError("app_settings root must be an object")
        return _normalize_settings(loaded)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("stored app_settings unreadable, using defaults: %s", exc)
        return _defaults()


def load_app_settings() -> dict[str, Any]:
    with session_scope() as session:
        return _load_app_settings(session)


def save_app_settings(patch: dict[str, Any]) -> dict[str, Any]:
    with session_scope() as session:
        reserve_sqlite_write(session)
        current = _load_app_settings(session)
        merged = _normalize_settings(_deep_merge(current, patch))
        serialized = json.dumps(merged, indent=2, sort_keys=True)
        AppSettingsRepository(session).set(_SETTINGS_KEY, serialized)
    return merged
