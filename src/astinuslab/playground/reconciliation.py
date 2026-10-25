from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from ..db.models import Job, JobNetwork, JobVM, JobVMNic, PlaygroundSession

logger = logging.getLogger("astinuslab.playground.reconciliation")

_TERMINAL_JOB_STATES = {"completed", "failed", "cancelled"}
_CLEANUP_PARENT_STATES = _TERMINAL_JOB_STATES | {
    "cancel_requested",
    "destroy_requested",
    "destroying",
    "destroy_failed",
    "failure_cleanup",
}
_TERMINAL_RESOURCE_STATES = {"destroyed", "failed", "teardown_failed"}
_TERMINAL_RUNTIME_STATES = {"destroyed", "teardown_failed"}
_MISSING_MESSAGE = "reconciler: libvirt resource missing"
_ABANDONED_MESSAGE = "reconciler: abandoned worker claim exceeded grace period"


def _owned_domain_name(name: str) -> bool:
    return name.startswith("astinuslab-")


def _owned_network_name(name: str) -> bool:
    return name.startswith("astinuslab-pg-")


def _domain_owner_job_id(name: str) -> str:
    """Candidate forensic job id encoded in an astinuslab-* domain name.

    Forensic domains follow astinuslab-<job_id> / astinuslab-<job_id>-attacker
    (see vm_support.teardown_vm). Playground domains (astinuslab-pg-*) yield
    a 'pg-…' candidate that never matches a Job id, so they are unaffected.
    """
    candidate = name.removeprefix("astinuslab-")
    return candidate.removesuffix("-attacker")


def _is_old_enough(value: datetime | None, *, now: datetime, grace_seconds: int) -> bool:
    if value is None:
        return True
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value <= now - timedelta(seconds=grace_seconds)


def _drop_incomplete_nic_if_parent_unchanged(
    session,
    *,
    nic_id: str,
    vm_id: str,
    network_id: str,
    mac: str,
    created_at: datetime,
    nic_state: str,
    vm_state: str,
    vm_updated_at: datetime,
) -> int:
    unchanged_parent = select(JobVM.id).where(
        JobVM.id == vm_id,
        JobVM.state == vm_state,
        JobVM.updated_at == vm_updated_at,
    )
    return (
        session.query(JobVMNic)
        .filter(
            JobVMNic.id == nic_id,
            JobVMNic.state == nic_state,
            JobVMNic.job_network_id == network_id,
            JobVMNic.mac == mac,
            JobVMNic.created_at == created_at,
            JobVMNic.job_vm_id.in_(unchanged_parent),
        )
        .delete(synchronize_session=False)
    )


def _recover_stale_resource_claims(
    session,
    *,
    result: dict[str, int],
    now: datetime,
    grace_seconds: int,
) -> None:
    """Fence abandoned worker states with state+timestamp compare-and-swap.

    Fresh workers retain custody. Once the configured grace expires, the
    reconciler moves the exact observed claim into the existing strict teardown
    path, so a crashed Celery worker cannot protect a host object forever.
    """
    vm_claim_states = {
        "starting",
        "provisioning_workload",
        "provisioning_workload_active",
        "powering_on",
        "powering_off",
        "rebooting",
        "destroy_queued",
        "destroying",
    }
    for vm in list(session.scalars(select(JobVM).where(JobVM.state.in_(list(vm_claim_states))))):
        observed_updated_at = vm.updated_at
        if not _is_old_enough(observed_updated_at or vm.created_at, now=now, grace_seconds=grace_seconds):
            continue
        observed_state = vm.state
        recovered = (
            session.query(JobVM)
            .filter(
                JobVM.id == vm.id,
                JobVM.state == observed_state,
                JobVM.updated_at == observed_updated_at,
            )
            .update(
                {
                    JobVM.state: "teardown_failed",
                    JobVM.error_summary: _ABANDONED_MESSAGE,
                    JobVM.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        if recovered != 1:
            continue
        result["abandoned_vm_claims_recovered"] += 1
        parent = session.get(PlaygroundSession, vm.session_id)
        if observed_state in {
            "starting",
            "provisioning_workload",
            "provisioning_workload_active",
            "powering_on",
            "powering_off",
            "rebooting",
        } and (parent is not None and parent.status not in _CLEANUP_PARENT_STATES):
            _mark_session_failed(session, vm.session_id, now)

    network_claim_states = {"creating", "destroy_queued", "destroying"}
    for network in list(session.scalars(select(JobNetwork).where(JobNetwork.state.in_(list(network_claim_states))))):
        observed_updated_at = network.updated_at
        if not _is_old_enough(observed_updated_at or network.created_at, now=now, grace_seconds=grace_seconds):
            continue
        observed_state = network.state
        recovered = (
            session.query(JobNetwork)
            .filter(
                JobNetwork.id == network.id,
                JobNetwork.state == observed_state,
                JobNetwork.updated_at == observed_updated_at,
            )
            .update(
                {
                    JobNetwork.state: "failed",
                    JobNetwork.error_summary: _ABANDONED_MESSAGE,
                    JobNetwork.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        if recovered != 1:
            continue
        result["abandoned_network_claims_recovered"] += 1
        parent = session.get(PlaygroundSession, network.session_id)
        if observed_state == "creating" and parent is not None and parent.status not in _CLEANUP_PARENT_STATES:
            _mark_session_failed(session, network.session_id, now)
    session.flush()


def _finalize_recovered_cleanup_sessions(session, now: datetime) -> None:
    """Release the global session lock only after every child is destroyed."""
    recoverable_parent_states = {
        "cancel_requested",
        "destroy_requested",
        "destroying",
        "destroy_failed",
        "failure_cleanup",
    }
    for parent in session.scalars(
        select(PlaygroundSession).where(PlaygroundSession.status.in_(list(recoverable_parent_states)))
    ):
        dirty_vm = session.scalar(
            select(JobVM.id).where(JobVM.session_id == parent.id, JobVM.state != "destroyed").limit(1)
        )
        dirty_network = session.scalar(
            select(JobNetwork.id).where(JobNetwork.session_id == parent.id, JobNetwork.state != "destroyed").limit(1)
        )
        if dirty_vm is not None or dirty_network is not None:
            continue
        parent.status = "failed" if parent.status == "failure_cleanup" else "cancelled"
        parent.finished_at = now
    session.flush()
    from . import repositories as pr

    pr.promote_cleanup_backlog(session)


def _destroy_domain_strict(domain: Any, libvirt_module: Any) -> None:
    """Stop and undefine only after libvirt confirms the guest is inactive."""
    try:
        if domain.isActive():
            domain.destroy()
            if domain.isActive():
                raise RuntimeError("domain remained active after destroy")
        domain.undefine()
    except libvirt_module.libvirtError as exc:
        raise RuntimeError(f"libvirt domain teardown failed: {exc}") from exc


def _domain_interface_macs(domain: Any) -> list[str]:
    """Parse the domain XML and return the list of <interface>/<mac> addresses
    found inside <devices>. Returns an empty list on parse failure.
    """
    try:
        xml = domain.XMLDesc(0)
    except Exception:
        return []
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml)  # noqa: S314  # parses local libvirt domain XML
    except ET.ParseError:
        return []
    macs: list[str] = []
    for iface in root.findall(".//devices/interface"):
        mac_el = iface.find("mac")
        if mac_el is not None:
            value = mac_el.get("address")
            if value:
                macs.append(value.lower())
    return macs


def _detach_host_orphan_nic(domain: Any, mac: str, libvirt_module: Any) -> bool:
    """Best-effort detach of an interface identified by ``mac``. Returns True
    on success.
    """
    from xml.sax.saxutils import escape

    xml = f"<interface type='network'><mac address='{escape(mac)}'/></interface>"
    live = getattr(libvirt_module, "VIR_DOMAIN_AFFECT_LIVE", 1)
    config = getattr(libvirt_module, "VIR_DOMAIN_AFFECT_CONFIG", 2)
    try:
        domain.detachDeviceFlags(xml, live | config)
        return True
    except libvirt_module.libvirtError:
        return False


def _destroy_network_strict(network: Any, libvirt_module: Any) -> None:
    """Stop and undefine only after libvirt confirms the network is inactive."""
    try:
        if network.isActive():
            network.destroy()
            if network.isActive():
                raise RuntimeError("network remained active after destroy")
        network.undefine()
    except libvirt_module.libvirtError as exc:
        raise RuntimeError(f"libvirt network teardown failed: {exc}") from exc


def _scan_libvirt_domains(conn: Any) -> dict[str, Any]:
    return {dom.name(): dom for dom in conn.listAllDomains()}


def _scan_libvirt_networks(conn: Any) -> dict[str, Any]:
    return {net.name(): net for net in conn.listAllNetworks()}


def _is_exact_libvirt_missing(exc: BaseException, libvirt_module: Any, error_name: str) -> bool:
    get_error_code = getattr(exc, "get_error_code", None)
    expected_code = getattr(libvirt_module, error_name, None)
    return expected_code is not None and callable(get_error_code) and get_error_code() == expected_code


def _lookup_domain_or_none(conn: Any, name: str, libvirt_module: Any) -> Any | None:
    try:
        return conn.lookupByName(name)
    except libvirt_module.libvirtError as exc:
        if _is_exact_libvirt_missing(exc, libvirt_module, "VIR_ERR_NO_DOMAIN"):
            return None
        raise RuntimeError(f"libvirt domain lookup failed for {name}: {exc}") from exc


def _lookup_network_or_none(conn: Any, name: str, libvirt_module: Any) -> Any | None:
    try:
        return conn.networkLookupByName(name)
    except libvirt_module.libvirtError as exc:
        if _is_exact_libvirt_missing(exc, libvirt_module, "VIR_ERR_NO_NETWORK"):
            return None
        raise RuntimeError(f"libvirt network lookup failed for {name}: {exc}") from exc


def _record_reconciliation_event(message: str, *args: object, level: int = logging.ERROR) -> None:
    if level >= logging.ERROR:
        logger.exception(message, *args)
    else:
        logger.warning(message, *args)


def reap_orphan_networks(
    *,
    session_scope,
    libvirt_module,
    libvirt_uri: str,
) -> int:
    """Destroy active ``astinuslab-pg-*`` libvirt networks that belong to no
    live playground session, returning the number destroyed.

    Playground networks are persistent host-side libvirt objects, so a session
    that ends (or a reset DB) can leave its network active on the host, squatting
    its subnet. Run this synchronously before defining a new network so a leaked
    subnet cannot block the next session ahead of the periodic reconciler.
    """
    conn = libvirt_module.open(libvirt_uri)
    try:
        libvirt_networks = _scan_libvirt_networks(conn)
        with session_scope() as session:
            # Every registered name remains under DB custody, including failed
            # and destroyed rows. Their leaked host object is reclaimed only
            # after the dedicated CAS teardown path claims that exact row; an
            # unconditional orphan pass could otherwise destroy a failed row
            # underneath a concurrent create retry.
            db_networks = set(session.scalars(select(JobNetwork.libvirt_net_name)))
        destroyed = 0
        for name, network in libvirt_networks.items():
            if not _owned_network_name(name) or name in db_networks:
                continue
            try:
                _destroy_network_strict(network, libvirt_module)
                destroyed += 1
            except Exception:
                _record_reconciliation_event("reaper: failed to destroy orphan network %s", name)
        return destroyed
    finally:
        close = getattr(conn, "close", None)
        if callable(close):
            close()


def _merge(result: dict[str, int], counts: dict[str, int]) -> None:
    """Fold one phase's counts into the pass total.

    Phases return what they did rather than incrementing a dict they were
    handed, so each one can be called and asserted on its own.
    """
    for key, value in counts.items():
        result[key] = result.get(key, 0) + value


def reconcile_libvirt_resources(
    *,
    session_scope,
    libvirt_module,
    libvirt_uri: str,
    grace_seconds: int,
) -> dict[str, int]:
    """Reconcile AstinusLab-owned libvirt resources with DB state."""
    # Imported here, not at module scope: the phases reach back into this
    # module for the primitives they share, so a top-level import either way
    # would be a cycle.
    from . import reconciliation_phases as phases

    result = {
        "orphan_domains_destroyed": 0,
        "orphan_networks_destroyed": 0,
        "zombie_vms_failed": 0,
        "zombie_networks_failed": 0,
        "zombie_runtimes_failed": 0,
        "stale_runtimes_released": 0,
        "orphan_nic_rows_pruned": 0,
        "host_orphan_nics_detached": 0,
        "pending_nics_promoted": 0,
        "pending_nics_dropped": 0,
        "detaching_nics_requeued": 0,
        "power_states_synced": 0,
        "legacy_vm_teardowns_recovered": 0,
        "legacy_network_teardowns_recovered": 0,
        "abandoned_vm_claims_recovered": 0,
        "abandoned_network_claims_recovered": 0,
    }
    now = datetime.now(UTC)
    conn = libvirt_module.open(libvirt_uri)
    try:
        libvirt_domains = _scan_libvirt_domains(conn)
        libvirt_networks = _scan_libvirt_networks(conn)

        db_domains, db_networks, live_job_id_set = phases._snapshot_owned_names(session_scope=session_scope)

        _merge(
            result,
            phases._destroy_orphan_domains(
                libvirt_domains=libvirt_domains,
                libvirt_module=libvirt_module,
                db_domains=db_domains,
                live_job_id_set=live_job_id_set,
            ),
        )
        _merge(
            result,
            phases._destroy_orphan_networks(
                libvirt_networks=libvirt_networks,
                libvirt_module=libvirt_module,
                db_networks=db_networks,
            ),
        )

        live_session_ids = phases._reconcile_database_rows(
            session_scope=session_scope,
            conn=conn,
            libvirt_module=libvirt_module,
            libvirt_domains=libvirt_domains,
            libvirt_networks=libvirt_networks,
            result=result,
            now=now,
            grace_seconds=grace_seconds,
        )

        _merge(
            result,
            phases._detach_host_orphan_nics(
                session_scope=session_scope,
                libvirt_domains=libvirt_domains,
                libvirt_module=libvirt_module,
            ),
        )

        with session_scope() as session:
            _queue_terminal_session_resources(session, now)

        _merge(result, phases._prune_orphan_nic_rows(session_scope=session_scope))
        _merge(
            result,
            phases._resolve_incomplete_nic_rows(
                session_scope=session_scope,
                libvirt_domains=libvirt_domains,
                now=now,
                grace_seconds=grace_seconds,
            ),
        )

        _merge(result, phases._requeue_stale_nic_detaches(session_scope=session_scope, now=now))

        _merge(
            result,
            phases._recover_legacy_vm_teardowns(
                session_scope=session_scope,
                libvirt_domains=libvirt_domains,
                libvirt_module=libvirt_module,
                conn=conn,
                now=now,
                grace_seconds=grace_seconds,
            ),
        )

        _merge(
            result,
            phases._recover_legacy_network_teardowns(
                session_scope=session_scope,
                libvirt_networks=libvirt_networks,
                libvirt_module=libvirt_module,
                conn=conn,
                grace_seconds=grace_seconds,
            ),
        )

        with session_scope() as session:
            _finalize_recovered_cleanup_sessions(session, now)

        _merge(
            result,
            phases._sync_power_states(
                session_scope=session_scope,
                libvirt_domains=libvirt_domains,
                live_session_ids=live_session_ids,
                now=now,
            ),
        )
    finally:
        close = getattr(conn, "close", None)
        if callable(close):
            close()

    return result


def _reconcile_vm_rows(
    session,
    *,
    conn: Any,
    libvirt_module: Any,
    libvirt_domains: dict[str, Any],
    live_session_ids: list[str],
    result: dict[str, int],
    now: datetime,
    grace_seconds: int,
) -> None:
    for vm in session.scalars(
        select(JobVM)
        .where(JobVM.session_id.in_(live_session_ids))
        .where(JobVM.state.not_in(list(_TERMINAL_RESOURCE_STATES)))
    ):
        if vm.domain_name in libvirt_domains:
            continue
        # Retry claims refresh updated_at while deliberately preserving the
        # original creation timestamp. Grace belongs to the latest transition,
        # otherwise an old failed row is declared missing immediately after a
        # new spawn takes custody and before it can define the domain.
        if not _is_old_enough(vm.updated_at or vm.created_at, now=now, grace_seconds=grace_seconds):
            continue
        # A worker can define the domain after the pass-start scan. Recheck the
        # exact name immediately before the missing-state CAS and retain the
        # object for the later NIC reconciliation in this pass.
        domain = _lookup_domain_or_none(conn, vm.domain_name, libvirt_module)
        if domain is not None:
            libvirt_domains[vm.domain_name] = domain
            continue
        if _fail_missing_vm_if_unchanged(
            session,
            resource_id=vm.id,
            observed_state=vm.state,
            observed_updated_at=vm.updated_at,
            now=now,
        ):
            result["zombie_vms_failed"] += 1
            _mark_session_failed(session, vm.session_id, now)


def _reconcile_network_rows(
    session,
    *,
    conn: Any,
    libvirt_module: Any,
    libvirt_networks: dict[str, Any],
    live_session_ids: list[str],
    result: dict[str, int],
    now: datetime,
    grace_seconds: int,
) -> None:
    for network in session.scalars(
        select(JobNetwork)
        .where(JobNetwork.session_id.in_(live_session_ids))
        .where(JobNetwork.state.not_in(list(_TERMINAL_RESOURCE_STATES)))
    ):
        if network.libvirt_net_name in libvirt_networks:
            continue
        if not _is_old_enough(network.updated_at or network.created_at, now=now, grace_seconds=grace_seconds):
            continue
        host_network = _lookup_network_or_none(conn, network.libvirt_net_name, libvirt_module)
        if host_network is not None:
            libvirt_networks[network.libvirt_net_name] = host_network
            continue
        if _fail_missing_network_if_unchanged(
            session,
            resource_id=network.id,
            observed_state=network.state,
            observed_updated_at=network.updated_at,
            now=now,
        ):
            result["zombie_networks_failed"] += 1
            _mark_session_failed(session, network.session_id, now)


def _fail_missing_vm_if_unchanged(
    session,
    *,
    resource_id: str,
    observed_state: str,
    observed_updated_at: datetime,
    now: datetime,
) -> bool:
    failed = (
        session.query(JobVM)
        .filter(
            JobVM.id == resource_id,
            JobVM.state == observed_state,
            JobVM.updated_at == observed_updated_at,
        )
        .update(
            {
                JobVM.state: "failed",
                JobVM.error_summary: _MISSING_MESSAGE,
                JobVM.updated_at: now,
            },
            synchronize_session=False,
        )
    )
    session.flush()
    return failed == 1


def _fail_missing_network_if_unchanged(
    session,
    *,
    resource_id: str,
    observed_state: str,
    observed_updated_at: datetime,
    now: datetime,
) -> bool:
    failed = (
        session.query(JobNetwork)
        .filter(
            JobNetwork.id == resource_id,
            JobNetwork.state == observed_state,
            JobNetwork.updated_at == observed_updated_at,
        )
        .update(
            {
                JobNetwork.state: "failed",
                JobNetwork.error_summary: _MISSING_MESSAGE,
                JobNetwork.updated_at: now,
            },
            synchronize_session=False,
        )
    )
    session.flush()
    return failed == 1


def _mark_job_failed(session, job_id: str, now: datetime) -> None:
    session.query(Job).filter(
        Job.id == job_id,
        Job.status.not_in(list(_TERMINAL_JOB_STATES)),
    ).update(
        {
            Job.status: "failed",
            Job.current_stage: "failed",
            Job.error_summary: _MISSING_MESSAGE,
            Job.finished_at: now,
        },
        synchronize_session=False,
    )
    session.flush()


def _mark_session_failed(session, session_id: str, now: datetime) -> None:
    marked = (
        session.query(PlaygroundSession)
        .filter(
            PlaygroundSession.id == session_id,
            PlaygroundSession.status.not_in(list(_TERMINAL_JOB_STATES)),
        )
        .update(
            {
                PlaygroundSession.status: "failure_cleanup",
                PlaygroundSession.error_summary: _MISSING_MESSAGE,
                PlaygroundSession.finished_at: None,
            },
            synchronize_session=False,
        )
    )
    if marked == 1:
        # Transfer only passive children. Active CAS owners (spawn, workload,
        # create, or destroy workers) must finish or fail their own host action;
        # stealing their row here would race libvirt and artifact writes.
        session.query(JobVM).filter(
            JobVM.session_id == session_id,
            JobVM.state.in_(["pending", "running", "stopped", "failed", "teardown_failed"]),
        ).update(
            {
                JobVM.state: "teardown_failed",
                JobVM.error_summary: _MISSING_MESSAGE,
                JobVM.updated_at: now,
            },
            synchronize_session=False,
        )
        session.query(JobNetwork).filter(
            JobNetwork.session_id == session_id,
            JobNetwork.state.in_(["pending", "active", "failed"]),
        ).update(
            {
                JobNetwork.state: "failed",
                JobNetwork.error_summary: _MISSING_MESSAGE,
                JobNetwork.updated_at: now,
            },
            synchronize_session=False,
        )
    session.flush()


def _queue_terminal_session_resources(session, now: datetime) -> None:
    """Move passive children of cleanup-bound parents into teardown custody.

    This runs every reconciliation pass, so an active worker that later
    publishes `running`/`active` is collected on the next pass without having
    its in-flight custody overwritten.
    """
    cleanup_sessions = select(PlaygroundSession.id).where(PlaygroundSession.status.in_(list(_CLEANUP_PARENT_STATES)))
    session.query(JobVM).filter(
        JobVM.session_id.in_(cleanup_sessions),
        JobVM.state.in_(["pending", "running", "stopped", "stopping", "failed", "teardown_failed"]),
    ).update(
        {
            JobVM.state: "teardown_failed",
            JobVM.error_summary: _MISSING_MESSAGE,
            JobVM.updated_at: now,
        },
        synchronize_session=False,
    )
    session.query(JobNetwork).filter(
        JobNetwork.session_id.in_(cleanup_sessions),
        JobNetwork.state.in_(["pending", "active", "failed"]),
    ).update(
        {
            JobNetwork.state: "failed",
            JobNetwork.error_summary: _MISSING_MESSAGE,
            JobNetwork.updated_at: now,
        },
        synchronize_session=False,
    )
    session.flush()
