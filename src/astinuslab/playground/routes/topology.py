from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ... import db as db_mod
from ...api.schemas import PlaygroundTopologyLayout, PlaygroundTopologyNode
from .. import repositories as pr
from .sessions import _raise_if_authoring_closed

router = APIRouter()


def _topology_node_out(row) -> PlaygroundTopologyNode:
    return PlaygroundTopologyNode(
        node_id=row.node_id,
        node_type=row.node_type,
        x=row.x,
        y=row.y,
    )


@router.get("/sessions/current/topology-layout", response_model=PlaygroundTopologyLayout)
def get_current_topology_layout_endpoint() -> PlaygroundTopologyLayout:
    with db_mod.session_scope() as s:
        job = pr.active_playground_session(s)
        if job is None:
            raise HTTPException(status_code=404, detail="no active playground session")
        return PlaygroundTopologyLayout(nodes=[_topology_node_out(row) for row in pr.list_topology_layout(s, job.id)])


@router.patch("/sessions/current/topology-layout/{node_id}", response_model=PlaygroundTopologyNode)
def save_current_topology_node_endpoint(node_id: str, payload: PlaygroundTopologyNode) -> PlaygroundTopologyNode:
    with db_mod.session_scope() as s:
        job = pr.active_playground_session(s)
        if job is None:
            raise HTTPException(status_code=404, detail="no active playground session")
        _raise_if_authoring_closed(s, job.id)
        if payload.node_id != node_id:
            raise HTTPException(status_code=400, detail="node_id in path does not match payload")
        row = pr.save_topology_node(s, job.id, node_id, payload.model_dump())
        return _topology_node_out(row)
