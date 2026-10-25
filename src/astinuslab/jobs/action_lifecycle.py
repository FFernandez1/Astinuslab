"""Single-host durable task claims and process-identity leases.

A broker acknowledgement is not an execution claim. Claims and compensation use
conditional database writes, so late/duplicate deliveries cannot repeat work.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path

from sqlalchemy import select, update

from ..db.models import HostAction

logger = logging.getLogger(__name__)
LEASE_SECONDS = 90
ANALYSIS_ACTIONS = {"run_vol3_plugin"}


def process_owner() -> str:
    pid = os.getpid()
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    start = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    return f"{boot}:{pid}:{start}"


def owner_alive(owner: str | None) -> bool:
    if owner is None:  # A pre-migration action has no live claim lease.
        return False
    try:
        boot, pid, start = owner.split(":")
        current_boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if boot != current_boot:
            return False
        fields = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[0] != "Z" and fields[19] == start
    except (FileNotFoundError, ProcessLookupError):
        return False
    except (OSError, ValueError, IndexError):
        # Lack of permission/identity certainty is not evidence the owner died.
        return True


def claim_action(session, job_id: str, action_id: int, *, owner: str, task_id: str | None = None) -> bool:
    now = datetime.now(UTC)
    return bool(
        session.execute(
            update(HostAction)
            .where(
                HostAction.id == action_id,
                HostAction.job_id == job_id,
                HostAction.status == "queued",
                HostAction.started_at.is_(None),
            )
            .values(
                status="running",
                started_at=now,
                owner_token=owner,
                lease_expires_at=now + timedelta(seconds=LEASE_SECONDS),
                celery_task_id=task_id,
            )
        ).rowcount
    )


def owned_action(fn):
    """Claim before entering runners (which also support direct fixture calls)."""

    @wraps(fn)
    def execute(job_id, action_id=None, *args, **kwargs):
        if action_id is None:
            return fn(job_id, action_id, *args, **kwargs)
        from celery import current_task

        from .. import db

        scope = fn.__globals__.get("session_scope", db.session_scope)
        owner = process_owner()
        task_id = getattr(getattr(current_task, "request", None), "id", None)
        with scope() as session:
            action = session.get(HostAction, action_id)
            if action is None or action.status != "queued":
                return None
            preclaim = getattr(fn, "_preclaim", None)
            if preclaim is not None and not preclaim(session, job_id, action_id):
                return None
            if not claim_action(session, job_id, action_id, owner=owner, task_id=task_id):
                session.rollback()
                logger.info("[%s] skipped unclaimable action %s", job_id, action_id)
                return None
        stop = threading.Event()

        def heartbeat():
            while not stop.wait(LEASE_SECONDS / 3):
                try:
                    with scope() as session:
                        session.execute(
                            update(HostAction)
                            .where(
                                HostAction.id == action_id,
                                HostAction.owner_token == owner,
                                HostAction.status == "running",
                            )
                            .values(lease_expires_at=datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS))
                        )
                except Exception:
                    logger.exception("[%s] action lease renewal failed", job_id)

        thread = threading.Thread(target=heartbeat, daemon=True, name=f"action-lease-{action_id}")
        thread.start()
        try:
            return fn(job_id, action_id, *args, **kwargs)
        except BaseException as exc:
            with scope() as session:
                session.execute(
                    update(HostAction)
                    .where(
                        HostAction.id == action_id,
                        HostAction.owner_token == owner,
                        HostAction.status == "running",
                    )
                    .values(
                        status="failed",
                        finished_at=datetime.now(UTC),
                        active_pid=None,
                        error_summary=f"Action interrupted: {type(exc).__name__}: {exc}",
                    )
                )
            raise
        finally:
            stop.set()
            thread.join(timeout=1)

    return execute


def reconcile_interrupted_actions(session_scope, *, service_identity="analysis", alive=owner_alive) -> int:
    """Recover actions without changing their completed parent or touching VMs.

    A live process identity wins over an expired lease: long pauses must not let
    another worker revoke an executing action. A dead exact identity is safe to
    fence immediately at startup, even before its last heartbeat expires.
    """
    recovered = 0
    with session_scope() as session:
        query = select(HostAction).where(HostAction.status == "running")
        query = (
            query.where(HostAction.action_type.in_(ANALYSIS_ACTIONS))
            if service_identity == "analysis"
            else query.where(HostAction.action_type.not_in(ANALYSIS_ACTIONS))
        )
        for action in session.scalars(query):
            if alive(action.owner_token):
                continue
            changed = session.execute(
                update(HostAction)
                .where(
                    HostAction.id == action.id,
                    HostAction.status == "running",
                    HostAction.owner_token == action.owner_token,
                )
                .values(
                    status="failed",
                    finished_at=datetime.now(UTC),
                    active_pid=None,
                    error_summary="Action interrupted: execution owner exited; retry is available",
                )
            )
            recovered += changed.rowcount
    return recovered


def recover_queued_dispatches(session_scope, *, service_identity):
    """Retry never-claimed sends once at service restart, without revoking claims."""
    with session_scope() as session:
        query = update(HostAction).where(HostAction.status == "queued", HostAction.started_at.is_(None))
        query = (
            query.where(HostAction.action_type.in_(ANALYSIS_ACTIONS))
            if service_identity == "analysis"
            else query.where(HostAction.action_type.not_in(ANALYSIS_ACTIONS))
        )
        return session.execute(query.values(dispatched_at=None)).rowcount
