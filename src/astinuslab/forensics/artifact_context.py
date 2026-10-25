"""Exact-source Playground analysis, using real Jobs only as action/run contexts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from ..db.models import Acquisition, Artifact, Job, JobVM, PlaygroundSession
from ..db.repositories import JobRepository


def source_artifact(session, artifact_id: str) -> Artifact:
    artifact = session.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(404, "Source artifact no longer exists")
    if artifact.kind != "memory":
        raise HTTPException(400, "Volatility requires a memory artifact, not disk evidence")
    if artifact.job_id or not artifact.session_id or artifact.ownership_reconciliation_json:
        raise HTTPException(409, "Artifact Playground ownership needs reconciliation")
    if session.get(PlaygroundSession, artifact.session_id) is None:
        raise HTTPException(409, "Source session no longer exists")
    acq = session.get(Acquisition, artifact.acquisition_id) if artifact.acquisition_id else None
    if acq is None or acq.session_id != artifact.session_id or acq.kind not in (None, "memory"):
        raise HTTPException(409, "Source acquisition does not agree with artifact ownership")
    if artifact.case_id and artifact.case_id != acq.case_id:
        raise HTTPException(409, "Source case does not agree with acquisition")
    if acq.job_vm_id:
        vm = session.get(JobVM, acq.job_vm_id)
        if vm is None or vm.session_id != artifact.session_id:
            raise HTTPException(409, "Source VM does not agree with acquisition")
    if not Path(artifact.path).is_file() or not artifact.sha256:
        raise HTTPException(409, "Source memory bytes or recorded checksum are unavailable")
    return artifact


def create_analysis_context(session, artifact_id, *, kernel_release, architecture, verified, provenance):
    artifact = source_artifact(session, artifact_id)
    kernel_release, architecture, provenance = kernel_release.strip(), architecture.strip(), provenance.strip()
    if not verified or not kernel_release or architecture != "x86_64" or not provenance:
        raise HTTPException(400, "Verified capture kernel, x86_64 architecture and verification note are required")
    if len(kernel_release) > 255 or len(provenance) > 2048 or any(c.isspace() for c in kernel_release):
        raise HTTPException(400, "Invalid capture context")
    existing = session.scalars(select(Job).where(Job.source_artifact_id == artifact_id)).first()
    if existing:
        if (existing.detected_kernel_release, existing.detected_architecture) != (kernel_release, architecture):
            raise HTTPException(
                409, "Analysis context already binds a different verified kernel; reconcile before rerunning"
            )
        return existing
    now = datetime.now(UTC)
    context = {
        "source": "operator_verified",
        "kernel_release": kernel_release,
        "architecture": architecture,
        "os_family": "linux",
        "verification_note": provenance,
        "recorded_at": now.isoformat(),
        "artifact_id": artifact.id,
        "sha256": artifact.sha256,
        "session_id": artifact.session_id,
        "acquisition_id": artifact.acquisition_id,
    }
    job = Job(
        id=f"analysis-{uuid4().hex[:16]}",
        target_id="artifact-analysis",
        scenario_id="artifact-analysis",
        execution_mode="artifact_analysis",
        status="completed",
        capture_type="none",
        source_artifact_id=artifact.id,
        acquisition_id=artifact.acquisition_id,
        detected_distro="linux",
        detected_kernel_release=kernel_release,
        detected_architecture=architecture,
        analysis_context_json=json.dumps(context, sort_keys=True),
        finished_at=now,
    )
    session.add(job)
    session.flush()
    return job


def validate_isf_context(data: bytes, kernel_release: str, architecture: str):
    """New artifact contexts require actual ISF metadata, never DB relabeling."""
    from .manual_upload import _kernel_release_from_isf_payload, _parse_isf_payload

    payload = _parse_isf_payload(data)
    linux = payload["metadata"]["linux"]
    actual_kernel = _kernel_release_from_isf_payload(payload)
    actual_arch = linux.get("architecture") or payload["metadata"].get("architecture")
    if actual_kernel != kernel_release or actual_arch != architecture:
        raise HTTPException(
            409,
            "ISF kernel/architecture metadata is unknown or does not match the verified capture; supply matching operator-verified symbols",
        )


def validate_selected_isf(artifact, kernel_release, architecture):
    if artifact is None:
        raise HTTPException(409, "A valid ISF matching the verified capture kernel and architecture is required")
    try:
        data = Path(artifact.path).read_bytes()
    except OSError as exc:
        raise HTTPException(409, "Matching ISF bytes are unavailable") from exc
    validate_isf_context(data, kernel_release, architecture)


def validate_analysis_context(session, job, *, require_symbols=False):
    artifact = source_artifact(session, job.source_artifact_id) if job.source_artifact_id else None
    if artifact is None:
        raise HTTPException(409, "Analysis source was removed; select retained evidence")
    try:
        context = json.loads(job.analysis_context_json or "{}")
    except ValueError as exc:
        raise HTTPException(409, "Invalid stored analysis context") from exc
    if (
        not isinstance(context, dict)
        or context.get("session_id") != artifact.session_id
        or context.get("acquisition_id") != artifact.acquisition_id
        or context.get("source") != "operator_verified"
        or context.get("artifact_id") != artifact.id
        or context.get("sha256") != artifact.sha256
        or context.get("kernel_release") != job.detected_kernel_release
        or context.get("architecture") != job.detected_architecture
        or not job.detected_kernel_release
        or job.detected_architecture != "x86_64"
        or job.acquisition_id != artifact.acquisition_id
    ):
        raise HTTPException(409, "Verified context no longer agrees with the original capture")
    if require_symbols:
        from .vol3_analysis import _select_isf_artifact

        repo = JobRepository(session)
        latest = repo.get_latest_isf_artifact(job.id)
        selected = _select_isf_artifact(
            repo,
            job.id,
            job.detected_kernel_release,
            job.detected_architecture,
            build_id=latest.build_id if latest else None,
        )
        validate_selected_isf(selected, job.detected_kernel_release, job.detected_architecture)
    return artifact, context


def playground_dumps(session):
    rows = []
    for artifact in session.scalars(
        select(Artifact).where(Artifact.kind == "memory", Artifact.session_id.is_not(None))
    ):
        try:
            source_artifact(session, artifact.id)
        except HTTPException:
            continue
        acq = session.get(Acquisition, artifact.acquisition_id)
        job = session.scalars(select(Job).where(Job.source_artifact_id == artifact.id)).first()
        ready = False
        if job:
            try:
                validate_analysis_context(session, job, require_symbols=True)
                ready = True
            except HTTPException:
                pass
        kernel = job.detected_kernel_release if job else None
        rows.append(
            dict(
                selection_id=f"artifact:{artifact.id}",
                artifact_id=artifact.id,
                job_id=job.id if job else None,
                session_id=artifact.session_id,
                vm_id=acq.job_vm_id,
                vm_label=acq.vm_label,
                file=artifact.filename or Path(artifact.path).name,
                os_family="linux",
                os_label="Linux (operator verified)" if job else "Unknown capture context",
                kernel=kernel or "—",
                source=f"{artifact.session_id} · {acq.vm_label or acq.job_vm_id or 'VM unknown'} · {artifact.id}",
                size_bytes=artifact.size_bytes,
                captured=artifact.created_at.isoformat(),
                symbols="ready" if ready else "missing",
                sym_label="ISF matched" if ready else ("ISF needed" if job else "Verified capture context needed"),
                context_required=job is None,
            )
        )
    return rows
