"""The locked export route, from a completed verification to archive bytes.

This is the one path that turns a clean run into a package other people will
trust. It deliberately does not take the caller's word for the experience: the
runbook and supporting files come from the saved draft rather than the request,
and every supporting file is re-opened from the content store and re-hashed
while exporting. A request that disagrees with what was verified is refused,
never quietly exported under the verified identity.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest

from astinuslab.core import fake_mode
from astinuslab.db.models import LabDraft, LabPackageAttestation, LabVerificationAttempt
from astinuslab.lab_packages.canonical import canonical_sha256
from astinuslab.lab_packages.content_store import ContentStore
from astinuslab.lab_packages.export_service import build_locked_definition
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
from astinuslab.lab_packages.package_service import LabPackageService

pytestmark = pytest.mark.unit

LAYER = b"clean layer bytes"
LAYER_DIGEST = hashlib.sha256(LAYER).hexdigest()
NOTES = b"# Notes\n"
NOTES_DIGEST = hashlib.sha256(NOTES).hexdigest()
RUNBOOK = "# Verified runbook\n"
DRAFT_DIGEST = "a" * 64


@pytest.fixture(autouse=True)
def real_mode(monkeypatch):
    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    monkeypatch.setattr(fake_mode, "FAKE_PLAYGROUND", False)


@pytest.fixture()
def service(package_client, monkeypatch, tmp_path: Path) -> LabPackageService:
    created = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    created.content_store.put(LAYER, sha256=LAYER_DIGEST, size_bytes=len(LAYER))
    created.content_store.put(NOTES, sha256=NOTES_DIGEST, size_bytes=len(NOTES))
    monkeypatch.setattr("astinuslab.lab_packages.api.default_package_service", lambda: created)
    return created


def _definition() -> LabDefinition:
    return LabDefinition(
        package_id="exported-lab",
        version="1.0.0",
        title="Exported lab",
        creator="Researcher",
        license="MIT",
        reproducibility="unlocked",
        resources=ResourceRecommendations(
            minimum=HostResources(cpu_threads=2, memory_mb=4096, free_disk_bytes=40 * 1024**3),
            recommended=HostResources(cpu_threads=4, memory_mb=8192, free_disk_bytes=40 * 1024**3),
        ),
        networks=(LabNetwork(id="isolated", subnet_cidr="192.0.2.0/29", reachability="isolated"),),
        vms=(
            LabVM(
                id="target",
                role="target",
                image_id="ubuntu-24.04",
                vcpus=2,
                memory_mb=2048,
                disk_gb=10,
                network_ids=("isolated",),
                base_image=LabImageSpec(
                    image_id="ubuntu-24.04",
                    architecture="x86_64",
                    sha256="b" * 64,
                    size_bytes=1024,
                    media_type="application/x-qcow2",
                ),
            ),
        ),
        experience=LabExperience(),
    )


def _layer_reference() -> AssetReference:
    return AssetReference(
        sha256=LAYER_DIGEST,
        size_bytes=len(LAYER),
        media_type="application/x-qcow2",
        embedded=True,
        license="MIT",
        redistribution_confirmed=True,
    )


def _locked() -> LabDefinition:
    return build_locked_definition(
        _definition(), boot_layers={"target": _layer_reference()}, redistribution_confirmed=True
    )


def _seed(
    session,
    *,
    runbook: str = RUNBOOK,
    supporting_files_json: str = "{}",
    with_draft: bool = True,
    with_attestation: bool = True,
    locked: LabDefinition | None = None,
) -> None:
    definition = locked if locked is not None else _locked()
    if with_draft:
        session.add(
            LabDraft(
                id="ld-1",
                definition_json=definition.model_dump_json(),
                definition_sha256=DRAFT_DIGEST,
                runbook=runbook,
                supporting_files_json=supporting_files_json,
            )
        )
    if with_attestation:
        session.add(
            LabPackageAttestation(
                id="lpat-1",
                input_draft_sha256=DRAFT_DIGEST,
                locked_definition_sha256=canonical_sha256(definition),
                input_digests_json="{}",
                runtime_json=json.dumps({"fake_mode": {"playground": False}}),
                result_json="{}",
                expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1),
            )
        )
    session.add(
        LabVerificationAttempt(
            id="lpv-1",
            draft_id="ld-1",
            definition_sha256=DRAFT_DIGEST,
            state="verified",
            result_json=json.dumps(
                {
                    "locked_definition": definition.model_dump(mode="json"),
                    "boot_layers": {"target": _layer_reference().model_dump(mode="json")},
                    "attestation_id": "lpat-1",
                }
            ),
        )
    )
    session.commit()


def _body(**overrides) -> dict:
    body = {
        "package_id": "exported-lab",
        "version": "1.0.0",
        "title": "Exported lab",
        "creator": "Researcher",
        "license": "MIT",
        "runbook": RUNBOOK,
        "reproducibility": "locked",
        "verification_id": "lpv-1",
        "embed_assets": True,
        "redistribution_confirmed": True,
        "resources": {
            "minimum": {"cpu_threads": 2, "memory_mb": 4096, "free_disk_bytes": 40 * 1024**3},
            "recommended": {"cpu_threads": 4, "memory_mb": 8192, "free_disk_bytes": 40 * 1024**3},
        },
    }
    body.update(overrides)
    return body


def _post(package_client, **overrides):
    return package_client.post("/api/v1/lab-packages/export", json=_body(**overrides))


# --- the verified path ---------------------------------------------------------


def test_a_verified_run_exports_an_archive(package_client, service, in_memory_session_scope):
    with in_memory_session_scope() as session:
        _seed(session)

    response = _post(package_client)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/vnd.astinuslab.lab+zip"
    assert response.headers["X-AstinusLab-Definition-SHA256"]
    assert "exported-lab-1.0.0.astinuslab" in response.headers["content-disposition"]
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        assert "lab.json" in archive.namelist()
        assert archive.read("experience/runbook.md").decode() == RUNBOOK


def test_the_saved_supporting_files_travel_with_the_export(package_client, service, in_memory_session_scope):
    with in_memory_session_scope() as session:
        _seed(session, supporting_files_json=json.dumps({"experience/notes.md": NOTES_DIGEST}))

    response = _post(package_client)

    assert response.status_code == 200, response.text
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        assert archive.read("experience/notes.md") == NOTES


def test_an_attestation_with_embedded_layers_cannot_be_exported_unembedded(
    package_client, service, in_memory_session_scope
):
    """The attested layers are part of what was verified.

    Dropping them would ship a package claiming a clean start it no longer
    carries the bytes for, so the export is refused rather than narrowed.
    """

    with in_memory_session_scope() as session:
        _seed(session)

    response = _post(package_client, embed_assets=False, redistribution_confirmed=False)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "embedding_required"


# --- refusals ------------------------------------------------------------------


def test_an_export_naming_another_identity_is_refused(package_client, service, in_memory_session_scope):
    with in_memory_session_scope() as session:
        _seed(session)

    response = _post(package_client, package_id="other-lab")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "attestation_mismatch"


def test_an_export_naming_another_version_is_refused(package_client, service, in_memory_session_scope):
    with in_memory_session_scope() as session:
        _seed(session)

    response = _post(package_client, version="2.0.0")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "attestation_mismatch"


def test_a_runbook_that_differs_from_the_verified_draft_is_refused(package_client, service, in_memory_session_scope):
    with in_memory_session_scope() as session:
        _seed(session)

    response = _post(package_client, runbook="# A different runbook\n")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "attestation_mismatch"
    assert "runbook" in response.json()["detail"]["message"]


def test_an_export_whose_draft_is_gone_is_refused(package_client, service, in_memory_session_scope):
    with in_memory_session_scope() as session:
        _seed(session)
        session.delete(session.get(LabDraft, "ld-1"))
        session.commit()

    response = _post(package_client)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "draft_not_found"


def test_an_export_whose_attestation_is_gone_is_refused(package_client, service, in_memory_session_scope):
    with in_memory_session_scope() as session:
        _seed(session, with_attestation=False)

    response = _post(package_client)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] in {"attestation_missing", "attestation_mismatch"}


@pytest.mark.parametrize("stored", ["{not json", "[]", '"a string"'])
def test_unreadable_saved_supporting_references_are_refused(package_client, service, in_memory_session_scope, stored):
    with in_memory_session_scope() as session:
        _seed(session, supporting_files_json=stored)

    response = _post(package_client)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "invalid_draft"


def test_a_supporting_file_that_is_not_cached_is_refused(package_client, service, in_memory_session_scope):
    with in_memory_session_scope() as session:
        _seed(session, supporting_files_json=json.dumps({"experience/notes.md": "c" * 64}))

    response = _post(package_client)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "supporting_file_unavailable"


def test_a_malformed_supporting_reference_is_refused(package_client, service, in_memory_session_scope):
    with in_memory_session_scope() as session:
        _seed(session, supporting_files_json=json.dumps({"experience/notes.md": 42}))

    response = _post(package_client)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "invalid_supporting_file"


def test_exporting_the_same_verified_identity_twice_is_a_conflict(package_client, service, in_memory_session_scope):
    with in_memory_session_scope() as session:
        _seed(session)
    assert _post(package_client).status_code == 200

    response = _post(package_client)

    assert response.status_code == 409
