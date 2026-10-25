from __future__ import annotations

import logging
import os
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

logger = logging.getLogger(__name__)

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ..api.schemas import (
    ImageDownloadConfig,
)
from ..catalog import (
    invalidate_catalog_caches,
)
from ..core.config import settings as _default_settings
from ..db import session_scope as _default_session_scope
from ..images.image_admin_support import (
    BOOTABLE_IMAGE_EXTENSIONS as _BOOTABLE_IMAGE_EXTENSIONS,
)
from ..images.image_admin_support import (
    IMAGE_EXTENSIONS as _IMAGE_EXTENSIONS,
)
from ..images.image_admin_support import (
    UPLOAD_IMAGE_EXTENSIONS as _UPLOAD_IMAGE_EXTENSIONS,
)
from ..images.image_admin_support import (
    URL_DOWNLOADS as _URL_DOWNLOADS,
)
from ..images.image_admin_support import URL_DOWNLOADS_LOCK as _URL_DOWNLOADS_LOCK
from ..images.image_admin_support import ImageStagingOperation as _ImageStagingOperation
from ..images.image_admin_support import (
    download_image_bg as _download_image_bg,
)
from ..images.image_admin_support import (
    extract_allowed_domains as _extract_allowed_domains,
)
from ..images.image_admin_support import (
    finalize_uploaded_image as _finalize_uploaded_image,
)
from ..images.image_admin_support import (
    image_download_settings_ctx as _image_download_settings_ctx,
)
from ..images.image_admin_support import (
    list_images as _list_images,
)
from ..images.image_admin_support import (
    load_image_download_config as _load_image_download_config,
)
from ..images.image_admin_support import (
    release_image_destination as _release_image_destination,
)
from ..images.image_admin_support import (
    reserve_image_destination as _reserve_image_destination,
)
from ..images.image_admin_support import (
    save_image_download_config as _save_image_download_config,
)
from ..images.image_admin_support import (
    uploaded_image_suffix as _uploaded_image_suffix,
)
from ..images.image_admin_support import (
    validate_remote_target as _validate_remote_target,
)
from ..images.image_custody import delete_images
from .jobs_helpers import (
    redirect_with_success as _redirect_with_success,
)

router = APIRouter()

_UPLOAD_CHUNK_BYTES = 1024 * 1024

# Test compatibility: route modules historically shared astinuslab.ui globals.


class _SettingsProxy:
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


async def _write_upload_with_limit(file: UploadFile, tmp: Path, max_bytes: int) -> None:
    bytes_done = 0
    try:
        with tmp.open("xb") as out:
            while chunk := await file.read(_UPLOAD_CHUNK_BYTES):
                bytes_done += len(chunk)
                if bytes_done > max_bytes:
                    raise HTTPException(413, "Uploaded image exceeds configured size limit")
                out.write(chunk)
    except HTTPException:
        raise


from .job_context import (
    _base_ctx,
    templates,
)


@router.get("/images", response_class=HTMLResponse)
def admin_images_page(request: Request):
    config = _load_image_download_config()
    with _URL_DOWNLOADS_LOCK:
        downloads = {key: dict(value) for key, value in _URL_DOWNLOADS.items()}
    return templates.TemplateResponse(
        request,
        "admin_images.html",
        {
            "images": _list_images(),
            "downloads": downloads,
            "message": request.query_params.get("message"),
            # Error-by-default: only this app's own redirects opt into the green banner.
            "message_type": "success" if request.query_params.get("message_type") == "success" else "error",
            **_image_download_settings_ctx(config),
            **_base_ctx(request),
        },
    )


@router.post("/images/settings")
def admin_images_update_settings(
    source_policy: str = Form(...),
    network_policy: str = Form(...),
    allowed_domains: str = Form(""),
    connect_timeout_seconds: int = Form(...),
    read_timeout_seconds: int = Form(...),
    max_size_gb: int = Form(...),
):
    if source_policy not in {"official_only", "allowlist", "unrestricted"}:
        raise HTTPException(400, "Invalid source policy")
    if network_policy not in {"safe", "unrestricted"}:
        raise HTTPException(400, "Invalid network policy")

    config = ImageDownloadConfig(
        source_policy=source_policy,
        network_policy=network_policy,
        allowed_domains=_extract_allowed_domains(allowed_domains),
        connect_timeout_seconds=max(1, min(connect_timeout_seconds, 300)),
        read_timeout_seconds=max(1, min(read_timeout_seconds, 7200)),
        max_size_gb=max(1, min(max_size_gb, 500)),
    )
    _save_image_download_config(config)
    return _redirect_with_success("/images", "Image download settings updated.")


@router.post("/images/upload")
async def admin_images_upload(
    request: Request,
    file: UploadFile = File(...),
):
    fname = Path(file.filename or "").name  # strip any path components
    suffix = _uploaded_image_suffix(fname)
    if suffix not in _UPLOAD_IMAGE_EXTENSIONS:
        raise HTTPException(400, f"Extension not allowed: {suffix}")
    config = _load_image_download_config()
    max_upload_bytes = config.max_size_gb * (1024**3)
    operation = _ImageStagingOperation.create(settings.images_dir)
    try:
        await _write_upload_with_limit(file, operation.payload_path, max_upload_bytes)
        dest = _finalize_uploaded_image(
            operation.payload_path,
            fname,
            settings.images_dir,
            max_decompressed_bytes=config.max_size_gb * 1024 * 1024 * 1024,
        )
    finally:
        operation.cleanup()
    from ..images.image_inspection import inspect_image_bg

    invalidate_catalog_caches()
    inspect_image_bg(dest, force=True)
    return RedirectResponse(url="/images", status_code=303)


@router.post("/images/fetch-url", response_class=HTMLResponse)
def admin_images_fetch_url(request: Request, url: str = Form(...)):
    config = _load_image_download_config()
    url = url.strip()
    _validate_remote_target(url, config)
    fname = url.split("?")[0].rstrip("/").split("/")[-1] or "image.qcow2"
    # Ensure it has a valid extension
    suffix = Path(fname).suffix.lower()
    if suffix and suffix not in _BOOTABLE_IMAGE_EXTENSIONS:
        raise HTTPException(400, f"Extension not allowed for auto-bootable images: {suffix}")
    if suffix not in _BOOTABLE_IMAGE_EXTENSIONS:
        fname += ".qcow2"
    dest = settings.images_dir / fname
    settings.images_dir.mkdir(parents=True, exist_ok=True)
    dl_id = uuid4().hex
    _reserve_image_destination(dest, dl_id)
    state = {
        "status": "running",
        "url": url,
        "filename": fname,
        "bytes_done": 0,
        "total": None,
        "error": None,
        "started_at": datetime.now(UTC),
        "policy": config.source_policy,
    }
    with _URL_DOWNLOADS_LOCK:
        _URL_DOWNLOADS[dl_id] = state
    try:
        threading.Thread(target=_download_image_bg, args=(dl_id, url, dest, config), daemon=True).start()
    except BaseException:
        with _URL_DOWNLOADS_LOCK:
            _URL_DOWNLOADS.pop(dl_id, None)
        _release_image_destination(dest, dl_id)
        raise
    with _URL_DOWNLOADS_LOCK:
        response_state = dict(_URL_DOWNLOADS[dl_id])
    return templates.TemplateResponse(
        request,
        "partials/download_progress.html",
        {"dl": response_state, "dl_id": dl_id, **_base_ctx(request)},
    )


@router.get("/images/fetch-status/{dl_id}", response_class=HTMLResponse)
def admin_images_fetch_status(request: Request, dl_id: str):
    with _URL_DOWNLOADS_LOCK:
        current = _URL_DOWNLOADS.get(dl_id)
        dl = dict(current) if current is not None else None
    if dl is None:
        return Response(content="", status_code=200)
    return templates.TemplateResponse(
        request,
        "partials/download_progress.html",
        {"dl": dl, "dl_id": dl_id, **_base_ctx(request)},
    )


@router.post("/images/delete")
def admin_images_delete(filename: str = Form(...)):
    safe = Path(filename).name
    target_path = settings.images_dir / safe
    if target_path.suffix.lower() not in _IMAGE_EXTENSIONS:
        raise HTTPException(404, "Image not found")
    with session_scope() as session:
        delete_images(session, [target_path], images_dir=settings.images_dir, disk_root=settings.playground_disk_dir)
    invalidate_catalog_caches()
    return _redirect_with_success("/images", f"Image deleted: {safe}.")


@router.post("/images/bulk-delete")
def admin_images_bulk_delete(filenames: list[str] = Form(default=[])):
    paths = [settings.images_dir / Path(filename).name for filename in filenames]
    with session_scope() as session:
        deleted = delete_images(
            session,
            [path for path in paths if path.suffix.lower() in _IMAGE_EXTENSIONS],
            images_dir=settings.images_dir,
            disk_root=settings.playground_disk_dir,
            missing_ok=True,
        )
    if deleted:
        invalidate_catalog_caches()
    return _redirect_with_success("/images", f"Images deleted: {deleted}.")


@router.post("/images/open-folder", response_class=Response)
def admin_images_open_folder():
    path = str(settings.images_dir.resolve())
    # A file manager can only appear when the server shares the operator's
    # desktop session, as a development checkout does. Installed services run
    # under systemd without one, so say where the folder is instead.
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return JSONResponse({"detail": f"No desktop session on the server. Images are in {path}"}, status_code=409)
    try:
        process = subprocess.Popen(
            ["xdg-open", path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except OSError:
        return JSONResponse({"detail": f"Could not run xdg-open. Images are in {path}"}, status_code=503)
    # Reap xdg-open when it exits so repeated clicks leave no zombies behind.
    threading.Thread(target=process.wait, name="xdg-open-reaper", daemon=True).start()
    return Response(status_code=204)


# ── Image metadata / inspection API ────────────────────────────────────────────

from dataclasses import asdict  # noqa: E402
from typing import Literal  # noqa: E402

from pydantic import BaseModel as _BaseModel  # noqa: E402
from pydantic import StrictInt as _StrictInt  # noqa: E402
from pydantic import model_validator as _model_validator  # noqa: E402

from ..images.image_inspection import (  # noqa: E402
    _INSPECTIONS_LOCK,
    IMAGE_INSPECTIONS,
    SIDECAR_SCHEMA_VERSION,
    WINDOWS_SIDECAR_SCHEMA_VERSION,
    ImageMetadata,
    Source,
    WindowsImageMetadata,
    classify_image_preflight,
    ensure_metadata,
    inspect_image_bg,
    load_sidecar,
    save_sidecar,
)


def _metadata_response(target: Path, meta: ImageMetadata | WindowsImageMetadata) -> dict:
    data = asdict(meta)
    preflight = classify_image_preflight(target, meta)
    data.update(
        {
            "bootable": preflight["bootable"],
            "isf_supported": preflight["isf_supported"],
            "reusable": preflight["reusable"],
            "needs_user_action": preflight["needs_user_action"],
            "preflight_reasons": preflight["reasons"],
        }
    )
    return data


class _MetadataPayload(_BaseModel):
    platform: Literal["linux", "windows"] | None = None
    arch: str | None = None
    architecture: str | None = None
    distro: str
    version: str
    family: Literal["debian", "redhat", "other", "windows"]
    default_username: str
    os_variant: str
    machine_type: str | None = None
    cloud_init_profile: str | None = None
    inject_network_config: bool | None = None
    edition: str | None = None
    build: str | None = None
    revision: _StrictInt | None = None
    source: Literal["operator_prepared"] | None = None
    generalization: str | None = None
    shutdown_state: str | None = None
    virtual_size_bytes: _StrictInt | None = None
    firmware: dict[str, object] | None = None
    guest_access: dict[str, object] | None = None
    windows_update: dict[str, object] | None = None
    sha256: str | None = None
    first_boot: dict[str, object] | None = None

    @_model_validator(mode="after")
    def _validate_platform_fields(self):
        windows_fields = (
            self.edition,
            self.build,
            self.revision,
            self.source,
            self.generalization,
            self.shutdown_state,
            self.virtual_size_bytes,
            self.firmware,
            self.guest_access,
            self.windows_update,
            self.sha256,
            self.first_boot,
        )
        if self.family == "windows" and self.platform != "windows":
            raise ValueError("family=windows requires platform=windows")
        if any(value is not None for value in windows_fields) and self.platform != "windows":
            raise ValueError("Windows-only metadata requires platform=windows")
        if self.platform == "windows" and self.family != "windows":
            raise ValueError("platform=windows requires family=windows")
        if self.first_boot is not None:
            from ..images.windows_bundle import parse_windows_bundle

            parse_windows_bundle(self.first_boot)
        return self


def _resolve_image(name: str) -> Path:
    safe = Path(name).name
    target = settings.images_dir / safe
    if not target.exists() or target.suffix.lower() not in _IMAGE_EXTENSIONS:
        raise HTTPException(404, "Image not found")
    return target


@router.get("/admin/images/{name}/metadata")
def admin_image_metadata_get(name: str):
    target = _resolve_image(name)
    meta = ensure_metadata(target)
    return _metadata_response(target, meta)


@router.put("/admin/images/{name}/metadata")
def admin_image_metadata_put(name: str, payload: _MetadataPayload):
    target = _resolve_image(name)
    previous = load_sidecar(target)
    edited_fields: list[str] = list(previous.user_edited_fields) if previous else []
    payload_dict = payload.model_dump()
    meta: ImageMetadata | WindowsImageMetadata

    if payload.platform == "windows":
        required_windows = {
            "edition": payload.edition,
            "build": payload.build,
            "revision": payload.revision,
            "source": payload.source,
            "generalization": payload.generalization,
            "shutdown_state": payload.shutdown_state,
            "virtual_size_bytes": payload.virtual_size_bytes,
            "firmware": payload.firmware,
            "guest_access": payload.guest_access,
            "windows_update": payload.windows_update,
            "sha256": payload.sha256,
            "first_boot": payload.first_boot,
        }
        missing = [name for name, value in required_windows.items() if value is None]
        if missing:
            raise HTTPException(400, f"Windows metadata missing required fields: {', '.join(missing)}")
        # Accept both provenance spellings without silently discarding conflicts
        # or inheriting Linux inspector data into the Windows contract.
        if "arch" in payload.model_fields_set:
            arch = payload.arch
        elif "architecture" in payload.model_fields_set:
            arch = payload.architecture
        else:
            arch = previous.arch if isinstance(previous, WindowsImageMetadata) else "x86_64"
        architecture = payload.architecture if "architecture" in payload.model_fields_set else arch
        meta = WindowsImageMetadata(
            schema_version=WINDOWS_SIDECAR_SCHEMA_VERSION,
            platform="windows",
            distro=payload.distro,
            version=payload.version,
            family=payload.family,
            default_username=payload.default_username,
            os_variant=payload.os_variant,
            machine_type=payload.machine_type,
            kernel_release=None,
            arch=arch,
            architecture=architecture,
            cloud_init_profile=payload.cloud_init_profile,
            inject_network_config=payload.inject_network_config
            if "inject_network_config" in payload.model_fields_set
            else False,
            # The required_windows check above rejects every None value.
            edition=cast(str, payload.edition),
            build=cast(str, payload.build),
            revision=cast(int, payload.revision),
            source=cast(Source, payload.source),
            generalization=cast(str, payload.generalization),
            shutdown_state=cast(str, payload.shutdown_state),
            virtual_size_bytes=cast(int, payload.virtual_size_bytes),
            firmware=cast(dict[str, object], payload.firmware),
            guest_access=cast(dict[str, object], payload.guest_access),
            windows_update=cast(dict[str, object], payload.windows_update),
            sha256=cast(str, payload.sha256),
            first_boot=payload.first_boot,
            detected_at=datetime.now(UTC).isoformat(timespec="seconds"),
            user_edited_fields=edited_fields,
            inspect_error=previous.inspect_error if previous else None,
        )
    else:
        meta = ImageMetadata(
            schema_version=SIDECAR_SCHEMA_VERSION,
            distro=payload.distro,
            version=payload.version,
            family=payload.family,
            default_username=payload.default_username,
            os_variant=payload.os_variant,
            machine_type=payload.machine_type,
            kernel_release=previous.kernel_release if previous else None,
            arch=previous.arch if previous else None,
            cloud_init_profile=payload.cloud_init_profile,
            inject_network_config=payload.inject_network_config,
            source="user",
            detected_at=datetime.now(UTC).isoformat(timespec="seconds"),
            user_edited_fields=edited_fields,
            inspect_error=previous.inspect_error if previous else None,
        )
    if previous is not None:
        for field_name in payload_dict:
            if (
                getattr(previous, field_name, None) != getattr(meta, field_name, None)
                and field_name not in edited_fields
            ):
                edited_fields.append(field_name)
    save_sidecar(target, meta)
    invalidate_catalog_caches()
    return _metadata_response(target, meta)


@router.post("/admin/images/{name}/inspect", status_code=202)
def admin_image_inspect_post(name: str):
    target = _resolve_image(name)
    inspect_image_bg(target, force=True)
    invalidate_catalog_caches()
    return {"status": "running", "filename": target.name}


@router.get("/admin/images/{name}/inspection")
def admin_image_inspect_status(name: str):
    safe = Path(name).name
    with _INSPECTIONS_LOCK:
        entry = IMAGE_INSPECTIONS.get(safe)
        if entry is None:
            return {"status": "idle"}
        snapshot = {
            "status": entry.get("status"),
            "error": entry.get("error"),
            "meta": entry.get("meta"),
        }
    out = {"status": snapshot["status"], "error": snapshot["error"]}
    if snapshot["meta"] is not None:
        out["meta"] = _metadata_response(settings.images_dir / safe, snapshot["meta"])
    return out


# ── Dump Vault ─────────────────────────────────────────────────────────────────
