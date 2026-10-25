from __future__ import annotations

import logging
import re
import shutil
from collections.abc import Callable
from datetime import UTC, datetime

from ..core.config import settings
from ..db import session_scope as _pg_session_scope

logger = logging.getLogger(__name__)

_ORPHAN_ACTION_STATUSES = {"running", "queued"}
_TERMINAL_ACTION_STATUSES = {"completed", "failed", "cancelled"}
_TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled"}
_INTERRUPTED_MESSAGE = "Job interrupted: worker restarted unexpectedly"

# Playground session directories are named `pg-<12 hex>`. The startup sweep
# only removes directories that match this pattern, AND only when their
# job_id is not in an active playground state (see ACTIVE_PLAYGROUND_STATES
# in playground.repositories). Capture artifacts live under a separate
# settings.playground_capture_dir tree and are never touched by this sweep
# — chain-of-custody requires they outlive the worker that produced them.
_PLAYGROUND_SESSION_DIR_RE = re.compile(r"^pg-[0-9a-f]+$")


def _teardown_id_from_vm_name(vm_name: str) -> str:
    return vm_name.removeprefix("astinuslab-")


def _libvirt_open():
    import libvirt

    return libvirt.open(settings.libvirt_uri)


def reconcile_orphaned_jobs(
    session_scope,
    repo_cls,
    teardown_fn: Callable[[str], bool | None],
    mark_runtime_destroyed_fn: Callable[[str, str], None],
    logger,
    settings,
) -> int:
    """Scan for orphaned jobs and recover them.

    Called once at Celery worker startup. An orphaned job is any job that:
    - is NOT in a terminal state (completed / failed / cancelled), AND
    - has already started (started_at IS NOT NULL)
    """
    with session_scope() as session:
        repo = repo_cls(session)
        from .action_lifecycle import owner_alive

        orphans = [
            (job.id, job.attacker_vm_name or (f"{job.id}-attacker" if job.attacker_vm_id else None))
            for job in repo.list_orphaned_jobs()
            if not any(
                action.status == "running" and action.owner_token and owner_alive(action.owner_token)
                for action in repo.list_actions(job.id)
            )
        ]

    if not orphans:
        logger.info("[startup_recovery] no orphaned jobs found")
        return 0

    logger.warning(
        "[startup_recovery] found %d orphaned job(s) from previous worker; recovering",
        len(orphans),
    )

    recovered = 0
    for job_id, attacker_vm_name in orphans:
        try:
            _recover_one_job(
                job_id=job_id,
                attacker_vm_name=attacker_vm_name or None,
                session_scope=session_scope,
                repo_cls=repo_cls,
                teardown_fn=teardown_fn,
                mark_runtime_destroyed_fn=mark_runtime_destroyed_fn,
                logger=logger,
            )
            recovered += 1
        except Exception as exc:
            logger.error("[startup_recovery] failed to recover job %s: %s", job_id, exc)

    logger.info("[startup_recovery] recovered %d orphaned job(s)", recovered)
    return recovered


def _recover_one_job(
    *,
    job_id: str,
    attacker_vm_name: str | None,
    session_scope,
    repo_cls,
    teardown_fn: Callable[[str], bool | None],
    mark_runtime_destroyed_fn: Callable[[str, str], None],
    logger,
) -> None:
    """Recover a single orphaned job. All sub-steps are non-fatal."""
    now = datetime.now(UTC)

    # Recheck before touching VMs: an action on a completed capture has an
    # independent recovery owner and must not turn into parent VM teardown.
    with session_scope() as session:
        job = repo_cls(session).get_job(job_id)
        if job is None or job.status in _TERMINAL_JOB_STATUSES:
            return

    # 1. Teardown VM (non-fatal)
    teardown_succeeded = True
    try:
        teardown_result = teardown_fn(job_id)
        teardown_succeeded = teardown_result is not False
    except Exception as exc:
        teardown_succeeded = False
        logger.warning("[startup_recovery] teardown failed for %s (non-fatal): %s", job_id, exc)

    # 2. Mark runtimes destroyed (non-fatal)
    if teardown_succeeded:
        try:
            mark_runtime_destroyed_fn(job_id, "target")
        except Exception as exc:
            logger.warning(
                "[startup_recovery] mark_runtime_destroyed target failed for %s (non-fatal): %s",
                job_id,
                exc,
            )

    if attacker_vm_name:
        attacker_teardown_id = _teardown_id_from_vm_name(attacker_vm_name)
        attacker_teardown_succeeded = True
        try:
            attacker_teardown_result = teardown_fn(attacker_teardown_id)
            attacker_teardown_succeeded = attacker_teardown_result is not False
        except Exception as exc:
            attacker_teardown_succeeded = False
            logger.warning(
                "[startup_recovery] teardown attacker failed for %s (%s, non-fatal): %s",
                job_id,
                attacker_vm_name,
                exc,
            )
        if attacker_teardown_succeeded:
            try:
                mark_runtime_destroyed_fn(job_id, "attacker")
            except Exception as exc:
                logger.warning(
                    "[startup_recovery] mark_runtime_destroyed attacker failed for %s (non-fatal): %s",
                    job_id,
                    exc,
                )

    should_mark_completed = False
    current_job_status: str | None = None

    # 3. Reconcile HostActions and capture the latest durable action status.
    try:
        with session_scope() as session:
            repo = repo_cls(session)
            job = repo.get_job(job_id)
            if job is None:
                logger.warning("[startup_recovery] job %s disappeared during recovery", job_id)
                return
            current_job_status = job.status
            if current_job_status in _TERMINAL_JOB_STATUSES:
                logger.info(
                    "[startup_recovery] job %s already terminal (%s); no status change needed",
                    job_id,
                    current_job_status,
                )
                return

            actions = repo.list_actions(job_id)
            latest_action = actions[-1] if actions else None
            latest_action_status = latest_action.status if latest_action is not None else None
            should_mark_completed = current_job_status == "publishing_artifact" and latest_action_status == "completed"
            for action in actions:
                if action.status in _ORPHAN_ACTION_STATUSES:
                    repo.update_action(
                        action.id,
                        status="failed",
                        error_summary=_INTERRUPTED_MESSAGE,
                        finished_at=now,
                    )
    except Exception as exc:
        logger.warning("[startup_recovery] action cleanup failed for %s (non-fatal): %s", job_id, exc)

    # 4. Mark job terminal based on the reconciled action state.
    with session_scope() as session:
        repo = repo_cls(session)
        job = repo.get_job(job_id)
        if job is None:
            return
        if job.status in _TERMINAL_JOB_STATUSES:
            return

        if should_mark_completed:
            repo.transition_job_status(job_id, "completed", finished_at=now)
            repo.add_event(
                job_id,
                "completed",
                "Job already completed before worker restart; reconciled as completed.",
            )
            return

        repo.transition_job_status(
            job_id,
            "failed",
            error_summary=_INTERRUPTED_MESSAGE,
            finished_at=now,
        )
        repo.add_event(job_id, "failed", _INTERRUPTED_MESSAGE)


def cleanup_stale_playground_resources() -> None:
    """Forcefully clean up libvirt and stale-disk resources for playground sessions.

    Called from the Celery worker_ready signal. The sweep:
      * destroys/undefines libvirt domains and networks with the
        astinuslab-pg- prefix,
      * purges only those `pg-<hex>` subdirectories under
        settings.playground_disk_dir whose job_id is NOT in
        ACTIVE_PLAYGROUND_STATES (i.e. truly orphaned overlays),
      * never touches settings.playground_capture_dir -- completed captures
        must survive worker restarts and session destruction (chain of
        custody),
      * delegates DB-backed cleanup of in-flight playground sessions to
        playground_cleanup.purge_session_resources.
    """
    logger.info("Cleaning up stale playground resources...")

    # Verification and package-instance lifecycle operations deliberately keep
    # their sessions' guests and overlays alive. Resolve host ownership through
    # the durable child rows instead of parsing names: both session ids and
    # labels may contain hyphens, so prefix splitting is ambiguous and can
    # destroy the wrong lab.
    from ..db.models import JobNetwork, JobVM, PlaygroundSession
    from ..playground.repositories import ACTIVE_PLAYGROUND_STATES, AUTHORING_CLOSED

    with _pg_session_scope() as _s:
        frozen_session_ids = {
            jid for (jid,) in _s.query(PlaygroundSession.id).filter(PlaygroundSession.status == AUTHORING_CLOSED).all()
        }
        package_session_ids = {
            session_id
            for (session_id,) in (
                _s.query(PlaygroundSession.id)
                .filter(
                    PlaygroundSession.lab_package_record_id.is_not(None),
                    PlaygroundSession.status.in_(ACTIVE_PLAYGROUND_STATES),
                )
                .all()
            )
        }
        protected_session_ids = frozen_session_ids | package_session_ids
        protected_domain_names = {
            dom
            for (dom,) in _s.query(JobVM.domain_name)
            .join(PlaygroundSession, PlaygroundSession.id == JobVM.session_id)
            .filter(PlaygroundSession.status == AUTHORING_CLOSED)
            .all()
            if dom
        }
        protected_network_names = {
            net
            for (net,) in _s.query(JobNetwork.libvirt_net_name)
            .join(PlaygroundSession, PlaygroundSession.id == JobNetwork.session_id)
            .filter(PlaygroundSession.status == AUTHORING_CLOSED)
            .all()
            if net
        }
        protected_domain_names.update(
            domain_name
            for (domain_name,) in (
                _s.query(JobVM.domain_name)
                .join(PlaygroundSession, PlaygroundSession.id == JobVM.session_id)
                .filter(PlaygroundSession.id.in_(package_session_ids))
                .all()
            )
            if domain_name
        )
        protected_network_names.update(
            network_name
            for (network_name,) in (
                _s.query(JobNetwork.libvirt_net_name)
                .join(PlaygroundSession, PlaygroundSession.id == JobNetwork.session_id)
                .filter(PlaygroundSession.id.in_(package_session_ids))
                .all()
            )
            if network_name
        )
        active_session_ids = {
            jid
            for (jid,) in _s.query(PlaygroundSession.id)
            .filter(PlaygroundSession.status.in_(ACTIVE_PLAYGROUND_STATES))
            .all()
        }

    def _is_active_package_session(session_id: str) -> bool:
        with _pg_session_scope() as _s:
            return (
                _s.query(PlaygroundSession.id)
                .filter(
                    PlaygroundSession.id == session_id,
                    PlaygroundSession.lab_package_record_id.is_not(None),
                    PlaygroundSession.status.in_(ACTIVE_PLAYGROUND_STATES),
                )
                .first()
                is not None
            )

    def _active_package_session_ids(_s) -> set[str]:
        return {
            session_id
            for (session_id,) in (
                _s.query(PlaygroundSession.id)
                .filter(
                    PlaygroundSession.lab_package_record_id.is_not(None),
                    PlaygroundSession.status.in_(ACTIVE_PLAYGROUND_STATES),
                )
                .all()
            )
        }

    def _is_protected_domain(dom_name: str) -> bool:
        if dom_name in protected_domain_names:
            return True
        if any(dom_name.startswith(f"astinuslab-pg-{sid}-") for sid in protected_session_ids):
            return True
        with _pg_session_scope() as _s:
            has_registered_name = (
                _s.query(JobVM.id)
                .join(PlaygroundSession, PlaygroundSession.id == JobVM.session_id)
                .filter(
                    JobVM.domain_name == dom_name,
                    PlaygroundSession.lab_package_record_id.is_not(None),
                    PlaygroundSession.status.in_(ACTIVE_PLAYGROUND_STATES),
                )
                .first()
                is not None
            )
            current_package_session_ids = _active_package_session_ids(_s)
            return has_registered_name or any(
                dom_name.startswith(f"astinuslab-pg-{session_id}-") for session_id in current_package_session_ids
            )

    def _is_protected_network(net_name: str) -> bool:
        if net_name in protected_network_names:
            return True
        if any(net_name.startswith(f"astinuslab-pg-{sid}-") for sid in protected_session_ids):
            return True
        with _pg_session_scope() as _s:
            has_registered_name = (
                _s.query(JobNetwork.id)
                .join(PlaygroundSession, PlaygroundSession.id == JobNetwork.session_id)
                .filter(
                    JobNetwork.libvirt_net_name == net_name,
                    PlaygroundSession.lab_package_record_id.is_not(None),
                    PlaygroundSession.status.in_(ACTIVE_PLAYGROUND_STATES),
                )
                .first()
                is not None
            )
            current_package_session_ids = _active_package_session_ids(_s)
            return has_registered_name or any(
                net_name.startswith(f"astinuslab-pg-{session_id}-") for session_id in current_package_session_ids
            )

    conn = None
    try:
        conn = _libvirt_open()
        if not conn:
            return

        # 1. Cleanup libvirt domains
        try:
            for dom in conn.listAllDomains():
                name = dom.name()
                if name.startswith("astinuslab-pg-"):
                    if _is_protected_domain(name):
                        logger.info("Startup cleanup: preserving protected VM %s", name)
                        continue
                    logger.info("Startup cleanup: destroying stale VM %s", name)
                    if dom.isActive():
                        dom.destroy()
                    dom.undefine()
        except Exception:
            logger.exception("Startup cleanup: failed to cleanup libvirt domains")

        # 2. Cleanup libvirt networks
        try:
            for net in conn.listAllNetworks():
                name = net.name()
                if name.startswith("astinuslab-pg-"):
                    if _is_protected_network(name):
                        logger.info("Startup cleanup: preserving protected network %s", name)
                        continue
                    logger.info("Startup cleanup: destroying stale network %s", name)
                    if net.isActive():
                        net.destroy()
                    net.undefine()
        except Exception:
            logger.exception("Startup cleanup: failed to cleanup libvirt networks")

    except ImportError:
        return
    except Exception:
        logger.exception("Startup cleanup: failed to connect to libvirt")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    # 3. Cleanup filesystem storage.
    # C1: settings.playground_capture_dir is intentionally NOT swept here.
    # Captures (memory ELF, manifest, sha256, disk qcow2) are forensic
    # artifacts referenced by Acquisition DB rows; deleting them on every
    # worker restart breaks chain of custody. Only stale disk overlays
    # (qcow2 backing files for VMs that no longer have an active session)
    # are purged, and only when their job_id is NOT in an active state.
    disk_base = settings.playground_disk_dir
    if disk_base.exists():
        try:
            for job_dir in disk_base.iterdir():
                if not job_dir.is_dir():
                    continue
                if not _PLAYGROUND_SESSION_DIR_RE.match(job_dir.name):
                    continue
                if job_dir.name in active_session_ids:
                    continue
                if _is_active_package_session(job_dir.name):
                    continue
                logger.info("Startup cleanup: purging playground directory %s", job_dir)
                shutil.rmtree(job_dir, ignore_errors=True)
        except Exception:
            logger.exception("Startup cleanup: failed to cleanup storage subdirectory %s", disk_base)

    # 4. Cleanup tmp directories
    tmp_base = settings.storage_dir / "tmp"
    if tmp_base.exists():
        try:
            for tmp_job_dir in tmp_base.iterdir():
                if not tmp_job_dir.is_dir():
                    continue
                if not _PLAYGROUND_SESSION_DIR_RE.match(tmp_job_dir.name):
                    continue
                if tmp_job_dir.name in package_session_ids:
                    continue
                if _is_active_package_session(tmp_job_dir.name):
                    continue
                shutil.rmtree(tmp_job_dir, ignore_errors=True)
        except Exception:
            logger.exception("Startup cleanup: failed to cleanup storage/tmp")

    # 5. DB-backed session cleanup
    from ..playground import cleanup as playground_cleanup

    with _pg_session_scope() as s:
        job_ids = [
            playground_session.id
            for playground_session in (
                s.query(PlaygroundSession)
                .filter(
                    PlaygroundSession.status.in_(("queued", "running")),
                    PlaygroundSession.lab_package_record_id.is_(None),
                )
                .all()
            )
        ]

    for job_id in job_ids:
        if _is_active_package_session(job_id):
            continue
        playground_cleanup.purge_session_resources(
            job_id=job_id,
            final_status="failed",
            error_summary="purged on backend startup",
        )


def recover_playground_sessions() -> None:
    """Alias for compatibility with celery_worker.py signal."""
    cleanup_stale_playground_resources()


def reconcile_publication_attempts(
    session_scope=None,
    *,
    storage_root=None,
    logger=logger,
):
    """Resolve publication attempts an interrupted worker left open.

    Runs at worker start, beside the job and Playground sweeps. Silent when there
    is nothing to do; anything it touched is worth an operator's attention,
    because each case means a capture did not finish the way it was supposed to.
    """
    from ..storage import publication

    scope = session_scope or _pg_session_scope
    root = storage_root if storage_root is not None else settings.storage_dir
    report = publication.reconcile(scope, storage_root=root)
    if (
        report.closed
        or report.quarantined
        or report.completed
        or report.integrity_failures
        or report.cleanup_incomplete
        or report.missing_evidence
    ):
        logger.warning("[startup_recovery] publication reconciliation: %s", report.summary())
        for path in report.missing_evidence:
            logger.error("[startup_recovery] published evidence is missing from disk: %s", path)
    return report


def reconcile_lab_package_storage(session_scope=None) -> dict[str, int]:
    """Finish or quarantine portable-package publications interrupted by a crash."""
    from ..lab_packages.package_service import default_package_service

    scope = session_scope or _pg_session_scope
    with scope() as session:
        return default_package_service().reconcile(session)
