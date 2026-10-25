from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from astinuslab.lab_packages.content_store import ContentStore
from astinuslab.lab_packages.export_service import (
    ExportVersionConflict,
    build_locked_definition,
    export_locked,
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
from astinuslab.lab_packages.verification_service import (
    VerificationError,
    run_verification,
    save_draft,
    start_verification,
)


def _definition(*, package_id: str = "export-service-lab", version: str = "1.0.0") -> LabDefinition:
    resources = ResourceRecommendations(
        minimum=HostResources(cpu_threads=1, memory_mb=512, free_disk_bytes=1),
        recommended=HostResources(cpu_threads=1, memory_mb=512, free_disk_bytes=1),
    )
    return LabDefinition(
        package_id=package_id,
        version=version,
        title="Export service lab",
        creator="Creator",
        license="MIT",
        reproducibility="unlocked",
        resources=resources,
        networks=(LabNetwork(id="isolated", subnet_cidr="192.0.2.0/29", reachability="isolated"),),
        vms=(
            LabVM(
                id="target",
                role="target",
                image_id="ubuntu",
                vcpus=1,
                memory_mb=512,
                disk_gb=1,
                network_ids=("isolated",),
                base_image=LabImageSpec(
                    image_id="ubuntu",
                    sha256="b" * 64,
                    size_bytes=1,
                    media_type="application/x-qcow2",
                ),
            ),
        ),
        experience=LabExperience(),
    )


def test_clean_run_persists_attestation_and_locked_definition(in_memory_session_scope, tmp_path: Path, monkeypatch):
    from astinuslab.core import fake_mode

    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    monkeypatch.setattr(fake_mode, "FAKE_PLAYGROUND", False)
    definition = _definition()
    base_path = tmp_path / "ubuntu.qcow2"
    base_path.write_bytes(b"catalog base image")
    from astinuslab.lab_packages.export_service import image_spec_from_catalog

    catalog_spec = image_spec_from_catalog("ubuntu", {"path": str(base_path)})
    definition = definition.model_copy(
        update={"vms": (definition.vms[0].model_copy(update={"base_image": catalog_spec}),)}
    )
    monkeypatch.setattr(
        "astinuslab.lab_packages.verification_service.orchestrator.lookup_playground_image",
        lambda _image_id, *, role="target": {"path": str(base_path)},
    )
    with in_memory_session_scope() as session:
        draft = save_draft(session, definition, runbook="# Runbook\n")
        attempt = start_verification(session, draft_id=draft.id)
        result = run_verification(
            session,
            attempt.id,
            content_store=ContentStore(tmp_path / "content"),
            captured_layers={"target": b"clean layer bytes"},
            readiness_checker=lambda _vm_id, _probes: True,
        )
        assert result.locked_definition.reproducibility == "locked"
        assert result.locked_definition.vms[0].boot_layer is not None
        assert result.attestation_id


def test_locked_builder_requires_redistribution_for_embedded_layer():
    definition = _definition()
    content = b"clean layer bytes"
    reference = AssetReference(
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        media_type="application/x-qcow2",
        embedded=True,
        license="MIT",
    )
    with pytest.raises(ValueError, match="redistribution"):
        build_locked_definition(definition, boot_layers={"target": reference})


@pytest.mark.parametrize("runtime_facts", [{"architecture": "aarch64"}, {"firmware": "unsupported"}])
def test_catalog_provenance_rejects_unsupported_runtime_facts(tmp_path, runtime_facts):
    from astinuslab.lab_packages.export_service import PackageExportError, image_spec_from_catalog

    base_path = tmp_path / "base.qcow2"
    base_path.write_bytes(b"catalog base bytes")

    with pytest.raises(PackageExportError) as failure:
        image_spec_from_catalog("unsupported-base", {"path": str(base_path), **runtime_facts})

    assert failure.value.code == "unsupported_base_image"


def test_locked_builder_rejects_ambiguous_network_reachability():
    definition = _definition().model_copy(
        update={"networks": (_definition().networks[0].model_copy(update={"reachability": None}),)}
    )
    content = b"clean layer bytes"
    reference = AssetReference(
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        media_type="application/x-qcow2",
        embedded=True,
        license="MIT",
        redistribution_confirmed=True,
    )
    with pytest.raises(ValueError, match="reachability"):
        build_locked_definition(definition, boot_layers={"target": reference}, redistribution_confirmed=True)


def test_clean_run_refuses_without_production_readiness_evaluator(in_memory_session_scope, tmp_path: Path, monkeypatch):
    from astinuslab.core import fake_mode

    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    monkeypatch.setattr(fake_mode, "FAKE_PLAYGROUND", False)
    with in_memory_session_scope() as session:
        draft = save_draft(session, _definition(), runbook="# Runbook\n")
        attempt = start_verification(session, draft_id=draft.id)
        with pytest.raises(VerificationError, match="readiness evaluator"):
            run_verification(
                session,
                attempt.id,
                content_store=ContentStore(tmp_path / "content"),
                captured_layers={"target": b"clean layer bytes"},
            )


def test_locked_export_reserves_final_identity_and_streams_file(in_memory_session_scope, tmp_path: Path, monkeypatch):
    from astinuslab.core import fake_mode

    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    monkeypatch.setattr(fake_mode, "FAKE_PLAYGROUND", False)
    definition = _definition()
    content = b"clean layer bytes"
    reference = AssetReference(
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        media_type="application/x-qcow2",
        embedded=True,
        license="MIT",
        redistribution_confirmed=True,
    )
    locked = build_locked_definition(
        definition,
        boot_layers={"target": reference},
        redistribution_confirmed=True,
    )
    store = ContentStore(tmp_path / "content")
    store.put(content, sha256=reference.sha256, size_bytes=reference.size_bytes)
    with in_memory_session_scope() as session:
        artifact = export_locked(
            session,
            definition=locked,
            runbook="# Runbook\n",
            content_store=store,
            package_root=tmp_path / "packages",
        )
        assert artifact.path.is_file()
        assert artifact.path.read_bytes()
        with pytest.raises(ExportVersionConflict, match="finalized"):
            export_locked(
                session,
                definition=locked,
                runbook="# Runbook\n",
                content_store=store,
                package_root=tmp_path / "packages",
            )
