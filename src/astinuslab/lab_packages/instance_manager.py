"""Durable package-instance state and action journal helpers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from ..db.models import LabInstance, LabInstanceAction, LabOperationOutbox

TERMINAL_INSTANCE_STATES = frozenset({"failed", "destroyed"})
_TRANSITIONS = {
    "registered": frozenset({"creating", "cancel_requested", "failed"}),
    "creating": frozenset({"ready", "failed", "rollback_pending", "cancel_requested", "destroying"}),
    "ready": frozenset({"resetting", "destroying", "cancel_requested"}),
    "failed": frozenset({"creating", "destroying", "destroyed"}),
    "cancel_requested": frozenset({"rollback_pending", "destroying", "destroyed"}),
    "rollback_pending": frozenset({"cleanup_incomplete", "destroyed", "failed"}),
    "cleanup_incomplete": frozenset({"destroying", "destroyed"}),
    "resetting": frozenset({"ready", "failed", "cleanup_incomplete", "cancel_requested", "destroying"}),
    "destroying": frozenset({"destroyed", "cleanup_incomplete"}),
    "destroyed": frozenset(),
}


class InstanceTransitionError(ValueError):
    pass


def append_action(
    session: Session, instance_id: str, action_type: str, payload: dict[str, Any] | None = None
) -> LabInstanceAction:
    action = LabInstanceAction(
        id=f"lia-{uuid4().hex[:16]}",
        instance_id=instance_id,
        action_type=action_type,
        payload_json=json.dumps(payload or {}, sort_keys=True, separators=(",", ":")),
    )
    session.add(action)
    session.flush()
    return action


def enqueue_operation(
    session: Session,
    *,
    kind: str,
    operation_id: str,
    payload: dict[str, Any] | None = None,
    reopen_terminal: bool = False,
) -> LabOperationOutbox:
    payload_json = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"))
    existing = session.query(LabOperationOutbox).filter_by(operation_kind=kind, operation_id=operation_id).one_or_none()
    if existing is not None:
        if reopen_terminal and existing.state in {"completed", "failed"}:
            existing.payload_json = payload_json
            existing.state = "pending"
            existing.claimed_at = None
            existing.completed_at = None
            session.flush()
        return existing
    event = LabOperationOutbox(
        id=f"loo-{uuid4().hex[:16]}",
        operation_kind=kind,
        operation_id=operation_id,
        payload_json=payload_json,
    )
    session.add(event)
    session.flush()
    return event


def transition_instance(session: Session, instance: LabInstance, state: str, *, error: str | None = None) -> None:
    if state not in _TRANSITIONS.get(instance.state, frozenset()):
        raise InstanceTransitionError(f"invalid instance transition: {instance.state} -> {state}")
    instance.state = state
    instance.error_summary = error
    instance.updated_at = datetime.now(UTC)
    session.flush()
