from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import subprocess
from pathlib import Path


def _isolated_publish_script(tmp_path: Path) -> Path:
    # Run the unchanged helper from a temporary installation with explicit
    # temporary storage, keeping artifacts and helper-local outputs isolated
    # from the developer checkout.
    source = Path(__file__).resolve().parents[3] / "ops" / "host" / "publish_disk_to_host.sh"
    script = tmp_path / "installation" / "ops" / "host" / source.name
    script.parent.mkdir(parents=True)
    shutil.copy2(source, script)
    return script


def test_publish_disk_to_host_stores_artifacts_under_job_id(tmp_path):
    script = _isolated_publish_script(tmp_path)
    source = tmp_path / "disk.img.gz"
    raw = b"disk-data"
    source.write_bytes(gzip.compress(raw))
    storage_root = tmp_path / "storage"
    outputs = []

    for job_id in ("pytest-disk-collision-a", "pytest-disk-collision-b"):
        result = subprocess.run(
            ["bash", str(script), job_id, str(source), "ubuntu", "22.04", "5.15.0-test"],
            cwd=script.parents[2],
            check=True,
            text=True,
            capture_output=True,
            env={**os.environ, "ASTINUSLAB_STORAGE_DIR": str(storage_root)},
        )
        final_path = storage_root / "dumps" / job_id / "ubuntu-22.04-kernel-5.15.0-test-disk.img.gz"
        assert f"disk_final_path={final_path}" in result.stdout
        assert f"disk_raw_sha256={hashlib.sha256(raw).hexdigest()}" in result.stdout
        outputs.append(final_path)

    assert outputs[0] != outputs[1]
    # Both must still exist after the second publication, with intact payloads.
    assert all(gzip.decompress(output.read_bytes()) == raw for output in outputs)


def test_publish_disk_honors_storage_root_env(tmp_path):
    script = _isolated_publish_script(tmp_path)
    source = tmp_path / "disk.img.gz"
    source.write_bytes(gzip.compress(b"disk-data"))
    alt_root = tmp_path / "alt-storage"

    result = subprocess.run(
        ["bash", str(script), "pytest-disk-altroot", str(source), "ubuntu", "22.04", "5.15.0-test"],
        cwd=script.parents[2],
        check=True,
        text=True,
        capture_output=True,
        env={**os.environ, "ASTINUSLAB_STORAGE_DIR": str(alt_root)},
    )

    expected = alt_root / "dumps" / "pytest-disk-altroot" / "ubuntu-22.04-kernel-5.15.0-test-disk.img.gz"
    assert f"disk_final_path={expected}" in result.stdout
    assert gzip.decompress(expected.read_bytes()) == b"disk-data"


def test_publish_disk_rejects_corrupt_gzip(tmp_path):
    script = _isolated_publish_script(tmp_path)
    source = tmp_path / "disk.img.gz"
    source.write_bytes(b"not-gzip-at-all")
    job_id = "pytest-disk-corrupt"
    storage_root = tmp_path / "storage"
    final_path = storage_root / "dumps" / job_id / "ubuntu-22.04-kernel-5.15.0-test-disk.img.gz"

    proc = subprocess.run(
        ["bash", str(script), job_id, str(source), "ubuntu", "22.04", "5.15.0-test"],
        cwd=script.parents[2],
        check=False,
        text=True,
        capture_output=True,
        env={**os.environ, "ASTINUSLAB_STORAGE_DIR": str(storage_root)},
    )

    assert proc.returncode != 0
    assert not final_path.exists()


def test_acquire_disk_host_preserves_qemu_img_stderr():
    script = Path(__file__).resolve().parents[3] / "ops" / "host" / "acquire_disk_host.sh"
    text = script.read_text(encoding="utf-8")
    assert "2>/dev/null | pigz" not in text
    assert "2>/dev/null | gzip" not in text
    assert '2>>"$QEMU_IMG_LOG"' in text
    assert "qemu_img_log=" in text
