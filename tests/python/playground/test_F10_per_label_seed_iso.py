from pathlib import Path

from astinuslab.playground.orchestrator import _spawn_artifact_paths


def test_F10_no_shared_seed_iso():
    disk_path = Path("/var/lib/astinuslab/playground/disks/pg-abc/web-1.qcow2")
    paths = _spawn_artifact_paths(disk_path)
    # The shared "seed.iso" in the parent must not appear — only label-scoped names.
    assert (disk_path.parent / "seed.iso") not in paths
    # The label-scoped seed name MUST still be cleaned up.
    assert (disk_path.with_name(f"{disk_path.stem}-seed.iso")) in paths
