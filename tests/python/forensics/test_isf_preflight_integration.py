import json
from types import SimpleNamespace

import pytest

from astinuslab.forensics.isf_preflight import PreflightError
from astinuslab.orchestration.runners import isf_matrix_runner
from astinuslab.orchestration.runners.isf_matrix_runner import MatrixRunConfig


def test_matrix_runner_aborts_on_preflight_failure(monkeypatch, tmp_path):
    def fake_check(libvirt_uri):
        raise PreflightError("required host tool missing: rpm2cpio. Install: sudo apt install rpm2cpio")

    monkeypatch.setattr("astinuslab.orchestration.runners.isf_matrix_runner.check_host_prerequisites", fake_check)

    run_dir = tmp_path / "run"
    config = MatrixRunConfig(run_dir=run_dir, dry_run=True)

    with pytest.raises(PreflightError, match="rpm2cpio"):
        isf_matrix_runner.run_matrix(config)


def test_matrix_runner_records_unready_dwarf2json_without_aborting(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "astinuslab.forensics.isf_preflight._check_tool",
        lambda name, install_hint=None: f"/usr/bin/{name}",
    )
    monkeypatch.setattr(
        "astinuslab.forensics.isf_preflight._check_libvirt",
        lambda uri: "1.2.3",
    )
    monkeypatch.setattr(
        "astinuslab.forensics.isf_preflight.check_dwarf2json_tool",
        lambda: SimpleNamespace(
            ready=False,
            code="dwarf2json_hash_mismatch",
            detail="The configured dwarf2json file does not match its SHA-256 pin.",
        ),
    )

    config = MatrixRunConfig(run_dir=tmp_path / "run", dry_run=True)

    # Inject the target catalog instead of reading the real one: `load_targets`
    # marks an entry unavailable unless the image file is actually on disk, so
    # relying on it would make this test pass only on machines that have run
    # ops/images/download_images.sh, and fail on a clean checkout or in CI.
    def _one_bootable_target() -> list[dict]:
        return [
            {
                "id": "ubuntu",
                "enabled": True,
                "image_available": True,
                "supports_cloud_image": True,
                "image_ref": "ubuntu.qcow2",
                "distro": "ubuntu",
                "version": "24.04",
                "display_name": "Ubuntu",
            }
        ]

    # The matrix runner never invokes dwarf2json itself: it drives ISF-only jobs
    # through the provider chain, where local_cache and prebuilt can deliver a ready
    # ISF without it. An unready dwarf2json is recorded as diagnostics rather than
    # aborting the whole matrix; the providers that do need it (debuginfod and
    # host_fetch, which return a vmlinux) re-verify immediately before use and
    # surface the readiness code on the failing attempt.
    isf_matrix_runner.run_matrix(config, targets_loader=_one_bootable_target)

    recorded = json.loads((tmp_path / "run" / "preflight.json").read_text(encoding="utf-8"))
    assert recorded["dwarf2json"] == "dwarf2json_hash_mismatch"
