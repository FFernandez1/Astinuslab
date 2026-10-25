from __future__ import annotations

import secrets
from contextlib import contextmanager
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ... import db as db_mod
from ...api.errors import ApiError
from ...api.schemas import PlaygroundSessionOut
from ...db.models import PlaygroundSession
from ...db.transactions import reserve_sqlite_write
from .. import orchestrator as po
from .. import repositories as pr
from ..enqueue import task_id
from ..serializers import NON_DESTROY_ACTIVE, PlaygroundAcceptedOut, serialize_session

router = APIRouter()


class PlaygroundFailureOut(BaseModel):
    session_id: str
    status: str
    error_summary: str | None = None
    finished_at: datetime | None = None


class PlaygroundCleanupOut(BaseModel):
    session_id: str
    status: str
    finished_at: datetime | None = None
    error_code: str | None = None


@contextmanager
def _integrity_as_409(detail: str, code: str = "conflict", context: dict | None = None):
    try:
        yield
    except IntegrityError as exc:
        raise ApiError(409, detail, code, context) from exc


def _raise_if_non_playground_job_active(session: Session) -> None:
    from ...db.repositories import has_active_non_playground_job

    if has_active_non_playground_job(session):
        raise ApiError(409, "a forensic job is currently active", "conflict")


def _raise_if_authoring_closed(session: Session, session_id: str) -> None:
    try:
        pr.require_session_open_for_authoring(session, session_id)
    except pr.AuthoringClosed as exc:
        raise ApiError(409, str(exc), "authoring_closed") from exc


def _ensure_active_session_id() -> tuple[str, bool]:
    with db_mod.session_scope() as s:
        job = pr.active_playground_session(s)
        if job is not None:
            _raise_if_authoring_closed(s, job.id)
            if job.status in NON_DESTROY_ACTIVE:
                return job.id, False
            raise ApiError(409, "playground session is no longer accepting resources", "conflict")
    job_id = f"pg-{secrets.token_hex(6)}"
    with db_mod.session_scope() as s:
        reserve_sqlite_write(s)
        _raise_if_non_playground_job_active(s)
        pr.create_playground_session(s, job_id=job_id)
    return job_id, True


@router.post("/sessions", status_code=status.HTTP_202_ACCEPTED, response_model=PlaygroundSessionOut)
def create_session_endpoint() -> PlaygroundSessionOut:
    job_id = f"pg-{secrets.token_hex(6)}"
    with _integrity_as_409("a playground session is already active"):
        with db_mod.session_scope() as s:
            reserve_sqlite_write(s)
            active = pr.active_playground_session(s)
            if active is not None:
                _raise_if_authoring_closed(s, active.id)
                raise ApiError(409, "a playground session is already active", "conflict")
            _raise_if_non_playground_job_active(s)
            job = pr.create_playground_session(s, job_id=job_id)
            return serialize_session(s, job)


@router.get("/sessions/current", response_model=PlaygroundSessionOut)
def get_current_session() -> PlaygroundSessionOut:
    with db_mod.session_scope() as s:
        job = pr.active_playground_session(s)
        if job is None:
            raise HTTPException(status_code=404, detail="no active playground session")
        return serialize_session(s, job)


@router.get("/sessions/current-or-null", response_model=PlaygroundSessionOut | None)
def get_current_session_or_null() -> PlaygroundSessionOut | None:
    with db_mod.session_scope() as s:
        job = pr.active_playground_session(s)
        if job is None:
            return None
        return serialize_session(s, job)


@router.get("/sessions/recent-failure", response_model=PlaygroundFailureOut | None)
def get_recent_failure() -> PlaygroundFailureOut | None:
    with db_mod.session_scope() as s:
        job = pr.latest_destroy_failed_session(s)
        if job is None:
            return None
        return PlaygroundFailureOut(
            session_id=job.id,
            status=job.status,
            error_summary=job.error_summary,
            finished_at=job.finished_at,
        )


@router.get("/sessions/{session_id}/cleanup-status", response_model=PlaygroundCleanupOut)
def get_cleanup_status(session_id: str) -> PlaygroundCleanupOut:
    with db_mod.session_scope() as s:
        job = s.get(PlaygroundSession, session_id)
        if job is None:
            raise HTTPException(status_code=404, detail="cleanup record not found")
        finished = job.finished_at
        if finished is not None:
            finished = finished.replace(tzinfo=UTC) if finished.tzinfo is None else finished.astimezone(UTC)
        return PlaygroundCleanupOut(
            session_id=job.id,
            status=job.status,
            finished_at=finished,
            error_code={"destroy_failed": "cleanup_incomplete", "failed": "session_failed"}.get(job.status),
        )


@router.delete("/sessions/{job_id}", status_code=status.HTTP_202_ACCEPTED, response_model=PlaygroundAcceptedOut)
def destroy_session_endpoint(job_id: str) -> PlaygroundAcceptedOut:
    with db_mod.session_scope() as s:
        job = pr.active_playground_session(s)
        if job is None or job.id != job_id:
            raise HTTPException(status_code=404, detail="session not found or not active")
        _raise_if_authoring_closed(s, job.id)
        job.status = "destroy_requested"
    result = po.destroy_session.delay(job_id=job_id)
    return PlaygroundAcceptedOut(resource_id=job_id, job_id=job_id, session_id=job_id, task_id=task_id(result))
