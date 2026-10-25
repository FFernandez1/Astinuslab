"""Disk-reservation contract for package launch preflight.

A reservation exists so a launch cannot start and then run out of storage
halfway through overlay creation. It has no database side effects, so the only
guarantees it can offer are local: the bytes are actually claimed, the
allocation method is recorded honestly for restart reconciliation, a partial
failure removes every file it already made, and release refuses to unlink
anything that is not still the reservation file it created.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from astinuslab.lab_packages.preflight import (
    DiskReservation,
    DiskReservationError,
    HostFacts,
    PackagePreflight,
    PreflightIssue,
    measure_host,
    reserve_disk_space,
)

pytestmark = pytest.mark.unit


def _reservations(directory: Path) -> list[Path]:
    return sorted(p for p in directory.iterdir() if p.name.startswith(".astinuslab-reservation-"))


# --- reserve_disk_space --------------------------------------------------------


def test_reservation_claims_the_requested_bytes(tmp_path: Path):
    reservation = reserve_disk_space(((tmp_path, 4096),), owner_id="lpi-1")

    assert len(reservation.paths) == 1
    assert reservation.paths[0].stat().st_size == 4096
    assert reservation.allocation_method == "posix_fallocate"


def test_reservation_covers_every_requested_directory(tmp_path: Path):
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()

    reservation = reserve_disk_space(((first, 1024), (second, 2048)), owner_id="lpi-2")

    assert [path.parent for path in reservation.paths] == [first, second]
    assert [path.stat().st_size for path in reservation.paths] == [1024, 2048]


def test_reservation_records_the_journaled_write_fallback(tmp_path: Path, monkeypatch):
    def unsupported_fallocate(descriptor, offset, length):
        raise OSError(errno.EOPNOTSUPP, "not supported")

    monkeypatch.setattr(os, "posix_fallocate", unsupported_fallocate)

    reservation = reserve_disk_space(((tmp_path, 2048),), owner_id="lpi-3")

    assert reservation.allocation_method == "journaled_write"
    assert reservation.paths[0].stat().st_size == 2048


def test_reservation_propagates_an_unexpected_allocation_failure(tmp_path: Path, monkeypatch):
    def refused_fallocate(descriptor, offset, length):
        raise OSError(errno.ENOSPC, "no space left on device")

    monkeypatch.setattr(os, "posix_fallocate", refused_fallocate)

    with pytest.raises(DiskReservationError, match="could not reserve launch storage"):
        reserve_disk_space(((tmp_path, 2048),), owner_id="lpi-4")

    assert _reservations(tmp_path) == []


def test_reservation_removes_earlier_files_when_a_later_one_fails(tmp_path: Path):
    first = tmp_path / "one"
    first.mkdir()
    missing = tmp_path / "absent"

    with pytest.raises(DiskReservationError, match="could not reserve launch storage"):
        reserve_disk_space(((first, 1024), (missing, 1024)), owner_id="lpi-5")

    assert _reservations(first) == []


def test_reservation_sanitizes_the_owner_into_the_filename(tmp_path: Path):
    reservation = reserve_disk_space(((tmp_path, 512),), owner_id="lpi/../6 unsafe")

    # Only alphanumerics, "-" and "_" survive, so separators and dots cannot
    # steer the reservation out of the directory it was asked for.
    assert reservation.paths[0].name.startswith(".astinuslab-reservation-lpi6unsafe-")
    assert reservation.paths[0].parent == tmp_path


@pytest.mark.parametrize(
    "requirements",
    [(), ((Path("."), 0),), ((Path("."), -1),), ((Path("."), True),), ((Path("."), 1.5),)],
)
def test_reservation_refuses_invalid_requirements(requirements):
    with pytest.raises(ValueError, match="invalid disk reservation requirements"):
        reserve_disk_space(requirements, owner_id="lpi-7")


def test_reservation_refuses_a_missing_owner(tmp_path: Path):
    with pytest.raises(ValueError, match="invalid disk reservation requirements"):
        reserve_disk_space(((tmp_path, 1024),), owner_id="")


def test_reservation_refuses_an_owner_with_no_safe_characters(tmp_path: Path):
    with pytest.raises(ValueError, match="invalid disk reservation owner"):
        reserve_disk_space(((tmp_path, 1024),), owner_id="///")


# --- DiskReservation.release ---------------------------------------------------


def test_release_removes_the_reserved_files(tmp_path: Path):
    reservation = reserve_disk_space(((tmp_path, 1024),), owner_id="lpi-8")

    reservation.release()

    assert _reservations(tmp_path) == []


def test_release_tolerates_files_that_are_already_gone(tmp_path: Path):
    reservation = reserve_disk_space(((tmp_path, 1024),), owner_id="lpi-9")
    reservation.paths[0].unlink()

    reservation.release()

    assert _reservations(tmp_path) == []


def test_release_refuses_a_path_that_is_no_longer_a_reservation(tmp_path: Path):
    unrelated = tmp_path / "overlay.qcow2"
    unrelated.write_bytes(b"someone else's overlay")

    DiskReservation((unrelated,), "posix_fallocate").release()

    assert unrelated.exists()


def test_release_leaves_a_reservation_that_gained_another_link(tmp_path: Path):
    reservation = reserve_disk_space(((tmp_path, 1024),), owner_id="lpi-10")
    handed_off = tmp_path / "handed-off.qcow2"
    os.link(reservation.paths[0], handed_off)

    reservation.release()

    assert reservation.paths[0].exists()
    assert handed_off.exists()


# --- measure_host --------------------------------------------------------------


def test_measure_host_reports_local_facts(tmp_path: Path):
    facts = measure_host(tmp_path)

    assert facts.cpu_threads >= 1
    assert facts.memory_mb >= 1
    assert facts.free_disk_bytes > 0
    assert facts.architecture == facts.architecture.lower()
    assert "offline" in facts.capabilities


def test_measure_host_reports_zero_free_disk_for_an_unreadable_path(tmp_path: Path, monkeypatch):
    import shutil

    def refused_usage(path):
        raise OSError("path is gone")

    monkeypatch.setattr(shutil, "disk_usage", refused_usage)

    assert measure_host(tmp_path).free_disk_bytes == 0


# --- PackagePreflight severity views -------------------------------------------


def _facts() -> HostFacts:
    return HostFacts(
        cpu_threads=4,
        memory_mb=8192,
        free_disk_bytes=64 * 1024**3,
        architecture="x86_64",
        kvm_available=True,
        capabilities=frozenset({"offline"}),
    )


def test_preflight_is_ready_when_only_warnings_are_present():
    warning = PreflightIssue("warning", "guest-egress", "Network lab permits guest egress")
    result = PackagePreflight(host=_facts(), required_disk_bytes=1, issues=(warning,))

    assert result.ready is True
    assert result.warnings == (warning,)
    assert result.errors == ()


def test_preflight_is_not_ready_when_an_error_is_present():
    warning = PreflightIssue("warning", "guest-egress", "Network lab permits guest egress")
    error = PreflightIssue("error", "image-unavailable", "VM target image is unavailable")
    result = PackagePreflight(host=_facts(), required_disk_bytes=1, issues=(warning, error))

    assert result.ready is False
    assert result.warnings == (warning,)
    assert result.errors == (error,)
