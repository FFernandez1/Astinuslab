from __future__ import annotations

import gzip
import logging
import os
import stat
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import HTTPException

from ..api.schemas import ImageDownloadConfig
from ..core.config import settings
from ..db import session_scope
from ..db.repositories import AppSettingsRepository
from .image_inspection import (
    BOOTABLE_IMAGE_EXTENSIONS,  # noqa: F401  (re-exported for ui.py)
    classify_image_preflight,
    ensure_metadata,
)
from .safe_fetch import OFFICIAL_IMAGE_DOMAINS, resolve_and_validate, stream_download

logger = logging.getLogger(__name__)

# In-memory download tracker: {dl_id: {status, url, filename, bytes_done, total, error, started_at}}
URL_DOWNLOADS: dict[str, dict] = {}
URL_DOWNLOADS_LOCK = threading.Lock()
_PUBLISH_LOCKS: dict[str, threading.Lock] = {}
_PUBLISH_LOCKS_GUARD = threading.Lock()
_DESTINATION_RESERVATIONS: dict[str, str] = {}
IMAGE_EXTENSIONS = {".qcow2", ".img", ".iso", ".raw", ".vmdk"}
UPLOAD_IMAGE_EXTENSIONS = BOOTABLE_IMAGE_EXTENSIONS | {".img.gz"}
IMAGE_DOWNLOAD_CONFIG_KEY = "image_download_config"
QEMU_IMG_CONVERSION_TIMEOUT_SECONDS = 30 * 60


def _publish_lock(filename: str) -> threading.Lock:
    with _PUBLISH_LOCKS_GUARD:
        return _PUBLISH_LOCKS.setdefault(filename, threading.Lock())


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns)


def reserve_image_destination(destination: Path, operation_id: str) -> None:
    with _publish_lock(destination.name):
        if destination.exists() or destination.is_symlink() or destination.name in _DESTINATION_RESERVATIONS:
            raise HTTPException(409, f"Image already exists: {destination.name}")
        _DESTINATION_RESERVATIONS[destination.name] = operation_id


def release_image_destination(destination: Path, operation_id: str) -> None:
    with _publish_lock(destination.name):
        if _DESTINATION_RESERVATIONS.get(destination.name) == operation_id:
            del _DESTINATION_RESERVATIONS[destination.name]


def _clear_directory_fd(directory_fd: int) -> None:
    """Recursively empty an open directory using only descriptor-relative calls."""
    with os.scandir(directory_fd) as entries:
        children = [(entry.name, entry.is_dir(follow_symlinks=False)) for entry in entries]
    for name, is_dir in children:
        if is_dir:
            try:
                child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            except OSError:
                continue
            try:
                _clear_directory_fd(child_fd)
            finally:
                os.close(child_fd)
            try:
                os.rmdir(name, dir_fd=directory_fd)
            except OSError:
                pass
        else:
            try:
                os.unlink(name, dir_fd=directory_fd)
            except OSError:
                pass


@dataclass(frozen=True)
class ImageStagingOperation:
    operation_id: str
    operation_dir: Path
    operation_identity: tuple[int, int]

    @classmethod
    def create(cls, images_dir: Path, operation_id: str | None = None) -> ImageStagingOperation:
        images_dir.mkdir(parents=True, exist_ok=True)
        if operation_id is None:
            identifier = uuid4().hex
        else:
            try:
                identifier = UUID(operation_id).hex
            except (AttributeError, ValueError) as exc:
                raise HTTPException(400, "Image staging operation ID must be a UUID") from exc
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            images_fd = os.open(images_dir, flags)
        except OSError as exc:
            raise HTTPException(400, "Image directory must not be a symlink") from exc
        try:
            try:
                os.mkdir(".staging", mode=0o700, dir_fd=images_fd)
            except FileExistsError:
                pass
            try:
                staging_fd = os.open(".staging", flags, dir_fd=images_fd)
            except OSError as exc:
                raise HTTPException(400, "Image staging root must be a private directory") from exc
            try:
                os.fchmod(staging_fd, 0o700)
                os.mkdir(identifier, mode=0o700, dir_fd=staging_fd)
                operation_stat = os.stat(identifier, dir_fd=staging_fd, follow_symlinks=False)
            finally:
                os.close(staging_fd)
        finally:
            os.close(images_fd)
        operation_dir = images_dir / ".staging" / identifier
        return cls(identifier, operation_dir, (operation_stat.st_dev, operation_stat.st_ino))

    @property
    def payload_path(self) -> Path:
        return self.operation_dir / "payload.part"

    def validate_output(self, output: Path) -> os.stat_result:
        if output.parent != self.operation_dir:
            raise HTTPException(400, "Staged image output is missing or empty")
        try:
            output_stat = output.stat(follow_symlinks=False)
        except FileNotFoundError as exc:
            raise HTTPException(400, "Staged image output is missing or empty") from exc
        if not stat.S_ISREG(output_stat.st_mode) or output_stat.st_size <= 0:
            raise HTTPException(400, "Staged image output is missing or empty")
        return output_stat

    def publish(self, output: Path, destination: Path) -> Path:
        output_stat = self.validate_output(output)
        if destination.parent != self.operation_dir.parent.parent:
            raise HTTPException(400, "Image destination is outside the image directory")
        with _publish_lock(destination.name):
            reservation = _DESTINATION_RESERVATIONS.get(destination.name)
            if reservation is not None and reservation != self.operation_id:
                raise HTTPException(409, f"Image already exists: {destination.name}")
            try:
                os.link(output, destination, follow_symlinks=False)
            except FileExistsError as exc:
                raise HTTPException(409, f"Image already exists: {destination.name}") from exc
            published_stat = destination.stat(follow_symlinks=False)
            if _file_identity(published_stat) != _file_identity(output_stat):
                destination.unlink(missing_ok=True)
                raise HTTPException(409, "Staged image changed during publication")
        return destination

    def cleanup(self) -> None:
        # Delete through a descriptor pinned to this operation's directory, never
        # by path: a path-based rmtree can be steered onto a different directory
        # if ours is renamed away between the identity check and the delete, which
        # would destroy another operation's staging data and leave ours behind.
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            directory_fd = os.open(self.operation_dir, flags)
        except OSError:
            return
        try:
            current = os.fstat(directory_fd)
            if (current.st_dev, current.st_ino) != self.operation_identity:
                return
            _clear_directory_fd(directory_fd)
        finally:
            os.close(directory_fd)
        # Only unlink the directory entry while it still resolves to the inode we
        # just emptied; if it was replaced meanwhile, the replacement is not ours.
        try:
            final = self.operation_dir.stat(follow_symlinks=False)
        except OSError:
            return
        if (final.st_dev, final.st_ino) == self.operation_identity:
            try:
                self.operation_dir.rmdir()
            except OSError:
                pass


def default_image_download_config() -> ImageDownloadConfig:
    return ImageDownloadConfig(allowed_domains=list(OFFICIAL_IMAGE_DOMAINS))


def load_image_download_config() -> ImageDownloadConfig:
    with session_scope() as session:
        repo = AppSettingsRepository(session)
        raw = repo.get(IMAGE_DOWNLOAD_CONFIG_KEY)
    if not raw:
        return default_image_download_config()
    try:
        cfg = ImageDownloadConfig.model_validate_json(raw)
        if cfg.source_policy in ("disabled", "open"):
            logger.warning(
                "Unsupported source_policy %r in stored config; falling back to 'official_only'",
                cfg.source_policy,
            )
            cfg = cfg.model_copy(update={"source_policy": "official_only"})
        return cfg
    except Exception:
        return default_image_download_config()


def save_image_download_config(config: ImageDownloadConfig) -> None:
    with session_scope() as session:
        repo = AppSettingsRepository(session)
        repo.set(IMAGE_DOWNLOAD_CONFIG_KEY, config.model_dump_json())


def extract_allowed_domains(text: str) -> list[str]:
    return [line.strip().lower() for line in text.splitlines() if line.strip()]


def image_download_settings_ctx(config: ImageDownloadConfig) -> dict:
    return {
        "download_config": config,
        "download_policies": (
            ("official_only", "Official repositories only"),
            ("allowlist", "Custom allowlist"),
            ("unrestricted", "Unrestricted — any domain"),
        ),
        "network_policies": (
            ("safe", "Safe"),
            ("unrestricted", "Unrestricted"),
        ),
        "allowed_domains_text": "\n".join(config.allowed_domains),
    }


def uploaded_image_suffix(filename: str) -> str:
    name = Path(filename).name.lower()
    if name.endswith(".img.gz"):
        return ".img.gz"
    return Path(name).suffix


def uploaded_image_output_name(filename: str) -> str:
    name = Path(filename).name
    suffix = uploaded_image_suffix(name)
    if suffix not in UPLOAD_IMAGE_EXTENSIONS:
        raise HTTPException(400, f"Extension not allowed: {suffix}")
    if suffix == ".img.gz":
        return name[: -len(".img.gz")] + ".qcow2"
    if suffix == ".img":
        return str(Path(name).with_suffix(".qcow2"))
    return name


def finalize_uploaded_image(
    tmp_path: Path,
    original_filename: str,
    images_dir: Path,
    *,
    max_decompressed_bytes: int | None = None,
) -> Path:
    suffix = uploaded_image_suffix(original_filename)
    if suffix not in UPLOAD_IMAGE_EXTENSIONS:
        raise HTTPException(400, f"Extension not allowed: {suffix}")

    operation_stat = tmp_path.parent.stat(follow_symlinks=False)
    operation = ImageStagingOperation(
        tmp_path.parent.name,
        tmp_path.parent,
        (operation_stat.st_dev, operation_stat.st_ino),
    )
    dest = images_dir / uploaded_image_output_name(original_filename)
    if suffix == ".qcow2":
        return operation.publish(tmp_path, dest)

    raw_tmp: Path | None = None
    converting = operation.operation_dir / "converted.qcow2"
    try:
        if suffix == ".img":
            source = tmp_path
        else:
            raw_tmp = operation.operation_dir / "source.raw"
            bytes_done = 0
            try:
                with gzip.open(tmp_path, "rb") as src, raw_tmp.open("wb") as out:
                    while chunk := src.read(1024 * 1024):
                        bytes_done += len(chunk)
                        if max_decompressed_bytes is not None and bytes_done > max_decompressed_bytes:
                            raise HTTPException(413, "Uploaded image exceeds configured size limit")
                        out.write(chunk)
            except HTTPException:
                raise
            except (gzip.BadGzipFile, EOFError, OSError) as exc:
                raise HTTPException(400, "Uploaded .img.gz could not be decompressed") from exc
            source = raw_tmp

        subprocess.run(
            ["qemu-img", "convert", "-f", "raw", "-O", "qcow2", str(source), str(converting)],
            check=True,
            timeout=QEMU_IMG_CONVERSION_TIMEOUT_SECONDS,
        )
        return operation.publish(converting, dest)
    except FileNotFoundError as exc:
        raise HTTPException(500, "qemu-img is required to convert uploaded raw images") from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "Uploaded image conversion timed out") from exc
    except subprocess.CalledProcessError as exc:
        raise HTTPException(400, "Uploaded image could not be converted to qcow2") from exc
    finally:
        tmp_path.unlink(missing_ok=True)
        converting.unlink(missing_ok=True)
        if raw_tmp is not None:
            raw_tmp.unlink(missing_ok=True)


def validate_remote_target(url: str, config: ImageDownloadConfig) -> None:
    resolve_and_validate(url, config)


def list_images() -> list[dict]:
    images_dir = settings.images_dir
    if not images_dir.exists():
        return []
    result = []
    for path in sorted(images_dir.iterdir()):
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        st = path.stat()
        meta = ensure_metadata(path)
        preflight = classify_image_preflight(path, meta)
        inspect_error = meta.inspect_error if meta.source != "filename" else None
        result.append(
            {
                "filename": path.name,
                "path": str(path),
                "size_bytes": st.st_size,
                "size_mb": st.st_size / (1024 * 1024),
                "modified": datetime.fromtimestamp(st.st_mtime),
                "distro": meta.distro,
                "version": meta.version,
                "family": meta.family,
                "platform": getattr(meta, "platform", None),
                "default_username": meta.default_username,
                "arch": meta.arch,
                "os_variant": meta.os_variant,
                "source": meta.source,
                "user_edited_fields": list(meta.user_edited_fields),
                "inspect_error": inspect_error,
                "bootable": preflight["bootable"],
                "isf_supported": preflight["isf_supported"],
                "needs_user_action": preflight["needs_user_action"],
                "reusable": preflight["reusable"],
                "preflight_reasons": preflight["reasons"],
            }
        )
    return result


def download_image_bg(dl_id: str, url: str, dest: Path, config: ImageDownloadConfig) -> None:
    with URL_DOWNLOADS_LOCK:
        state = URL_DOWNLOADS[dl_id]
    operation: ImageStagingOperation | None = None
    try:
        operation = ImageStagingOperation.create(dest.parent, dl_id)

        def update_progress(bytes_done: int, total: int | None) -> None:
            with URL_DOWNLOADS_LOCK:
                state["bytes_done"] = bytes_done
                state["total"] = total

        stream_download(url, operation.payload_path, config, update_progress)
        operation.publish(operation.payload_path, dest)
        with URL_DOWNLOADS_LOCK:
            state["status"] = "done"
            state["filename"] = dest.name
        from ..catalog import invalidate_catalog_caches
        from .image_inspection import inspect_image_bg

        invalidate_catalog_caches()
        inspect_image_bg(dest, force=True)
    except Exception as exc:
        with URL_DOWNLOADS_LOCK:
            state["status"] = "error"
            state["error"] = str(exc)
    finally:
        if operation is not None:
            operation.cleanup()
        release_image_destination(dest, dl_id)
