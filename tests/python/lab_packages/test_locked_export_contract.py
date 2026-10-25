"""Preconditions for producing a locked package.

A locked package is a reproducibility claim, so the export path refuses
everything that would make the claim untrue: an unlocked definition, a VM with
no attested clean layer or no base-image provenance, an attestation written
for a different definition, embedding without an explicit redistribution
declaration, and any export attempted while fake mode is active — synthetic
evidence must never leave the host as a reproducible package.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from astinuslab.core import fake_mode
from astinuslab.db.models import LabPackage, LabPackageExportAttempt
from astinuslab.lab_packages.canonical import canonical_sha256
from astinuslab.lab_packages.content_store import ContentStore
from astinuslab.lab_packages.export_service import (
    ExportVersionConflict,
    PackageExportError,
    build_locked_definition,
    export_locked,
    image_spec_from_catalog,
)
from astinuslab.lab_packages.models import (
    AssetReference,
    HostResources,
    LabDefinition,
    LabExperience,
    LabImageSpec,
    LabNetwork,
    LabVM,
    ResourceRecommendations,
)

pytestmark = pytest.mark.unit

LAYER = b"clean layer bytes"
LAYER_DIGEST = hashlib.sha256(LAYER).hexdigest()


@pytest.fixture(autouse=True)
def real_mode(monkeypatch):
    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    monkeypatch.setattr(fake_mode, "FAKE_PLAYGROUND", False)


def _base_image() -> LabImageSpec:
    return LabImageSpec(
        image_id="ubuntu-24.04",
        architecture="x86_64",
        sha256="b" * 64,
        size_bytes=1024,
        media_type="application/x-qcow2",
    )


def _definition(*, vm_ids: tuple[str, ...] = ("target",)) -> LabDefinition:
    return LabDefinition(
        package_id="locked-lab",
        version="1.0.0",
        title="Locked lab",
        creator="Researcher",
        license="MIT",
        reproducibility="unlocked",
        resources=ResourceRecommendations(
            minimum=HostResources(cpu_threads=2, memory_mb=4096, free_disk_bytes=40 * 1024**3),
            recommended=HostResources(cpu_threads=4, memory_mb=8192, free_disk_bytes=40 * 1024**3),
        ),
        networks=(LabNetwork(id="isolated", subnet_cidr="192.0.2.0/29", reachability="isolated"),),
        vms=tuple(
            LabVM(
                id=vm_id,
                role="target",
                image_id="ubuntu-24.04",
                vcpus=2,
                memory_mb=2048,
                disk_gb=10,
                network_ids=("isolated",),
                base_image=_base_image(),
            )
            for vm_id in vm_ids
        ),
        experience=LabExperience(),
    )


def _layer(*, embedded: bool = True, confirmed: bool = True) -> AssetReference:
    return AssetReference(
        sha256=LAYER_DIGEST,
        size_bytes=len(LAYER),
        media_type="application/x-qcow2",
        embedded=embedded,
        license="MIT",
        redistribution_confirmed=confirmed,
    )


@pytest.fixture()
def store(tmp_path: Path) -> ContentStore:
    created = ContentStore(tmp_path / "content")
    created.put(LAYER, sha256=LAYER_DIGEST, size_bytes=len(LAYER))
    return created


# --- build_locked_definition ---------------------------------------------------


def test_a_locked_definition_binds_its_layers_and_provenance():
    locked = build_locked_definition(_definition(), boot_layers={"target": _layer()}, redistribution_confirmed=True)

    assert locked.reproducibility == "locked"
    assert locked.vms[0].effective_boot_layer is not None
    assert locked.vms[0].base_image is not None


def test_locking_without_any_layer_is_refused():
    with pytest.raises(PackageExportError, match="requires a clean boot layer") as caught:
        build_locked_definition(_definition(), boot_layers={})

    assert caught.value.code == "boot_layer_missing"


def test_locking_a_vm_with_no_layer_of_its_own_is_refused():
    with pytest.raises(PackageExportError, match="no clean layer for VM") as caught:
        build_locked_definition(
            _definition(vm_ids=("target", "attacker")),
            boot_layers={"target": _layer()},
            redistribution_confirmed=True,
        )

    assert caught.value.code == "boot_layer_missing"


def test_locking_a_vm_with_no_base_image_provenance_is_refused():
    bare = _definition().model_copy(update={"vms": (_definition().vms[0].model_copy(update={"base_image": None}),)})

    with pytest.raises(PackageExportError, match="no base image provenance") as caught:
        build_locked_definition(bare, boot_layers={"target": _layer()}, redistribution_confirmed=True)

    assert caught.value.code == "base_image_missing"


def test_embedding_without_a_redistribution_declaration_is_refused():
    with pytest.raises(PackageExportError, match="redistribution confirmation") as caught:
        build_locked_definition(_definition(), boot_layers={"target": _layer()}, redistribution_confirmed=False)

    assert caught.value.code == "redistribution_unconfirmed"


def test_an_unembedded_layer_is_marked_embedded_when_embedding():
    locked = build_locked_definition(
        _definition(),
        boot_layers={"target": _layer(embedded=False, confirmed=False)},
        redistribution_confirmed=True,
    )

    bound = locked.vms[0].effective_boot_layer
    assert bound is not None
    assert bound.embedded is True
    assert bound.redistribution_confirmed is True


def test_an_embedded_layer_is_unmarked_when_not_embedding():
    locked = build_locked_definition(
        _definition(),
        boot_layers={"target": _layer()},
        embed_layers=False,
        redistribution_confirmed=False,
    )

    bound = locked.vms[0].effective_boot_layer
    assert bound is not None
    assert bound.embedded is False


def test_a_locked_definition_can_override_its_license():
    locked = build_locked_definition(
        _definition(),
        boot_layers={"target": _layer()},
        redistribution_confirmed=True,
        license="CC-BY-4.0",
    )

    assert locked.license == "CC-BY-4.0"


# --- image_spec_from_catalog ---------------------------------------------------


def test_a_catalog_entry_becomes_a_base_image_spec(tmp_path: Path):
    image = tmp_path / "ubuntu.qcow2"
    image.write_bytes(LAYER)

    spec = image_spec_from_catalog("ubuntu-24.04", {"path": str(image), "architecture": "x86_64"})

    assert spec.image_id == "ubuntu-24.04"
    assert spec.architecture == "x86_64"
    assert spec.sha256 == LAYER_DIGEST
    assert spec.size_bytes == len(LAYER)


def test_a_catalog_entry_defaults_its_runtime_facts(tmp_path: Path):
    image = tmp_path / "ubuntu.qcow2"
    image.write_bytes(LAYER)

    spec = image_spec_from_catalog("ubuntu-24.04", {"path": str(image)})

    assert spec.architecture == "x86_64"
    assert spec.firmware == "bios"


@pytest.mark.parametrize("entry", [{}, {"path": ""}, {"path": 42}])
def test_a_catalog_entry_with_no_usable_path_is_refused(entry):
    with pytest.raises(PackageExportError, match="base image is unavailable") as caught:
        image_spec_from_catalog("ubuntu-24.04", entry)

    assert caught.value.code == "base_image_unavailable"


def test_a_catalog_entry_whose_bytes_are_gone_is_refused(tmp_path: Path):
    with pytest.raises(PackageExportError):
        image_spec_from_catalog("ubuntu-24.04", {"path": str(tmp_path / "absent.qcow2")})


@pytest.mark.parametrize(
    "facts",
    [{"architecture": "aarch64"}, {"firmware": "coreboot"}, {"architecture": "riscv64"}],
)
def test_a_catalog_entry_with_unusable_runtime_facts_is_refused(tmp_path: Path, facts):
    image = tmp_path / "ubuntu.qcow2"
    image.write_bytes(LAYER)

    with pytest.raises(PackageExportError, match="unsupported runtime facts") as caught:
        image_spec_from_catalog("ubuntu-24.04", {"path": str(image), **facts})

    assert caught.value.code == "unsupported_base_image"


# --- export_locked preconditions -----------------------------------------------


def _locked() -> LabDefinition:
    return build_locked_definition(_definition(), boot_layers={"target": _layer()}, redistribution_confirmed=True)


def test_an_unlocked_definition_cannot_be_exported_as_locked(in_memory_session_scope, store, tmp_path):
    with in_memory_session_scope() as session:
        with pytest.raises(PackageExportError, match="only a locked definition") as caught:
            export_locked(
                session,
                definition=_definition(),
                runbook="# Runbook\n",
                content_store=store,
                package_root=tmp_path / "packages",
            )

    assert caught.value.code == "not_locked"


def test_locked_export_is_unavailable_in_fake_mode(in_memory_session_scope, store, tmp_path, monkeypatch):
    monkeypatch.setattr(fake_mode, "FAKE_PLAYGROUND", True)

    with in_memory_session_scope() as session:
        with pytest.raises(PackageExportError, match="unavailable in fake mode") as caught:
            export_locked(
                session,
                definition=_locked(),
                runbook="# Runbook\n",
                content_store=store,
                package_root=tmp_path / "packages",
            )

    assert caught.value.code == "fake_mode_unsupported"


def test_an_attestation_for_another_definition_is_refused(in_memory_session_scope, store, tmp_path):
    from astinuslab.db.models import LabPackageAttestation

    attestation = LabPackageAttestation(
        id="lpat-1",
        input_draft_sha256="a" * 64,
        locked_definition_sha256="d" * 64,
        input_digests_json="{}",
        runtime_json="{}",
        result_json="{}",
    )

    with in_memory_session_scope() as session:
        with pytest.raises(PackageExportError, match="attestation does not match") as caught:
            export_locked(
                session,
                definition=_locked(),
                runbook="# Runbook\n",
                content_store=store,
                attestation=attestation,
                package_root=tmp_path / "packages",
            )

    assert caught.value.code == "attestation_mismatch"


def test_a_matching_attestation_is_accepted(in_memory_session_scope, store, tmp_path):
    from astinuslab.db.models import LabPackageAttestation

    locked = _locked()
    attestation = LabPackageAttestation(
        id="lpat-1",
        input_draft_sha256="a" * 64,
        locked_definition_sha256=canonical_sha256(locked),
        input_digests_json="{}",
        runtime_json='{"fake_mode": {"playground": false}}',
        result_json="{}",
    )

    with in_memory_session_scope() as session:
        session.add(attestation)
        session.commit()

        artifact = export_locked(
            session,
            definition=locked,
            runbook="# Runbook\n",
            content_store=store,
            attestation=attestation,
            package_root=tmp_path / "packages",
        )

        assert artifact.path.is_file()
        assert artifact.path.read_bytes()


def test_a_version_already_registered_cannot_be_exported_again(in_memory_session_scope, store, tmp_path):
    locked = _locked()

    with in_memory_session_scope() as session:
        session.add(
            LabPackage(
                id="lp-existing",
                package_id="locked-lab",
                version="1.0.0",
                title="Locked lab",
                creator="Researcher",
                license="MIT",
                reproducibility="locked",
                definition_json="{}",
                manifest_json="{}",
                manifest_sha256="e" * 64,
                archive_sha256="f" * 64,
                archive_path=str(tmp_path / "existing.astinuslab"),
                archive_size_bytes=1,
            )
        )
        session.commit()

        with pytest.raises(ExportVersionConflict, match="already registered"):
            export_locked(
                session,
                definition=locked,
                runbook="# Runbook\n",
                content_store=store,
                package_root=tmp_path / "packages",
            )


def test_a_completed_export_finalizes_its_journal_row(in_memory_session_scope, store, tmp_path):
    locked = _locked()

    with in_memory_session_scope() as session:
        export_locked(
            session,
            definition=locked,
            runbook="# Runbook\n",
            content_store=store,
            package_root=tmp_path / "packages",
        )

        attempt = session.query(LabPackageExportAttempt).filter_by(package_id="locked-lab").one()
        assert attempt.state == "completed"
        assert attempt.archive_sha256
        assert attempt.expires_at is None


def test_supporting_files_travel_with_the_locked_archive(in_memory_session_scope, store, tmp_path):
    from astinuslab.lab_packages.archive import inspect_archive

    locked = _locked()

    with in_memory_session_scope() as session:
        artifact = export_locked(
            session,
            definition=locked,
            runbook="# Runbook\n",
            content_store=store,
            supporting_files={"experience/notes.md": b"# Notes\n"},
            package_root=tmp_path / "packages",
        )

    inspection = inspect_archive(artifact.path)
    assert inspection.payloads["experience/notes.md"] == b"# Notes\n"
