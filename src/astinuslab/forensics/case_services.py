"""Service-layer operations for cases, artifacts, reports, acquisitions, and CVEs.

Validates request-level domain rules and translates the case/evidence repositories
into API response models. See ``job_services`` for job lifecycle and
``forensic_services`` for capture/Volatility operations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..catalog import load_workloads
from ..db.repositories import (
    FORENSIC_LIVE_STATUSES,
    AcquisitionRepository,
    ArtifactRepository,
    CaseRepository,
    ReportRepository,
)

if TYPE_CHECKING:
    from ..api.schemas import AcquisitionOut, ArtifactOut, CaseOut, ReportOut

_ENGAGEMENT_LIVE_STATUSES = FORENSIC_LIVE_STATUSES


def _acquisition_out(acq, *, job_count: int | None = None, live_job_count: int | None = None) -> AcquisitionOut:
    from ..api.schemas import AcquisitionOut

    if job_count is None or live_job_count is None:
        job_count = len(acq.jobs)
        live_job_count = sum(1 for job in acq.jobs if job.status in _ENGAGEMENT_LIVE_STATUSES)
    return AcquisitionOut(
        id=acq.id,
        name=acq.name,
        status=acq.status,
        created_at=acq.created_at,
        sealed_at=acq.sealed_at,
        job_count=job_count,
        live_job_count=live_job_count,
    )


def _case_out(case) -> CaseOut:
    from ..api.schemas import CaseOut

    return CaseOut.model_validate(case)


def _artifact_out(artifact) -> ArtifactOut:
    from ..api.schemas import ArtifactOut

    return ArtifactOut.model_validate(artifact)


def _report_out(report) -> ReportOut:
    from ..api.schemas import ReportOut

    return ReportOut.model_validate(report)


def list_cases_service(session: Session, *, limit: int | None = None, offset: int = 0) -> list:
    return [_case_out(case) for case in CaseRepository(session).list_cases(limit=limit, offset=offset)]


def create_case_service(session: Session, payload) -> CaseOut:
    return _case_out(CaseRepository(session).create_case(payload.name))


def get_case_service(session: Session, case_id: str) -> CaseOut:
    case = CaseRepository(session).get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return _case_out(case)


def update_case_status_service(session: Session, case_id: str, status: str) -> CaseOut:
    try:
        case = CaseRepository(session).update_case_status(case_id, status)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Case not found") from exc
    return _case_out(case)


def list_artifacts_service(
    session: Session,
    *,
    case_id: str | None = None,
    job_id: str | None = None,
    acquisition_id: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list:
    """Return artifacts with optional case scoping while preserving API response-model shape."""
    repo = ArtifactRepository(session)
    return [
        _artifact_out(artifact)
        for artifact in repo.list_artifacts(
            case_id=case_id,
            job_id=job_id,
            acquisition_id=acquisition_id,
            limit=limit,
            offset=offset,
        )
    ]


def get_artifact_service(session: Session, artifact_id: str) -> ArtifactOut:
    artifact = ArtifactRepository(session).get_artifact(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return _artifact_out(artifact)


def list_reports_service(
    session: Session,
    *,
    case_id: str | None = None,
    job_id: str | None = None,
    acquisition_id: str | None = None,
    input_artifact_id: str | None = None,
    output_artifact_id: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list:
    """Return reports with optional case/job/acquisition filters applied at the service boundary."""
    repo = ReportRepository(session)
    return [
        _report_out(report)
        for report in repo.list_reports(
            case_id=case_id,
            job_id=job_id,
            acquisition_id=acquisition_id,
            input_artifact_id=input_artifact_id,
            output_artifact_id=output_artifact_id,
            limit=limit,
            offset=offset,
        )
    ]


def get_report_service(session: Session, report_id: str) -> ReportOut:
    report = ReportRepository(session).get_report(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return _report_out(report)


def list_acquisitions_service(session: Session, *, limit: int | None = None, offset: int = 0) -> list:
    repo = AcquisitionRepository(session)
    return [
        _acquisition_out(acquisition, job_count=job_count, live_job_count=live_job_count)
        for acquisition, job_count, live_job_count in repo.list_acquisitions_with_job_counts(
            limit=limit,
            offset=offset,
        )
    ]


def get_acquisition_service(session: Session, acquisition_id: str):
    repo = AcquisitionRepository(session)
    acq = repo.get_acquisition(acquisition_id)
    if acq is None:
        raise HTTPException(status_code=404, detail="Acquisition not found")
    return _acquisition_out(acq)


def create_acquisition_service(session: Session, payload) -> AcquisitionOut:
    repo = AcquisitionRepository(session)
    acq = repo.create_acquisition(payload.name)
    return _acquisition_out(acq)


def update_acquisition_status_service(session: Session, acquisition_id: str, status: str) -> AcquisitionOut:
    repo = AcquisitionRepository(session)
    try:
        acq = repo.update_acquisition_status(acquisition_id, status)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Acquisition not found") from exc
    return _acquisition_out(acq)


def list_cves_service():
    index: dict[str, list[str]] = {}
    for w in load_workloads():
        for cve in w.get("cve_ids", []):
            upper = cve.upper()
            index.setdefault(upper, []).append(w["id"])
    return [{"cve_id": cve, "workload_ids": wids} for cve, wids in sorted(index.items())]
