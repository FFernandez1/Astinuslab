"""Exporting a completed verification long after its authoring session is gone.

A clean-run verification is the durable record of what was proved. The creator
must be able to turn it into an archive without the Playground session that
authored it and without the browser state that started it, so this route takes
only consent flags: identity, resources, experience and runbook all come from
the attested draft. Every attestation and custody check of the locked export
still applies, and a request that cannot satisfy them is refused rather than
narrowed.
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
RUNBOOK = "# Verified runbook\n"
DRAFT_DIGEST = "c" * 64
EXPORT_PATH = "/api/v1/lab-packages/verifications/lpv-1/export"


@pytest.fixture(autouse=True)
def real_mode(monkeypatch):
    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    monkeypatch.setattr(fake_mode, "FAKE_PLAYGROUND", False)


@pytest.fixture()
def service(package_client, monkeypatch, tmp_path: Path) -> LabPackageService:
    created = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    created.content_store.put(LAYER, sha256=LAYER_DIGEST, size_bytes=len(LAYER))
    monkeypatch.setattr("astinuslab.lab_packages.api.default_package_service", lambda: created)
    return created


def _definition() -> LabDefinition:
    return LabDefinition(
        package_id="verified-lab",
        version="2.1.0",
        title="Verified lab",
        description="Exported from a completed clean run.",
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
                    sha256="d" * 64,
                    size_bytes=1024,
                    media_type="application/x-qcow2",
                ),
            ),
        ),
        experience=LabExperience(objectives=("Find the artifact",)),
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
    state: str = "verified",
    runbook: str = RUNBOOK,
    attested_definition: LabDefinition | None = None,
    result_definition: LabDefinition | None = None,
) -> None:
    locked = _locked()
    attested = attested_definition or locked
    session.add(
        LabDraft(
            id="ld-1",
            definition_json=locked.model_dump_json(),
            definition_sha256=DRAFT_DIGEST,
            runbook=runbook,
            supporting_files_json="{}",
        )
    )
    session.add(
        LabPackageAttestation(
            id="lpat-1",
            input_draft_sha256=DRAFT_DIGEST,
            locked_definition_sha256=canonical_sha256(attested),
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
            state=state,
            # The authoring session is deliberately absent: a verified attempt
            # outlives the Playground session that produced it.
            authored_session_id=None,
            result_json=json.dumps(
                {
                    "locked_definition": (result_definition or locked).model_dump(mode="json"),
                    "boot_layers": {"target": _layer_reference().model_dump(mode="json")},
                    "attestation_id": "lpat-1",
                }
            )
            if state == "verified"
            else None,
        )
    )
    session.commit()


# --- the exportable record -----------------------------------------------------


@pytest.mark.parametrize(
    "case, expected",
    [
        ("future", "unexpired"),
        ("expired", "expired"),
        ("boundary", "expired"),
        ("no-expiry", "expired"),
        ("missing", "unavailable"),
        ("malformed", "unavailable"),
        ("runtime", "unavailable"),
        ("running", "unavailable"),
    ],
)
def test_verification_projection_binds_package_and_exact_attestation_deadline(
    package_client, service, in_memory_session_scope, monkeypatch, case, expected
):
    now = datetime(2026, 9, 6, 10)
    monkeypatch.setattr("astinuslab.lab_packages.verification_service._now", lambda: now)
    with in_memory_session_scope() as session:
        _seed(session, state="running" if case == "running" else "verified")
        attestation = session.get(LabPackageAttestation, "lpat-1")
        expiry: datetime | None = now + timedelta(hours=1)
        if case == "expired":
            expiry = now - timedelta(seconds=1)
        if case == "boundary":
            expiry = now
        if case == "no-expiry":
            expiry = None
        attestation.expires_at = expiry
        if case == "missing":
            session.delete(attestation)
        if case == "runtime":
            attestation.runtime_json = "not json"
        if case == "malformed":
            session.get(LabVerificationAttempt, "lpv-1").result_json = '{"attestation_id": []}'
        session.commit()
    response = package_client.get("/api/v1/lab-packages/verifications/lpv-1")
    assert response.status_code == 200
    record = response.json()
    assert (record["package_id"], record["package_version"], record["package_title"]) == (
        "verified-lab",
        "2.1.0",
        "Verified lab",
    )
    assert record["attestation_status"] == expected
    assert record["attestation_expires_at"] == (
        expiry.isoformat() + "Z" if expiry is not None and expected != "unavailable" else None
    )
    activity = package_client.get("/api/v1/lab-packages/activity").json()["verifications"]
    assert activity == [record]


def test_a_verified_record_exports_without_its_authoring_session(package_client, service, in_memory_session_scope):
    with in_memory_session_scope() as session:
        _seed(session)

    response = package_client.post(EXPORT_PATH, json={"embed_assets": True, "redistribution_confirmed": True})

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/vnd.astinuslab.lab+zip"
    assert "verified-lab-2.1.0.astinuslab" in response.headers["content-disposition"]
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        # Identity and runbook come from what was verified, not from the caller.
        assert archive.read("experience/runbook.md").decode() == RUNBOOK
        manifest = json.loads(archive.read("lab.json"))
        assert manifest["package_id"] == "verified-lab"
        assert manifest["reproducibility"] == "locked"


def test_the_verified_attempt_stays_listed_in_activity(package_client, in_memory_session_scope):
    """A reload must find the completed verification again, not lose it."""

    with in_memory_session_scope() as session:
        _seed(session)

    activity = package_client.get("/api/v1/lab-packages/activity")

    assert activity.status_code == 200, activity.text
    listed = activity.json()["verifications"]
    assert [item["id"] for item in listed] == ["lpv-1"]
    assert listed[0]["state"] == "verified"


# --- refusals ------------------------------------------------------------------


def test_an_incomplete_verification_cannot_be_exported(package_client, service, in_memory_session_scope):
    with in_memory_session_scope() as session:
        _seed(session, state="running")

    response = package_client.post(EXPORT_PATH, json={"embed_assets": True, "redistribution_confirmed": True})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "verification_incomplete"


def test_a_tampered_result_is_refused_by_the_attestation_check(package_client, service, in_memory_session_scope):
    """Editing the stored result cannot smuggle a different lab under the attestation."""

    tampered = _locked().model_copy(update={"title": "Renamed after verification"})
    with in_memory_session_scope() as session:
        _seed(session, result_definition=tampered)

    response = package_client.post(EXPORT_PATH, json={"embed_assets": True, "redistribution_confirmed": True})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "attestation_mismatch"


def test_embedding_without_redistribution_consent_is_refused(package_client, service, in_memory_session_scope):
    with in_memory_session_scope() as session:
        _seed(session)

    response = package_client.post(EXPORT_PATH, json={"embed_assets": True, "redistribution_confirmed": False})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "redistribution_unconfirmed"


def test_the_default_request_never_silently_drops_attested_layers(package_client, service, in_memory_session_scope):
    """Embedding is opt-in, so the default request asks for no embedded bytes.

    This attestation covers embedded clean-start layers, so the export is
    refused with the code that tells the creator to opt in rather than shipping
    a package that claims a clean start it no longer carries.
    """

    with in_memory_session_scope() as session:
        _seed(session)

    response = package_client.post(EXPORT_PATH, json={})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "embedding_required"


def test_the_route_accepts_only_consent_flags(package_client, service, in_memory_session_scope):
    """Identity is not a request field: it belongs to the verified definition."""

    with in_memory_session_scope() as session:
        _seed(session)

    response = package_client.post(EXPORT_PATH, json={"package_id": "other-lab"})

    assert response.status_code == 422


def test_a_missing_verification_is_reported_as_such(package_client, service):
    response = package_client.post(
        "/api/v1/lab-packages/verifications/lpv-missing/export",
        json={"embed_assets": True, "redistribution_confirmed": True},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "verification_not_found"


@pytest.mark.parametrize("route", ["verify-clean-run", "verifications"])
@pytest.mark.parametrize("runbook", ["", " \n\t"])
def test_verification_rejects_blank_runbook_before_freezing(
    package_client, in_memory_session_scope, monkeypatch, route, runbook
):
    with in_memory_session_scope() as session:
        _seed(session, state="failed", runbook=runbook)

    def must_not_freeze(*args, **kwargs):
        pytest.fail("blank runbook reached the session-freezing operation")

    monkeypatch.setattr("astinuslab.lab_packages.api.start_verification", must_not_freeze)
    response = package_client.post(f"/api/v1/lab-packages/{route}", json={"draft_id": "ld-1"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "runbook_missing"


def test_completed_history_does_not_extend_attestation_expiry(package_client, service, in_memory_session_scope):
    with in_memory_session_scope() as session:
        _seed(session)
        session.get(LabPackageAttestation, "lpat-1").expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            seconds=1
        )
        session.commit()
    response = package_client.post(EXPORT_PATH, json={"embed_assets": True, "redistribution_confirmed": True})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "attestation_expired"
