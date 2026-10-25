"""Trust evaluation on import, and deletion against live custody.

Signing is optional, so the interesting states are the ones between unsigned
and trusted: a valid signature from a key the operator never listed is not the
same as a trusted one, and the recipient must be able to tell them apart before
launching anything. Deletion has the mirror-image problem — an archive that a
running operation still leases cannot be removed just because someone asked.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from astinuslab.db.models import LabPackage, LabStorageLease
from astinuslab.lab_packages.archive import inspect_archive, write_archive
from astinuslab.lab_packages.content_store import ContentStore
from astinuslab.lab_packages.models import (
    HostResources,
    LabDefinition,
    LabExperience,
    LabNetwork,
    LabVM,
    ResourceRecommendations,
)
from astinuslab.lab_packages.package_service import (
    LabPackageService,
    PackageDeletionConflictError,
)
from astinuslab.lab_packages.signing import (
    LocalTrustPolicy,
    SignatureStatus,
    generate_key_pair,
    public_key_from_private,
    sign_manifest,
)

pytestmark = pytest.mark.unit

ASSET = b"embedded clean layer"
ASSET_DIGEST = hashlib.sha256(ASSET).hexdigest()


def _definition() -> LabDefinition:
    return LabDefinition(
        package_id="trust-lab",
        version="1.0.0",
        title="Trust lab",
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


@pytest.fixture()
def service(tmp_path: Path) -> LabPackageService:
    return LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))


def _unsigned() -> bytes:
    return write_archive(
        _definition(), "# Runbook\n", assets={ASSET_DIGEST: ASSET}, allow_legacy_unreferenced_assets=True
    )


def _signed(private_key: bytes) -> bytes:
    manifest = inspect_archive(_unsigned()).payloads["manifest.json"]
    return write_archive(
        _definition(),
        "# Runbook\n",
        assets={ASSET_DIGEST: ASSET},
        allow_legacy_unreferenced_assets=True,
        signatures={
            "signatures/manifest.ed25519": sign_manifest(manifest, private_key),
            "signatures/creator.pub": public_key_from_private(private_key),
        },
    )


def _write(tmp_path: Path, payload: bytes, name: str = "input.astinuslab") -> Path:
    path = tmp_path / name
    path.write_bytes(payload)
    return path


# --- trust evaluation on import ------------------------------------------------


def test_an_unsigned_package_imports_as_unsigned(in_memory_session_scope, service, tmp_path: Path):
    with in_memory_session_scope() as session:
        imported = service.import_path(_write(tmp_path, _unsigned()), session)

        assert imported.signature.status is SignatureStatus.UNSIGNED


def test_a_signed_package_with_no_trust_policy_is_valid_but_untrusted(in_memory_session_scope, service, tmp_path: Path):
    private_key, _ = generate_key_pair()

    with in_memory_session_scope() as session:
        imported = service.import_path(_write(tmp_path, _signed(private_key)), session)

        assert imported.signature.status is SignatureStatus.VALID_UNTRUSTED_KEY


def test_a_signed_package_from_a_listed_key_imports_as_trusted(in_memory_session_scope, service, tmp_path: Path):
    private_key, public_key = generate_key_pair()
    policy = LocalTrustPolicy.from_public_keys([public_key])

    with in_memory_session_scope() as session:
        imported = service.import_path(_write(tmp_path, _signed(private_key)), session, trust_policy=policy)

        assert imported.signature.status is SignatureStatus.VALID_LOCALLY_TRUSTED_KEY
        assert imported.signature.fingerprint


def test_a_signed_package_from_an_unlisted_key_stays_untrusted(in_memory_session_scope, service, tmp_path: Path):
    private_key, _ = generate_key_pair()
    _, other_public = generate_key_pair()
    policy = LocalTrustPolicy.from_public_keys([other_public])

    with in_memory_session_scope() as session:
        imported = service.import_path(_write(tmp_path, _signed(private_key)), session, trust_policy=policy)

        assert imported.signature.status is SignatureStatus.VALID_UNTRUSTED_KEY


def test_an_empty_trust_policy_trusts_nothing(in_memory_session_scope, service, tmp_path: Path):
    private_key, _ = generate_key_pair()

    with in_memory_session_scope() as session:
        imported = service.import_path(
            _write(tmp_path, _signed(private_key)), session, trust_policy=LocalTrustPolicy.empty()
        )

        assert imported.signature.status is SignatureStatus.VALID_UNTRUSTED_KEY


def test_a_fingerprint_policy_also_establishes_trust(in_memory_session_scope, service, tmp_path: Path):
    private_key, public_key = generate_key_pair()
    from astinuslab.lab_packages.signing import public_key_fingerprint

    fingerprint = [public_key_fingerprint(public_key)]

    with in_memory_session_scope() as session:
        imported = service.import_path(
            _write(tmp_path, _signed(private_key)),
            session,
            trust_policy=LocalTrustPolicy.from_fingerprints(fingerprint),
        )

        assert imported.signature.status is SignatureStatus.VALID_LOCALLY_TRUSTED_KEY


def test_a_staged_inspection_carries_its_trust_decision(in_memory_session_scope, service, tmp_path: Path):
    private_key, public_key = generate_key_pair()
    policy = LocalTrustPolicy.from_public_keys([public_key])

    with in_memory_session_scope() as session:
        staged = service.stage_inspection(_write(tmp_path, _signed(private_key)), session, trust_policy=policy)
        session.commit()

        assert staged.signature.status is SignatureStatus.VALID_LOCALLY_TRUSTED_KEY

        committed = service.commit_inspection(staged.id, session, trust_policy=policy)

        assert committed.signature.status is SignatureStatus.VALID_LOCALLY_TRUSTED_KEY


# --- deletion against live custody ---------------------------------------------


def _lease(record_id: str, *, expires_in: timedelta) -> LabStorageLease:
    return LabStorageLease(
        id="lease-1",
        resource_kind="package",
        resource_id=record_id,
        operation_kind="lab_package_prepare",
        owner_id="lpa-1",
        expires_at=datetime.now(UTC).replace(tzinfo=None) + expires_in,
    )


def test_a_package_with_no_live_operation_can_be_deleted(in_memory_session_scope, service, tmp_path: Path):
    with in_memory_session_scope() as session:
        record = service.import_path(_write(tmp_path, _unsigned()), session).record
        archive = Path(record.archive_path)
        session.commit()

        service.delete_package(record.id, session)
        session.commit()

        assert session.get(LabPackage, record.id) is None

    assert not archive.exists()


def test_a_package_a_live_operation_leases_cannot_be_deleted(in_memory_session_scope, service, tmp_path: Path):
    with in_memory_session_scope() as session:
        record = service.import_path(_write(tmp_path, _unsigned()), session).record
        session.add(_lease(record.id, expires_in=timedelta(hours=1)))
        session.commit()

        with pytest.raises(PackageDeletionConflictError, match="leased by an active operation"):
            service.delete_package(record.id, session)


def test_an_expired_lease_does_not_block_deletion(in_memory_session_scope, service, tmp_path: Path):
    with in_memory_session_scope() as session:
        record = service.import_path(_write(tmp_path, _unsigned()), session).record
        session.add(_lease(record.id, expires_in=-timedelta(minutes=1)))
        session.commit()

        service.delete_package(record.id, session)
        session.commit()

        assert session.get(LabPackage, record.id) is None


def test_deleting_an_unknown_package_is_refused(in_memory_session_scope, service):
    with in_memory_session_scope() as session:
        with pytest.raises(Exception):
            service.delete_package("lp-absent", session)
