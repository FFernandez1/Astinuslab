"""Version reservation and export recovery contract.

An export reserves ``package_id@version`` before any bytes exist, so two
exports cannot quietly produce two different archives claiming the same
identity. When the process dies mid-export the journal row outlives the
staging file, and recovery has to decide from the bytes on disk rather than
from the row's optimism: adopt a staged archive only when it is inside the
configured package root, still within its reservation, and hashes to what the
journal recorded. Everything else is failed and swept.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from astinuslab.db.models import LabPackageExportAttempt, LabStorageLease
from astinuslab.lab_packages.archive import write_archive
from astinuslab.lab_packages.canonical import canonical_sha256
from astinuslab.lab_packages.export_service import (
    ExportVersionConflict,
    reconcile_exports,
    reserve_export_version,
    stream_file,
)
from astinuslab.lab_packages.models import (
    HostResources,
    LabDefinition,
    LabExperience,
    LabNetwork,
    LabVM,
    ResourceRecommendations,
)

pytestmark = pytest.mark.unit


def _definition() -> LabDefinition:
    return LabDefinition(
        package_id="export-recon-lab",
        version="1.0.0",
        title="Export recon lab",
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


DEFINITION = _definition()
DEFINITION_DIGEST = canonical_sha256(DEFINITION)


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    packages = tmp_path / "packages"
    packages.mkdir()
    return packages


def _staged(root: Path, name: str = "export.astinuslab") -> Path:
    path = root / name
    path.write_bytes(write_archive(DEFINITION, "# Runbook\n"))
    return path


def _attempt(
    *,
    attempt_id: str = "lpe-1",
    state: str = "reserved",
    staging_path: Path | None,
    archive_sha256: str | None = None,
    definition_sha256: str = DEFINITION_DIGEST,
    expires_in: timedelta | None = timedelta(hours=1),
) -> LabPackageExportAttempt:
    return LabPackageExportAttempt(
        id=attempt_id,
        package_id="export-recon-lab",
        version="1.0.0",
        definition_sha256=definition_sha256,
        staging_path=None if staging_path is None else str(staging_path),
        archive_sha256=archive_sha256,
        state=state,
        expires_at=None if expires_in is None else datetime.now(UTC).replace(tzinfo=None) + expires_in,
    )


# --- reserve_export_version ----------------------------------------------------


def test_a_free_identity_can_be_reserved(in_memory_session_scope, root):
    with in_memory_session_scope() as session:
        attempt = reserve_export_version(
            session,
            package_id="export-recon-lab",
            version="1.0.0",
            definition_sha256=DEFINITION_DIGEST,
            staging_path=root / "export.astinuslab",
        )

        assert attempt.state == "reserved"
        assert attempt.expires_at is not None


def test_a_live_reservation_blocks_a_second_export(in_memory_session_scope, root):
    with in_memory_session_scope() as session:
        session.add(_attempt(staging_path=_staged(root)))
        session.commit()

        with pytest.raises(ExportVersionConflict):
            reserve_export_version(
                session,
                package_id="export-recon-lab",
                version="1.0.0",
                definition_sha256=DEFINITION_DIGEST,
                staging_path=root / "second.astinuslab",
            )


def test_an_expired_reservation_is_released_for_the_next_export(in_memory_session_scope, root):
    with in_memory_session_scope() as session:
        session.add(_attempt(staging_path=_staged(root), expires_in=-timedelta(minutes=1)))
        session.commit()

        attempt = reserve_export_version(
            session,
            package_id="export-recon-lab",
            version="1.0.0",
            definition_sha256=DEFINITION_DIGEST,
            staging_path=root / "second.astinuslab",
        )

        assert attempt.state == "reserved"
        assert session.get(LabPackageExportAttempt, "lpe-1").state == "failed"


def test_another_version_of_the_same_package_is_not_blocked(in_memory_session_scope, root):
    with in_memory_session_scope() as session:
        session.add(_attempt(staging_path=_staged(root)))
        session.commit()

        attempt = reserve_export_version(
            session,
            package_id="export-recon-lab",
            version="2.0.0",
            definition_sha256=DEFINITION_DIGEST,
            staging_path=root / "v2.astinuslab",
        )

        assert attempt.version == "2.0.0"


# --- reconcile_exports ---------------------------------------------------------


def test_recovery_reports_nothing_for_an_empty_journal(in_memory_session_scope, root):
    with in_memory_session_scope() as session:
        report = reconcile_exports(session, package_root=root)

    assert report["completed"] == 0
    assert report["failed"] == 0


def test_recovery_adopts_a_staged_archive_that_matches_its_reservation(in_memory_session_scope, root):
    staged = _staged(root)
    with in_memory_session_scope() as session:
        session.add(_attempt(staging_path=staged))
        session.commit()

        report = reconcile_exports(session, package_root=root)

        assert report["completed"] == 1
        recovered = session.get(LabPackageExportAttempt, "lpe-1")
        assert recovered.state == "completed"
        assert recovered.archive_sha256 == hashlib.sha256(staged.read_bytes()).hexdigest()
        assert recovered.expires_at is None


def test_recovery_releases_the_content_leases_it_completes(in_memory_session_scope, root):
    staged = _staged(root)
    with in_memory_session_scope() as session:
        session.add(_attempt(staging_path=staged))
        session.add(
            LabStorageLease(
                id="lease-export",
                resource_kind="content",
                resource_id="a" * 64,
                operation_kind="lab_package_export",
                owner_id="lpe-1",
                expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1),
            )
        )
        session.commit()

        reconcile_exports(session, package_root=root)

        assert session.get(LabStorageLease, "lease-export") is None


def test_recovery_refuses_a_published_archive_whose_digest_changed(in_memory_session_scope, root):
    staged = _staged(root)
    with in_memory_session_scope() as session:
        session.add(_attempt(state="published", staging_path=staged, archive_sha256="b" * 64))
        session.commit()

        report = reconcile_exports(session, package_root=root)

        assert report["completed"] == 0


def test_recovery_refuses_an_archive_for_another_definition(in_memory_session_scope, root):
    staged = _staged(root)
    with in_memory_session_scope() as session:
        session.add(_attempt(staging_path=staged, definition_sha256="c" * 64))
        session.commit()

        report = reconcile_exports(session, package_root=root)

        assert report["completed"] == 0


def test_recovery_ignores_a_staging_path_outside_the_package_root(in_memory_session_scope, root, tmp_path: Path):
    outside = tmp_path / "outside.astinuslab"
    outside.write_bytes(write_archive(DEFINITION, "# Runbook\n"))
    with in_memory_session_scope() as session:
        session.add(_attempt(staging_path=outside, expires_in=-timedelta(minutes=1)))
        session.commit()

        report = reconcile_exports(session, package_root=root)

        assert report["completed"] == 0
        assert report["failed"] == 1

    assert outside.exists()


def test_recovery_fails_an_expired_attempt_and_sweeps_its_files(in_memory_session_scope, root):
    staged = _staged(root)
    partial = staged.with_name(f".{staged.name}.partial")
    partial.write_bytes(b"resumable prefix")
    with in_memory_session_scope() as session:
        session.add(_attempt(staging_path=staged, expires_in=-timedelta(minutes=1)))
        session.commit()

        report = reconcile_exports(session, package_root=root)

        assert report["failed"] == 1
        assert session.get(LabPackageExportAttempt, "lpe-1").state == "failed"

    assert not staged.exists()
    assert not partial.exists()


def test_recovery_leaves_a_live_reservation_with_no_bytes_alone(in_memory_session_scope, root):
    with in_memory_session_scope() as session:
        session.add(_attempt(staging_path=root / "never-written.astinuslab"))
        session.commit()

        report = reconcile_exports(session, package_root=root)

        assert report == {"completed": 0, "failed": 0}
        assert session.get(LabPackageExportAttempt, "lpe-1").state == "reserved"


def test_recovery_refuses_bytes_that_are_not_a_package(in_memory_session_scope, root):
    staged = root / "junk.astinuslab"
    staged.write_bytes(b"not a zip archive")
    with in_memory_session_scope() as session:
        session.add(_attempt(staging_path=staged, expires_in=-timedelta(minutes=1)))
        session.commit()

        report = reconcile_exports(session, package_root=root)

        assert report["completed"] == 0
        assert report["failed"] == 1


# --- stream_file ---------------------------------------------------------------


def test_stream_file_yields_the_whole_archive(root):
    staged = _staged(root)

    streamed = b"".join(stream_file(staged, chunk_size=8))

    assert streamed == staged.read_bytes()
