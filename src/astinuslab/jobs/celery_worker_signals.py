from __future__ import annotations

import copy
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from celery.signals import task_failure as _task_failure_signal
from celery.signals import task_postrun as _task_postrun_signal
from celery.signals import task_prerun as _task_prerun_signal
from celery.signals import task_revoked as _task_revoked_signal
from celery.signals import worker_ready as _worker_ready_signal
from celery.signals import worker_shutting_down as _worker_shutting_down_signal
from celery.worker.control import inspect_command

from ..core import fake_mode
from ..core.config import settings
from ..core.request_context import (
    REQUEST_ID_HEADER,
    bind_job_context,
    bind_request_id,
    current_job_context,
    current_request_id,
    set_request_id,
)
from ..virtualization.host_script_manifest import check_privileged_helpers

if TYPE_CHECKING:
    from ..readiness import ReadinessCheck

logger = logging.getLogger(__name__)

_RUNNER_READINESS_CACHE_SECONDS = 10.0
_runner_readiness_cache_lock = threading.Lock()
_runner_readiness_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="astinuslab-runner-readiness")
_runner_readiness_future: Future[None] | None = None
_runner_readiness_cached_at = 0.0
_runner_readiness_cached_payload: dict[str, object] | None = None


def check_libvirt_connection(uri: str) -> ReadinessCheck:
    """Run the libvirt probe inside the runner trust domain."""
    from ..readiness import check_libvirt_connection as check

    return check(uri)


def _worker_service_identity(sender: Any) -> str:
    hostname = str(getattr(sender, "hostname", ""))
    return "analysis" if hostname.startswith("analysis@") else "runner"


def _analysis_readiness_payload() -> dict[str, object]:
    return {
        "service_identity": "analysis",
        "helper_sudo": {
            "status": "not_applicable",
            "code": "not_applicable",
        },
    }


def _runner_readiness_payload_uncached() -> dict[str, object]:
    if fake_mode.FAKE_LIBVIRT:
        helper_sudo: dict[str, object] = {
            "status": "not_applicable",
            "code": "fake_libvirt_active",
        }
    else:
        helpers = check_privileged_helpers()
        ready = bool(helpers) and all(row["installed"] and row["sudo_ok"] for row in helpers)
        helper_sudo = {
            "status": "passed" if ready else "failed",
            "code": "ready" if ready else "privileged_helper_sudo_unavailable",
        }
    libvirt = check_libvirt_connection(settings.libvirt_uri)
    return {
        "service_identity": "runner",
        "helper_sudo": helper_sudo,
        "libvirt_connection": {"status": libvirt.status, "code": libvirt.code},
    }


def _runner_readiness_in_progress_payload() -> dict[str, object]:
    return {
        "service_identity": "runner",
        "helper_sudo": {
            "status": "failed",
            "code": "runner_readiness_probe_in_progress",
        },
        "libvirt_connection": {
            "status": "failed",
            "code": "runner_readiness_probe_in_progress",
        },
    }


def _refresh_runner_readiness_cache() -> None:
    global _runner_readiness_cached_at, _runner_readiness_cached_payload
    try:
        payload = _runner_readiness_payload_uncached()
    except Exception:  # noqa: BLE001 - control replies expose only stable readiness codes
        payload = {
            "service_identity": "runner",
            "helper_sudo": {
                "status": "failed",
                "code": "privileged_helper_sudo_unavailable",
            },
            "libvirt_connection": {
                "status": "failed",
                "code": "libvirt_connection_unavailable",
            },
        }
    with _runner_readiness_cache_lock:
        _runner_readiness_cached_at = time.monotonic()
        _runner_readiness_cached_payload = copy.deepcopy(payload)


def worker_readiness_payload(service_identity: str) -> dict[str, object]:
    """Return an identity-owned, bounded readiness snapshot.

    Runner sudo inspection can take longer than the API's control-reply timeout.
    A short-lived cache plus a nonblocking single-flight lock prevents repeated
    broadcasts from accumulating more privileged probe work in the worker.
    """
    global _runner_readiness_future

    if service_identity != "runner":
        return _analysis_readiness_payload()

    now = time.monotonic()
    with _runner_readiness_cache_lock:
        if (
            _runner_readiness_cached_payload is not None
            and now - _runner_readiness_cached_at <= _RUNNER_READINESS_CACHE_SECONDS
        ):
            return copy.deepcopy(_runner_readiness_cached_payload)
        if _runner_readiness_future is None or _runner_readiness_future.done():
            _runner_readiness_future = _runner_readiness_executor.submit(_refresh_runner_readiness_cache)
    return _runner_readiness_in_progress_payload()


def _reset_worker_readiness_cache_for_tests() -> None:
    """Clear process-local readiness state between focused unit scenarios."""
    global _runner_readiness_cached_at, _runner_readiness_cached_payload, _runner_readiness_future
    future = _runner_readiness_future
    if future is not None:
        future.result(timeout=1.0)
    with _runner_readiness_cache_lock:
        _runner_readiness_cached_at = 0.0
        _runner_readiness_cached_payload = None
        _runner_readiness_future = None


@inspect_command(name="astinuslab_readiness")
def _worker_readiness_control(state: Any) -> dict[str, object]:
    return worker_readiness_payload(_worker_service_identity(state.consumer))


# --- Task outcome records ----------------------------------------------------
#
# ``owned_action`` already fails the HostAction row when a task body raises, and
# it is the single writer of that lifecycle state. These handlers only explain,
# in the log, what Celery did — which is the part nothing recorded. They cover
# the tasks ``owned_action`` never wraps (beat tasks, ``run_vol3_plugin``) and
# revocations that never enter a task body at all. Deliberately no DB writes: a
# second writer on HostAction would race the owner.


def _task_identifiers(args: Any, kwargs: Any) -> tuple[str, str]:
    """Best-effort (job_id, action_id) from a task's call signature.

    Every AstinusLab task takes ``(job_id, action_id=None)``, dispatched either
    positionally or by keyword. A task with neither (retention, reconciliation)
    reports ``-``, matching the request-id convention in the log format.
    """
    args = tuple(args or ())
    kwargs = dict(kwargs or {})

    job_id = kwargs.get("job_id")
    if job_id is None and args:
        job_id = args[0]
    action_id = kwargs.get("action_id")
    if action_id is None and len(args) > 1:
        action_id = args[1]

    return ("-" if job_id is None else str(job_id), "-" if action_id is None else str(action_id))


def _exception_detail(exception: BaseException | None) -> str:
    if exception is None:
        return "unknown"
    try:
        return f"{type(exception).__name__}: {exception}"
    except Exception:  # noqa: BLE001 - a broken __str__ must not lose the record
        return type(exception).__name__


# Keep a per-thread stack of values so nested eager tasks restore their parent
# task before the outermost task restores the surrounding HTTP request.
_saved_context = threading.local()


@_task_prerun_signal.connect
def _on_task_prerun(task: Any = None, args: Any = None, kwargs: Any = None, **_: Any) -> None:
    """Bind the publisher's request id and this task's Job for every record.

    A task published outside an HTTP request (beat, or a worker follow-up after
    a restart) still gets an identifier: records with no key at all cannot be
    grouped, and "which run was this" is the first question asked of them.
    """
    if not hasattr(_saved_context, "stack"):
        _saved_context.stack = []
    _saved_context.stack.append((current_request_id(), current_job_context()))
    request = getattr(task, "request", None)
    getter = getattr(request, "get", None)
    published_id = getter(REQUEST_ID_HEADER) if callable(getter) else None
    bind_request_id(published_id if isinstance(published_id, str) else None)
    job_id, action_id = _task_identifiers(args, kwargs)
    bind_job_context(
        None if job_id == "-" else job_id,
        None if action_id == "-" else action_id,
    )


@_task_postrun_signal.connect
def _on_task_postrun(task: Any = None, **_: Any) -> None:
    """Restore whatever context was in place before this task ran.

    In a worker thread that is nothing, so this clears. Under
    ``task_always_eager`` the task ran inside the HTTP request that dispatched
    it, and clearing would strip that request's id off every record the rest of
    the request produces — including its own completion line.
    """
    stack = getattr(_saved_context, "stack", [])
    request_id, (job_id, action_id) = stack.pop() if stack else ("-", ("-", "-"))
    set_request_id(request_id)
    bind_job_context(
        None if job_id == "-" else job_id,
        None if action_id == "-" else action_id,
    )


@_task_failure_signal.connect
def _on_task_failure(
    sender: Any = None,
    task_id: Any = None,
    exception: BaseException | None = None,
    args: Any = None,
    kwargs: Any = None,
    einfo: Any = None,
    **_: Any,
) -> None:
    """Record a task that raised, naming the Job it was working on."""
    job_id, action_id = _task_identifiers(args, kwargs)
    logger.error(
        "task_failed task=%s task_id=%s job_id=%s action_id=%s detail=%s",
        getattr(sender, "name", "unknown"),
        task_id or "-",
        job_id,
        action_id,
        _exception_detail(exception),
    )


@_task_revoked_signal.connect
def _on_task_revoked(
    sender: Any = None,
    request: Any = None,
    terminated: Any = None,
    signum: Any = None,
    expired: Any = None,
    **_: Any,
) -> None:
    """Record a revocation, which may never reach the task body at all."""
    job_id, action_id = _task_identifiers(getattr(request, "args", None), getattr(request, "kwargs", None))
    logger.warning(
        "task_revoked task=%s task_id=%s job_id=%s action_id=%s terminated=%s signal=%s expired=%s",
        getattr(request, "task", "unknown"),
        getattr(request, "id", None) or "-",
        job_id,
        action_id,
        terminated,
        signum if signum is not None else "-",
        expired,
    )


@_worker_shutting_down_signal.connect
def _on_worker_shutting_down(sig: Any = None, how: Any = None, exitcode: Any = None, **_: Any) -> None:
    """Mark the boundary a graceful restart leaves in the log.

    An action still running past this line was interrupted by the shutdown, not
    by its own failure; startup recovery reconciles it on the next boot.
    """
    logger.info(
        "worker_shutting_down signal=%s how=%s exitcode=%s",
        sig if sig is not None else "-",
        how if how is not None else "-",
        exitcode if exitcode is not None else "-",
    )


def _startup_teardown_adapter(
    session_scope: Any,
    repo_cls: Any,
    log: Any,
    config: Any,
    mark_destroyed: Any,
    mark_failed: Any,
) -> Callable[[str], bool]:
    """Bind runner recovery commands and preserve independent cleanup outcomes."""
    from ..virtualization.process_support import run_command
    from ..virtualization.vm_support import teardown_vm
    from ..workloads.workload_support import cleanup_job_network_isolation
    from .job_control import ACTION_BY_JOB, JOB_PROCESSES, PROCESS_LOCK

    def cleanup_command(cmd: list[str], **kwargs: Any) -> str:
        if fake_mode.FAKE_ORCHESTRATION:
            from ..orchestration.fake_runtime import _refuse

            _refuse(cmd)
        return run_command(
            log,
            session_scope,
            repo_cls,
            ACTION_BY_JOB,
            JOB_PROCESSES,
            PROCESS_LOCK,
            cmd,
            storage_dir=config.storage_dir,
            **kwargs,
        )

    def teardown(job_id: str) -> bool:
        if fake_mode.FAKE_ORCHESTRATION:
            from ..orchestration.fake_runtime import _refuse

            _refuse(["teardown_vm", job_id])
        network_clean = cleanup_job_network_isolation(session_scope, repo_cls, cleanup_command, log, job_id)
        vm_clean = teardown_vm(cleanup_command, log, config, mark_destroyed, job_id, mark_failed)
        return vm_clean and network_clean

    return teardown


@_worker_ready_signal.connect
def _on_worker_ready(sender: Any, **kwargs: Any) -> None:
    """Recover orphaned jobs left by a previously crashed worker."""
    import logging as _logging

    # Apply AstinusLab's log format and level after Celery logging initialises.
    from ..core.logging_config import configure_logging as _configure_logging
    from ..core.logging_config import log_file_for as _log_file_for

    service_identity = _worker_service_identity(sender)
    _configure_logging(
        settings.api_log_level,
        log_file=_log_file_for(service_identity),
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
    )
    _log = _logging.getLogger(__name__)

    try:
        from ..virtualization.host_script_manifest import preflight_problems as _host_preflight_problems

        for _problem in _host_preflight_problems(service_identity=service_identity):
            _log.warning("host-script preflight: %s", _problem)
    except Exception as _exc:
        _log.warning("host-script preflight skipped: %s", _exc)

    if service_identity == "analysis":
        from ..db import session_scope
        from .action_lifecycle import reconcile_interrupted_actions, recover_queued_dispatches

        reconcile_interrupted_actions(session_scope, service_identity="analysis")
        recover_queued_dispatches(session_scope, service_identity="analysis")
        from .capacity_runtime import dispatch_analysis_intents

        dispatch_analysis_intents(session_scope)
        _log.info("Analysis action recovery complete; VM recovery remains runner-owned")
        return

    if not fake_mode.FAKE_LIBVIRT:
        from ..lab_packages.runner_preflight import initialize_runner_storage

        try:
            initialize_runner_storage()
        except OSError:
            _log.exception("Runner Playground storage initialization failed; package preflight stays closed")

    from ..db import session_scope as _session_scope
    from ..db.repositories import JobRepository as _JobRepository
    from .capacity_runtime import adopt_legacy_reservations, schedule_capacity
    from .runtime_support import mark_runtime_destroyed as _mark_runtime_destroyed
    from .runtime_support import mark_runtime_teardown_failed as _mark_runtime_teardown_failed
    from .startup_recovery import reconcile_orphaned_jobs

    with _session_scope() as session:
        adopt_legacy_reservations(session, settings)

    def _mark_runtime_destroyed_bound(job_id: str, role: str) -> None:
        _mark_runtime_destroyed(_session_scope, _JobRepository, job_id, role)

    def _mark_runtime_teardown_failed_bound(job_id: str, role: str) -> None:
        _mark_runtime_teardown_failed(_session_scope, _JobRepository, job_id, role)

    teardown = _startup_teardown_adapter(
        _session_scope,
        _JobRepository,
        _log,
        settings,
        _mark_runtime_destroyed_bound,
        _mark_runtime_teardown_failed_bound,
    )

    reconcile_orphaned_jobs(
        _session_scope,
        _JobRepository,
        teardown_fn=teardown,
        mark_runtime_destroyed_fn=_mark_runtime_destroyed_bound,
        logger=_log,
        settings=settings,
    )

    from .action_lifecycle import reconcile_interrupted_actions, recover_queued_dispatches

    reconcile_interrupted_actions(_session_scope, service_identity="runner")
    recover_queued_dispatches(_session_scope, service_identity="runner")

    try:
        from .startup_recovery import reconcile_publication_attempts as _reconcile_publications

        _reconcile_publications(_session_scope)
    except Exception as _exc:
        _log.warning("[startup_recovery] publication reconciliation skipped: %s", _exc)

    package_reconciliation_ok = True
    try:
        from .startup_recovery import reconcile_lab_package_storage as _reconcile_packages

        _reconcile_packages(_session_scope)
    except Exception as _exc:
        package_reconciliation_ok = False
        _log.warning("[startup_recovery] package reconciliation skipped: %s", _exc)

    if package_reconciliation_ok:
        try:
            from .package_instance_coordinator import reconcile_package_instances as _reconcile_package_instances

            _reconcile_package_instances()
        except Exception as _exc:
            package_reconciliation_ok = False
            _log.warning("[startup_recovery] package instance reconciliation skipped: %s", _exc)

    if package_reconciliation_ok:
        try:
            from ..lab_packages.verification_service import (
                reconcile_authored_sessions as _reconcile_authored_sessions,
            )
            from ..lab_packages.verification_service import reconcile_verifications as _reconcile_verifications

            # A frozen authoring session owns live guests and disk overlays.
            # Fail interrupted attempts closed and transfer those resources to
            # cleanup before ordinary verification redelivery or the generic
            # Playground recovery sweep can touch them.
            _reconcile_authored_sessions()
            _reconcile_verifications()
        except Exception as _exc:
            _log.warning("[startup_recovery] package verification reconciliation skipped: %s", _exc)
        try:
            from ..lab_packages.prepare_service import reconcile_prepares as _reconcile_prepares

            _reconcile_prepares()
        except Exception as _exc:
            _log.warning("[startup_recovery] package prepare reconciliation skipped: %s", _exc)
        try:
            from ..lab_packages.export_service import reconcile_exports as _reconcile_exports

            with _session_scope() as _export_session:
                _reconcile_exports(_export_session)
        except Exception as _exc:
            _log.warning("[startup_recovery] package export reconciliation skipped: %s", _exc)

    # Package-owned archives and instance intent must be reconciled before the
    # generic Playground sweep is allowed to remove any related runtime state.
    if package_reconciliation_ok:
        try:
            from .startup_recovery import recover_playground_sessions as _recover_pg

            _recover_pg()
        except Exception as _exc:
            _log.warning("[startup_recovery] playground recovery skipped: %s", _exc)
    else:
        _log.error("[startup_recovery] Playground cleanup skipped because package reconciliation failed")

    if package_reconciliation_ok:
        try:
            schedule_capacity(_session_scope, settings)
        except Exception as _exc:
            _log.warning("[startup_recovery] capacity reconciliation deferred: %s", _exc)

    try:
        import shutil
        from pathlib import Path

        tmp_dir = Path(settings.storage_dir) / "tmp"
        for p in tmp_dir.glob("astinuslab-vol3-*"):
            if p.is_dir():
                _log.info("Cleaning up orphaned vol3 temporary directory: %s", p)
                shutil.rmtree(p, ignore_errors=True)
    except Exception as _exc:
        _log.warning("Failed to clean up orphaned vol3 temporary directories: %s", _exc)
