"""Argument and state guards on the package service.

These are the checks that run before the service touches storage or the
ledger. A caller that asks for a non-positive expiry, staging age, or lease
duration has expressed something the service cannot honour safely, and a
publication or inspection asked to advance from the wrong state would commit a
package whose bytes were never verified in that state. Each guard refuses
before any side effect.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from astinuslab.db.models import LabPackageInspection, LabPackagePublicationAttempt
from astinuslab.lab_packages.archive import write_archive
from astinuslab.lab_packages.content_store import ContentStore
from astinuslab.lab_packages.models import (
    HostResources,
    LabDefinition,
    LabExperience,
    LabNetwork,
    LabVM,
    ResourceRecommendations,
)
from astinuslab.lab_packages.package_service import LabPackageService, PackageImportError

pytestmark = pytest.mark.unit

_BAD_INTEGERS = [0, -1, True, 1.5, "60", None]


def _definition() -> LabDefinition:
    return LabDefinition(
        package_id="guard-lab",
        version="1.0.0",
        title="Guard lab",
        creator="Researcher",
        license="CC-BY-4.0",
        reproducibility="unlocked",
        resources=ResourceRecommendations(
            minimum=HostResources(cpu_threads=2, memory_mb=4096, free_disk_bytes=40 * 1024**3),
            recommended=HostResources(cpu_threads=4, memory_mb=8192, free_disk_bytes=40 * 1024**3),
        ),
        networks=(LabNetwork(id="isolated", subnet_cidr="192.0.2.0/29"),),
        vms=(
            LabVM(
                id="target",
                role="target",
                image_id="ubuntu-24.04",
                vcpus=2,
                memory_mb=2048,
                disk_gb=10,
                network_ids=("isolated",),
            ),
        ),
        experience=LabExperience(),
    )


ARCHIVE = write_archive(_definition(), "# Runbook\n")
ARCHIVE_DIGEST = hashlib.sha256(ARCHIVE).hexdigest()


@pytest.fixture()
def service(tmp_path: Path) -> LabPackageService:
    return LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))


@pytest.fixture()
def source(tmp_path: Path) -> Path:
    path = tmp_path / "input.astinuslab"
    path.write_bytes(ARCHIVE)
    return path


# --- argument guards -----------------------------------------------------------


@pytest.mark.parametrize("age", _BAD_INTEGERS)
def test_staging_cleanup_refuses_a_non_positive_age(service, age):
    with pytest.raises(ValueError, match="staging age must be a positive integer"):
        service.cleanup_staging(max_age_seconds=age)


@pytest.mark.parametrize("expiry", _BAD_INTEGERS)
def test_reserving_an_inspection_refuses_a_non_positive_expiry(in_memory_session_scope, service, expiry):
    with in_memory_session_scope() as session:
        with pytest.raises(ValueError, match="inspection expiry must be a positive integer"):
            service.reserve_inspection(session, expires_in_seconds=expiry)


@pytest.mark.parametrize("expiry", _BAD_INTEGERS)
def test_staging_an_inspection_refuses_a_non_positive_expiry(in_memory_session_scope, service, source, expiry):
    with in_memory_session_scope() as session:
        with pytest.raises(ValueError, match="inspection expiry must be a positive integer"):
            service.stage_inspection(source, session, expires_in_seconds=expiry)


@pytest.mark.parametrize("ttl", _BAD_INTEGERS)
def test_acquiring_a_lease_refuses_a_non_positive_duration(in_memory_session_scope, service, ttl):
    with in_memory_session_scope() as session:
        with pytest.raises(ValueError, match="lease duration must be a positive integer"):
            service._acquire_lease(
                session,
                kind="content",
                resource_id="a" * 64,
                owner_id="owner-1",
                operation="test",
                ttl_seconds=ttl,
            )


def test_staging_cleanup_on_an_empty_area_removes_nothing(service):
    assert service.cleanup_staging(max_age_seconds=1) == 0


# --- inspection lookups --------------------------------------------------------


def test_an_unknown_staged_inspection_is_refused(in_memory_session_scope, service):
    with in_memory_session_scope() as session:
        with pytest.raises(PackageImportError, match="package inspection not found"):
            service.get_staged_inspection("lpi-absent", session)


def test_a_reserved_inspection_is_readable_by_id(in_memory_session_scope, service):
    with in_memory_session_scope() as session:
        reserved = service.reserve_inspection(session, expires_in_seconds=3600)
        session.commit()

        assert service.get_staged_inspection(reserved.id, session).id == reserved.id


def test_abandoning_an_inspection_records_its_error(in_memory_session_scope, service):
    with in_memory_session_scope() as session:
        reserved = service.reserve_inspection(session, expires_in_seconds=3600)
        session.commit()

        service.abandon_inspection(reserved.id, session, error_summary="upload aborted")

        row = session.get(LabPackageInspection, reserved.id)
        assert row.state not in {"reserved", "staged"}
        assert "aborted" in (row.error_summary or "")


# --- publication state guards --------------------------------------------------


def _published(service, *, attempt_id: str = "lpa-1", state: str = "published", digest: str = ARCHIVE_DIGEST):
    final = service.package_root / "guard-lab-1.0.0.astinuslab"
    return LabPackagePublicationAttempt(
        id=attempt_id,
        package_id="guard-lab",
        version="1.0.0",
        archive_sha256=digest,
        staging_path=str(service.package_root / ".astinuslab-import-lpa-1"),
        final_path=str(final),
        state=state,
    )


def test_completing_an_unknown_publication_is_refused(in_memory_session_scope, service):
    with in_memory_session_scope() as session:
        with pytest.raises(PackageImportError, match="not ready to complete"):
            service._complete_published_attempt("lpa-absent", session)


@pytest.mark.parametrize("state", ["reserved", "failed", "completed", "cleanup_incomplete"])
def test_completing_a_publication_in_the_wrong_state_is_refused(in_memory_session_scope, service, state):
    with in_memory_session_scope() as session:
        session.add(_published(service, state=state))
        session.commit()

        with pytest.raises(PackageImportError, match="not ready to complete"):
            service._complete_published_attempt("lpa-1", session)


def test_completing_a_publication_whose_bytes_changed_is_refused(in_memory_session_scope, service):
    service.package_root.mkdir(parents=True, exist_ok=True)
    attempt = _published(service)
    Path(attempt.final_path).write_bytes(ARCHIVE)

    with in_memory_session_scope() as session:
        session.add(_published(service, digest="b" * 64))
        session.commit()

        with pytest.raises(PackageImportError, match="archive changed before completion"):
            service._complete_published_attempt("lpa-1", session)


def test_completing_a_publication_for_another_identity_is_refused(in_memory_session_scope, service):
    service.package_root.mkdir(parents=True, exist_ok=True)
    attempt = _published(service)
    Path(attempt.final_path).write_bytes(ARCHIVE)

    with in_memory_session_scope() as session:
        row = _published(service)
        row.package_id = "another-lab"
        session.add(row)
        session.commit()

        with pytest.raises(PackageImportError, match="does not match its reservation"):
            service._complete_published_attempt("lpa-1", session)


# --- commit state guards -------------------------------------------------------


def test_committing_an_unknown_inspection_is_refused(in_memory_session_scope, service):
    with in_memory_session_scope() as session:
        with pytest.raises(PackageImportError):
            service.commit_inspection("lpi-absent", session)


def test_committing_a_reserved_inspection_is_refused(in_memory_session_scope, service):
    with in_memory_session_scope() as session:
        reserved = service.reserve_inspection(session, expires_in_seconds=3600)
        session.commit()

        with pytest.raises(PackageImportError, match="not ready to commit"):
            service.commit_inspection(reserved.id, session)


def test_committing_an_inspection_whose_bytes_vanished_is_refused(in_memory_session_scope, service, source):
    with in_memory_session_scope() as session:
        staged = service.stage_inspection(source, session)
        session.commit()
        Path(session.get(LabPackageInspection, staged.id).staged_path).unlink()

        with pytest.raises(PackageImportError):
            service.commit_inspection(staged.id, session)


def test_committing_an_inspection_whose_bytes_changed_is_refused(in_memory_session_scope, service, source):
    with in_memory_session_scope() as session:
        staged = service.stage_inspection(source, session)
        session.commit()
        Path(session.get(LabPackageInspection, staged.id).staged_path).write_bytes(b"replaced bytes")

        with pytest.raises(PackageImportError):
            service.commit_inspection(staged.id, session)


def test_committing_the_same_inspection_twice_is_refused(in_memory_session_scope, service, source):
    with in_memory_session_scope() as session:
        staged = service.stage_inspection(source, session)
        session.commit()
        service.commit_inspection(staged.id, session)
        session.commit()

        with pytest.raises(PackageImportError, match="already committed"):
            service.commit_inspection(staged.id, session)
