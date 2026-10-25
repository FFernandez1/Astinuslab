"""Operator-configured disk admission policy shared by API and UI Jobs."""

from __future__ import annotations

import shutil

from fastapi import HTTPException


def enforce_disk_guardrail() -> None:
    """Block new Jobs below the configured floor, preserving probe-failure policy."""
    from ..app_settings import load_app_settings
    from ..core.config import settings

    min_gib = int(load_app_settings().get("disk_guardrail", {}).get("min_free_gib", 0) or 0)
    if min_gib <= 0:
        return
    try:
        free_gib = shutil.disk_usage(settings.storage_dir).free / (1024**3)
    except OSError:
        return
    if free_gib < min_gib:
        raise HTTPException(
            status_code=507,
            detail=(
                f"Disk guardrail: {free_gib:.1f} GiB free is below the {min_gib} GiB minimum. "
                "Free space or lower the threshold in Settings before starting new jobs."
            ),
        )
