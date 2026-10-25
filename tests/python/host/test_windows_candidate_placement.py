"""Mocked candidate mutation order; real execution requires separate approval."""

import subprocess
from pathlib import Path


def test_exact_acl_gates_payload_and_survives_remount():
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            "perl",
            "-I",
            str(root / "tools/dev/windows_ntfs_acl"),
            str(root / "tests/python/_support/windows_candidate_placement.pl"),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "not ok" not in result.stdout
