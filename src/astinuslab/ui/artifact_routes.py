from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ..core.config import settings as _default_settings
from ..db import session_scope as _default_session_scope
from ..db.repositories import JobRepository
from ..storage.downloads import PinnedFileResponse
from .jobs_helpers import (
    redirect_with_message as _redirect_with_message,
)
from .jobs_helpers import (
    redirect_with_success as _redirect_with_success,
)

router = APIRouter()

# Test compatibility: route modules historically shared astinuslab.ui globals.


class _SettingsProxy:
    """Read-only test seam: resolves astinuslab.ui.settings on each access.

    Kept deliberately (B03 review): many tests monkeypatch
    astinuslab.ui.settings / .session_scope; the proxy is read-only
    indirection with no global mutation. The canonical /api/v1 routes in
    volatility_api use real module imports and are unaffected.
    """

    def __getattr__(self, name):
        import astinuslab.ui as ui_mod

        current = getattr(ui_mod, "settings", _default_settings)
        if current is self:
            current = _default_settings
        try:
            return getattr(current, name)
        except (AttributeError, TypeError):
            return getattr(_default_settings, name)

    def model_dump(self, *args, **kwargs):
        import astinuslab.ui as ui_mod

        current = getattr(ui_mod, "settings", _default_settings)
        if current is self:
            current = _default_settings
        return current.model_dump(*args, **kwargs)


class _SessionScopeProxy:
    def __call__(self, *args, **kwargs):
        import astinuslab.ui as ui_mod

        current = getattr(ui_mod, "session_scope", _default_session_scope)
        if current is self:
            current = _default_session_scope
        return current(*args, **kwargs)


settings = _SettingsProxy()
session_scope = _SessionScopeProxy()


from .job_context import (
    _base_ctx,
    templates,
)


def _read_sha256_log(job_id: str, kind: str = "memory") -> str | None:
    """Read SHA-256 from the log file written by publish_dump_to_host.sh / publish_disk_to_host.sh."""
    suffix = "-disk-dump.sha256" if kind == "disk" else "-dump.sha256"
    log_path = settings.storage_dir / "logs" / f"{job_id}{suffix}"
    if log_path.exists():
        try:
            return log_path.read_text(encoding="utf-8").strip().split()[0]
        except Exception:
            return None
    return None


def _dumps_dir() -> Path:
    return settings.storage_dir / "dumps"


def _is_inside_dumps(path: Path) -> bool:
    root = _dumps_dir().resolve()
    resolved = path.resolve()
    return resolved == root or resolved.is_relative_to(root)


_DUMP_KINDS = ("memory", "disk")


def _resolve_dump_file(filename: str, stored_path: str | None = None) -> Path | None:
    safe = Path(filename).name
    if stored_path:
        candidate = Path(stored_path)
        if candidate.name == safe and candidate.exists() and candidate.is_file() and _is_inside_dumps(candidate):
            return candidate

    root = _dumps_dir()
    top_level = root / safe
    if top_level.exists() and top_level.is_file():
        return top_level
    if root.exists():
        for candidate in root.rglob(safe):
            if candidate.name == safe and candidate.is_file() and _is_inside_dumps(candidate):
                return candidate
    return None


def _resolve_job_dump_file(job_id: str, kind: str) -> Path | None:
    """Resolve one job's dump file (kind: memory|disk), never another job's."""
    if kind not in _DUMP_KINDS:
        return None
    with session_scope() as session:
        job = JobRepository(session).get_job(job_id)
        if job is None:
            return None
        if kind == "disk":
            filename, stored_path = job.disk_dump_filename, job.disk_dump_path
        else:
            filename, stored_path = job.dump_filename, job.dump_path
    if not filename:
        return None
    safe = Path(filename).name
    if stored_path:
        candidate = Path(stored_path)
        if candidate.name == safe and candidate.is_file() and _is_inside_dumps(candidate):
            return candidate
    job_dir = _dumps_dir() / Path(job_id).name
    if job_dir.is_dir() and _is_inside_dumps(job_dir):
        for candidate in job_dir.rglob(safe):
            if candidate.name == safe and candidate.is_file() and _is_inside_dumps(candidate):
                return candidate
    return None


def _parse_dump_item(item: str) -> tuple[str, str] | None:
    """Parse a '<job_id>:<kind>' form value; returns None when malformed."""
    job_id, sep, kind = item.rpartition(":")
    if not sep or not job_id or kind not in _DUMP_KINDS:
        return None
    return Path(job_id).name, kind


def _dump_entry(
    filename: str | None,
    path: str | None,
    size_bytes: int | None,
    checksum: str | None,
    job_id: str,
    kind: str,
    live_acquisition: bool = False,
) -> dict | None:
    if not filename:
        return None
    sha = checksum or _read_sha256_log(job_id, kind) or None
    resolved = _resolve_dump_file(filename, path)
    on_disk = resolved is not None
    size_gb = (size_bytes or 0) / (1024**3)
    # If the file is on disk but size_bytes is missing from DB, read from filesystem
    if resolved is not None and not size_bytes:
        try:
            size_bytes = resolved.stat().st_size
            size_gb = size_bytes / (1024**3)
        except OSError:
            pass
    return {
        "filename": filename,
        "path": path,
        "size_bytes": size_bytes,
        "size_gb": size_gb,
        "checksum": sha,
        "exists": on_disk,
        "kind": kind,
        "live_acquisition": live_acquisition,
    }


def _list_dump_groups() -> dict:
    """Returns job-centric groups plus orphan files for the Dump Vault."""
    dumps_dir = _dumps_dir()
    total_files = 0
    total_bytes = 0

    groups: list[dict] = []

    with session_scope() as session:
        repo = JobRepository(session)
        all_jobs = repo.list_jobs()
        known_job_ids = {j.id for j in all_jobs}
        jobs_with_dumps = [j for j in all_jobs if j.dump_filename or j.disk_dump_filename]
        jobs_with_dumps.sort(key=lambda j: j.finished_at or j.created_at, reverse=True)
        for job in jobs_with_dumps:
            memory = _dump_entry(
                job.dump_filename,
                job.dump_path,
                job.dump_size_bytes,
                job.dump_checksum,
                job.id,
                "memory",
            )
            disk_artifact = job._latest_artifact("disk")
            disk_live = False
            if disk_artifact is not None and disk_artifact.metadata_json:
                try:
                    disk_live = bool(json.loads(disk_artifact.metadata_json).get("live_acquisition"))
                except ValueError:
                    pass
            disk = _dump_entry(
                job.disk_dump_filename,
                job.disk_dump_path,
                job.disk_dump_size_bytes,
                job.disk_dump_checksum,
                job.id,
                "disk",
                live_acquisition=disk_live,
            )
            # Summarize acquisitions shown in the vault, not sidecars, logs,
            # or symbol-build metadata that have no corresponding dump row.
            for artifact in (memory, disk):
                if artifact and artifact["exists"]:
                    total_files += 1
                    total_bytes += artifact["size_bytes"] or 0
            groups.append(
                {
                    "job_id": job.id,
                    "target_id": job.target_id,
                    "status": job.status,
                    "capture_type": job.capture_type or "memory",
                    "finished_at": job.finished_at,
                    "memory": memory,
                    "disk": disk,
                }
            )

    # Orphans are dump directories under dumps/ with no owning job in the DB.
    # Each job stores its acquisition in dumps/<job_id>/ (a *-memory.dump or
    # *-disk.img.gz plus .sha256 and .manifest.json sidecars), so the orphan
    # unit is the directory.
    orphans = []
    if dumps_dir.exists():
        for entry in dumps_dir.iterdir():
            if not entry.is_dir() or entry.name in known_job_ids:
                continue
            dir_bytes = 0
            dir_files = 0
            latest = 0.0
            has_dump = False
            for f in entry.rglob("*"):
                if not f.is_file():
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                if f.name.endswith("-memory.dump") or f.name == "memory.dump" or f.name.endswith("-disk.img.gz"):
                    has_dump = True
                    total_files += 1
                    total_bytes += st.st_size
                dir_bytes += st.st_size
                dir_files += 1
                latest = max(latest, st.st_mtime)
            # Skip dirs that hold only Volatility 3 / ISF analysis byproducts
            # (vol3 symbol caches, build logs, guest metadata) and no acquisition.
            if not has_dump:
                continue
            orphans.append(
                {
                    "dirname": entry.name,
                    "job_id": entry.name,
                    "size_bytes": dir_bytes,
                    "size_gb": dir_bytes / (1024**3),
                    "file_count": dir_files,
                    "modified": datetime.fromtimestamp(latest, tz=UTC) if latest else None,
                }
            )
    orphans.sort(
        key=lambda o: cast("datetime | None", o["modified"]) or datetime.fromtimestamp(0, tz=UTC),
        reverse=True,
    )

    return {
        "groups": groups,
        "orphans": orphans,
        "total_files": total_files,
        "total_gb": total_bytes / (1024**3),
    }


@router.get("/dumps", response_class=HTMLResponse)
def admin_dumps_page(request: Request):
    return templates.TemplateResponse(
        request,
        "admin_dumps.html",
        {
            "vault": _list_dump_groups(),
            "message": request.query_params.get("message"),
            # Error-by-default: only this app's own redirects opt into the green banner.
            "message_type": "success" if request.query_params.get("message_type") == "success" else "error",
            **_base_ctx(request),
        },
    )


@router.get("/dumps/{job_id}/{kind}/download")
def admin_dumps_download(job_id: str, kind: str):
    safe_job_id = Path(job_id).name
    dump_path = _resolve_job_dump_file(safe_job_id, kind)
    if dump_path is None:
        return _redirect_with_message("/dumps", f"Archivo no encontrado: {safe_job_id}:{kind}")
    dump_path = dump_path.resolve()
    if not _is_inside_dumps(dump_path):
        return _redirect_with_message("/dumps", "Artifact file is not available")
    return PinnedFileResponse(
        path=str(dump_path),
        filename=dump_path.name,
        media_type="application/octet-stream",
    )


def _remove_dump(job_id: str, kind: str) -> tuple[int, list[str]]:
    """Delete one job's dump through the custody-aware service.

    Returns (deleted_count, refusal_reasons). This route used to unlink the file
    and drop the rows unconditionally, which meant a button in the UI could
    destroy evidence that retention would have refused to touch.
    """
    from ..db.models import Artifact
    from ..storage import deletion

    safe_job_id = Path(job_id).name
    if kind not in _DUMP_KINDS:
        return 0, [f"unknown dump kind: {kind}"]

    ids = []
    deleted = 0
    refusals: list[str] = []
    roots = deletion.evidence_roots(settings)
    with session_scope() as session:
        artifacts = (
            session.query(Artifact)
            .filter(
                Artifact.producer == "job_capture",
                Artifact.job_id == safe_job_id,
                Artifact.kind == kind,
            )
            .all()
        )
        for artifact in artifacts:
            outcome = deletion.delete_artifact(
                session,
                artifact,
                actor="ui:dumps",
                reason="deleted from the Dump Vault",
                roots=roots,
            )
            if outcome.deleted:
                deleted += 1
                if outcome.tombstone_id is None:
                    raise RuntimeError("deleted artifact is missing its custody record")
                ids.append(outcome.tombstone_id)
            elif outcome.protection_reason:
                refusals.append(outcome.protection_reason)
        session.commit()
        for outcome in deletion.cleanup_deletions(session, ids, roots=roots):
            refusals.extend(outcome.errors)
    return deleted, refusals


@router.post("/dumps/delete")
def admin_dumps_delete(item: str = Form(...)):
    parsed = _parse_dump_item(item)
    if parsed is None:
        return _redirect_with_message("/dumps", f"Invalid dump reference: {item}")
    job_id, kind = parsed
    deleted, refusals = _remove_dump(job_id, kind)
    if not deleted:
        detail = refusals[0] if refusals else "nothing to delete"
        return _redirect_with_message("/dumps", f"Dump kept: {job_id} ({kind}) — {detail}")
    if refusals:
        return _redirect_with_message(
            "/dumps", f"Evidence disposal recorded; file cleanup incomplete: {'; '.join(refusals)}"
        )
    return _redirect_with_success("/dumps", f"Dump deleted: {job_id} ({kind}).")


@router.post("/dumps/bulk-delete")
def admin_dumps_bulk_delete(items: list[str] = Form(default=[])):
    deleted = 0
    kept = 0
    cleanup_errors: list[str] = []
    for item in items:
        parsed = _parse_dump_item(item)
        if parsed is None:
            continue
        job_id, kind = parsed
        removed, refusals = _remove_dump(job_id, kind)
        deleted += removed
        if not removed and refusals:
            kept += 1
        elif removed:
            cleanup_errors.extend(refusals)
    if cleanup_errors:
        return _redirect_with_message(
            "/dumps",
            f"Evidence disposals recorded: {deleted}. Kept: {kept}. File cleanup incomplete: {'; '.join(cleanup_errors)}",
        )
    if kept:
        # Silence here would read as success; protected evidence staying put is
        # the whole point of the service.
        return _redirect_with_message("/dumps", f"Dumps deleted: {deleted}. Kept (protected): {kept}.")
    return _redirect_with_success("/dumps", f"Dumps deleted: {deleted}.")


def _remove_orphan_dir(dirname: str) -> bool:
    """Audit orphan files and apply the same custody and durable cleanup policy."""
    from uuid import uuid4

    from ..db.models import Artifact
    from ..db.transactions import reserve_sqlite_write
    from ..storage import deletion

    safe = Path(dirname).name
    if not safe or safe in (".", ".."):
        return False
    target = _dumps_dir() / safe
    if target.is_symlink() or not target.is_dir() or not _is_inside_dumps(target):
        return False
    entries = list(target.rglob("*"))
    if any(path.is_symlink() or not (path.is_file() or path.is_dir()) for path in entries):
        return False
    roots = deletion.evidence_roots(settings)
    with session_scope() as session:
        reserve_sqlite_write(session)
        if JobRepository(session).get_job(safe) is not None:
            return False
        # A missing job does not imply missing custody. Match primary files and
        # sidecars so a case-owned artifact cannot be erased via its directory.
        artifacts = [
            artifact
            for artifact in session.query(Artifact).all()
            if any(
                Path(raw).resolve().is_relative_to(target.resolve())
                for raw in (artifact.path, artifact.manifest_path, artifact.sha256_sidecar_path)
                if raw
            )
        ]
        if any(
            not Path(raw).resolve().is_relative_to(target.resolve())
            for artifact in artifacts
            for raw in (artifact.path, artifact.manifest_path, artifact.sha256_sidecar_path)
            if raw
        ):
            return False
        if any(deletion.protection_reason(session, artifact) for artifact in artifacts):
            return False
        recorded = {
            str(Path(raw))
            for artifact in artifacts
            for raw in (artifact.path, artifact.manifest_path, artifact.sha256_sidecar_path)
            if raw
        }
        # Untracked files gain a canonical row just long enough to use the same
        # tombstone transition; only the tombstone survives the commit.
        for path in entries:
            if path.is_file() and str(path) not in recorded:
                artifact = Artifact(
                    id=f"orphan-{uuid4().hex}",
                    kind="orphan",
                    producer="orphan_cleanup",
                    path=str(path),
                    filename=path.name,
                    size_bytes=path.stat().st_size,
                )
                session.add(artifact)
                artifacts.append(artifact)
        session.flush()
        ids = []
        for artifact in artifacts:
            outcome = deletion.delete_artifact(
                session, artifact, actor="ui:orphans", reason="orphan dump directory cleanup", roots=roots
            )
            if not outcome.deleted:
                session.rollback()
                return False
            if outcome.tombstone_id is None:
                raise RuntimeError("deleted artifact is missing its custody record")
            ids.append(outcome.tombstone_id)
        session.commit()
        outcomes = deletion.cleanup_deletions(session, ids, roots=roots)
        if any(outcome.errors for outcome in outcomes):
            return False
    try:
        for path in sorted((p for p in entries if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            path.rmdir()
        target.rmdir()
    except OSError:
        return False
    return True


@router.post("/dumps/orphans/delete")
def admin_dumps_orphans_delete(dirnames: list[str] = Form(default=[])):
    selected = list(dict.fromkeys(dirnames))
    if not selected:
        return _redirect_with_message("/dumps", "Select at least one orphan dump directory.")
    deleted = sum(1 for dirname in selected if _remove_orphan_dir(dirname))
    not_removed = len(selected) - deleted
    if not_removed:
        return _redirect_with_message(
            "/dumps", f"Orphan cleanup incomplete. Deleted: {deleted}. Not removed: {not_removed}."
        )
    return _redirect_with_success("/dumps", f"Orphan dumps deleted: {deleted}.")


# ── SSH Key Management ─────────────────────────────────────────────────────────
