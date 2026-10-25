from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, HTTPException, Query, status

from ... import db as db_mod
from ...api.errors import ApiError
from ...api.schemas import (
    PlaygroundCidrSuggestionOut,
    PlaygroundCreateNetworkRequest,
    PlaygroundNetworkOut,
    PlaygroundSessionOut,
)
from ...db.transactions import reserve_sqlite_write
from ...playground_constants import (
    DEFAULT_PLAYGROUND_NETWORK_CIDR,
    DEFAULT_PLAYGROUND_NETWORK_NAME,
)
from .. import orchestrator as po
from .. import repositories as pr
from ..enqueue import ENQUEUE_FAILED_DETAIL as _ENQUEUE_FAILED_DETAIL
from ..enqueue import enqueue_or_raise, task_id
from ..serializers import NON_DESTROY_ACTIVE, PlaygroundAcceptedOut, count_attached_vms, network_out, serialize_session
from .sessions import _integrity_as_409, _raise_if_authoring_closed, _raise_if_non_playground_job_active

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/networks/suggest-cidr", response_model=PlaygroundCidrSuggestionOut)
def suggest_cidr_endpoint(
    job_id: str | None = None,
    extra: list[str] = Query(default=[]),
) -> PlaygroundCidrSuggestionOut:
    """Suggest the next free 10.42.x.0/24 for a new playground network.

    `job_id` (optional) seeds the taken set with that session's networks;
    `extra` (optional, repeatable) adds in-wizard draft CIDRs not yet persisted.
    """
    taken = list(extra)
    if job_id:
        with db_mod.session_scope() as s:
            taken.extend(n.subnet_cidr for n in pr.list_live_session_networks(s, job_id))
    try:
        return PlaygroundCidrSuggestionOut(subnet_cidr=pr.next_free_cidr(taken))
    except RuntimeError as exc:
        raise ApiError(409, str(exc), "conflict") from exc


@router.post("/networks", status_code=status.HTTP_202_ACCEPTED, response_model=PlaygroundSessionOut)
def ensure_session_and_create_network_endpoint(
    payload: PlaygroundCreateNetworkRequest,
) -> PlaygroundSessionOut:
    """Seamless first-network flow.

    Reuses the active playground session, or creates one if none exists.
    Returns the full session state so the frontend can transition out of
    the idle "create first network" view in a single round trip.

    The network is registered as pending before the Celery task is queued,
    allowing the frontend to poll session state instead of waiting here.
    """
    created_new_session = False
    with db_mod.session_scope() as s:
        job = pr.active_playground_session(s)
        job_id = job.id if job else None

    with _integrity_as_409("session or network already exists"):
        with db_mod.session_scope() as s:
            if job_id is None:
                reserve_sqlite_write(s)
            _raise_if_non_playground_job_active(s)
            if job_id is None:
                job_id = f"pg-{secrets.token_hex(6)}"
                pr.create_playground_session(s, job_id=job_id)
                created_new_session = True
            else:
                from ...db.models import PlaygroundSession

                parent = s.get(PlaygroundSession, job_id)
                if parent is None:
                    raise ApiError(409, "playground session is no longer accepting resources", "conflict")
                _raise_if_authoring_closed(s, parent.id)
                if parent.status not in NON_DESTROY_ACTIVE:
                    raise ApiError(409, "playground session is no longer accepting resources", "conflict")
            existing = pr.list_live_session_networks(s, job_id)
            if any(n.name == payload.name for n in existing):
                raise ApiError(409, f"network name '{payload.name}' already exists", "conflict")
            net_id = f"net-{secrets.token_hex(6)}"
            final_cidr, substituted_from = pr.resolve_cidr(s, job_id, payload.subnet_cidr)
            try:
                _register_pending_network(
                    s,
                    job_id=job_id,
                    network_id=net_id,
                    name=payload.name,
                    subnet_cidr=final_cidr,
                    isolated=payload.isolated,
                )
            except RuntimeError as exc:
                # A newly created parent lives in this same transaction, so
                # the context-manager rollback removes it without an async
                # cleanup handoff.
                raise ApiError(409, str(exc), "conflict") from exc

    try:
        _enqueue_create_network(
            job_id=job_id,
            network_id=net_id,
            name=payload.name,
            subnet_cidr=final_cidr,
            isolated=payload.isolated,
        )
    except Exception as exc:
        detail = str(exc)
        _mark_network_failed(net_id, detail)
        if created_new_session:
            with db_mod.session_scope() as s:
                pr.mark_session_failed(s, job_id, detail)
        raise HTTPException(status_code=500, detail=detail) from exc

    with db_mod.session_scope() as s:
        job = pr.active_playground_session(s)
        if job is None:
            raise HTTPException(status_code=500, detail="session lost after network creation")
        out = serialize_session(s, job)
        for n in out.networks:
            if n.id == net_id:
                n.cidr_substituted_from = substituted_from
        return out


def _register_pending_network(
    session,
    *,
    job_id: str,
    network_id: str,
    name: str,
    subnet_cidr: str,
    isolated: bool,
):
    return pr.register_network(
        session,
        network_id=network_id,
        job_id=job_id,
        name=name,
        subnet_cidr=subnet_cidr,
        isolated=isolated,
        libvirt_net_name=po.libvirt_net_name(job_id, name),
    )


def _enqueue_create_network(
    *,
    job_id: str,
    network_id: str,
    name: str,
    subnet_cidr: str,
    isolated: bool,
    reservation_token: str | None = None,
) -> str | None:
    result = po.create_network.delay(
        job_id=job_id,
        network_id=network_id,
        name=name,
        subnet_cidr=subnet_cidr,
        isolated=isolated,
        **({"reservation_token": reservation_token} if reservation_token is not None else {}),
    )
    return task_id(result)


def _mark_network_failed(network_id: str, detail: str) -> None:
    try:
        with db_mod.session_scope() as s:
            pr.update_network_state(s, network_id, "failed", error=detail)
    except Exception as exc:
        _ = exc
        logger.exception("mark_network_failed: could not update network %s", network_id)


def _create_inline_networks(job_id: str, networks: list[PlaygroundCreateNetworkRequest]) -> list[str]:
    created: list[tuple[str, PlaygroundCreateNetworkRequest, str]] = []
    with db_mod.session_scope() as s:
        _raise_if_authoring_closed(s, job_id)
        existing = pr.list_live_session_networks(s, job_id)
        for payload in networks:
            if any(n.name == payload.name for n in existing):
                raise ApiError(409, f"network name '{payload.name}' already exists", "conflict")
            network_id = f"net-{secrets.token_hex(6)}"
            final_cidr, substituted_from = pr.resolve_cidr(s, job_id, payload.subnet_cidr)
            if substituted_from is not None:
                logger.debug(
                    "inline network %s: CIDR %s overlapped; substituted %s",
                    payload.name,
                    substituted_from,
                    final_cidr,
                )
            try:
                _register_pending_network(
                    s,
                    job_id=job_id,
                    network_id=network_id,
                    name=payload.name,
                    subnet_cidr=final_cidr,
                    isolated=payload.isolated,
                )
            except RuntimeError as exc:
                raise ApiError(409, str(exc), "conflict") from exc
            created.append((network_id, payload, final_cidr))
            existing = pr.list_live_session_networks(s, job_id)

    for network_id, payload, final_cidr in created:
        try:
            _enqueue_create_network(
                job_id=job_id,
                network_id=network_id,
                name=payload.name,
                subnet_cidr=final_cidr,
                isolated=payload.isolated,
            )
        except Exception as exc:
            detail = str(exc)
            _mark_network_failed(network_id, detail)
            raise HTTPException(status_code=500, detail=detail) from exc
    return [network_id for network_id, _payload, _cidr in created]


def _ensure_default_network(job_id: str) -> str:
    with db_mod.session_scope() as s:
        # Serialize the existence check with registration. Without this lock,
        # concurrent create-if-needed VM requests can all observe no default
        # network, then race to register one. The losing request may own the
        # freshly shared session and incorrectly send it to failure cleanup.
        pr.lock_resource_transitions(s)
        _raise_if_authoring_closed(s, job_id)
        networks = pr.list_session_networks(s, job_id)
        for net in networks:
            if net.name == DEFAULT_PLAYGROUND_NETWORK_NAME and net.state != "destroyed":
                if not net.isolated or net.subnet_cidr != DEFAULT_PLAYGROUND_NETWORK_CIDR:
                    raise ApiError(
                        409,
                        (
                            "default playground network is incompatible; "
                            f"expected isolated {DEFAULT_PLAYGROUND_NETWORK_CIDR}"
                        ),
                        "conflict",
                    )
                return net.id
        network_id = f"net-{secrets.token_hex(6)}"
        try:
            _register_pending_network(
                s,
                job_id=job_id,
                network_id=network_id,
                name=DEFAULT_PLAYGROUND_NETWORK_NAME,
                subnet_cidr=DEFAULT_PLAYGROUND_NETWORK_CIDR,
                isolated=True,
            )
        except RuntimeError as exc:
            raise ApiError(409, str(exc), "conflict") from exc
    _enqueue_create_network(
        job_id=job_id,
        network_id=network_id,
        name=DEFAULT_PLAYGROUND_NETWORK_NAME,
        subnet_cidr=DEFAULT_PLAYGROUND_NETWORK_CIDR,
        isolated=True,
    )
    return network_id


@router.post("/sessions/{job_id}/networks", status_code=status.HTTP_202_ACCEPTED, response_model=PlaygroundNetworkOut)
def create_network_endpoint(job_id: str, payload: PlaygroundCreateNetworkRequest) -> PlaygroundNetworkOut:
    with _integrity_as_409(f"network name '{payload.name}' already exists"):
        with db_mod.session_scope() as s:
            job = pr.active_playground_session(s)
            if job is None or job.id != job_id:
                raise HTTPException(status_code=404, detail="session not found or not active")
            _raise_if_authoring_closed(s, job.id)
            if job.status not in NON_DESTROY_ACTIVE:
                raise HTTPException(status_code=404, detail="session not found or not active")
            _raise_if_non_playground_job_active(s)
            existing = pr.list_live_session_networks(s, job_id)
            if any(n.name == payload.name for n in existing):
                raise ApiError(409, f"network name '{payload.name}' already exists", "conflict")
            net_id = f"net-{secrets.token_hex(6)}"
            final_cidr, substituted_from = pr.resolve_cidr(s, job_id, payload.subnet_cidr)
            try:
                _register_pending_network(
                    s,
                    job_id=job_id,
                    network_id=net_id,
                    name=payload.name,
                    subnet_cidr=final_cidr,
                    isolated=payload.isolated,
                )
            except RuntimeError as exc:
                raise ApiError(409, str(exc), "conflict") from exc

    try:
        _enqueue_create_network(
            job_id=job_id,
            network_id=net_id,
            name=payload.name,
            subnet_cidr=final_cidr,
            isolated=payload.isolated,
        )
    except Exception as exc:
        detail = str(exc)
        _mark_network_failed(net_id, detail)
        raise HTTPException(status_code=500, detail=detail) from exc

    with db_mod.session_scope() as s:
        from ...db.models import JobNetwork

        n = s.query(JobNetwork).filter_by(id=net_id).one()
        active = pr.active_playground_session(s)
        session_active = active is not None and active.id == n.session_id and active.status in NON_DESTROY_ACTIVE
        out = network_out(
            n,
            session_active=session_active,
            attached_vm_count=count_attached_vms(s, n.id),
        )
        out.cidr_substituted_from = substituted_from
        return out


@router.delete("/networks/{network_id}", status_code=status.HTTP_202_ACCEPTED, response_model=PlaygroundAcceptedOut)
def delete_network_endpoint(network_id: str) -> PlaygroundAcceptedOut:
    with db_mod.session_scope() as s:
        from ...db.models import JobNetwork

        pr.lock_resource_transitions(s)
        active = pr.active_playground_session(s)
        net = s.query(JobNetwork).filter_by(id=network_id).with_for_update().one_or_none()
        if net is None or active is None or net.session_id != active.id:
            raise HTTPException(status_code=404, detail="network not found")
        _raise_if_authoring_closed(s, active.id)
        if pr.network_has_attached_vms(s, network_id):
            raise ApiError(409, "network has attached VMs; destroy them first", "conflict")
        job_id = active.id
        prior_state = net.state
        if not pr.queue_network_for_destroy(s, network_id):
            raise ApiError(409, f"network destroy unavailable in state {prior_state}", "conflict")

    def _on_destroy_enqueue_failure(_exc: Exception) -> None:
        with db_mod.session_scope() as s:
            pr.cancel_queued_network_destroy(s, network_id, prior_state, error=_ENQUEUE_FAILED_DETAIL)

    queued_task_id = enqueue_or_raise(
        po.destroy_network,
        on_failure=_on_destroy_enqueue_failure,
        network_id=network_id,
    )
    return PlaygroundAcceptedOut(resource_id=network_id, job_id=job_id, task_id=queued_task_id)
