"""The phases `reconcile_libvirt_resources` runs, in the order it runs them.

Split out of `reconciliation.py` so that module stays readable: the entry point
and the primitives the phases are built from are one subject, and these eleven
passes over the host and the database are another.

Primitives are reached through the module object (`rc.<name>`) rather than
imported by name. That is not style. `reconciliation.py` is what tests
monkeypatch -- `reconciliation._fail_missing_network_if_unchanged` and friends --
and a `from .reconciliation import _foo` here would bind the original at import
time, so a patched primitive would silently not apply to these phases. Attribute
access resolves at call time and keeps that working.

`reconciliation.py` imports this module inside the entry function rather than at
module scope, which is what keeps the two from forming an import cycle.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from ..db.models import CapacityReservation, Job, JobNetwork, JobRuntime, JobVM, JobVMNic, PlaygroundSession
from . import reconciliation as rc


def _destroy_orphan_domains(
    *,
    libvirt_domains: dict[str, Any],
    libvirt_module,
    db_domains: set[str],
    live_job_id_set: set[str],
) -> dict[str, int]:
    """Destroy AstinusLab-owned host domains that no database row claims."""
    destroyed = 0
    for name, domain in libvirt_domains.items():
        if not rc._owned_domain_name(name) or name in db_domains:
            continue
        if rc._domain_owner_job_id(name) in live_job_id_set:
            # A live or deliberately retained job can lack a runtime row if
            # boot failed before it committed. Dedicated teardown owns it.
            rc.logger.warning("reconciler: skipping domain %s owned by live or retained job", name)
            continue
        try:
            rc._destroy_domain_strict(domain, libvirt_module)
            destroyed += 1
        except Exception:
            rc._record_reconciliation_event("reconciler: failed to destroy orphan domain %s", name)
    return {"orphan_domains_destroyed": destroyed}


def _destroy_orphan_networks(
    *,
    libvirt_networks: dict[str, Any],
    libvirt_module,
    db_networks: set[str],
) -> dict[str, int]:
    """Destroy AstinusLab-owned host networks that no database row claims."""
    destroyed = 0
    for name, network in libvirt_networks.items():
        if not rc._owned_network_name(name) or name in db_networks:
            continue
        try:
            rc._destroy_network_strict(network, libvirt_module)
            destroyed += 1
        except Exception:
            rc._record_reconciliation_event("reconciler: failed to destroy orphan network %s", name)
    return {"orphan_networks_destroyed": destroyed}


def _detach_host_orphan_nics(
    *,
    session_scope,
    libvirt_domains: dict[str, Any],
    libvirt_module,
) -> dict[str, int]:
    """Detach host interfaces on owned domains that no live NIC row claims."""
    with session_scope() as session:
        live_db_macs_by_domain: dict[str, set[str]] = {}
        for vm in session.scalars(select(JobVM).where(JobVM.state.not_in(list(rc._TERMINAL_RESOURCE_STATES)))):
            live_db_macs_by_domain[vm.domain_name] = {nic.mac.lower() for nic in vm.nics}

    detached = 0
    for name, domain in libvirt_domains.items():
        if not rc._owned_domain_name(name):
            continue
        expected = live_db_macs_by_domain.get(name)
        if expected is None:
            continue
        for mac in rc._domain_interface_macs(domain):
            if mac in expected:
                continue
            # attach_nic commits its pending row before mutating libvirt.
            # The snapshot above may predate that commit while this XML
            # already includes the new host interface, so re-read current
            # ownership immediately before any destructive detach.
            with session_scope() as session:
                current_owner = session.scalar(
                    select(JobVMNic.id)
                    .join(JobVM, JobVM.id == JobVMNic.job_vm_id)
                    .where(
                        JobVM.domain_name == name,
                        JobVM.state.not_in(list(rc._TERMINAL_RESOURCE_STATES)),
                        JobVMNic.mac == mac,
                    )
                    .limit(1)
                )
            if current_owner is not None:
                continue
            if rc._detach_host_orphan_nic(domain, mac, libvirt_module):
                detached += 1
            else:
                rc._record_reconciliation_event(
                    "reconciler: failed to detach host-orphan NIC mac=%s on %s",
                    mac,
                    name,
                    level=logging.WARNING,
                )
    return {"host_orphan_nics_detached": detached}


def _prune_orphan_nic_rows(*, session_scope) -> dict[str, int]:
    """Delete NIC rows only after their parent VM's destruction is confirmed."""
    with session_scope() as session:
        # Keep the parent-state predicate inside the DELETE statement. A
        # read-then-delete id snapshot can race failed→starting retry custody
        # and remove NICs that have become live again.
        # Failed teardown can leave a live domain. Its NICs still protect the
        # attached networks and must survive until host cleanup succeeds.
        destroyed_vm_ids = select(JobVM.id).where(JobVM.state == "destroyed")
        pruned = (
            session.query(JobVMNic).filter(JobVMNic.job_vm_id.in_(destroyed_vm_ids)).delete(synchronize_session=False)
        )
    return {"orphan_nic_rows_pruned": pruned}


def _resolve_incomplete_nic_rows(
    *,
    session_scope,
    libvirt_domains: dict[str, Any],
    now: datetime,
    grace_seconds: int,
) -> dict[str, int]:
    """Promote or drop NIC rows still `pending`/`attaching`, by asking the host.

    Every drop is a compare-and-swap against the row and its parent, so a
    concurrent retry that has already moved either one wins.
    """
    promoted_count = 0
    dropped_total = 0

    with session_scope() as session:
        pending_nics = list(session.scalars(select(JobVMNic).where(JobVMNic.state.in_(["pending", "attaching"]))))
        for nic in pending_nics:
            vm = session.get(JobVM, nic.job_vm_id)
            if vm is None:
                parent_exists = select(JobVM.id).where(JobVM.id == nic.job_vm_id).exists()
                dropped = (
                    session.query(JobVMNic)
                    .filter(
                        JobVMNic.id == nic.id,
                        JobVMNic.state == nic.state,
                        JobVMNic.job_vm_id == nic.job_vm_id,
                        JobVMNic.job_network_id == nic.job_network_id,
                        JobVMNic.mac == nic.mac,
                        JobVMNic.created_at == nic.created_at,
                        ~parent_exists,
                    )
                    .delete(synchronize_session=False)
                )
                dropped_total += dropped
                continue
            observed_vm_state = vm.state
            observed_vm_updated_at = vm.updated_at
            unchanged_parent = select(JobVM.id).where(
                JobVM.id == nic.job_vm_id,
                JobVM.state == observed_vm_state,
                JobVM.updated_at == observed_vm_updated_at,
            )

            if observed_vm_state == "destroyed":
                dropped_total += rc._drop_incomplete_nic_if_parent_unchanged(
                    session,
                    nic_id=nic.id,
                    vm_id=nic.job_vm_id,
                    network_id=nic.job_network_id,
                    mac=nic.mac,
                    created_at=nic.created_at,
                    nic_state=nic.state,
                    vm_state=observed_vm_state,
                    vm_updated_at=observed_vm_updated_at,
                )
                continue
            if observed_vm_state in {"failed", "teardown_failed"}:
                # Even an incomplete attach may have reached the host. Keep
                # network custody until the VM teardown confirms destruction.
                continue
            domain = libvirt_domains.get(vm.domain_name)
            if domain is None:
                if rc._is_old_enough(nic.created_at, now=now, grace_seconds=grace_seconds):
                    dropped_total += rc._drop_incomplete_nic_if_parent_unchanged(
                        session,
                        nic_id=nic.id,
                        vm_id=nic.job_vm_id,
                        network_id=nic.job_network_id,
                        mac=nic.mac,
                        created_at=nic.created_at,
                        nic_state=nic.state,
                        vm_state=observed_vm_state,
                        vm_updated_at=observed_vm_updated_at,
                    )
                continue
            if nic.mac.lower() in {m.lower() for m in rc._domain_interface_macs(domain)}:
                promoted = (
                    session.query(JobVMNic)
                    .filter(
                        JobVMNic.id == nic.id,
                        JobVMNic.state == nic.state,
                        JobVMNic.job_network_id == nic.job_network_id,
                        JobVMNic.mac == nic.mac,
                        JobVMNic.created_at == nic.created_at,
                        JobVMNic.job_vm_id.in_(unchanged_parent),
                    )
                    .update({JobVMNic.state: "active"}, synchronize_session=False)
                )
                promoted_count += promoted
            elif rc._is_old_enough(nic.created_at, now=now, grace_seconds=grace_seconds):
                dropped_total += rc._drop_incomplete_nic_if_parent_unchanged(
                    session,
                    nic_id=nic.id,
                    vm_id=nic.job_vm_id,
                    network_id=nic.job_network_id,
                    mac=nic.mac,
                    created_at=nic.created_at,
                    nic_state=nic.state,
                    vm_state=observed_vm_state,
                    vm_updated_at=observed_vm_updated_at,
                )

    return {"pending_nics_promoted": promoted_count, "pending_nics_dropped": dropped_total}


def _requeue_stale_nic_detaches(*, session_scope, now: datetime) -> dict[str, int]:
    """Re-enqueue queued detaches and reopen abandoned detach claims."""
    requeued = 0
    # A worker can die after claiming a queued detach but before the
    # libvirt call. Reopen only stale, exact claims; the next pass (or an
    # API retry) can then enqueue the token again. Fresh claims remain
    # exclusive so a duplicate delivery cannot mutate the host twice.
    from . import repositories as pr

    stale_detaches: list[tuple[str, str, str, str]] = []
    with session_scope() as session:
        pr.lock_resource_transitions(session)
        for nic in list(session.scalars(select(JobVMNic).where(JobVMNic.state == "detaching"))):
            token = nic.detach_token
            vm = session.get(JobVM, nic.job_vm_id)
            if token is not None and vm is not None:
                # This is the durable queued state. Requeueing is safe:
                # the worker's queued -> claimed CAS admits only one host
                # mutator, even if a broker delivery is duplicated.
                stale_detaches.append((vm.session_id, vm.id, nic.id, token))
        stale_before = now - timedelta(seconds=pr.NIC_DETACH_CLAIM_SECONDS)
        for nic in list(
            session.scalars(
                select(JobVMNic).where(
                    JobVMNic.state == "detach_claimed",
                    JobVMNic.detach_claimed_at.is_not(None),
                    JobVMNic.detach_claimed_at <= stale_before,
                )
            )
        ):
            token = nic.detach_token
            vm = session.get(JobVM, nic.job_vm_id)
            if token is None or vm is None:
                continue
            new_token = secrets.token_hex(16)
            if pr.reopen_nic_detach_claim(
                session,
                nic.id,
                vm_id=nic.job_vm_id,
                detach_token=token,
                claimed_at=nic.detach_claimed_at,
                new_token=new_token,
            ):
                stale_detaches.append((vm.session_id, vm.id, nic.id, new_token))

    if stale_detaches:
        from .orchestrator import detach_nic as detach_nic_task

        for session_id, vm_id, nic_id, token in stale_detaches:
            detach_nic_task.delay(job_id=session_id, vm_id=vm_id, nic_id=nic_id, detach_token=token)
            requeued += 1

    return {"detaching_nics_requeued": requeued}


def _sync_power_states(
    *,
    session_scope,
    libvirt_domains: dict[str, Any],
    live_session_ids: list[str],
    now: datetime,
) -> dict[str, int]:
    """Bring recorded VM power state in line with what the host reports."""
    synced = 0
    with session_scope() as session:
        for vm in session.scalars(
            select(JobVM)
            .where(JobVM.session_id.in_(live_session_ids))
            .where(JobVM.state.in_(["booting", "stopping", "running"]))
        ):
            observed_state = vm.state
            observed_updated_at = vm.updated_at
            domain = libvirt_domains.get(vm.domain_name)
            if domain is None:
                continue
            try:
                active = bool(domain.isActive())
            except Exception as exc:
                rc.logger.debug("reconciliation: cannot probe domain state: %s", exc)
                continue
            next_state = None
            if active and observed_state == "booting":
                next_state = "running"
            elif not active and observed_state in {"stopping", "running"}:
                next_state = "stopped"
            if next_state is not None:
                updated = (
                    session.query(JobVM)
                    .filter(
                        JobVM.id == vm.id,
                        JobVM.state == observed_state,
                        JobVM.updated_at == observed_updated_at,
                    )
                    .update(
                        {JobVM.state: next_state, JobVM.updated_at: now},
                        synchronize_session=False,
                    )
                )
                synced += updated

    return {"power_states_synced": synced}


def _recover_legacy_vm_teardowns(
    *,
    session_scope,
    libvirt_domains: dict[str, Any],
    libvirt_module,
    conn,
    now: datetime,
    grace_seconds: int,
) -> dict[str, int]:
    """Finish teardown for VM rows a previous pass or worker left behind."""
    from . import repositories as pr

    recovered = 0
    # Retry the complete teardown, including staged disk/cloud-init files.
    # A missing libvirt domain is not enough to call the row destroyed: the
    # original failure may have been descriptor-safe artifact cleanup.
    with session_scope() as session:
        from .orchestrator import _playground_artifacts_present

        teardown_candidates = []
        for vm in session.scalars(select(JobVM).where(JobVM.state.in_(["teardown_failed", "failed", "destroyed"]))):
            # A terminal row created earlier in this reconciliation pass is
            # not legacy residue. More importantly, derive candidates from
            # this fresh state instead of a stale start-of-pass id snapshot:
            # a concurrent retry may already own the row as `starting`.
            if vm.state != "teardown_failed" and not rc._is_old_enough(
                vm.updated_at,
                now=now,
                grace_seconds=grace_seconds,
            ):
                continue
            residue_present = False
            if vm.state != "teardown_failed" and vm.domain_name not in libvirt_domains:
                try:
                    residue_present = _playground_artifacts_present(Path(vm.disk_path))
                except (OSError, RuntimeError):
                    # Let the strict cleanup path retain the row as
                    # teardown_failed with the concrete custody error.
                    residue_present = True
            if vm.state == "teardown_failed" or vm.domain_name in libvirt_domains or residue_present:
                teardown_candidates.append((vm.id, vm.state, vm.updated_at, vm.error_summary))
    for vm_id, expected_state, expected_updated_at, prior_error_summary in teardown_candidates:
        with session_scope() as session:
            from . import repositories as pr

            if expected_state == "teardown_failed":
                claimed = pr.claim_vm_for_teardown_retry(session, vm_id)
            else:
                claimed = pr.claim_legacy_vm_for_teardown(
                    session,
                    vm_id,
                    expected_state,
                    expected_updated_at,
                )
            if not claimed:
                continue
            vm = session.get(JobVM, vm_id)
            assert vm is not None  # noqa: S101 - successful CAS guarantees the row
            session.refresh(vm)
            domain_name = vm.domain_name
            disk_path = Path(vm.disk_path)
            lease_token = vm.updated_at
        from .orchestrator import _remove_playground_artifacts
        from .resource_custody import LostResourceCustody, ResourceClaimLease

        claim_lease = ResourceClaimLease(
            kind="vm",
            resource_id=vm_id,
            state="destroying",
            token=lease_token,
            session_scope_factory=session_scope,
            grace_seconds=grace_seconds,
        )
        try:
            claim_lease.start()
            claim_lease.assert_owned()
            # The pass-start map can predate a concurrent worker defining
            # this exact registered name. Once teardown custody is claimed,
            # resolve the host object again before deleting artifacts or
            # publishing `destroyed`.
            domain = rc._lookup_domain_or_none(conn, domain_name, libvirt_module)
            if domain is not None:
                rc._destroy_domain_strict(domain, libvirt_module)
            claim_lease.assert_owned()
            _remove_playground_artifacts(disk_path)
            claim_lease.assert_owned()
            publication_token = claim_lease.publication_token()
            with session_scope() as session:
                if not pr.finish_vm_destroy(
                    session,
                    vm_id,
                    "destroyed",
                    expected_updated_at=publication_token,
                    error=None if expected_state == "teardown_failed" else prior_error_summary,
                ):
                    raise LostResourceCustody(f"VM {vm_id} lost reconciliation teardown custody")
                session.query(JobVMNic).filter_by(job_vm_id=vm_id).delete()
                if expected_state in {"failed", "destroyed"}:
                    recovered += 1
        except LostResourceCustody as exc:
            rc._record_reconciliation_event(
                "reconciler: lost teardown custody for %s: %s",
                domain_name,
                exc,
            )
        except Exception as exc:
            try:
                publication_token = claim_lease.publication_token()
                with session_scope() as session:
                    if not pr.finish_vm_destroy(
                        session,
                        vm_id,
                        "teardown_failed",
                        expected_updated_at=publication_token,
                        error=f"reconciler: teardown retry failed: {exc}",
                    ):
                        raise LostResourceCustody(f"VM {vm_id} lost reconciliation teardown custody on failure")
            except LostResourceCustody as custody_error:
                rc._record_reconciliation_event(
                    "reconciler: lost teardown custody for %s: %s",
                    domain_name,
                    custody_error,
                )
            rc._record_reconciliation_event(
                "reconciler: retry destroy failed for %s: %s",
                domain_name,
                exc,
            )
        finally:
            claim_lease.stop()

    return {"legacy_vm_teardowns_recovered": recovered}


def _recover_legacy_network_teardowns(
    *,
    session_scope,
    libvirt_networks: dict[str, Any],
    libvirt_module,
    conn,
    grace_seconds: int,
) -> dict[str, int]:
    """Finish teardown for network rows a previous pass or worker left behind."""
    from . import repositories as pr

    recovered = 0
    # Networks are torn down only after terminal-session VMs and their NICs.
    # Create retry accepts the same legacy states, so the compare-and-swap
    # decides which side owns libvirt and prevents double-destroy.
    with session_scope() as session:
        legacy_network_candidates = [
            (network.id, network.state, network.updated_at, network.libvirt_net_name)
            for network in session.scalars(
                select(JobNetwork)
                .join(PlaygroundSession, PlaygroundSession.id == JobNetwork.session_id)
                .where(JobNetwork.state.in_(["failed", "destroyed", "teardown_failed"]))
                .where(
                    # Leaked host state, whatever the row says; or a failed
                    # row under a parent being cleaned up, which still
                    # blocks _finalize_recovered_cleanup_sessions and has to
                    # settle to 'destroyed' exactly once. A destroyed row
                    # with nothing left in libvirt is finished work: match
                    # it and every pass re-claims it (destroyed ->
                    # destroying -> destroyed) and never converges.
                    (JobNetwork.libvirt_net_name.in_(list(libvirt_networks)))
                    | (JobNetwork.state == "teardown_failed")
                    | ((JobNetwork.state == "failed") & PlaygroundSession.status.in_(list(rc._CLEANUP_PARENT_STATES)))
                )
            )
        ]
    for network_id, expected_state, expected_updated_at, libvirt_name in legacy_network_candidates:
        try:
            with session_scope() as session:
                from . import repositories as pr

                # Serialize attachment checks with the teardown claim, just
                # as the normal network destroy path does.
                pr.lock_resource_transitions(session)
                if pr.network_has_attached_vms(session, network_id):
                    continue
                if not pr.claim_legacy_network_for_teardown(
                    session,
                    network_id,
                    expected_state,
                    expected_updated_at,
                ):
                    continue
                network_row = session.get(JobNetwork, network_id)
                assert network_row is not None  # noqa: S101 - successful CAS guarantees the row
                session.refresh(network_row)
                lease_token = network_row.updated_at
        except IntegrityError:
            # ux_job_networks_session_id_name admits duplicate session+name
            # rows only while destroyed, so a sibling parked in 'failed'
            # blocks this claim. Skip the row: it is unreachable until that
            # sibling settles, and raising here aborted every remaining
            # step of the pass.
            rc._record_reconciliation_event(
                "reconciler: legacy network %s not claimable; session+name held by another row",
                libvirt_name,
                level=logging.WARNING,
            )
            continue
        from .resource_custody import LostResourceCustody, ResourceClaimLease

        claim_lease = ResourceClaimLease(
            kind="network",
            resource_id=network_id,
            state="destroying",
            token=lease_token,
            session_scope_factory=session_scope,
            grace_seconds=grace_seconds,
        )
        try:
            claim_lease.start()
            claim_lease.assert_owned()
            # As with domains, never finalize from the pass-start snapshot:
            # a create worker may have defined the network before losing
            # the row-level CAS to this teardown claim.
            network = rc._lookup_network_or_none(conn, libvirt_name, libvirt_module)
            if network is not None:
                rc._destroy_network_strict(network, libvirt_module)
            claim_lease.assert_owned()
            publication_token = claim_lease.publication_token()
            with session_scope() as session:
                if not pr.finish_network_destroy(
                    session,
                    network_id,
                    "destroyed",
                    expected_updated_at=publication_token,
                ):
                    raise LostResourceCustody(f"network {network_id} lost reconciliation teardown custody")
                recovered += 1
        except LostResourceCustody as exc:
            rc._record_reconciliation_event(
                "reconciler: lost network teardown custody for %s: %s",
                libvirt_name,
                exc,
            )
        except Exception as exc:
            try:
                publication_token = claim_lease.publication_token()
                with session_scope() as session:
                    if not pr.finish_network_destroy(
                        session,
                        network_id,
                        "teardown_failed" if expected_state == "teardown_failed" else "failed",
                        expected_updated_at=publication_token,
                        error=f"reconciler: network teardown recovery failed: {exc}",
                    ):
                        raise LostResourceCustody(
                            f"network {network_id} lost reconciliation teardown custody on failure"
                        )
            except LostResourceCustody as custody_error:
                rc._record_reconciliation_event(
                    "reconciler: lost network teardown custody for %s: %s",
                    libvirt_name,
                    custody_error,
                )
            rc._record_reconciliation_event(
                "reconciler: retry destroy failed for network %s: %s",
                libvirt_name,
                exc,
            )
        finally:
            claim_lease.stop()

    return {"legacy_network_teardowns_recovered": recovered}


def _snapshot_owned_names(*, session_scope) -> tuple[set[str], set[str], set[str]]:
    """The names the database claims, plus live or deliberately retained job ids.

    Returned rather than recomputed per phase: every later phase must decide
    orphanhood against the *same* snapshot, or two phases can disagree about
    who owns a name while a concurrent spawn is claiming it.
    """
    with session_scope() as session:
        # JobVM/JobNetwork rows are owned by playground sessions; JobRuntime
        # rows are owned by forensic jobs.
        live_jobs = select(Job.id).where(Job.status.not_in(list(rc._TERMINAL_JOB_STATES)))
        # Match capacity cleanup's durable keep_failed_vm policy. Terminal
        # status alone does not make a deliberately retained VM an orphan.
        retained_jobs = (
            select(Job.id)
            .join(CapacityReservation)
            .where(
                Job.status == "failed",
                CapacityReservation.state == "reserved",
                CapacityReservation.boot_started.is_(True),
                CapacityReservation.preserve_failed_vm.is_(True),
            )
        )
        live_sessions = select(PlaygroundSession.id).where(
            PlaygroundSession.status.not_in(list(rc._TERMINAL_JOB_STATES))
        )
        db_domains = set(
            session.scalars(
                select(JobVM.domain_name)
                .where(JobVM.session_id.in_(live_sessions))
                .where(JobVM.state.not_in(list(rc._TERMINAL_RESOURCE_STATES)))
            )
        )
        db_domains.update(
            session.scalars(
                select(JobRuntime.vm_name).where(
                    or_(
                        JobRuntime.job_id.in_(live_jobs) & JobRuntime.state.not_in(list(rc._TERMINAL_RUNTIME_STATES)),
                        JobRuntime.job_id.in_(retained_jobs),
                    )
                )
            )
        )
        # Every registered playground row owns its domain name, including
        # terminal/retryable rows. A concurrent spawn can atomically claim
        # one after this snapshot; the generic orphan pass must never tear
        # down a registered domain underneath that new boot. Dedicated VM
        # teardown paths own cleanup for these rows.
        db_domains.update(session.scalars(select(JobVM.domain_name)))
        db_networks = set(
            session.scalars(
                select(JobNetwork.libvirt_net_name)
                .where(JobNetwork.session_id.in_(live_sessions))
                .where(JobNetwork.state.not_in(list(rc._TERMINAL_RESOURCE_STATES)))
            )
        )
        # As with VM names, every registered network name remains owned by
        # its row. A failed/destroyed row can be claimed concurrently for a
        # create retry after this snapshot; generic orphan cleanup must not
        # destroy the newly created host network underneath that custody.
        db_networks.update(session.scalars(select(JobNetwork.libvirt_net_name)))
        live_job_id_set = set(session.scalars(live_jobs))
        live_job_id_set.update(session.scalars(retained_jobs))

    return db_domains, db_networks, live_job_id_set


def _reconcile_database_rows(
    *,
    session_scope,
    conn,
    libvirt_module,
    libvirt_domains: dict[str, Any],
    libvirt_networks: dict[str, Any],
    result: dict[str, int],
    now: datetime,
    grace_seconds: int,
) -> list[str]:
    """Reconcile VM, network and runtime rows against the host, in one session.

    These four run in a single transaction on purpose, so returns the live
    session ids the power-state pass needs rather than re-querying them.
    """
    with session_scope() as session:
        rc._recover_stale_resource_claims(
            session,
            result=result,
            now=now,
            grace_seconds=grace_seconds,
        )
        live_job_ids = list(session.scalars(select(Job.id).where(Job.status.not_in(list(rc._TERMINAL_JOB_STATES)))))
        live_session_ids = list(
            session.scalars(
                select(PlaygroundSession.id).where(PlaygroundSession.status.not_in(list(rc._TERMINAL_JOB_STATES)))
            )
        )
        rc._reconcile_vm_rows(
            session,
            conn=conn,
            libvirt_module=libvirt_module,
            libvirt_domains=libvirt_domains,
            live_session_ids=live_session_ids,
            result=result,
            now=now,
            grace_seconds=grace_seconds,
        )
        rc._reconcile_network_rows(
            session,
            conn=conn,
            libvirt_module=libvirt_module,
            libvirt_networks=libvirt_networks,
            live_session_ids=live_session_ids,
            result=result,
            now=now,
            grace_seconds=grace_seconds,
        )
        for runtime in session.scalars(
            select(JobRuntime)
            .where(JobRuntime.job_id.in_(live_job_ids))
            .where(JobRuntime.state.not_in(list(rc._TERMINAL_RUNTIME_STATES)))
        ):
            if runtime.vm_name in libvirt_domains:
                continue
            parent = session.get(Job, runtime.job_id)
            if (
                runtime.state == "pending"
                and parent is not None
                and parent.started_at is None
                and parent.status in {"queued", "waiting_for_capacity"}
            ):
                # The API registers target/attacker placeholders before
                # admission. Queue age cannot prove a missing VM: no boot
                # is expected until the launch worker claims this Job.
                continue
            if not rc._is_old_enough(runtime.created_at, now=now, grace_seconds=grace_seconds):
                continue
            runtime.state = "teardown_failed"
            runtime.power_state = "unknown"
            runtime.updated_at = now
            result["zombie_runtimes_failed"] += 1
            rc._mark_job_failed(session, runtime.job_id, now)

        # Runtime rows whose job is terminal or gone are invisible to the
        # sweep above, yet has_active_non_playground_job() still reads them
        # as a live forensic job and capacity.shortage() still charges them
        # host memory. Release them once libvirt proves the VM is gone:
        # without this a single failed teardown blocks the Playground and
        # starves admission permanently, with no operator escape hatch.
        for runtime in session.scalars(
            select(JobRuntime).where(JobRuntime.job_id.not_in(live_job_ids)).where(JobRuntime.state != "destroyed")
        ):
            if runtime.vm_name in libvirt_domains:
                continue  # real residue: stay charged until the domain is gone
            if not rc._is_old_enough(runtime.updated_at, now=now, grace_seconds=grace_seconds):
                continue
            runtime.state = "destroyed"
            runtime.power_state = "off"
            runtime.updated_at = now
            result["stale_runtimes_released"] += 1

    return live_session_ids
