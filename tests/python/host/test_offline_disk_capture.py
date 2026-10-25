"""Actual QEMU/compressors on disposable patterned images; no libvirt or guests."""

import gzip
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.skipif(shutil.which("qemu-img") is None, reason="qemu-img required")


def fixture_release(tmp_path, compressor="gzip"):
    release = tmp_path / "release"
    scripts = release / "ops/host"
    scripts.mkdir(parents=True)
    for name in ("acquire_disk_host.sh", "publish_disk_to_host.sh"):
        shutil.copy(ROOT / "ops/host" / name, scripts / name)
    # No writable storage can exist under this release, even when tests run as root.
    (release / "storage").write_text("immutable-release-storage-guard")
    storage = tmp_path / "configured-storage"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in (
        "bash",
        "dirname",
        "mkdir",
        "mktemp",
        "rm",
        "stat",
        "df",
        "python3",
        "qemu-img",
        "gzip",
        "cp",
        "mv",
        "sha256sum",
        "cut",
        "tee",
        "tr",
        "realpath",
    ):
        executable = shutil.which(name)
        assert executable, name
        (bin_dir / name).symlink_to(executable)
    if compressor == "pigz":
        executable = shutil.which("pigz")
        if not executable:
            pytest.skip("pigz required for actual parallel-compressor check")
        (bin_dir / "pigz").symlink_to(executable)
    env = {**os.environ, "PATH": str(bin_dir), "ASTINUSLAB_STORAGE_DIR": str(storage)}
    return release, storage, bin_dir, env


def patterned_image(tmp_path):
    raw = tmp_path / "pattern.raw"
    # Mixed zero/nonzero blocks plus a nonzero last sector detect holes/truncation.
    raw.write_bytes(bytes(range(256)) * 4096 + bytes(2**20) + b"last-sector-pattern".ljust(512, b"X"))
    image = tmp_path / "pattern.qcow2"
    subprocess.run(["qemu-img", "convert", "-f", "raw", "-O", "qcow2", str(raw), str(image)], check=True)
    expected = tmp_path / "independent.raw"
    subprocess.run(["qemu-img", "convert", "-f", "qcow2", "-O", "raw", str(image), str(expected)], check=True)
    assert expected.read_bytes() == raw.read_bytes()
    return image, expected


def capture(release, image, env):
    return subprocess.run(
        [
            "/bin/bash",
            str(release / "ops/host/acquire_disk_host.sh"),
            "disposable-job",
            "target",
            "fixture",
            "1",
            "kernel-fixture",
            str(image),
        ],
        env=env,
        text=True,
        capture_output=True,
    )


@pytest.mark.parametrize("compressor", ["gzip", "pigz"])
def test_patterned_qcow2_actual_helper_hash_size_storage_and_cleanup(tmp_path, compressor):
    release, storage, _, env = fixture_release(tmp_path, compressor)
    image, expected = patterned_image(tmp_path)
    result = capture(release, image, env)
    assert result.returncode == 0, result.stdout + result.stderr
    output = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    artifact = Path(output["disk_final_path"])
    assert artifact.is_relative_to(storage / "dumps/disposable-job")
    assert output["compressor"] == compressor
    actual = gzip.decompress(artifact.read_bytes())
    assert len(actual) == expected.stat().st_size
    assert hashlib.sha256(actual).hexdigest() == hashlib.sha256(expected.read_bytes()).hexdigest()
    assert output["disk_raw_sha256"] == hashlib.sha256(actual).hexdigest()
    assert not list((storage / "tmp").rglob("*.raw"))
    assert not list((storage / "tmp").rglob("*.partial"))
    assert not list((storage / "tmp").rglob("offline-disk.*"))
    assert Path(output["qemu_img_log"]).is_relative_to(storage / "logs")


@pytest.mark.parametrize("failure", ["conversion", "compression", "capacity", "publication"])
def test_failed_capture_keeps_diagnostics_without_scratch_or_publication(tmp_path, failure):
    release, storage, bin_dir, env = fixture_release(tmp_path)
    image, _ = patterned_image(tmp_path)
    if failure == "conversion":
        (bin_dir / "qemu-img").unlink()
        executable = shutil.which("qemu-img")
        (bin_dir / "qemu-img").write_text(
            f'#!/bin/bash\nif [[ "$1" == info ]]; then exec {executable} "$@"; fi\necho injected-conversion-failure >&2\necho partial > "${{@: -1}}"\nexit 27\n'
        )
        (bin_dir / "qemu-img").chmod(0o755)
    elif failure == "compression":
        (bin_dir / "gzip").unlink()
        (bin_dir / "gzip").write_text("#!/bin/bash\necho injected-compression-failure >&2\necho partial\nexit 28\n")
        (bin_dir / "gzip").chmod(0o755)
    elif failure == "capacity":
        (bin_dir / "df").unlink()
        (bin_dir / "df").write_text(
            '#!/bin/bash\nprintf "Filesystem 1-blocks Used Available Capacity Mounted on\\nfixture 100 99 1 99%% /\\n"\n'
        )
        (bin_dir / "df").chmod(0o755)
    else:
        # Real publisher fails after creating a partial copy; no final artifact.
        (bin_dir / "cp").unlink()
        (bin_dir / "cp").write_text(
            '#!/bin/bash\necho partial > "${@: -1}"\necho injected-publication-failure >&2\nexit 29\n'
        )
        (bin_dir / "cp").chmod(0o755)
    result = capture(release, image, env)
    assert result.returncode != 0
    logs = "\n".join(path.read_text() for path in (storage / "logs").glob("*.log"))
    expected = "Insufficient" if failure == "capacity" else f"injected-{failure}-failure"
    assert expected in logs + result.stdout + result.stderr
    assert "qemu_img_log=" in result.stdout
    assert not list((storage / "tmp").rglob("*.raw"))
    assert not list((storage / "tmp").rglob("*.partial"))
    assert not list((storage / "dumps").rglob("*.gz"))
