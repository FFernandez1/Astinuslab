from __future__ import annotations

import gzip
import json
import stat
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException

from tests.python._support.helpers import autospec_setattr


def test_two_same_name_uploads_publish_only_one_private_attempt(tmp_path):
    from astinuslab.images.image_admin_support import ImageStagingOperation

    images_dir = tmp_path / "images"
    operations = [ImageStagingOperation.create(images_dir) for _ in range(2)]
    assert all(stat.S_IMODE(operation.operation_dir.stat().st_mode) == 0o700 for operation in operations)
    payloads = [b"first-image", b"second-image"]
    barrier = threading.Barrier(2)

    def publish(operation, payload):
        with operation.payload_path.open("xb") as staged:
            staged.write(payload)
        barrier.wait(timeout=5)
        try:
            operation.publish(operation.payload_path, images_dir / "same.qcow2")
            return 303
        except HTTPException as exc:
            return exc.status_code
        finally:
            operation.cleanup()

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(publish, operations, payloads))

    assert sorted(statuses) == [303, 409]
    assert (images_dir / "same.qcow2").read_bytes() in set(payloads)
    staging = images_dir / ".staging"
    assert staging.is_dir()
    assert stat.S_IMODE(staging.stat().st_mode) == 0o700
    assert list(staging.iterdir()) == []


def test_duplicate_upload_preserves_existing_bytes_and_metadata(client, tmp_images_dir, monkeypatch):
    import astinuslab.images.image_inspection as image_inspection

    image = tmp_images_dir / "same.qcow2"
    sidecar = tmp_images_dir / "same.qcow2.meta.json"
    image.write_bytes(b"original-image")
    sidecar.write_text(json.dumps({"binding": "original-image"}), encoding="utf-8")
    autospec_setattr(monkeypatch, image_inspection, "inspect_image_bg", lambda path, force=True: None)

    response = client.post(
        "/images/upload",
        files={"file": ("same.qcow2", b"replacement-image", "application/octet-stream")},
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert image.read_bytes() == b"original-image"
    assert json.loads(sidecar.read_text(encoding="utf-8")) == {"binding": "original-image"}


def test_publish_failure_cleans_only_its_operation_directory(tmp_path, monkeypatch):
    from astinuslab.images import image_admin_support

    images_dir = tmp_path / "images"
    first = image_admin_support.ImageStagingOperation.create(images_dir)
    second = image_admin_support.ImageStagingOperation.create(images_dir)
    first.payload_path.write_bytes(b"first")
    second.payload_path.write_bytes(b"second")
    monkeypatch.setattr(
        image_admin_support.os,
        "link",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("publish failed")),
    )

    try:
        with pytest.raises(OSError, match="publish failed"):
            first.publish(first.payload_path, images_dir / "image.qcow2")
    finally:
        first.cleanup()

    assert not first.operation_dir.exists()
    assert second.payload_path.read_bytes() == b"second"
    second.cleanup()


def test_staging_operation_rejects_non_uuid_identifier_outside_staging(tmp_path):
    from astinuslab.images.image_admin_support import ImageStagingOperation

    images_dir = tmp_path / "images"

    with pytest.raises(HTTPException, match="operation ID"):
        ImageStagingOperation.create(images_dir, "../outside-operation")

    assert not (images_dir / "outside-operation").exists()


def test_staging_operation_rejects_symlinked_staging_root(tmp_path):
    from astinuslab.images.image_admin_support import ImageStagingOperation

    images_dir = tmp_path / "images"
    outside = tmp_path / "outside"
    images_dir.mkdir()
    outside.mkdir()
    (images_dir / ".staging").symlink_to(outside, target_is_directory=True)

    with pytest.raises(HTTPException, match="staging"):
        ImageStagingOperation.create(images_dir)

    assert list(outside.iterdir()) == []


def test_cleanup_does_not_remove_replacement_operation_directory(tmp_path):
    from astinuslab.images.image_admin_support import ImageStagingOperation

    images_dir = tmp_path / "images"
    operation = ImageStagingOperation.create(images_dir)
    moved = operation.operation_dir.with_name(f"{operation.operation_id}-moved")
    operation.operation_dir.rename(moved)
    operation.operation_dir.mkdir()
    replacement = operation.operation_dir / "other-attempt"
    replacement.write_bytes(b"keep")

    operation.cleanup()

    assert replacement.read_bytes() == b"keep"


def test_cleanup_pins_owned_directory_before_path_replacement(tmp_path, monkeypatch):
    from astinuslab.images import image_admin_support

    images_dir = tmp_path / "images"
    operation = image_admin_support.ImageStagingOperation.create(images_dir)
    operation.payload_path.write_bytes(b"owned")
    moved = operation.operation_dir.with_name(f"{operation.operation_id}-moved")
    real_clear = image_admin_support._clear_directory_fd

    def replace_then_clear(directory_fd):
        operation.operation_dir.rename(moved)
        operation.operation_dir.mkdir()
        (operation.operation_dir / "other-attempt").write_bytes(b"keep")
        real_clear(directory_fd)

    monkeypatch.setattr(image_admin_support, "_clear_directory_fd", replace_then_clear)

    operation.cleanup()

    assert (operation.operation_dir / "other-attempt").read_bytes() == b"keep"
    assert list(moved.iterdir()) == []


def test_publish_rejects_source_swapped_after_validation(tmp_path, monkeypatch):
    from astinuslab.images import image_admin_support

    images_dir = tmp_path / "images"
    outside = tmp_path / "outside.qcow2"
    outside.write_bytes(b"outside")
    operation = image_admin_support.ImageStagingOperation.create(images_dir)
    operation.payload_path.write_bytes(b"staged")
    real_link = image_admin_support.os.link

    def swap_then_link(src, dst, **kwargs):
        operation.payload_path.unlink()
        operation.payload_path.symlink_to(outside)
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(image_admin_support.os, "link", swap_then_link)

    with pytest.raises(HTTPException, match="changed during publication"):
        operation.publish(operation.payload_path, images_dir / "image.qcow2")

    assert not (images_dir / "image.qcow2").exists()
    operation.cleanup()


def test_url_download_state_is_recorded_under_full_operation_uuid(client, monkeypatch):
    import astinuslab.ui.image_routes as image_routes

    started: list[tuple] = []
    monkeypatch.setattr(image_routes, "_validate_remote_target", lambda *_args: None)
    monkeypatch.setattr(
        image_routes.threading,
        "Thread",
        lambda *, target, args, daemon: SimpleNamespace(start=lambda: started.append(args)),
    )
    before = set(image_routes._URL_DOWNLOADS)

    response = client.post("/images/fetch-url", data={"url": "https://images.example/new.qcow2"})

    operation_ids = set(image_routes._URL_DOWNLOADS) - before
    assert response.status_code == 200
    assert len(operation_ids) == 1
    operation_id = operation_ids.pop()
    assert UUID(operation_id).hex == operation_id
    assert started[0][0] == operation_id


def test_uploaded_image_output_name_converts_raw_and_gzip_to_qcow2():
    from astinuslab.images.image_admin_support import uploaded_image_output_name

    assert uploaded_image_output_name("openwrt.img") == "openwrt.qcow2"
    assert uploaded_image_output_name("openwrt.img.gz") == "openwrt.qcow2"
    assert uploaded_image_output_name("ubuntu.qcow2") == "ubuntu.qcow2"


def test_storage_page_upload_hint_mentions_img_gz_conversion(client, tmp_images_dir):
    response = client.get("/images")

    assert response.status_code == 200
    assert ".img.gz" in response.text
    assert "auto-convert to .qcow2" in response.text
    assert 'accept=".qcow2,.img,.img.gz"' in response.text


def test_upload_over_limit_returns_413_and_cleans_temp_file(client, tmp_images_dir, monkeypatch):
    import astinuslab.images.image_inspection as image_inspection
    import astinuslab.ui.image_routes as image_routes

    inspected: list[Path] = []
    autospec_setattr(monkeypatch, image_routes, "_load_image_download_config", lambda: SimpleNamespace(max_size_gb=0))
    autospec_setattr(
        monkeypatch, image_inspection, "inspect_image_bg", lambda path, force=True: inspected.append(Path(path))
    )

    response = client.post(
        "/images/upload",
        files={"file": ("too-large.qcow2", b"x", "application/octet-stream")},
        follow_redirects=False,
    )

    assert response.status_code == 413
    assert not (tmp_images_dir / "too-large.qcow2.uploading").exists()
    assert not (tmp_images_dir / "too-large.qcow2").exists()
    assert inspected == []


def test_upload_img_gz_converts_to_qcow2(client, tmp_images_dir, monkeypatch):
    import astinuslab.images.image_admin_support as image_admin_support
    import astinuslab.images.image_inspection as image_inspection

    calls: list[list[str]] = []
    timeouts: list[int | None] = []
    inspected: list[Path] = []

    def fake_run(cmd, check=True, timeout=None):
        assert check is True
        calls.append([str(part) for part in cmd])
        timeouts.append(timeout)
        src = Path(cmd[-2])
        dest = Path(cmd[-1])
        assert src.read_bytes() == b"raw-openwrt-image"
        dest.write_bytes(b"qcow2-openwrt-image")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(image_admin_support.subprocess, "run", fake_run)
    autospec_setattr(
        monkeypatch, image_inspection, "inspect_image_bg", lambda path, force=True: inspected.append(Path(path))
    )

    payload = gzip.compress(b"raw-openwrt-image")
    response = client.post(
        "/images/upload",
        files={"file": ("openwrt.img.gz", payload, "application/gzip")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert (tmp_images_dir / "openwrt.qcow2").read_bytes() == b"qcow2-openwrt-image"
    assert not (tmp_images_dir / "openwrt.img.gz").exists()
    assert calls[0][:6] == ["qemu-img", "convert", "-f", "raw", "-O", "qcow2"]
    assert Path(calls[0][-2]).name == "source.raw"
    assert Path(calls[0][-1]).name == "converted.qcow2"
    assert Path(calls[0][-2]).parent == Path(calls[0][-1]).parent
    assert Path(calls[0][-2]).parent.parent == tmp_images_dir / ".staging"
    assert timeouts == [image_admin_support.QEMU_IMG_CONVERSION_TIMEOUT_SECONDS]
    assert inspected == [tmp_images_dir / "openwrt.qcow2"]


def test_upload_img_converts_to_qcow2(client, tmp_images_dir, monkeypatch):
    import astinuslab.images.image_admin_support as image_admin_support
    import astinuslab.images.image_inspection as image_inspection

    calls: list[list[str]] = []

    def fake_run(cmd, check=True, timeout=None):
        assert check is True
        assert timeout == image_admin_support.QEMU_IMG_CONVERSION_TIMEOUT_SECONDS
        calls.append([str(part) for part in cmd])
        Path(cmd[-1]).write_bytes(b"qcow2-router-image")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(image_admin_support.subprocess, "run", fake_run)
    autospec_setattr(monkeypatch, image_inspection, "inspect_image_bg", lambda path, force=True: None)

    response = client.post(
        "/images/upload",
        files={"file": ("router.img", b"raw-router-image", "application/octet-stream")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert (tmp_images_dir / "router.qcow2").read_bytes() == b"qcow2-router-image"
    assert not (tmp_images_dir / "router.img").exists()
    assert calls[0][:6] == ["qemu-img", "convert", "-f", "raw", "-O", "qcow2"]
    assert Path(calls[0][-2]).name == "payload.part"
    assert Path(calls[0][-1]).name == "converted.qcow2"
    assert Path(calls[0][-2]).parent == Path(calls[0][-1]).parent
    assert Path(calls[0][-2]).parent.parent == tmp_images_dir / ".staging"


def test_upload_img_conversion_failure_keeps_existing_qcow2(client, tmp_images_dir, monkeypatch):
    import astinuslab.images.image_admin_support as image_admin_support
    import astinuslab.images.image_inspection as image_inspection

    existing = tmp_images_dir / "router.qcow2"
    existing.write_bytes(b"existing-qcow2")

    def fail_run(cmd, check=True, timeout=None):
        assert check is True
        assert timeout == image_admin_support.QEMU_IMG_CONVERSION_TIMEOUT_SECONDS
        Path(cmd[-1]).write_bytes(b"partial-qcow2")
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(image_admin_support.subprocess, "run", fail_run)
    autospec_setattr(monkeypatch, image_inspection, "inspect_image_bg", lambda path, force=True: None)

    response = client.post(
        "/images/upload",
        files={"file": ("router.img", b"raw-router-image", "application/octet-stream")},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert existing.read_bytes() == b"existing-qcow2"
    assert not (tmp_images_dir / "router.img.uploading").exists()
    assert not (tmp_images_dir / "router.qcow2.converting").exists()


def test_upload_img_conversion_timeout_cleans_temp_files(client, tmp_images_dir, monkeypatch):
    import astinuslab.images.image_admin_support as image_admin_support
    import astinuslab.images.image_inspection as image_inspection

    existing = tmp_images_dir / "router.qcow2"
    existing.write_bytes(b"existing-qcow2")

    def timeout_run(cmd, check=True, timeout=None):
        assert check is True
        assert timeout == image_admin_support.QEMU_IMG_CONVERSION_TIMEOUT_SECONDS
        Path(cmd[-1]).write_bytes(b"partial-qcow2")
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(image_admin_support.subprocess, "run", timeout_run)
    autospec_setattr(monkeypatch, image_inspection, "inspect_image_bg", lambda path, force=True: None)

    response = client.post(
        "/images/upload",
        files={"file": ("router.img", b"raw-router-image", "application/octet-stream")},
        follow_redirects=False,
    )

    assert response.status_code == 504
    assert "conversion timed out" in response.text
    assert existing.read_bytes() == b"existing-qcow2"
    assert not (tmp_images_dir / "router.img.uploading").exists()
    assert not (tmp_images_dir / "router.qcow2.converting").exists()


def test_upload_bad_img_gz_cleans_temp_files(client, tmp_images_dir, monkeypatch):
    import astinuslab.images.image_admin_support as image_admin_support
    import astinuslab.images.image_inspection as image_inspection

    autospec_setattr(monkeypatch, image_admin_support.subprocess, "run", lambda *args, **kwargs: None)
    autospec_setattr(monkeypatch, image_inspection, "inspect_image_bg", lambda path, force=True: None)

    response = client.post(
        "/images/upload",
        files={"file": ("broken.img.gz", b"not-gzip", "application/gzip")},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert not (tmp_images_dir / "broken.img.gz.uploading").exists()
    assert not (tmp_images_dir / "broken.img.raw").exists()
    assert not (tmp_images_dir / "broken.qcow2").exists()
