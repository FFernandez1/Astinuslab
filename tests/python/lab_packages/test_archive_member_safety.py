"""Member-level safety contract for the package reader.

Everything in this module runs before a single byte of a package is trusted.
An imported archive is attacker-controlled input: its member names decide where
bytes would land, its headers decide how much work the reader will do, and its
JSON decides what the definition says. These helpers are the layer that refuses
traversal, absolute paths, case collisions, device and link members, encrypted
entries, compression bombs, duplicate JSON keys, and non-finite numbers.
"""

from __future__ import annotations

import io
import json
import stat
import zipfile

import pytest

from astinuslab.lab_packages.archive import (
    PACKAGE_MAX_PATH_DEPTH,
    PackageArchiveError,
    _bounded_read,
    _BoundedBytesIO,
    _check_member_header,
    _member_mode,
    _path_collision_key,
    _reject_duplicate_keys,
    _reject_nonfinite,
    _reject_path_collisions,
    _safe_member_name,
    _strict_json,
    _validate_embedded_asset_path,
    _validate_payload_kind,
)

pytestmark = pytest.mark.unit


def _info(name: str, *, mode: int | None = None, size: int = 1, compress: int = 1, flags: int = 0):
    info = zipfile.ZipInfo(name)
    info.file_size = size
    info.compress_size = compress
    info.flag_bits = flags
    if mode is not None:
        info.external_attr = (mode & 0xFFFF) << 16
    return info


# --- _safe_member_name ---------------------------------------------------------


@pytest.mark.parametrize("name", ["lab.json", "experience/runbook.md", "assets/sha256/" + "a" * 64])
def test_ordinary_member_names_are_accepted(name):
    assert _safe_member_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "../escape.json",
        "experience/../../escape.json",
        "/absolute.json",
        "experience//double.md",
        "experience/./here.md",
        "drive:name.json",
        "",
        "trailing/",
        "/".join(["deep"] * (PACKAGE_MAX_PATH_DEPTH + 1)),
    ],
)
def test_unsafe_member_names_are_refused(name):
    with pytest.raises(PackageArchiveError, match="unsafe package path"):
        _safe_member_name(name)


# --- _path_collision_key / _reject_path_collisions -----------------------------


def test_collision_keys_fold_case_and_unicode_form():
    normalized, folded = _path_collision_key("Experience/Runbook.md")

    assert normalized == "Experience/Runbook.md"
    assert folded == "experience/runbook.md"


def test_distinct_paths_do_not_collide():
    _reject_path_collisions({"lab.json": b"a", "experience/runbook.md": b"b"})


def test_paths_differing_only_by_case_are_refused():
    with pytest.raises(PackageArchiveError):
        _reject_path_collisions({"experience/runbook.md": b"a", "experience/Runbook.md": b"b"})


# --- _member_mode --------------------------------------------------------------


def test_a_regular_member_mode_is_accepted():
    assert _member_mode(_info("lab.json", mode=0o100644)) == stat.S_IFREG


@pytest.mark.parametrize("mode", [stat.S_IFLNK, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, stat.S_IFSOCK])
def test_link_and_device_members_are_refused(mode):
    with pytest.raises(PackageArchiveError, match="unsupported package member type"):
        _member_mode(_info("lab.json", mode=mode | 0o644))


# --- _check_member_header ------------------------------------------------------


def test_a_well_formed_header_is_accepted():
    _check_member_header(_info("lab.json", mode=0o100644, size=100, compress=50))


def test_a_directory_member_is_refused():
    with pytest.raises(PackageArchiveError, match="unsafe package path"):
        _check_member_header(_info("experience/", mode=0o40755))


def test_an_encrypted_member_is_refused():
    with pytest.raises(PackageArchiveError, match="encrypted package member"):
        _check_member_header(_info("lab.json", mode=0o100644, flags=0x1))


def test_a_member_over_the_ceiling_is_refused():
    with pytest.raises(PackageArchiveError, match="too large"):
        _check_member_header(_info("lab.json", mode=0o100644, size=100), max_member_bytes=10)


def test_a_compression_bomb_is_refused():
    with pytest.raises(PackageArchiveError, match="compression ratio too high"):
        _check_member_header(_info("lab.json", mode=0o100644, size=10_000_000, compress=1))


def test_a_member_that_claims_zero_compressed_size_is_refused():
    with pytest.raises(PackageArchiveError, match="compression ratio too high"):
        _check_member_header(_info("lab.json", mode=0o100644, size=1000, compress=0))


# --- strict JSON ---------------------------------------------------------------


def test_strict_json_parses_an_object():
    assert _strict_json(b'{"a": 1}', "lab.json") == {"a": 1}


def test_duplicate_json_keys_are_refused():
    with pytest.raises(PackageArchiveError):
        _strict_json(b'{"a": 1, "a": 2}', "lab.json")


def test_the_duplicate_key_guard_accepts_distinct_pairs():
    assert _reject_duplicate_keys([("a", 1), ("b", 2)]) == {"a": 1, "b": 2}


def test_the_duplicate_key_guard_refuses_a_repeat():
    with pytest.raises(ValueError, match="duplicate"):
        _reject_duplicate_keys([("a", 1), ("a", 2)])


@pytest.mark.parametrize("payload", [b"NaN", b"Infinity", b'{"x": NaN}', b'{"x": -Infinity}'])
def test_non_finite_numbers_are_refused(payload):
    with pytest.raises(PackageArchiveError):
        _strict_json(payload, "lab.json")


def test_the_non_finite_guard_always_raises():
    with pytest.raises(ValueError, match="non-finite JSON number"):
        _reject_nonfinite("NaN")


def test_a_json_document_that_is_not_an_object_is_refused():
    with pytest.raises(PackageArchiveError):
        _strict_json(b"[1, 2, 3]", "lab.json")


def test_malformed_json_is_refused():
    with pytest.raises(PackageArchiveError):
        _strict_json(b"{not json", "lab.json")


# --- payload kinds and asset paths ---------------------------------------------


@pytest.mark.parametrize("name", ["lab.json", "experience/runbook.md", "assets/sha256/" + "a" * 64])
def test_known_payload_kinds_are_accepted(name):
    _validate_payload_kind(name)


@pytest.mark.parametrize("name", ["unexpected.bin", "assets/md5/abc", "lab.json.bak"])
def test_unknown_payload_kinds_are_refused(name):
    with pytest.raises(PackageArchiveError):
        _validate_payload_kind(name)


def test_an_embedded_asset_path_must_name_its_digest():
    digest = "a" * 64
    assert _validate_embedded_asset_path(f"assets/sha256/{digest}") == digest


@pytest.mark.parametrize(
    "name",
    ["assets/sha256/not-a-digest", "assets/sha256/" + "A" * 64, "assets/sha256/", "assets/sha256/a/b"],
)
def test_a_malformed_embedded_asset_path_is_refused(name):
    with pytest.raises(PackageArchiveError):
        _validate_embedded_asset_path(name)


# --- bounded reads -------------------------------------------------------------


def _zip_with(name: str, payload: bytes) -> zipfile.ZipFile:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, payload)
    buffer.seek(0)
    return zipfile.ZipFile(buffer)


def test_a_bounded_buffer_accepts_writes_inside_its_ceiling():
    buffer = _BoundedBytesIO(64)

    buffer.write(b"payload")

    assert buffer.getvalue() == b"payload"


def test_a_bounded_buffer_refuses_a_write_past_its_ceiling():
    buffer = _BoundedBytesIO(4)

    with pytest.raises(PackageArchiveError, match="archive is too large"):
        buffer.write(b"payload")


def test_bounded_read_returns_a_small_member():
    payload = b"payload"
    with _zip_with("lab.json", payload) as archive:
        info = archive.getinfo("lab.json")

        assert _bounded_read(archive, info, 64) == payload


def test_bounded_read_refuses_a_member_over_the_ceiling():
    with _zip_with("lab.json", b"payload" * 100) as archive:
        info = archive.getinfo("lab.json")

        with pytest.raises(PackageArchiveError, match="too large"):
            _bounded_read(archive, info, 8)


def test_bounded_read_verifies_a_declared_digest():
    payload = b"payload"
    with _zip_with("lab.json", payload) as archive:
        info = archive.getinfo("lab.json")

        with pytest.raises(PackageArchiveError, match="digest mismatch"):
            _bounded_read(archive, info, 64, expected_sha256="a" * 64)


def test_bounded_read_verifies_a_declared_size():
    payload = b"payload"
    with _zip_with("lab.json", payload) as archive:
        info = archive.getinfo("lab.json")

        with pytest.raises(PackageArchiveError, match="size mismatch"):
            _bounded_read(archive, info, 64, expected_size=len(payload) + 1)


def test_bounded_read_can_verify_without_retaining_the_bytes():
    payload = b"payload"
    with _zip_with("lab.json", payload) as archive:
        info = archive.getinfo("lab.json")

        assert _bounded_read(archive, info, 64, retain=False) == b""


def test_json_round_trips_through_the_strict_parser():
    document = {"package_id": "x", "nested": {"list": [1, 2, 3]}}

    assert _strict_json(json.dumps(document).encode(), "lab.json") == document
