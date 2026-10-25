"""Smoke tests for the libvirt_storage_admin.sh argument contract."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
HELPER = ROOT / "ops" / "host" / "libvirt_storage_admin.sh"


def _run(args: list[str], **env_overrides) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(HELPER), *args],
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.mark.skipif(shutil.which("realpath") is None, reason="realpath required")
class TestInstallBaseImageSourcePrefix:
    def test_rejects_source_outside_allowed_prefix(self, tmp_path):
        # allowed_prefix must be inside BASE_DIR (two levels up from the script)
        # to pass the new installation-directory safety check.  Use the actual
        # ops/host directory; then place the source outside it.
        allowed = ROOT / "ops" / "host"
        outside = tmp_path / "outside.qcow2"
        outside.write_bytes(b"x")

        result = _run(
            [
                "install_base_image",
                str(allowed),
                str(outside),
                "/var/lib/libvirt/images/astinuslab/base/x.qcow2",
            ]
        )

        assert result.returncode != 0
        assert "outside allowed source prefix" in (result.stderr + result.stdout)

    def test_accepts_source_inside_allowed_prefix(self, tmp_path):
        # allowed_prefix must be inside BASE_DIR for the new installation-directory
        # safety check to pass.  Use ops/host (the script's own directory) as
        # the allowed prefix and the helper script itself as the source, so the
        # source-prefix check succeeds.
        # Destination is intentionally outside the hard-coded ALLOWED_PREFIX so
        # the script aborts on the destination check, after passing source check.
        allowed = ROOT / "ops" / "host"
        src = HELPER

        result = _run(
            [
                "install_base_image",
                str(allowed),
                str(src),
                str(tmp_path / "dest.qcow2"),
            ]
        )

        output = result.stderr + result.stdout
        assert result.returncode != 0
        assert "outside allowed source prefix" not in output
        assert "outside allowed prefix" in output


class TestResetDefaultNetwork:
    def test_rejects_invalid_libvirt_network_name(self):
        result = _run(["reset_default_network"], LIBVIRT_NETWORK="../default")

        assert result.returncode != 0
        assert "DENY: invalid libvirt network name" in (result.stderr + result.stdout)


class TestReadSerialLog:
    def test_unused_tmp_reader_is_not_supported(self):
        result = _run(["read_serial_log", "job-ok_123"])

        assert result.returncode != 0
        assert "Unsupported subcommand: read_serial_log" in (result.stderr + result.stdout)


class TestFlattenImageDestination:
    def test_rejects_traversal_destination_outside_allowed_roots(self):
        src = "/var/lib/libvirt/images/astinuslab/job-123/disk.qcow2"
        dst = ROOT / "images" / "definitions" / ".." / ".." / "pwned.qcow2"

        result = _run(["flatten_image", src, str(dst)])

        assert result.returncode != 0
        assert "outside allowed prefixes" in (result.stderr + result.stdout)


PYTHON_HELPER = ROOT / "ops" / "host" / "libvirt_storage_admin.py"


def _render_python_helper(tmp_path: Path, install_root: Path, libvirt_root: Path) -> Path:
    install_info = install_root.stat()
    replacements = {
        "@@INSTALL_DIR@@": str(install_root),
        "@@INSTALL_DEV@@": str(install_info.st_dev),
        "@@INSTALL_INO@@": str(install_info.st_ino),
        "@@LIBVIRT_IMAGE_ROOT@@": str(libvirt_root),
    }
    rendered = PYTHON_HELPER.read_text(encoding="utf-8")
    for token, value in replacements.items():
        rendered = rendered.replace(token, value)
    destination = tmp_path / "libvirt_storage_admin.py"
    destination.write_text(rendered, encoding="utf-8")
    destination.chmod(0o755)
    return destination


def _run_python(helper: Path, arguments: list[str]) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop("SUDO_USER", None)
    return subprocess.run([str(helper), *arguments], capture_output=True, text=True, env=env)


def test_install_base_image_verifies_content_and_never_replaces_conflict(tmp_path):
    install_root = tmp_path / "install"
    source_root = install_root / "images" / "definitions"
    source_root.mkdir(parents=True)
    source = source_root / "base.qcow2"
    source.write_bytes(b"trusted-base")
    libvirt_root = tmp_path / "libvirt"
    libvirt_root.mkdir()
    destination = libvirt_root / "base" / "base.qcow2"
    helper = _render_python_helper(tmp_path, install_root, libvirt_root)

    first = _run_python(
        helper,
        ["install_base_image", str(source_root), str(source), str(destination)],
    )
    second = _run_python(
        helper,
        ["install_base_image", str(source_root), str(source), str(destination)],
    )
    source.write_bytes(b"hostile-base")
    conflict = _run_python(
        helper,
        ["install_base_image", str(source_root), str(source), str(destination)],
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert conflict.returncode != 0
    assert "conflicts" in conflict.stderr
    assert destination.read_bytes() == b"trusted-base"


def test_finalize_job_artifacts_rejects_symlink_without_chmodding_target(tmp_path):
    install_root = tmp_path / "install"
    install_root.mkdir()
    libvirt_root = tmp_path / "libvirt"
    job_dir = libvirt_root / "job"
    job_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_bytes(b"secret")
    outside.chmod(0o600)
    (job_dir / "disk.qcow2").symlink_to(outside)
    seed = job_dir / "seed.iso"
    seed.write_bytes(b"seed")
    helper = _render_python_helper(tmp_path, install_root, libvirt_root)

    result = _run_python(
        helper,
        ["finalize_job_artifacts", str(job_dir / "disk.qcow2"), str(seed)],
    )

    assert result.returncode != 0
    assert "without following symlinks" in result.stderr
    assert outside.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(shutil.which("qemu-img") is None, reason="qemu-img required")
def test_create_job_overlay_writes_through_bound_descriptor(tmp_path):
    install_root = tmp_path / "install"
    install_root.mkdir()
    libvirt_root = tmp_path / "libvirt"
    base_dir = libvirt_root / "base"
    job_dir = libvirt_root / "job"
    base_dir.mkdir(parents=True)
    job_dir.mkdir()
    backing = base_dir / "base.qcow2"
    overlay = job_dir / "disk.qcow2"
    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", str(backing), "1M"],
        check=True,
        capture_output=True,
        text=True,
    )
    helper = _render_python_helper(tmp_path, install_root, libvirt_root)

    result = _run_python(
        helper,
        ["create_job_overlay", str(backing), str(overlay), "1"],
    )

    assert result.returncode == 0, result.stderr
    assert overlay.is_file()
    info = subprocess.run(
        ["qemu-img", "info", "--output=json", "--backing-chain", str(overlay)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert str(backing) in info.stdout


@pytest.mark.skipif(shutil.which("qemu-img") is None, reason="qemu-img required")
def test_flatten_image_forces_qcow2_input_instead_of_probing(tmp_path):
    install_root = tmp_path / "install"
    install_root.mkdir()
    libvirt_root = tmp_path / "libvirt"
    job_dir = libvirt_root / "job"
    job_dir.mkdir(parents=True)
    qcow2_source = job_dir / "disk.qcow2"
    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", str(qcow2_source), "1M"],
        check=True,
        capture_output=True,
        text=True,
    )
    raw_source = job_dir / "raw.qcow2"
    raw_source.write_bytes(b"\x00" * 65536)
    helper = _render_python_helper(tmp_path, install_root, libvirt_root)

    flattened = _run_python(helper, ["flatten_image", str(qcow2_source), str(job_dir / "flat.qcow2")])
    probed = _run_python(helper, ["flatten_image", str(raw_source), str(job_dir / "probed.qcow2")])

    assert flattened.returncode == 0, flattened.stderr
    assert probed.returncode != 0


def test_remove_missing_job_directory_is_idempotent(tmp_path):
    install_root = tmp_path / "install"
    install_root.mkdir()
    libvirt_root = tmp_path / "libvirt"
    libvirt_root.mkdir()
    helper = _render_python_helper(tmp_path, install_root, libvirt_root)

    result = _run_python(helper, ["remove_job_dir", str(libvirt_root / "missing-job")])

    assert result.returncode == 0, result.stderr


def test_flatten_rejects_relative_destination_without_traceback(tmp_path):
    install_root = tmp_path / "install"
    install_root.mkdir()
    libvirt_root = tmp_path / "libvirt"
    job_dir = libvirt_root / "job"
    job_dir.mkdir(parents=True)
    source = job_dir / "disk.qcow2"
    source.write_bytes(b"disk")
    helper = _render_python_helper(tmp_path, install_root, libvirt_root)

    result = _run_python(
        helper,
        ["flatten_image", str(source), "relative-output.qcow2"],
    )

    output = result.stderr + result.stdout
    assert result.returncode != 0
    assert "DENY:" in output
    assert "Traceback" not in output


def test_remove_job_directory_rejects_shared_base_directory(tmp_path):
    install_root = tmp_path / "install"
    install_root.mkdir()
    libvirt_root = tmp_path / "libvirt"
    base_dir = libvirt_root / "base"
    base_dir.mkdir(parents=True)
    base_image = base_dir / "trusted.qcow2"
    base_image.write_bytes(b"trusted")
    helper = _render_python_helper(tmp_path, install_root, libvirt_root)

    result = _run_python(helper, ["remove_job_dir", str(base_dir)])

    assert result.returncode != 0
    assert "job directory" in result.stderr
    assert base_image.read_bytes() == b"trusted"


def test_ensure_dirs_rejects_base_directory_as_job_directory(tmp_path):
    install_root = tmp_path / "install"
    install_root.mkdir()
    libvirt_root = tmp_path / "libvirt"
    libvirt_root.mkdir()
    helper = _render_python_helper(tmp_path, install_root, libvirt_root)

    result = _run_python(helper, ["ensure_dirs", str(libvirt_root / "base"), str(libvirt_root / "base")])
    assert result.returncode != 0
    assert "job directory" in result.stderr


def test_clear_storage_root_removes_contents_but_preserves_bound_root(tmp_path):
    install_root = tmp_path / "install"
    install_root.mkdir()
    libvirt_root = tmp_path / "libvirt"
    base_image = libvirt_root / "base" / "base.qcow2"
    job_disk = libvirt_root / "job-1" / "disk.qcow2"
    base_image.parent.mkdir(parents=True)
    job_disk.parent.mkdir(parents=True)
    base_image.write_bytes(b"base")
    job_disk.write_bytes(b"job")
    helper = _render_python_helper(tmp_path, install_root, libvirt_root)

    result = _run_python(helper, ["clear_storage_root"])

    assert result.returncode == 0, result.stderr
    assert libvirt_root.is_dir()
    assert list(libvirt_root.iterdir()) == []


def test_install_root_is_bound_by_identity_not_by_path(tmp_path):
    """A directory that merely sits at the bound path is not the install root.

    The helper records the install root's (st_dev, st_ino) at sudoers-install
    time. Replacing that directory with a different one at the same path must
    be refused, and the original directory must still be accepted.
    """
    install_root = tmp_path / "install"
    install_root.mkdir()
    libvirt_root = tmp_path / "libvirt"
    libvirt_root.mkdir()
    helper = _render_python_helper(tmp_path, install_root, libvirt_root)

    bound = _run_python(helper, ["clear_storage_root"])

    saved_root = tmp_path / "saved-install"
    install_root.rename(saved_root)
    install_root.mkdir()
    impostor = _run_python(helper, ["clear_storage_root"])

    install_root.rmdir()
    saved_root.rename(install_root)
    restored = _run_python(helper, ["clear_storage_root"])

    assert bound.returncode == 0, bound.stderr
    assert impostor.returncode != 0
    assert "identity changed" in impostor.stderr
    assert restored.returncode == 0, restored.stderr


def test_missing_install_root_is_denied_without_traceback(tmp_path):
    """A bound install root that is gone must name itself in the refusal.

    Helpers rendered for a checkout that was later renamed or removed died with
    `FileNotFoundError: [Errno 2] ... 'astinuslab-lane1'`: a bare component name
    and a stack trace, carrying nothing to tell an operator that the helpers are
    still pinned to a tree that no longer exists. Every other refusal in this
    script is a DENY that names what it refused, and this one has to be too.
    """
    install_root = tmp_path / "install"
    install_root.mkdir()
    libvirt_root = tmp_path / "libvirt"
    libvirt_root.mkdir()
    helper = _render_python_helper(tmp_path, install_root, libvirt_root)

    bound = _run_python(helper, ["clear_storage_root"])
    install_root.rmdir()
    missing = _run_python(helper, ["clear_storage_root"])

    assert bound.returncode == 0, bound.stderr
    output = missing.stderr + missing.stdout
    assert missing.returncode != 0
    assert "Traceback" not in output
    assert "DENY:" in output
    assert str(install_root) in output
