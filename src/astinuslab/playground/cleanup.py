from __future__ import annotations

import logging

from ..db import session_scope

logger = logging.getLogger(__name__)


def purge_session_resources(*, job_id: str, final_status: str, error_summary: str | None = None) -> None:
    """Destroy all VMs and networks owned by a playground session, then
    transition the session to a terminal state.

    The session is moved to `destroying` before any cleanup runs and only
    reaches the terminal state once every VM/network destroy task has
    returned (or has failed and been recorded). This keeps
    active_playground_session() returning the session for the whole window
    so the API blocks new sessions and forensic jobs until cleanup is
    truly done.

    If any VM ends in `teardown_failed` or any network ends in `failed` or
    `destroying`, the session is moved to `destroy_failed` regardless of
    the originally requested final_status so the operator can see that
    libvirt state may still be dirty.
    """
    from . import repositories as pr
    from .orchestrator import destroy_network, destroy_vm

    with session_scope() as session:
        pr.mark_session_destroying(session, job_id)

    with session_scope() as session:
        vm_ids = [
            vm.id for vm in pr.list_session_vms(session, job_id) if vm.state not in {"destroyed", "teardown_failed"}
        ]
        network_ids = [net.id for net in pr.list_session_networks(session, job_id) if net.state != "destroyed"]

    cleanup_errors: list[str] = []

    for vm_id in vm_ids:
        try:
            destroy_vm.apply(kwargs={"vm_id": vm_id}).get(disable_sync_subtasks=False)
        except Exception as exc:
            logger.warning("purge_session_resources: destroy_vm %s failed: %s", vm_id, exc)
            cleanup_errors.append(f"vm {vm_id}: {exc}")

    for network_id in network_ids:
        try:
            destroy_network.apply(kwargs={"network_id": network_id}).get(disable_sync_subtasks=False)
        except Exception as exc:
            logger.warning("purge_session_resources: destroy_network %s failed: %s", network_id, exc)
            cleanup_errors.append(f"network {network_id}: {exc}")

    with session_scope() as session:
        unsuccessful_vm = any(vm.state in {"teardown_failed", "failed"} for vm in pr.list_session_vms(session, job_id))
        unsuccessful_net = any(
            net.state in {"failed", "destroying"} for net in pr.list_session_networks(session, job_id)
        )

    if cleanup_errors or unsuccessful_vm or unsuccessful_net:
        summary = error_summary
        if cleanup_errors:
            joined = "; ".join(cleanup_errors)
            summary = f"{summary}: {joined}" if summary else joined
        with session_scope() as session:
            pr.mark_session_destroy_failed(session, job_id, summary)
        return

    with session_scope() as session:
        if final_status == "completed":
            pr.mark_session_completed(session, job_id)
        elif final_status == "cancelled":
            pr.mark_session_cancelled(session, job_id, error_summary)
        else:
            pr.mark_session_failed(session, job_id, error_summary or final_status)
