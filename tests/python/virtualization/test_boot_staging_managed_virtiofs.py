from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.unit]


def _stage(monkeypatch, tmp_path: Path):
    from astinuslab.core import fake_mode
    from astinuslab.virtualization import boot_staging

    storage = tmp_path / "storage"
    storage.mkdir()
    storage.chmod(0o2770)
    monkeypatch.setattr(boot_staging, "settings", SimpleNamespace(storage_dir=storage))
    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)

    def reject_spawn(*args, **kwargs):
        pytest.fail("Job staging must leave daemon startup to libvirt")

    monkeypatch.setattr(subprocess, "Popen", reject_spawn)
    return boot_staging.stage_boot_runtime_paths(
        job_id="job-managed-virtiofs",
        base_tmp_dir=storage / "tmp",
        serial_log_dir=tmp_path / "logs",
        run_command=lambda cmd, **kwargs: None,
        acl_helper_resolver=lambda: Path("/helper.sh"),
    )


def test_stage_boot_runtime_paths_does_not_spawn_an_external_virtiofsd(monkeypatch, tmp_path):
    """Job staging must not start virtiofsd itself.

    A runner-spawned daemon dies under the service confinement ("Error entering
    sandbox: MountProc EPERM") and the socket it leaves behind only surfaces later
    as a libvirt connection refusal. libvirt now owns the daemon through the
    domain's managed <source dir>, so staging just prepares the shared directory.
    """
    tmp_root, cloud_init_dir, share_dir = _stage(monkeypatch, tmp_path)

    assert not (tmp_root / "virtiofsd.sock").exists()
    assert not (tmp_root / "virtiofsd.pid").exists()
    assert cloud_init_dir.is_dir()


def test_stage_boot_runtime_paths_still_prepares_a_traversable_share(monkeypatch, tmp_path):
    """The managed daemon reads the share as libvirt's qemu identity, so the
    staging directory and its parent must stay traversable for it."""
    tmp_root, _cloud_init_dir, share_dir = _stage(monkeypatch, tmp_path)

    assert share_dir == tmp_root / "virtiofs-staging"
    assert share_dir.is_dir()
    assert share_dir.stat().st_mode & 0o7777 == 0o755
    assert tmp_root.stat().st_mode & 0o7777 == 0o755
