"""Operational metrics aggregated from rows the database already holds.

``/ready`` answers whether the platform can work. This answers how well it has
been working: how long each kind of action takes on this host, how many jobs
reached each terminal status, and whether work is piling up in front of a
worker that runs one action at a time.

Deliberately not a metrics backend. There is no collector, no exporter, no new
dependency and no new table — ``Job`` and ``HostAction`` already carry
``created_at``/``started_at``/``finished_at`` and a status, and nothing was
reading them back. A single-host forensic appliance does not need a time-series
database to answer these questions; it needs the questions asked.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import HostAction, Job

DEFAULT_WINDOW_HOURS = 24
MAX_WINDOW_HOURS = 24 * 90
# Statuses that mean the action ran to completion, so its elapsed time measures
# the work rather than how quickly it gave up.
_COMPLETED_STATUSES = ("completed", "succeeded")
_FAILED_STATUSES = ("failed", "cancelled")


def _percentile(sorted_values: list[float], fraction: float) -> float:
    """Nearest-rank percentile.

    Not interpolated: with the handful of samples a single host produces in a
    day, an interpolated p95 invents a number that no capture actually took.
    """
    if not sorted_values:
        raise ValueError("percentile of an empty sample")
    rank = max(1, min(len(sorted_values), ceil(fraction * len(sorted_values))))
    return sorted_values[rank - 1]


def _elapsed_seconds(action: HostAction) -> float | None:
    if action.started_at is None or action.finished_at is None:
        return None
    return max((action.finished_at - action.started_at).total_seconds(), 0.0)


def _action_rows(session: Session, since: datetime) -> list[dict[str, Any]]:
    actions = session.scalars(select(HostAction).where(HostAction.created_at >= since)).all()

    durations: dict[str, list[float]] = defaultdict(list)
    completed: dict[str, int] = defaultdict(int)
    failed: dict[str, int] = defaultdict(int)
    seen: set[str] = set()

    for action in actions:
        seen.add(action.action_type)
        if action.status in _COMPLETED_STATUSES:
            completed[action.action_type] += 1
            elapsed = _elapsed_seconds(action)
            if elapsed is not None:
                durations[action.action_type].append(elapsed)
        elif action.status in _FAILED_STATUSES:
            failed[action.action_type] += 1

    rows: list[dict[str, Any]] = []
    for action_type in sorted(seen):
        sample = sorted(durations[action_type])
        rows.append(
            {
                "action_type": action_type,
                "completed": completed[action_type],
                "failed": failed[action_type],
                "p50_seconds": round(_percentile(sample, 0.50), 3) if sample else None,
                "p95_seconds": round(_percentile(sample, 0.95), 3) if sample else None,
                "max_seconds": round(sample[-1], 3) if sample else None,
            }
        )
    return rows


def _queue_row(session: Session, now: datetime) -> dict[str, Any]:
    """Backlog in front of the workers.

    Not limited to the window: an action queued a week ago is exactly the thing
    this exists to surface, and a 24-hour cutoff would hide it.
    """
    queued = session.scalars(select(HostAction).where(HostAction.status == "queued")).all()
    running = session.scalar(select(func.count()).select_from(HostAction).where(HostAction.status == "running")) or 0

    oldest: float | None = None
    if queued:
        earliest = min(action.created_at for action in queued)
        oldest = max((now.replace(tzinfo=None) - earliest).total_seconds(), 0.0)

    return {
        "queued": len(queued),
        "running": int(running),
        "oldest_queued_age_seconds": round(oldest, 3) if oldest is not None else None,
        # A dispatch that has been attempted repeatedly is a redelivery loop,
        # which the queue depth alone does not distinguish from a long capture.
        "max_dispatch_attempts": max((action.dispatch_attempts for action in queued), default=0),
    }


def _job_rows(session: Session, since: datetime) -> dict[str, int]:
    rows = session.execute(select(Job.status, func.count()).where(Job.created_at >= since).group_by(Job.status)).all()
    return {str(status): int(count) for status, count in rows}


def collect_metrics(session: Session, *, window_hours: int = DEFAULT_WINDOW_HOURS) -> dict[str, Any]:
    """Aggregate the window's activity into one read-only report."""
    window_hours = max(1, min(int(window_hours), MAX_WINDOW_HOURS))
    now = datetime.now(UTC)
    since = (now - timedelta(hours=window_hours)).replace(tzinfo=None)

    return {
        "generated_at": now.isoformat(),
        "window_hours": window_hours,
        "actions": _action_rows(session, since),
        "jobs": _job_rows(session, since),
        "queue": _queue_row(session, now),
    }
