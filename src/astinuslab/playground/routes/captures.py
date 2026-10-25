from __future__ import annotations

import asyncio
import json
import secrets
import time
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ... import db as db_mod
from ...api.errors import ApiError
from ...api.schemas import PlaygroundCaptureRequest
from ...core import config as config_mod
from .. import acquisition as pa
from .. import orchestrator as po
from .. import repositories as pr
from ..enqueue import ENQUEUE_FAILED_DETAIL as _ENQUEUE_FAILED_DETAIL
from ..enqueue import enqueue_or_raise
from .sessions import _raise_if_authoring_closed

router = APIRouter()
TERMINAL_STATUS = {"succeeded", "completed", "failed", "cancelled", "error"}


class PlaygroundCaptureOut(BaseModel):
    acquisition_id: str
    status: str = "queued"
    task_id: str | None = None


class PlaygroundAcquisitionOut(BaseModel):
    id: str
    name: str
    status: str
    created_at: datetime
    path: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    kind: str | None = None
    session_id: str | None = None
    vm_label: str | None = None


@router.post("/vms/{vm_id}/captures", status_code=status.HTTP_202_ACCEPTED, response_model=PlaygroundCaptureOut)
def trigger_capture_endpoint(vm_id: str, payload: PlaygroundCaptureRequest) -> PlaygroundCaptureOut:
    with db_mod.session_scope() as s:
        from ...db.models import Acquisition, JobVM

        active = pr.active_playground_session(s)
        vm = s.query(JobVM).filter_by(id=vm_id).one_or_none()
        if vm is None or active is None or vm.session_id != active.id:
            raise HTTPException(status_code=404, detail="VM not found")
        _raise_if_authoring_closed(s, active.id)
        if vm.role == "network":
            raise ApiError(409, "capture disabled for Network Nodes", "conflict")
        if vm.state != "running":
            raise ApiError(409, f"VM not running (state={vm.state})", "conflict")
        acq_id = secrets.token_hex(8)
        marker = "mem" if payload.kind == "memory" else "disk"
        s.add(
            Acquisition(
                id=acq_id,
                name=f"{marker}-{acq_id}",
                status="queued",
                job_vm_id=vm_id,
                kind=payload.kind,
                session_id=vm.session_id,
                vm_label=vm.label,
            )
        )
    pa.record_acquisition_event(acq_id, "queued", f"queued {payload.kind} capture")
    task = po.capture_memory if payload.kind == "memory" else po.capture_disk

    def _on_capture_enqueue_failure(_exc: Exception) -> None:
        with db_mod.session_scope() as s:
            from ...db.models import Acquisition

            acq = s.query(Acquisition).filter_by(id=acq_id).one_or_none()
            if acq is not None:
                acq.status = "failed"
        pa.record_acquisition_event(acq_id, "failed", _ENQUEUE_FAILED_DETAIL)

    task_id = enqueue_or_raise(task, on_failure=_on_capture_enqueue_failure, vm_id=vm_id, acquisition_id=acq_id)
    return PlaygroundCaptureOut(acquisition_id=acq_id, task_id=task_id)


@router.get("/vms/{vm_id}/captures", response_model=list[PlaygroundAcquisitionOut])
def list_captures_endpoint(vm_id: str) -> list[PlaygroundAcquisitionOut]:
    with db_mod.session_scope() as s:
        from ...db.models import Acquisition

        rows = s.query(Acquisition).filter_by(job_vm_id=vm_id).order_by(Acquisition.created_at.desc()).all()
        return [
            PlaygroundAcquisitionOut(
                id=r.id,
                name=r.name,
                status=r.status,
                created_at=r.created_at,
                path=r.path,
                sha256=r.sha256,
                size_bytes=r.size_bytes,
                kind=r.kind,
                session_id=r.session_id,
                vm_label=r.vm_label,
            )
            for r in rows
        ]


@router.get("/captures/{acq_id}/events")
async def capture_events_endpoint(acq_id: str, request: Request) -> StreamingResponse:
    from ...db.models import Acquisition

    with db_mod.session_scope() as s:
        if s.query(Acquisition).filter_by(id=acq_id).one_or_none() is None:
            raise HTTPException(status_code=404, detail="acquisition not found")

    async def event_stream():
        from ...db.models import AcquisitionEvent

        last_id = 0
        deadline = time.monotonic() + config_mod.settings.playground_capture_stream_timeout_seconds
        while True:
            if await request.is_disconnected():
                return
            with db_mod.session_scope() as s:
                events = (
                    s.query(AcquisitionEvent)
                    .filter(AcquisitionEvent.acquisition_id == acq_id, AcquisitionEvent.id > last_id)
                    .order_by(AcquisitionEvent.id)
                    .all()
                )
                rows = [
                    {
                        "id": event.id,
                        "status": event.status,
                        "message": event.message,
                        "created_at": event.created_at.isoformat(),
                    }
                    for event in events
                ]
            for row in rows:
                row_id = row["id"]
                assert isinstance(row_id, int)  # noqa: S101  # mypy narrowing; unreachable by construction
                last_id = row_id
                yield f"event: {row['status']}\ndata: {json.dumps(row, separators=(',', ':'))}\n\n"
                if row["status"] in TERMINAL_STATUS:
                    return
            if time.monotonic() >= deadline:
                payload = {"status": "stream_timeout", "message": "server closed stream"}
                yield f"event: stream_timeout\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
