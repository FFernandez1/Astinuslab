"""Operator-supplied (manual) ISF and memory-dump uploads.

These endpoints let an operator who "knows what they are doing" attach a custom
ISF or memory dump to a job, bypassing the strict automatic safeguards:

* ISF: the GNU build-id cache match that normally forces a fresh live-guest
  build when a kernel is recompiled.
* Memory dump: the ELF PT_LOAD completeness check applied to host-captured
  dumps.

Uploaded artifacts are recorded with ``source``/``producer`` = ``manual_upload``
so their (unverified) provenance is honest in manifests and the UI.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, UploadFile

from ..api.schemas import JobCreate
from ..catalog import is_windows_family, load_scenarios, load_targets
from ..core.config import settings
from ..core.locks import blocking_file_lock
from ..db import session_scope
from ..db.repositories import ArtifactRepository, JobRepository
from ..jobs.job_status import TERMINAL_STATUSES
from ..storage import publication
from ..storage.artifacts import build_artifact_manifest
from .capture_support import memory_dump_filename
from .vol3_support import (
    _isf_cache_lock,
    _new_temp_isf_path,
    _replace_with_valid_isf,
    _sha256_file,
    _symbol_slug,
)

logger = logging.getLogger(__name__)

_DUMP_CHUNK_SIZE = 8 * 1024 * 1024
_DUMP_MAX_UPLOAD_BYTES = 64 * 1024 * 1024 * 1024
_DUMP_MIN_FREE_BYTES = 1024 * 1024 * 1024
_ISF_SCENARIO_MODES = {"immediate", "post_script"}
_VMLINUX_PREFIX = "vmlinux-"
_LINUX_VERSION_RE = re.compile(r"\bLinux version\s+(?P<release>\S+)")


def _linux_symbols_dir() -> Path:
    return settings.vol3_symbols_path / "linux"


def _job_is_terminal(job, *, label: str) -> None:
    if job.status not in TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail=f"manual {label} upload is only available for inactive jobs")


def _manual_dump_lock_path(job_id: str) -> Path:
    digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:24]
    return Path(settings.storage_dir) / "tmp" / "manual-dump-upload-locks" / f"{digest}.lock"


def _ensure_dump_storage_headroom(dump_dir: Path) -> None:
    try:
        free_bytes = shutil.disk_usage(dump_dir).free
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"failed to inspect dump storage capacity: {exc}") from exc
    if free_bytes < _DUMP_MIN_FREE_BYTES:
        raise HTTPException(status_code=507, detail="insufficient free space for manual dump upload")


def _parse_isf_payload(data: bytes) -> dict:
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"uploaded file is not a valid ISF: {exc}") from exc
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(status_code=400, detail="uploaded file is not a valid ISF: expected a JSON object")
    metadata = payload.get("metadata")
    linux = metadata.get("linux") if isinstance(metadata, dict) else None
    if not isinstance(linux, dict):
        raise HTTPException(status_code=400, detail="uploaded file is not a valid ISF: missing metadata.linux object")
    if not isinstance(payload.get("symbols"), dict) or not payload.get("symbols"):
        raise HTTPException(
            status_code=400, detail="uploaded file is not a valid ISF: missing non-empty symbols object"
        )
    return payload


def _kernel_release_from_isf_payload(payload: dict) -> str | None:
    metadata = payload.get("metadata")
    linux = metadata.get("linux") if isinstance(metadata, dict) else None
    if not isinstance(linux, dict):
        return None

    for key in ("kernel_release", "release"):
        value = linux.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    symbol_entries = linux.get("symbols")
    candidates: list[object]
    if isinstance(symbol_entries, dict):
        candidates = list(symbol_entries.values())
    elif isinstance(symbol_entries, list):
        candidates = symbol_entries
    else:
        candidates = []
    for entry in candidates:
        name = entry.get("name") if isinstance(entry, dict) else entry
        if isinstance(name, str) and name.startswith(_VMLINUX_PREFIX):
            release = name.removeprefix(_VMLINUX_PREFIX).strip()
            if release:
                return release

    for key in ("banner", "version"):
        value = linux.get(key)
        if isinstance(value, str):
            match = _LINUX_VERSION_RE.search(value)
            if match:
                return match.group("release")
    return None


def _isf_target(target_id: str) -> dict:
    target = next((item for item in load_targets() if str(item.get("id")) == target_id), None)
    if target is None or not target.get("enabled"):
        raise HTTPException(status_code=400, detail="target_id is unsupported or disabled")
    if is_windows_family(target.get("family")):
        raise HTTPException(status_code=400, detail="Upload ISF is only available for Linux targets")
    role = str(target.get("role") or "target").strip().lower()
    if role not in {"target", "vm"}:
        raise HTTPException(status_code=400, detail="Upload ISF is only available for target VMs")
    return target


def _isf_scenario(scenario_id: str) -> dict:
    scenarios = [
        scenario
        for scenario in load_scenarios()
        if scenario.get("enabled") and scenario.get("mode") in _ISF_SCENARIO_MODES
    ]
    if scenario_id:
        scenario = next((item for item in scenarios if str(item.get("id")) == scenario_id), None)
    else:
        scenario = next((item for item in scenarios if item.get("mode") == "immediate"), None) or (
            scenarios[0] if scenarios else None
        )
    if scenario is None:
        raise HTTPException(status_code=400, detail="Scenario is not available for Volatility symbol uploads")
    return scenario


def _find_reusable_isf_job(repo: JobRepository, target_id: str, kernel_release: str):
    for job in repo.list_jobs():
        if job.target_id != target_id:
            continue
        if not job.build_vol3_isf or job.capture_type != "none" or job.status not in TERMINAL_STATUSES:
            continue
        if job.status != "completed":
            continue
        if job.detected_kernel_release not in (None, kernel_release):
            continue
        return job
    return None


def _create_isf_anchor_job(repo: JobRepository, target: dict, scenario: dict, kernel_release: str):
    payload = JobCreate(
        target_id=str(target["id"]),
        scenario_id=str(scenario["id"]),
        execution_mode=str(scenario["mode"]),
        capture_type="none",
        build_vol3_isf=True,
    )
    job = repo.create_job(payload)
    now = datetime.now(UTC)
    repo.force_transition(
        job.id,
        "completed",
        detected_distro=target.get("distro"),
        detected_version=target.get("version"),
        detected_kernel_release=kernel_release,
        detected_architecture=str(target.get("architecture") or target.get("arch") or "x86_64"),
        started_at=now,
        finished_at=now,
    )
    repo.add_event(job.id, "completed", "Manual ISF upload created anchor job")
    return job


def store_manual_isf(job_id: str, data: bytes) -> dict:
    """Store an operator-supplied ISF for a job, bypassing build-id enforcement.

    The uploaded JSON is validated as a Volatility 3 ISF (``metadata.linux`` plus
    a non-empty ``symbols`` object), placed in the Linux symbol cache under the
    job kernel's build-id-qualified slug, and recorded as an ISFArtifact with
    ``source="manual_upload"`` and ``validation_status="valid"`` so that
    ``run_vol3_analysis`` selects it without requiring a live-guest rebuild.

    The artifact is stamped with the job's most recently known build id (if any),
    which is what makes the build-id-keyed selection match — i.e. the manual
    upload deliberately stands in for the rebuilt kernel's symbols.
    """
    with session_scope() as session:
        repo = JobRepository(session)
        job = repo.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        _job_is_terminal(job, label="ISF")
        kernel_release = job.detected_kernel_release
        distro = job.detected_distro
        version = job.detected_version
        architecture = job.detected_architecture or "x86_64"
        if job.execution_mode == "artifact_analysis":
            from .artifact_context import validate_analysis_context, validate_isf_context

            validate_analysis_context(session, job)
            validate_isf_context(data, kernel_release or "", architecture)
        known_build_id = repo.get_job_known_build_id(job_id)

    if not kernel_release:
        raise HTTPException(
            status_code=400,
            detail="job has no detected kernel release; a manual ISF cannot be associated with it",
        )

    slug = _symbol_slug(kernel_release, known_build_id)
    final_path = _linux_symbols_dir() / f"{slug}.json"

    with _isf_cache_lock(slug):
        tmp_path = _new_temp_isf_path(final_path)
        try:
            tmp_path.write_bytes(data)
        except OSError as exc:
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=f"failed to stage uploaded ISF: {exc}") from exc
        # _replace_with_valid_isf validates the staged file (metadata.linux +
        # non-empty symbols), atomically replaces final_path on success, and
        # unlinks the temp file on failure.
        placed, reason = _replace_with_valid_isf(tmp_path, final_path)
        if not placed:
            raise HTTPException(status_code=400, detail=f"uploaded file is not a valid ISF: {reason}")

    sha256 = _sha256_file(final_path)

    with session_scope() as session:
        repo = JobRepository(session)
        artifact = repo.create_isf_artifact(
            job_id,
            kernel_release,
            str(final_path),
            "manual_upload",
            distro=distro,
            version=version,
            sha256=sha256,
            build_id=known_build_id,
            validation_status="valid",
            architecture=architecture,
        )
        artifact_id = artifact.id
        repo.add_event(
            job_id,
            "vol3_isf_uploaded",
            f"Manual ISF uploaded (unverified): {final_path} sha256:{sha256}",
        )

    logger.info("[%s] manual ISF stored at %s (build_id=%s)", job_id, final_path, known_build_id)
    return {
        "status": "stored",
        "isf_path": str(final_path),
        "sha256": sha256,
        "kernel_release": kernel_release,
        "build_id": known_build_id,
        "source": "manual_upload",
        "artifact_id": artifact_id,
    }


def store_manual_isf_for_target(target_id: str, scenario_id: str, data: bytes) -> dict:
    """Store an operator-supplied ISF from the Volatility target row.

    The workbench row has no Job yet, so derive the kernel release from the ISF
    metadata and create (or reuse) a completed ISF-only Job to anchor artifact
    provenance and make the target row read as built on reload.
    """
    payload = _parse_isf_payload(data)
    kernel_release = _kernel_release_from_isf_payload(payload)
    if not kernel_release:
        raise HTTPException(status_code=400, detail="uploaded ISF does not expose a Linux kernel release")

    target = _isf_target(target_id)
    scenario = _isf_scenario(scenario_id.strip())

    with session_scope() as session:
        repo = JobRepository(session)
        job = _find_reusable_isf_job(repo, target_id, kernel_release)
        job_created = job is None
        if job is None:
            job = _create_isf_anchor_job(repo, target, scenario, kernel_release)
        elif job.detected_kernel_release is None:
            repo.update_job(job.id, detected_kernel_release=kernel_release)
        job_id = job.id

    result = store_manual_isf(job_id, data)
    result["job_id"] = job_id
    result["job_created"] = job_created
    result["url"] = f"/jobs/{job_id}"
    return result


_MANUAL_UPLOAD_CAVEAT = (
    "Operator-supplied upload: these bytes were provided through the manual dump "
    "route, not acquired by AstinusLab. ELF PT_LOAD completeness validation was "
    "deliberately skipped and the contents are unverified; the recorded SHA-256 "
    "attests only to what was stored, not to how it was obtained."
)


def store_manual_dump(job_id: str, upload: UploadFile) -> dict:
    """Store an operator-supplied memory dump for a job, bypassing ELF validation.

    The upload is streamed to ``storage/dumps/<job_id>/`` (so the existing
    download route's path policy still serves it) and recorded as a
    ``kind="memory"`` Artifact with ``producer="manual_upload"``. Unlike a
    host capture, the ELF PT_LOAD completeness check is intentionally skipped;
    only a non-empty file is required.
    """
    with session_scope() as session:
        repo = JobRepository(session)
        job = repo.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        _job_is_terminal(job, label="memory dump")
        distro = job.detected_distro or "linux"
        version = job.detected_version or ""
        kernel_release = job.detected_kernel_release

    dump_dir = Path(settings.storage_dir) / "dumps" / job_id
    dump_dir.mkdir(parents=True, exist_ok=True)
    # Prefix avoids clobbering a genuine host capture's file. The per-upload
    # nonce keeps prior Artifact rows immutable when operators upload again.
    upload_nonce = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:12]}"
    filename = f"manual-{upload_nonce}-" + memory_dump_filename(distro, version, kernel_release)
    output_path = dump_dir / filename

    digest = hashlib.sha256()
    size_bytes = 0
    with blocking_file_lock(_manual_dump_lock_path(job_id)):
        _ensure_dump_storage_headroom(dump_dir)
        with publication.attempt(
            session_scope,
            producer="manual_upload",
            kind="memory",
            final_path=output_path,
            staging_parent=dump_dir,
            job_id=job_id,
        ) as attempt:
            try:
                with attempt.open_payload() as out:
                    while chunk := upload.file.read(_DUMP_CHUNK_SIZE):
                        size_bytes += len(chunk)
                        if size_bytes > _DUMP_MAX_UPLOAD_BYTES:
                            raise HTTPException(status_code=413, detail="manual dump upload is too large")
                        out.write(chunk)
                        digest.update(chunk)
                        _ensure_dump_storage_headroom(dump_dir)
                    if size_bytes == 0:
                        raise HTTPException(status_code=400, detail="uploaded dump is empty")
            except HTTPException:
                raise
            except OSError as exc:
                raise HTTPException(status_code=500, detail=f"failed to write uploaded dump: {exc}") from exc

            sha256 = digest.hexdigest()

            def build_manual_manifest(staged_sha256: str, staged_size: int) -> dict:
                # Two independent digests: one of the bytes as they arrived, one
                # of the bytes now on disk. Disagreement means something rewrote
                # the file mid-upload, and it is no longer what was sent.
                if staged_sha256 != sha256 or staged_size != size_bytes:
                    raise HTTPException(
                        status_code=500,
                        detail="uploaded dump changed while it was being stored",
                    )
                return build_artifact_manifest(
                    artifact_path=output_path,
                    kind="memory",
                    producer="manual_upload",
                    sha256=staged_sha256,
                    size_bytes=staged_size,
                    job_id=job_id,
                    distro=distro,
                    version=version,
                    kernel_release=kernel_release,
                    capture_method="manual_upload",
                    forensic_caveat=_MANUAL_UPLOAD_CAVEAT,
                )

            triad = attempt.stage(build_manual_manifest)
            published = attempt.publish()

            with session_scope() as session:
                artifact = ArtifactRepository(session).create_artifact(
                    kind="memory",
                    producer="manual_upload",
                    path=str(published.artifact_path),
                    filename=filename,
                    sha256=triad.sha256,
                    size_bytes=triad.size_bytes,
                    manifest_path=str(published.manifest_path),
                    sha256_sidecar_path=str(published.sidecar_path),
                    job_id=job_id,
                )
                JobRepository(session).add_event(
                    job_id,
                    "manual_dump_uploaded",
                    f"Manual memory dump uploaded (unverified): {output_path} sha256:{sha256}",
                )
                attempt.complete(session, artifact_id=artifact.id)
            attempt.finish()

    logger.info("[%s] manual memory dump stored at %s (%d bytes)", job_id, output_path, size_bytes)
    return {
        "status": "stored",
        "dump_filename": filename,
        "dump_path": str(output_path),
        "sha256": sha256,
        "size_bytes": size_bytes,
        "source": "manual_upload",
    }
