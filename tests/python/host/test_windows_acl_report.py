"""Strict descriptor decoding is observation, never an effective-access verdict."""

import importlib.util
import struct
import subprocess
from pathlib import Path

import pytest

MODULES = Path(__file__).resolve().parents[3] / "tools/dev/windows_ntfs_acl"


def report(raw):
    spec = importlib.util.spec_from_file_location("acl_report", MODULES / "acl_report.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.describe(raw)


@pytest.fixture
def descriptor():
    return subprocess.check_output(
        ["perl", "-I", str(MODULES), "-MRestrictedAcl", "-e", "print RestrictedAcl::descriptor()"], timeout=10
    )


def test_protected_public_descriptor(descriptor):
    value = report(descriptor)
    assert value["owner"] == "S-1-5-18"
    assert value["group"] == "S-1-5-32-544"
    assert value["control"] == 0x9004 and value["sacl"] is None
    assert value["dacl"]["entries"] == [
        {"type": 0, "flags": 0, "size": 20, "mask": 0x1F01FF, "sid": "S-1-5-18"},
        {"type": 0, "flags": 0, "size": 24, "mask": 0x1F01FF, "sid": "S-1-5-32-544"},
    ]
    assert value["basic_aces_only"] is True
    assert "safe" not in value and "effective_access" not in value


@pytest.mark.parametrize(
    "offset,replacement",
    [
        (0, b"\x02"),
        (2, b"\x04\x10"),
        (4, struct.pack("<I", 101)),
        (4, struct.pack("<I", 29)),
        (4, struct.pack("<I", 20)),
        (12, struct.pack("<I", 20)),
        (20, b"\x03"),
        (21, b"\x01"),
        (22, struct.pack("<H", 65535)),
        (24, struct.pack("<H", 3)),
        (30, struct.pack("<H", 4)),
        (37, b"\x10"),
        (72, b"\x02"),
    ],
)
def test_malformed_bounds_alignment_overlap_and_revision_rejected(descriptor, offset, replacement):
    raw = descriptor[:offset] + replacement + descriptor[offset + len(replacement) :]
    with pytest.raises(ValueError):
        report(raw)


@pytest.mark.parametrize("raw", [b"", b"x" * 19, b"x" * 8193])
def test_descriptor_size_bound(raw):
    with pytest.raises(ValueError):
        report(raw)


def test_every_truncation_rejected(descriptor):
    for length in range(len(descriptor)):
        with pytest.raises(ValueError):
            report(descriptor[:length])


def test_unknown_ace_is_opaque_and_never_interpreted_as_basic_allow(descriptor):
    raw = descriptor[:28] + b"\x09" + descriptor[29:]
    value = report(raw)
    assert value["basic_aces_only"] is False
    assert set(value["dacl"]["entries"][0]) == {"type", "flags", "size", "sha256"}


def test_null_dacl_distinct_from_empty_dacl(descriptor):
    null = struct.pack("<BBHIIII", 1, 0, 0x8004, 0, 0, 0, 0)
    empty = struct.pack("<BBHIIII", 1, 0, 0x8004, 0, 0, 0, 20) + struct.pack("<BBHHH", 2, 0, 8, 0, 0)
    assert report(null)["dacl"] is None
    assert report(empty)["dacl"]["entries"] == []


def test_shared_owner_group_sid_allowed_and_nonzero_padding_rejected(descriptor):
    raw = descriptor[:8] + struct.pack("<I", 72) + descriptor[12:84]
    assert report(raw)["group"] == "S-1-5-18"
    with pytest.raises(ValueError):
        report(raw + b"private trailing bytes")
