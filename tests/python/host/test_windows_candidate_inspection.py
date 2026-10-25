"""Mocked read-only appliance boundaries; these do not prove Windows acceptance."""

import subprocess
from pathlib import Path

import pytest


def test_candidate_collector_readonly_binding_and_failures():
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            "perl",
            "-I",
            str(root / "tools/dev/windows_ntfs_acl"),
            str(root / "tests/python/_support/windows_candidate_inspection.pl"),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "not ok" not in result.stdout


def test_statns_schema_matches_installed_binding_field_names():
    root = Path(__file__).resolve().parents[3]
    library = Path("/usr/lib/x86_64-linux-gnu/perl5/5.38/auto/Sys/Guestfs/Guestfs.so")
    if not library.exists():
        pytest.skip("installed Perl libguestfs binding required")
    result = subprocess.check_output(
        [
            "perl",
            "-I",
            str(root / "tools/dev/windows_ntfs_acl"),
            "-MCandidateInspection",
            "-e",
            "print join(qq{\\n}, CandidateInspection::statns_fields())",
        ],
        timeout=10,
    )
    names = result.splitlines()
    assert len(names) == len(set(names)) == 22
    assert set(b"st_spare" + str(n).encode() for n in range(1, 7)) <= set(names)
    installed = library.read_bytes()
    assert all(b"\0" + name + b"\0" in installed for name in names)
