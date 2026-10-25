"""Thin UI delegates for job mutation routes.

Route handlers own FastAPI form/request wiring and response shape. These helpers own
form-to-service translation, shared error handling, and old UI monkeypatch seams.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from starlette.datastructures import FormData

from ..api.errors import ApiError
from ..api.schemas import JobCreate
from ..forensics.services import enqueue_acquire_memory_dump_service, enqueue_vol3_analysis_service
from ..jobs.job_services import (
    control_runtime_service,
    create_job_service,
    delete_job_service,
    list_scenarios_service,
    reset_lab_service,
    retry_job_service,
)

logger = logging.getLogger(__name__)

_VALID_CAPTURE = ("memory", "disk", "both", "none")


@dataclass(frozen=True)
class BulkDeleteJobsResult:
    """Outcome of a bulk job delete: the operator tally plus its banner severity.

    `message` alone cannot carry severity -- a partial failure is appended to the
    same success tally -- so the route had no way to choose between the error and
    success redirect helpers and painted every bulk delete green.  `failed` is that
    missing bit: true when any selected job was skipped or errored, or when the
    request deleted nothing at all.
    """

    message: str
    failed: bool = False


@dataclass(frozen=True)
class JobMutationResult:
    redirect_url: str
    error_message: str | None = None


from ..jobs.job_control import request_job_cancel
from ..jobs.job_services import force_kill_job_service


def _current_session_scope():
    """Return monkeypatch-aware UI session scope."""
    import astinuslab.ui as ui_mod

    from ..db import session_scope as default_session_scope

    return getattr(ui_mod, "session_scope", default_session_scope)


def _current_request_job_cancel():
    """Return monkeypatch-aware cancel helper."""
    return request_job_cancel


def _current_force_kill_job_service():
    """Return monkeypatch-aware force-kill service."""
    return force_kill_job_service


def _enforce_disk_guardrail() -> None:
    """Keep the existing form validation seam on the shared admission policy."""
    from ..storage.guardrails import enforce_disk_guardrail

    enforce_disk_guardrail()


def create_job_from_form(
    *,
    target_id: str,
    scenario_id: str,
    capture_type: str,
    custom_script: str,
    workload_id: str,
    attacker_vm_id: str,
    vm_vcpus: str,
    vm_memory_mb: str,
    vm_disk_gb: str,
    attacker_vm_vcpus: str,
    attacker_vm_memory_mb: str,
    attacker_vm_disk_gb: str,
    build_vol3_isf: str,
    target_options: list[dict[str, Any]],
) -> str:
    """Create a job from HTML form values and return new job id."""
    _enforce_disk_guardrail()
    scenarios = list_scenarios_service()
    if target_id not in {str(target["id"]) for target in target_options}:
        raise HTTPException(status_code=400, detail="Target is not available for Jobs")
    scenario = next((s for s in scenarios if s["id"] == scenario_id), None)
    if scenario is None:
        raise HTTPException(status_code=400, detail="Scenario not found")

    normalized_capture_type = (
        "none" if build_vol3_isf == "1" else (capture_type if capture_type in _VALID_CAPTURE else "memory")
    )
    payload = JobCreate(
        target_id=target_id,
        scenario_id=scenario_id,
        execution_mode=scenario["mode"],
        capture_type=normalized_capture_type,  # type: ignore[arg-type]  # runtime-validated above
        custom_script=custom_script.strip() or None,
        workload_id=workload_id.strip() or None,
        attacker_vm_id=attacker_vm_id.strip() or None,
        vm_vcpus=int(vm_vcpus) if vm_vcpus.strip() else None,
        vm_memory_mb=int(vm_memory_mb) if vm_memory_mb.strip() else None,
        vm_disk_gb=int(vm_disk_gb) if vm_disk_gb.strip() else None,
        attacker_vm_vcpus=int(attacker_vm_vcpus) if attacker_vm_vcpus.strip() else None,
        attacker_vm_memory_mb=int(attacker_vm_memory_mb) if attacker_vm_memory_mb.strip() else None,
        attacker_vm_disk_gb=int(attacker_vm_disk_gb) if attacker_vm_disk_gb.strip() else None,
        build_vol3_isf=(build_vol3_isf == "1"),
    )
    with _current_session_scope()() as session:
        job = create_job_service(session, payload)
        return job.id


def bulk_delete_jobs_from_form(form: FormData) -> BulkDeleteJobsResult:
    """Delete selected jobs and preserve existing partial-failure message behavior."""
    job_ids = form.getlist("job_ids")
    delete_files = form.get("delete_files") == "true"
    deleted = 0
    skipped = 0
    error_msg = None

    for job_id in job_ids:
        if not isinstance(job_id, str):
            skipped += 1
            continue
        try:
            with _current_session_scope()() as session:
                from ..core.config import settings
                from ..storage import deletion

                ids = delete_job_service(session, job_id, delete_files=delete_files)
                session.commit()
                deletion.cleanup_deletions(session, ids, roots=deletion.evidence_roots(settings))
            deleted += 1
        except HTTPException:
            skipped += 1
        except Exception as e:
            if not error_msg:
                error_msg = f"Error deleting {job_id}: {str(e)}"
            skipped += 1

    msg = f"Jobs deleted: {deleted}. Skipped: {skipped}."
    if delete_files:
        msg += " Associated files removed."
    if error_msg:
        msg += f" {error_msg}"
    if not job_ids:
        # Nothing was selected, so nothing happened: a corrective notice, not a win.
        return BulkDeleteJobsResult(message="No jobs selected for deletion.", failed=True)
    return BulkDeleteJobsResult(message=msg, failed=skipped > 0 or error_msg is not None)


def acquire_memory_dump_from_ui(
    job_id: str, exploit_tags: str, exploit_notes: str, capture_type: str
) -> JobMutationResult:
    """Queue operator-triggered acquisition and translate unexpected failures to UI messages."""
    tags = [t.strip() for t in exploit_tags.split(",") if t.strip()] if exploit_tags.strip() else None
    notes = exploit_notes.strip() or None
    try:
        with _current_session_scope()() as session:
            enqueue_acquire_memory_dump_service(
                session,
                job_id,
                exploit_tags=tags,
                exploit_notes=notes,
                capture_type=capture_type.strip() or None,
            )
    except (ApiError, HTTPException):
        raise
    except Exception as exc:
        logger.error("[%s] acquire-memory-dump failed: %s", job_id, exc)
        return JobMutationResult(redirect_url=f"/jobs/{job_id}", error_message=f"Capture request failed: {exc}")
    return JobMutationResult(redirect_url=f"/jobs/{job_id}")


def run_vol3_analysis_from_ui(job_id: str) -> JobMutationResult:
    try:
        with _current_session_scope()() as session:
            enqueue_vol3_analysis_service(session, job_id)
    except Exception as exc:
        logger.error("[%s] vol3-analysis failed: %s", job_id, exc)
        return JobMutationResult(redirect_url=f"/jobs/{job_id}", error_message=f"Analysis request failed: {exc}")
    return JobMutationResult(redirect_url=f"/jobs/{job_id}")


def force_kill_job_from_ui(job_id: str) -> JobMutationResult:
    try:
        with _current_session_scope()() as session:
            _current_force_kill_job_service()(session, job_id)
    except (ApiError, HTTPException):
        raise
    except Exception as exc:
        logger.error("[%s] force-kill failed: %s", job_id, exc)
        return JobMutationResult(redirect_url=f"/jobs/{job_id}", error_message=f"Force kill failed: {exc}")
    return JobMutationResult(redirect_url=f"/jobs/{job_id}")


def retry_job_from_ui(job_id: str) -> JobMutationResult:
    try:
        with _current_session_scope()() as session:
            new_job = retry_job_service(session, job_id)
            new_job_id = new_job.id
    except (ApiError, HTTPException):
        raise
    except Exception as exc:
        logger.error("[%s] retry failed: %s", job_id, exc)
        return JobMutationResult(redirect_url=f"/jobs/{job_id}", error_message=f"Retry failed: {exc}")
    return JobMutationResult(redirect_url=f"/jobs/{new_job_id}")


def reset_lab_from_ui(job_id: str) -> JobMutationResult:
    try:
        reset_lab_service(job_id)
    except (ApiError, HTTPException):
        raise
    except Exception as exc:
        logger.error("[%s] reset-lab failed: %s", job_id, exc)
        return JobMutationResult(redirect_url=f"/jobs/{job_id}", error_message=f"Reset failed: {exc}")
    return JobMutationResult(redirect_url=f"/jobs/{job_id}")


def control_runtime_from_ui(job_id: str, role: str, action: str) -> JobMutationResult:
    try:
        control_runtime_service(job_id, role, action)
    except Exception as exc:
        logger.error("[%s] runtime action %s failed: %s", job_id, action, exc)
        return JobMutationResult(redirect_url=f"/jobs/{job_id}", error_message=f"Action failed: {exc}")
    return JobMutationResult(redirect_url=f"/jobs/{job_id}")


def cancel_job_from_ui(job_id: str) -> JobMutationResult:
    if not _current_request_job_cancel()(job_id):
        return JobMutationResult(
            redirect_url=f"/jobs/{job_id}",
            error_message="Job cannot be cancelled in its current state (captures are not interruptible).",
        )
    return JobMutationResult(redirect_url=f"/jobs/{job_id}")


def delete_job_from_ui(job_id: str, *, delete_files: bool) -> None:
    with _current_session_scope()() as session:
        from ..core.config import settings
        from ..storage import deletion

        ids = delete_job_service(session, job_id, delete_files=delete_files)
        session.commit()
        deletion.cleanup_deletions(session, ids, roots=deletion.evidence_roots(settings))
