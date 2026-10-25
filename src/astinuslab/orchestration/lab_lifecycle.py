"""Leaf module for shared lab lifecycle operations (teardown, lab snapshots, cleanup).

Owns the dependency wiring (logger, settings, session_scope, JobRepository,
job_control registries) for the lifecycle flows that both the service layer and
the orchestrator need, so the dependency direction stays strictly
``services → lab_lifecycle ← orchestrator``. This module must not import the
orchestrator or any service module, at module level or inside functions
(enforced by tests/python/test_import_graph.py).
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..core.config import settings
from ..db import session_scope
from ..db.repositories import JobRepository
from ..jobs.job_control import (
    ACTION_BY_JOB,
    JOB_PROCESSES,
    PROCESS_LOCK,
    is_cancel_requested,
)
from ..jobs.runtime_support import (
    mark_runtime_destroyed as _mark_runtime_destroyed_impl,
)
from ..jobs.runtime_support import (
    mark_runtime_teardown_failed as _mark_runtime_teardown_failed_impl,
)
from ..jobs.runtime_support import (
    sync_runtime_record as _sync_runtime_record_impl,
)
from ..virtualization.process_support import run_command as _run_command_impl
from ..virtualization.vm_support import (
    teardown_vm as _teardown_vm_impl,
)
from .support import (
    create_lab_snapshot as _create_lab_snapshot_impl,
)
from .support import (
    revert_lab_snapshot as _revert_lab_snapshot_impl,
)

logger = logging.getLogger(__name__)


def _run_command(
    cmd: list[str],
    cwd: Path | None = None,
    job_id: str | None = None,
    extra_env: dict[str, str] | None = None,
    timeout: float | None = 3600.0,
    quiet_fail: bool = False,
) -> str:
    from ..core import fake_mode

    if fake_mode.FAKE_ORCHESTRATION:
        from .fake_runtime import _refuse

        _refuse(cmd)
    return _run_command_impl(
        logger,
        session_scope,
        JobRepository,
        ACTION_BY_JOB,
        JOB_PROCESSES,
        PROCESS_LOCK,
        cmd,
        cwd=cwd,
        job_id=job_id,
        extra_env=extra_env,
        timeout=timeout,
        is_cancel_requested_fn=is_cancel_requested,
        quiet_fail=quiet_fail,
        storage_dir=settings.storage_dir,
    )


def _sync_runtime_record(
    job_id: str,
    role: str,
    *,
    vm_name: str | None = None,
    ip_address: str | None = None,
    state: str | None = None,
    power_state: str | None = None,
    snapshot_name: str | None = None,
    capture_memory_enabled: bool | None = None,
    capture_disk_enabled: bool | None = None,
) -> None:
    _sync_runtime_record_impl(
        session_scope,
        JobRepository,
        job_id,
        role,
        vm_name=vm_name,
        ip_address=ip_address,
        state=state,
        power_state=power_state,
        snapshot_name=snapshot_name,
        capture_memory_enabled=capture_memory_enabled,
        capture_disk_enabled=capture_disk_enabled,
    )


def _mark_runtime_destroyed(job_id: str, role: str) -> None:
    _mark_runtime_destroyed_impl(session_scope, JobRepository, job_id, role)


def _mark_runtime_teardown_failed(job_id: str, role: str) -> None:
    _mark_runtime_teardown_failed_impl(session_scope, JobRepository, job_id, role)


def teardown_vm(job_id: str) -> bool:
    from ..core import fake_mode
    from ..workloads.workload_support import cleanup_job_network_isolation

    if fake_mode.FAKE_ORCHESTRATION:
        from .fake_runtime import _refuse

        _refuse(["teardown_vm", job_id])
    network_clean = cleanup_job_network_isolation(session_scope, JobRepository, _run_command, logger, job_id)
    vm_clean = _teardown_vm_impl(
        _run_command,
        logger,
        settings,
        _mark_runtime_destroyed,
        job_id,
        _mark_runtime_teardown_failed,
    )
    return vm_clean and network_clean


def create_lab_snapshot(
    job_id: str,
    *,
    description: str = "Clean lab state",
    event_state: str = "waiting_for_capture",
    event_message: str = "Lab snapshot created: lab-clean",
) -> str:
    return _create_lab_snapshot_impl(
        session_scope,
        JobRepository,
        _run_command,
        _sync_runtime_record,
        settings,
        logger,
        job_id,
        description=description,
        event_state=event_state,
        event_message=event_message,
    )


def revert_lab_snapshot(job_id: str) -> bool:
    return _revert_lab_snapshot_impl(
        session_scope,
        JobRepository,
        _run_command,
        _sync_runtime_record,
        settings,
        logger,
        job_id,
    )


def cleanup_terminated_job(job_id: str) -> None:
    with session_scope() as session:
        from ..db.models import PlaygroundSession

        is_playground = session.get(PlaygroundSession, job_id) is not None

    if is_playground:
        _cleanup_playground_session(job_id)


def _cleanup_playground_session(job_id: str) -> None:
    from ..playground import cleanup as playground_cleanup

    playground_cleanup.purge_session_resources(job_id=job_id, final_status="cancelled")
