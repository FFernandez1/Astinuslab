"""Attestation recheck contract for locked export.

A locked package claims that two clean installations will boot identical
initial bytes. The only thing standing behind that claim is the attestation
written when a clean run completed, so it is rechecked at export time rather
than trusted because a row exists. Every refusal here is a way a package could
otherwise ship a reproducibility claim it cannot honour: an expired
attestation, one written for a different draft or a different locked
definition, one whose runtime facts say the run was synthetic, or a stored
result whose boot layers no longer match the definition they attest.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from astinuslab.db.models import LabDraft, LabPackageAttestation, LabVerificationAttempt
from astinuslab.lab_packages.canonical import canonical_sha256
from astinuslab.lab_packages.models import (
    AssetReference,
    HostResources,
    LabDefinition,
    LabExperience,
    LabNetwork,
    LabVM,
    ResourceRecommendations,
)
from astinuslab.lab_packages.verification_service import (
    VerificationError,
    verified_result,
    verify_attestation,
)

pytestmark = pytest.mark.unit

LAYER = b"clean boot layer bytes"
LAYER_DIGEST = hashlib.sha256(LAYER).hexdigest()
DRAFT_DIGEST = "a" * 64


def _layer_reference() -> AssetReference:
    return AssetReference(
        sha256=LAYER_DIGEST,
        size_bytes=len(LAYER),
        media_type="application/x-qcow2",
        license="CC-BY-4.0",
    )


def _definition(*, with_boot_layer: bool = True) -> LabDefinition:
    return LabDefinition(
        package_id="attested-lab",
        version="1.0.0",
        title="Attested lab",
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
                boot_layer=_layer_reference() if with_boot_layer else None,
            ),
        ),
        experience=LabExperience(),
    )


def _attestation(
    *,
    attestation_id: str = "lpat-1",
    draft_digest: str = DRAFT_DIGEST,
    definition: LabDefinition | None = None,
    runtime: object = None,
    expires_in: timedelta | None = timedelta(hours=1),
) -> LabPackageAttestation:
    target = definition if definition is not None else _definition()
    return LabPackageAttestation(
        id=attestation_id,
        input_draft_sha256=draft_digest,
        locked_definition_sha256=canonical_sha256(target),
        input_digests_json=json.dumps({}),
        runtime_json=json.dumps({"fake_mode": {"playground": False}}) if runtime is None else runtime,
        result_json=json.dumps({}),
        expires_at=None if expires_in is None else datetime.now(UTC).replace(tzinfo=None) + expires_in,
    )


# --- verify_attestation --------------------------------------------------------


def test_a_matching_attestation_is_accepted(in_memory_session_scope):
    definition = _definition()
    with in_memory_session_scope() as session:
        session.add(_attestation(definition=definition))
        session.commit()

        result = verify_attestation(session, attestation_id="lpat-1", draft_digest=DRAFT_DIGEST, definition=definition)

        assert result.id == "lpat-1"


def test_a_missing_attestation_is_refused(in_memory_session_scope):
    with in_memory_session_scope() as session, pytest.raises(VerificationError) as caught:
        verify_attestation(session, attestation_id="lpat-absent", draft_digest=DRAFT_DIGEST, definition=_definition())

    assert caught.value.code == "attestation_missing"


def test_an_expired_attestation_is_refused(in_memory_session_scope):
    definition = _definition()
    with in_memory_session_scope() as session:
        session.add(_attestation(definition=definition, expires_in=-timedelta(minutes=1)))
        session.commit()

        with pytest.raises(VerificationError) as caught:
            verify_attestation(session, attestation_id="lpat-1", draft_digest=DRAFT_DIGEST, definition=definition)

    assert caught.value.code == "attestation_expired"


def test_an_attestation_without_an_expiry_is_refused(in_memory_session_scope):
    definition = _definition()
    with in_memory_session_scope() as session:
        session.add(_attestation(definition=definition, expires_in=None))
        session.commit()

        with pytest.raises(VerificationError) as caught:
            verify_attestation(session, attestation_id="lpat-1", draft_digest=DRAFT_DIGEST, definition=definition)

    assert caught.value.code == "attestation_expired"


@pytest.mark.parametrize("runtime", ["not json at all", json.dumps(["a", "list"]), json.dumps("a string")])
def test_unreadable_runtime_facts_are_refused(in_memory_session_scope, runtime):
    definition = _definition()
    with in_memory_session_scope() as session:
        session.add(_attestation(definition=definition, runtime=runtime))
        session.commit()

        with pytest.raises(VerificationError) as caught:
            verify_attestation(session, attestation_id="lpat-1", draft_digest=DRAFT_DIGEST, definition=definition)

    assert caught.value.code == "invalid_attestation"


def test_a_run_recorded_under_fake_mode_cannot_be_exported(in_memory_session_scope):
    definition = _definition()
    with in_memory_session_scope() as session:
        session.add(_attestation(definition=definition, runtime=json.dumps({"fake_mode": {"playground": True}})))
        session.commit()

        with pytest.raises(VerificationError) as caught:
            verify_attestation(session, attestation_id="lpat-1", draft_digest=DRAFT_DIGEST, definition=definition)

    assert caught.value.code == "fake_mode_unsupported"


def test_an_attestation_for_another_draft_is_refused(in_memory_session_scope):
    definition = _definition()
    with in_memory_session_scope() as session:
        session.add(_attestation(definition=definition, draft_digest="b" * 64))
        session.commit()

        with pytest.raises(VerificationError, match="does not match the saved draft") as caught:
            verify_attestation(session, attestation_id="lpat-1", draft_digest=DRAFT_DIGEST, definition=definition)

    assert caught.value.code == "attestation_mismatch"


def test_an_attestation_for_another_definition_is_refused(in_memory_session_scope):
    with in_memory_session_scope() as session:
        session.add(_attestation(definition=_definition(with_boot_layer=False)))
        session.commit()

        with pytest.raises(VerificationError, match="does not match the locked definition") as caught:
            verify_attestation(session, attestation_id="lpat-1", draft_digest=DRAFT_DIGEST, definition=_definition())

    assert caught.value.code == "attestation_mismatch"


# --- verified_result -----------------------------------------------------------


def _stored_result(definition: LabDefinition, *, refs: dict | None = None) -> str:
    layers = refs if refs is not None else {"target": _layer_reference().model_dump(mode="json")}
    return json.dumps(
        {
            "locked_definition": definition.model_dump(mode="json"),
            "boot_layers": layers,
            "attestation_id": "lpat-1",
        }
    )


def _seed_verified(session, *, result_json: str, definition: LabDefinition) -> None:
    session.add(
        LabDraft(
            id="lpd-1",
            definition_json=definition.model_dump_json(),
            definition_sha256=DRAFT_DIGEST,
            runbook="# Runbook\n",
        )
    )
    session.add(_attestation(definition=definition))
    session.add(
        LabVerificationAttempt(
            id="lpv-1",
            draft_id="lpd-1",
            definition_sha256=DRAFT_DIGEST,
            state="verified",
            result_json=result_json,
        )
    )


def test_a_completed_verification_is_reloaded_with_its_layers(in_memory_session_scope):
    definition = _definition()
    with in_memory_session_scope() as session:
        _seed_verified(session, result_json=_stored_result(definition), definition=definition)
        session.commit()

        result = verified_result(session, "lpv-1")

        assert result.attempt_id == "lpv-1"
        assert result.locked_definition.package_id == "attested-lab"
        assert result.boot_layers["target"].sha256 == LAYER_DIGEST


def test_an_incomplete_verification_cannot_be_exported(in_memory_session_scope):
    definition = _definition()
    with in_memory_session_scope() as session:
        session.add(
            LabDraft(
                id="lpd-1",
                definition_json=definition.model_dump_json(),
                definition_sha256=DRAFT_DIGEST,
                runbook="# Runbook\n",
            )
        )
        session.add(
            LabVerificationAttempt(id="lpv-1", draft_id="lpd-1", definition_sha256=DRAFT_DIGEST, state="running")
        )
        session.commit()

        with pytest.raises(VerificationError) as caught:
            verified_result(session, "lpv-1")

    assert caught.value.code == "verification_incomplete"


@pytest.mark.parametrize(
    "result_json",
    ["not json", json.dumps({"boot_layers": {}}), json.dumps({"locked_definition": {"bad": True}})],
)
def test_a_corrupt_stored_result_is_refused(in_memory_session_scope, result_json):
    definition = _definition()
    with in_memory_session_scope() as session:
        _seed_verified(session, result_json=result_json, definition=definition)
        session.commit()

        with pytest.raises(VerificationError) as caught:
            verified_result(session, "lpv-1")

    assert caught.value.code == "invalid_result"


def test_a_result_whose_layers_disagree_with_the_definition_is_refused(in_memory_session_scope):
    definition = _definition()
    other = AssetReference(
        sha256="c" * 64,
        size_bytes=99,
        media_type="application/x-qcow2",
        license="CC-BY-4.0",
    )
    with in_memory_session_scope() as session:
        _seed_verified(
            session,
            result_json=_stored_result(definition, refs={"target": other.model_dump(mode="json")}),
            definition=definition,
        )
        session.commit()

        with pytest.raises(VerificationError, match="attested boot layers") as caught:
            verified_result(session, "lpv-1")

    assert caught.value.code == "attestation_mismatch"


def test_a_result_missing_a_layer_for_a_vm_is_refused(in_memory_session_scope):
    definition = _definition()
    with in_memory_session_scope() as session:
        _seed_verified(session, result_json=_stored_result(definition, refs={}), definition=definition)
        session.commit()

        with pytest.raises(VerificationError, match="attested boot layers") as caught:
            verified_result(session, "lpv-1")

    assert caught.value.code == "attestation_mismatch"
