"""The playground cloud-init seed: what the writer grants, and who reaches it.

These cases were in `test_workloads.py` while the writer was in
`vm_lifecycle.py`, which was a misfiling in both directions. They belong with
`playground/cloud_init_seed.py`, and moving them is what makes the two patch
sites this module owns findable: a search for the writer's consumers now lands on
one file named after it, which is the search that missed two `raising=False`
sites when the module was extracted.

Nothing here is moved or rewritten beyond the file it lives in. What the cases
assert is unchanged: the guest account the seed authorizes comes from the image
rather than a default, the staging directory holding that authorization is 0700
even under the installed worker's `UMask=0007`, and the spawn path reaches the
writer through the module object so that patching it here really intercepts a
boot.
"""

from __future__ import annotations

import os

from astinuslab.playground import orchestrator as po


def test_cloud_init_authorizes_helper_user(tmp_path, monkeypatch):
    from pathlib import Path

    from astinuslab.playground import cloud_init_seed as cis

    pub = tmp_path / "id.pub"
    pub.write_text("ssh-ed25519 AAAA test-key\n", encoding="utf-8")
    monkeypatch.setattr(cis, "get_active_key_paths", lambda: (tmp_path / "id", pub))

    image = {"id": "kali-rolling", "username": "kali"}
    paths = cis._write_playground_cloud_init(
        job_id="pg-1",
        vm_id="vm-1",
        label="atk",
        image=image,
        disk_path=tmp_path / "atk.qcow2",
    )
    user_data = Path(paths["user_data_path"]).read_text(encoding="utf-8")
    assert "  - name: kali" in user_data
    assert "ssh-ed25519 AAAA test-key" in user_data


def test_cloud_init_staging_directory_stays_private_under_service_umask(tmp_path, monkeypatch):
    """The ACL helper rejects group-writable child directories by design.

    Installed workers use UMask=0007, so relying on mkdir's default mode makes
    this runtime staging directory 0770 and prevents every real VM boot.
    """
    from astinuslab.playground import cloud_init_seed as cis

    pub = tmp_path / "id.pub"
    pub.write_text("ssh-ed25519 AAAA test-key\n", encoding="utf-8")
    monkeypatch.setattr(cis, "get_active_key_paths", lambda: (tmp_path / "id", pub))

    previous_umask = os.umask(0o0007)
    try:
        paths = cis._write_playground_cloud_init(
            job_id="pg-1",
            vm_id="vm-1",
            label="target",
            image={"id": "ubuntu-24.04", "username": "ubuntu"},
            disk_path=tmp_path / "target.qcow2",
        )
    finally:
        os.umask(previous_umask)

    cloud_init_dir = paths["user_data_path"].parent
    assert cloud_init_dir.stat().st_mode & 0o777 == 0o700


def test_spawn_path_resolves_the_cloud_init_writer_through_the_module(monkeypatch, tmp_path):
    """`vm_lifecycle` must reach the seed writer by attribute, at call time.

    The writer moved to `cloud_init_seed`, and three cases patch it there to make
    the spawn path skip cloud-init, fail inside it, or assert it never runs. A
    `from .cloud_init_seed import _write_playground_cloud_init` in `vm_lifecycle`
    would bind the original at import time and ignore all three -- and the case
    asserting that a locked boot generates no cloud-init would pass either way,
    because a locked boot never calls the writer. That one cannot catch the
    binding, so this case does.

    `cloud_init_seed` is imported here before the patch on purpose.
    `vm_lifecycle` imports it at module scope, so importing first reproduces the
    binding a running worker already holds rather than one made after the patch,
    which would pick the patched value up under either design.
    """
    import contextlib
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from astinuslab.playground import cloud_init_seed as cis
    from astinuslab.playground import repositories as pr
    from astinuslab.playground import vm_lifecycle as vml

    assert cis  # imported before the patch, see above

    written: list[tuple] = []

    def write_seed(*, job_id, vm_id, label, image, disk_path):
        written.append((job_id, vm_id, label, disk_path))
        return {"seed_iso_path": disk_path.with_name("seed.iso")}

    monkeypatch.setattr(cis, "_write_playground_cloud_init", write_seed)
    monkeypatch.setattr(cis, "_playground_cloud_init_enabled", lambda _image: True)
    monkeypatch.setattr(po, "session_scope", lambda: contextlib.nullcontext(MagicMock()))
    monkeypatch.setattr(pr, "require_session_open_for_authoring", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(po, "settings", SimpleNamespace(libvirt_uri="qemu:///system", base_dir=tmp_path))
    lifecycle = MagicMock()

    vml._boot_playground_vm(
        job_id="pg-1",
        vm_id="vm-1",
        label="target",
        image={"cloud_init": True},
        domain_name="astinuslab-pg-pg-1-target",
        disk_path=tmp_path / "target.qcow2",
        base_image_path=tmp_path / "base.qcow2",
        nic_rows=[],
        effective_vcpus=2,
        effective_memory_mb=2048,
        effective_disk_gb=None,
        lifecycle=lifecycle,
        # Not a MagicMock: `mock` refuses to hand out an `assert_owned` attribute.
        claim_lease=SimpleNamespace(assert_owned=lambda: None),
    )

    assert written == [("pg-1", "vm-1", "target", tmp_path / "target.qcow2")]
    # The returned paths are splatted into the boot call, so a wrapper that
    # dropped them would leave the guest with no seed to read.
    assert lifecycle.boot_playground.call_args.kwargs["seed_iso_path"] == tmp_path / "seed.iso"
