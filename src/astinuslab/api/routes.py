from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from pydantic import ValidationError
from sqlalchemy.orm import Session

from .. import catalog
from ..core.operational_metrics import DEFAULT_WINDOW_HOURS, MAX_WINDOW_HOURS, collect_metrics
from ..db import session_scope
from ..db.repositories import ArtifactRepository
from ..forensics.case_services import (
    create_acquisition_service,
    create_case_service,
    get_acquisition_service,
    get_artifact_service,
    get_case_service,
    get_report_service,
    list_acquisitions_service,
    list_artifacts_service,
    list_cases_service,
    list_cves_service,
    list_reports_service,
    update_acquisition_status_service,
    update_case_status_service,
)
from ..forensics.manual_upload import store_manual_dump, store_manual_isf, store_manual_isf_for_target
from ..forensics.services import (
    enqueue_acquire_memory_dump_service,
)
from ..jobs.job_services import (
    cancel_job_service,
    control_runtime_service,
    create_job_service,
    delete_job_service,
    force_fail_job_service,
    get_job_logs_service,
    get_job_service,
    get_lab_info_service,
    list_host_actions_service,
    list_job_runtimes_service,
    list_jobs_service,
    list_scenarios_service,
    list_targets_service,
    list_workloads_service,
    reset_lab_service,
    retry_job_service,
    trigger_retention_service,
)
from ..jobs.job_services import (
    safe_artifact_path as _safe_artifact_path,
)
from ..playground.routes.recipes import export_current_recipe_endpoint, import_recipe_endpoint
from ..storage.downloads import PinnedFileResponse
from .schemas import (
    AcquisitionCreate,
    AcquisitionOut,
    AcquisitionStatusUpdate,
    ArtifactOut,
    CaptureRequest,
    CaseCreate,
    CaseOut,
    CaseStatusUpdate,
    ControlRuntimeOut,
    CveOut,
    HostActionOut,
    JobCreate,
    JobEventOut,
    JobOut,
    JobRuntimeOut,
    LabInfoOut,
    PlaygroundRecipe,
    PlaygroundSessionOut,
    ReportOut,
    ResetLabOut,
    ScenarioOut,
    TargetOut,
    WorkloadOut,
)

router = APIRouter(prefix="/api/v1")

CollectionLimit = Annotated[int, Query(ge=1, le=500)]
CollectionOffset = Annotated[int, Query(ge=0)]


def get_session():
    with session_scope() as session:
        yield session


@router.get("/targets", response_model=list[TargetOut])
def list_targets():
    return list_targets_service()


@router.get("/scenarios", response_model=list[ScenarioOut])
def list_scenarios():
    return list_scenarios_service()


@router.get("/metrics")
def operational_metrics(
    window_hours: int = Query(default=DEFAULT_WINDOW_HOURS, ge=1, le=MAX_WINDOW_HOURS),
    session: Session = Depends(get_session),
):
    """Read-only activity summary, aggregated from Job and HostAction rows.

    Complements `/ready`, which answers whether the platform can work: this
    answers how well it has been working. Behind the admin gate like the rest of
    `/api/v1`, because action types and job counts describe the operator's lab.
    """
    return collect_metrics(session, window_hours=window_hours)


@router.get("/workloads", response_model=list[WorkloadOut])
def list_workloads(cve: str | None = None, target_id: str | None = None, compatible_only: bool = False):
    return list_workloads_service(target_id, cve, compatible_only)


@router.get("/cves", response_model=list[CveOut])
def list_cves():
    """Return all CVE IDs indexed in the workload catalog, with associated workload IDs."""
    return list_cves_service()


@router.get("/scenario-packages/playground/current")
def export_current_playground_scenario_package():
    recipe = export_current_recipe_endpoint()
    recipe_data = recipe.model_dump(mode="json")
    package = catalog.build_scenario_package(
        recipe_data,
        metadata={"name": recipe.name or recipe_data.get("name") or "playground-scenario"},
    )
    raw_filename = recipe.name or "playground-scenario"
    safe_filename = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in raw_filename).strip("-")
    filename = f"{safe_filename or 'playground-scenario'}.zip"
    return Response(
        content=package,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _read_scenario_package_upload(file: UploadFile) -> bytes:
    data = bytearray()
    chunk_size = 1024 * 1024
    while chunk := await file.read(chunk_size):
        data.extend(chunk)
        if len(data) > catalog.PACKAGE_MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="scenario package zip is too large")
    return bytes(data)


_ISF_MAX_UPLOAD_BYTES = 512 * 1024 * 1024


async def _read_isf_upload(file: UploadFile) -> bytes:
    data = bytearray()
    chunk_size = 1024 * 1024
    while chunk := await file.read(chunk_size):
        data.extend(chunk)
        if len(data) > _ISF_MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="ISF upload is too large")
    return bytes(data)


@router.post(
    "/scenario-packages/import",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PlaygroundSessionOut,
)
async def import_playground_scenario_package(
    file: UploadFile = File(...),
    resource_overrides: str | None = Form(default=None),
) -> PlaygroundSessionOut:
    try:
        package_data = await _read_scenario_package_upload(file)
        package = catalog.read_scenario_package(package_data)
        overrides = None
        if resource_overrides:
            overrides = json.loads(resource_overrides)
            if not isinstance(overrides, dict):
                raise catalog.ScenarioPackageError("resource_overrides must be a JSON object")
        recipe_data = catalog.apply_scenario_package_resource_overrides(package["recipe"], overrides)
        recipe = PlaygroundRecipe.model_validate(recipe_data)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="resource_overrides must be valid JSON") from exc
    except (catalog.ScenarioPackageError, ValidationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return import_recipe_endpoint(recipe)


@router.post("/jobs", response_model=JobOut, status_code=201)
def create_job(payload: JobCreate, session: Session = Depends(get_session)):
    job = create_job_service(session, payload)
    return JobOut.model_validate(job)


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(
    limit: CollectionLimit = 100,
    offset: CollectionOffset = 0,
    session: Session = Depends(get_session),
):
    return [JobOut.model_validate(job) for job in list_jobs_service(session, limit=limit, offset=offset)]


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, session: Session = Depends(get_session)):
    job = get_job_service(session, job_id)
    return JobOut.model_validate(job)


@router.get("/jobs/{job_id}/logs", response_model=list[JobEventOut])
def get_job_logs(
    job_id: str,
    limit: CollectionLimit = 100,
    offset: CollectionOffset = 0,
    session: Session = Depends(get_session),
):
    return [
        JobEventOut.model_validate(event) for event in get_job_logs_service(session, job_id, limit=limit, offset=offset)
    ]


@router.get("/jobs/{job_id}/artifact")
def download_artifact(job_id: str, session: Session = Depends(get_session)):
    job = get_job_service(session, job_id)
    dump_path_str = job.dump_path
    dump_filename = job.dump_filename
    if not dump_path_str or not dump_filename:
        raise HTTPException(status_code=404, detail="artifact is not available for this job")
    dump_path = _safe_artifact_path(dump_path_str)
    headers: dict[str, str] = {}
    if job.dump_checksum:
        headers["X-Astinuslab-SHA256"] = job.dump_checksum
    if job.dump_size_bytes is not None:
        headers["X-Astinuslab-Size"] = str(job.dump_size_bytes)
    return PinnedFileResponse(
        path=str(dump_path),
        filename=dump_filename,
        media_type="application/octet-stream",
        headers=headers,
    )


@router.get("/jobs/{job_id}/disk-artifact")
def download_disk_artifact(job_id: str, session: Session = Depends(get_session)):
    job = get_job_service(session, job_id)
    disk_dump_path_str = job.disk_dump_path
    disk_dump_filename = job.disk_dump_filename
    if not disk_dump_path_str or not disk_dump_filename:
        raise HTTPException(status_code=404, detail="disk artifact is not available for this job")
    disk_dump_path = _safe_artifact_path(disk_dump_path_str)
    headers: dict[str, str] = {}
    if job.disk_dump_checksum:
        headers["X-Astinuslab-SHA256"] = job.disk_dump_checksum
    if job.disk_dump_size_bytes is not None:
        headers["X-Astinuslab-Size"] = str(job.disk_dump_size_bytes)
    return PinnedFileResponse(
        path=str(disk_dump_path),
        filename=disk_dump_filename,
        media_type="application/octet-stream",
        headers=headers,
    )


@router.post("/jobs/{job_id}/isf/upload")
async def upload_isf(job_id: str, file: UploadFile = File(...)):
    """Store an operator-supplied ISF for a job, bypassing GNU build-id enforcement.

    Advanced override: the uploaded JSON must be a valid Volatility 3 ISF, but
    no build-id match against the live guest is required. The artifact is
    recorded with source="manual_upload" (unverified provenance).
    """
    data = await _read_isf_upload(file)
    return store_manual_isf(job_id, data)


@router.post("/volatility/targets/{target_id}/isf/upload")
async def upload_target_isf(
    target_id: str,
    scenario_id: str = Form(""),
    file: UploadFile = File(...),
):
    """Store an operator-supplied ISF from the Volatility workbench target row."""
    data = await _read_isf_upload(file)
    return store_manual_isf_for_target(target_id, scenario_id, data)


@router.post("/jobs/{job_id}/dump/upload")
def upload_dump(job_id: str, file: UploadFile = File(...)):
    """Store an operator-supplied memory dump for a job, bypassing ELF validation.

    Advanced override: the ELF PT_LOAD completeness check applied to host
    captures is skipped; only a non-empty file is required. The dump is recorded
    with producer="manual_upload" (unverified provenance).
    """
    return store_manual_dump(job_id, file)


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: str, session: Session = Depends(get_session)):
    """Request cancellation of a running job."""
    job = cancel_job_service(session, job_id)
    return JobOut.model_validate(job)


@router.post("/jobs/{job_id}/capture", response_model=JobOut)
@router.post(
    "/jobs/{job_id}/acquire-memory-dump",
    response_model=JobOut,
    deprecated=True,
    summary="Deprecated alias of /jobs/{job_id}/capture (removal: next minor version)",
)
def acquire_memory_dump(job_id: str, body: CaptureRequest | None = None, session: Session = Depends(get_session)):
    """Manually trigger a memory/disk capture for a job in 'waiting_for_capture' state."""
    job = enqueue_acquire_memory_dump_service(
        session,
        job_id,
        exploit_tags=body.exploit_tags if body else None,
        exploit_notes=body.exploit_notes if body else None,
        capture_type=body.capture_type if body else None,
    )
    return JobOut.model_validate(job)


@router.delete("/jobs/{job_id}", status_code=204)
def delete_job(job_id: str, delete_files: bool = False, session: Session = Depends(get_session)):
    """Delete a job and all its associated data."""
    from ..jobs import job_services
    from ..storage import deletion

    ids = delete_job_service(session, job_id, delete_files=delete_files)
    session.commit()
    deletion.cleanup_deletions(session, ids, roots=deletion.evidence_roots(job_services.settings))


@router.post("/jobs/{job_id}/retry", response_model=JobOut, status_code=201)
def retry_job(job_id: str, session: Session = Depends(get_session)):
    """Create a new job based on a failed job's configuration."""
    job = retry_job_service(session, job_id)
    return JobOut.model_validate(job)


@router.post("/jobs/{job_id}/force-fail", response_model=JobOut)
def force_fail_job(job_id: str, session: Session = Depends(get_session)):
    job = force_fail_job_service(session, job_id)
    return JobOut.model_validate(job)


@router.get("/jobs/{job_id}/lab-info", response_model=LabInfoOut)
def get_lab_info(job_id: str, session: Session = Depends(get_session)):
    return get_lab_info_service(session, job_id)


@router.post("/jobs/{job_id}/reset-lab", response_model=ResetLabOut, status_code=202)
def reset_lab(job_id: str, session: Session = Depends(get_session)):
    """Reset the lab to its clean snapshot."""
    reset_lab_service(job_id)
    return {"status": "reset_requested"}


@router.get("/jobs/{job_id}/runtimes", response_model=list[JobRuntimeOut])
def list_job_runtimes(job_id: str, session: Session = Depends(get_session)):
    runtimes = list_job_runtimes_service(session, job_id)
    return [JobRuntimeOut.model_validate(runtime) for runtime in runtimes]


@router.post("/jobs/{job_id}/runtimes/{role}/{action}", response_model=ControlRuntimeOut, status_code=202)
def control_runtime(job_id: str, role: str, action: str, session: Session = Depends(get_session)):
    control_runtime_service(job_id, role, action)
    return {"status": "accepted", "role": role, "action": action}


@router.get("/host-actions", response_model=list[HostActionOut])
def list_host_actions(
    limit: CollectionLimit = 100,
    offset: CollectionOffset = 0,
    session: Session = Depends(get_session),
):
    return [
        HostActionOut.model_validate(action)
        for action in list_host_actions_service(session, limit=limit, offset=offset)
    ]


@router.post("/run-retention")
def trigger_retention():
    return trigger_retention_service()


@router.get("/cases", response_model=list[CaseOut])
def list_cases(
    limit: CollectionLimit = 100,
    offset: CollectionOffset = 0,
    session: Session = Depends(get_session),
):
    return list_cases_service(session, limit=limit, offset=offset)


@router.post("/cases", response_model=CaseOut, status_code=201)
def create_case(payload: CaseCreate, session: Session = Depends(get_session)):
    return create_case_service(session, payload)


@router.get("/cases/{case_id}", response_model=CaseOut)
def get_case(case_id: str, session: Session = Depends(get_session)):
    return get_case_service(session, case_id)


@router.patch("/cases/{case_id}/status", response_model=CaseOut)
def update_case_status(
    case_id: str,
    payload: CaseStatusUpdate,
    session: Session = Depends(get_session),
):
    return update_case_status_service(session, case_id, payload.status)


@router.get("/artifacts", response_model=list[ArtifactOut])
def list_artifacts(
    case_id: str | None = None,
    job_id: str | None = None,
    acquisition_id: str | None = None,
    limit: CollectionLimit = 100,
    offset: CollectionOffset = 0,
    session: Session = Depends(get_session),
):
    return list_artifacts_service(
        session,
        case_id=case_id,
        job_id=job_id,
        acquisition_id=acquisition_id,
        limit=limit,
        offset=offset,
    )


@router.get("/artifacts/{artifact_id}", response_model=ArtifactOut)
def get_artifact(artifact_id: str, session: Session = Depends(get_session)):
    return get_artifact_service(session, artifact_id)


def _artifact_file_response(artifact: ArtifactOut, path: str | None, *, missing_detail: str):
    if not path:
        raise HTTPException(status_code=404, detail=missing_detail)
    file_path = _safe_artifact_path(path)
    headers: dict[str, str] = {}
    if artifact.sha256:
        headers["X-Astinuslab-SHA256"] = artifact.sha256
    if artifact.size_bytes is not None:
        headers["X-Astinuslab-Size"] = str(artifact.size_bytes)
    return PinnedFileResponse(
        path=str(file_path),
        filename=file_path.name,
        media_type="application/octet-stream",
        headers=headers,
    )


def _get_artifact_row(session: Session, artifact_id: str):
    artifact = ArtifactRepository(session).get_artifact(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return artifact


@router.get("/artifacts/{artifact_id}/download")
def download_artifact_by_id(artifact_id: str, session: Session = Depends(get_session)):
    artifact = _get_artifact_row(session, artifact_id)
    return _artifact_file_response(artifact, artifact.path, missing_detail="artifact file is not available")


@router.get("/artifacts/{artifact_id}/manifest")
def download_artifact_manifest(artifact_id: str, session: Session = Depends(get_session)):
    artifact = _get_artifact_row(session, artifact_id)
    return _artifact_file_response(
        artifact, artifact.manifest_path, missing_detail="artifact manifest is not available"
    )


@router.get("/artifacts/{artifact_id}/sha256")
def download_artifact_sha256_sidecar(artifact_id: str, session: Session = Depends(get_session)):
    artifact = _get_artifact_row(session, artifact_id)
    return _artifact_file_response(
        artifact, artifact.sha256_sidecar_path, missing_detail="artifact sha256 sidecar is not available"
    )


@router.get("/reports", response_model=list[ReportOut])
def list_reports(
    case_id: str | None = None,
    job_id: str | None = None,
    acquisition_id: str | None = None,
    input_artifact_id: str | None = None,
    output_artifact_id: str | None = None,
    limit: CollectionLimit = 100,
    offset: CollectionOffset = 0,
    session: Session = Depends(get_session),
):
    return list_reports_service(
        session,
        case_id=case_id,
        job_id=job_id,
        acquisition_id=acquisition_id,
        input_artifact_id=input_artifact_id,
        output_artifact_id=output_artifact_id,
        limit=limit,
        offset=offset,
    )


@router.get("/reports/{report_id}", response_model=ReportOut)
def get_report(report_id: str, session: Session = Depends(get_session)):
    return get_report_service(session, report_id)


@router.get("/acquisitions", response_model=list[AcquisitionOut])
def list_acquisitions(
    limit: CollectionLimit = 100,
    offset: CollectionOffset = 0,
    session: Session = Depends(get_session),
):
    return list_acquisitions_service(session, limit=limit, offset=offset)


@router.post("/acquisitions", response_model=AcquisitionOut, status_code=201)
def create_acquisition(payload: AcquisitionCreate, session: Session = Depends(get_session)):
    return create_acquisition_service(session, payload)


@router.get("/acquisitions/{acquisition_id}", response_model=AcquisitionOut)
def get_acquisition(acquisition_id: str, session: Session = Depends(get_session)):
    return get_acquisition_service(session, acquisition_id)


@router.patch("/acquisitions/{acquisition_id}/status", response_model=AcquisitionOut)
def update_acquisition_status(
    acquisition_id: str,
    payload: AcquisitionStatusUpdate,
    session: Session = Depends(get_session),
):
    return update_acquisition_status_service(session, acquisition_id, payload.status)
