"""Clean-run guards: checkpoints, capture sources, and the frozen session.

A locked package's reproducibility claim is only as good as the run that
produced it, so verification refuses anything that would let an unattested or
partly-attested run reach an attestation: a checkpoint that is not a typed
guest operation, a checkpoint with no evaluator behind it, an authored
Playground session that is no longer frozen, or a capture that cannot reach
the VM it claims to have captured.
"""

from __future__ import annotations

import pytest

from astinuslab.db.models import LabDraft, LabVerificationAttempt, PlaygroundSession
from astinuslab.lab_packages.content_store import ContentStore
from astinuslab.lab_packages.models import (
    HostResources,
    LabDefinition,
    LabExperience,
    LabNetwork,
    LabVM,
    ResourceRecommendations,
)
from astinuslab.lab_packages.verification_service import (
    VerificationError,
    _capture_sources,
    _evaluate_checkpoints,
    _require_frozen_authored_session,
)
from astinuslab.playground import repositories

pytestmark = pytest.mark.unit

CHECKPOINT = {
    "operation": "observe",
    "scope": "guest",
    "vm_id": "target",
    "path": "flag.json",
}


def _definition(*, checkpoints: tuple = ()) -> LabDefinition:
    return LabDefinition(
        package_id="guarded-lab",
        version="1.0.0",
        title="Guarded lab",
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
        experience=LabExperience(checkpoints=checkpoints),
    )


def _draft(definition: LabDefinition, *, authored_session_id: str | None = None) -> LabDraft:
    return LabDraft(
        id="ld-1",
        definition_json=definition.model_dump_json(),
        definition_sha256="a" * 64,
        runbook="# Runbook\n",
        authored_session_id=authored_session_id,
    )


# --- _evaluate_checkpoints -----------------------------------------------------


def test_a_definition_without_checkpoints_evaluates_to_nothing():
    assert _evaluate_checkpoints(_definition(), checkpoint_evaluator=None) == []


def test_a_passing_checkpoint_is_recorded_with_its_operation():
    results = _evaluate_checkpoints(_definition(checkpoints=(CHECKPOINT,)), checkpoint_evaluator=lambda _op: True)

    assert len(results) == 1
    assert results[0]["passed"] is True
    assert results[0]["operation"]["operation"] == "observe"


def test_a_checkpoint_with_no_evaluator_cannot_pass():
    with pytest.raises(VerificationError, match="checkpoint failed") as caught:
        _evaluate_checkpoints(_definition(checkpoints=(CHECKPOINT,)), checkpoint_evaluator=None)

    assert caught.value.code == "checkpoint_failed"


def test_a_failing_checkpoint_stops_the_run():
    with pytest.raises(VerificationError) as caught:
        _evaluate_checkpoints(_definition(checkpoints=(CHECKPOINT,)), checkpoint_evaluator=lambda _op: False)

    assert caught.value.code == "checkpoint_failed"


def test_an_untyped_checkpoint_is_refused():
    with pytest.raises(VerificationError) as caught:
        _evaluate_checkpoints(
            _definition(checkpoints=({"operation": "observe", "shell": "rm -rf /"},)),
            checkpoint_evaluator=lambda _op: True,
        )

    assert caught.value.code == "untyped_checkpoint"


def test_the_heartbeat_runs_once_per_checkpoint():
    beats: list[int] = []

    _evaluate_checkpoints(
        _definition(checkpoints=(CHECKPOINT, CHECKPOINT)),
        checkpoint_evaluator=lambda _op: True,
        heartbeat=lambda: beats.append(1),
    )

    assert len(beats) == 2


# --- _require_frozen_authored_session ------------------------------------------


def _attempt(*, authored_session_id: str | None) -> LabVerificationAttempt:
    return LabVerificationAttempt(
        id="lpv-1",
        draft_id="ld-1",
        definition_sha256="a" * 64,
        authored_session_id=authored_session_id,
        state="running",
    )


def test_an_attempt_with_no_session_and_no_draft_pointer_is_accepted(in_memory_session_scope):
    definition = _definition()
    with in_memory_session_scope() as session:
        _require_frozen_authored_session(session, _attempt(authored_session_id=None), _draft(definition))


def test_an_unbound_attempt_whose_draft_names_a_session_is_refused(in_memory_session_scope):
    definition = _definition()
    with in_memory_session_scope() as session, pytest.raises(VerificationError) as caught:
        _require_frozen_authored_session(
            session, _attempt(authored_session_id=None), _draft(definition, authored_session_id="pg-1")
        )

    assert caught.value.code == "authored_session_unbound"


def test_a_frozen_authored_session_is_accepted(in_memory_session_scope):
    definition = _definition()
    with in_memory_session_scope() as session:
        session.add(PlaygroundSession(id="pg-1", status=repositories.AUTHORING_CLOSED))
        session.commit()

        _require_frozen_authored_session(
            session, _attempt(authored_session_id="pg-1"), _draft(definition, authored_session_id="pg-1")
        )


def test_a_missing_authored_session_is_refused(in_memory_session_scope):
    definition = _definition()
    with in_memory_session_scope() as session, pytest.raises(VerificationError) as caught:
        _require_frozen_authored_session(session, _attempt(authored_session_id="pg-gone"), _draft(definition))

    assert caught.value.code == "authored_session_unavailable"


def test_a_session_that_is_no_longer_frozen_is_refused(in_memory_session_scope):
    definition = _definition()
    with in_memory_session_scope() as session:
        session.add(PlaygroundSession(id="pg-1", status="running"))
        session.commit()

        with pytest.raises(VerificationError) as caught:
            _require_frozen_authored_session(session, _attempt(authored_session_id="pg-1"), _draft(definition))

    assert caught.value.code == "authored_session_unavailable"


# --- _capture_sources ----------------------------------------------------------


def test_explicitly_supplied_layers_are_used_verbatim(in_memory_session_scope, tmp_path):
    definition = _definition()
    store = ContentStore(tmp_path / "content", max_bytes=4096)
    with in_memory_session_scope() as session:
        result = _capture_sources(
            session,
            _draft(definition),
            captured_layers={"target": b"supplied layer"},
            content_store=store,
        )

    assert result == {"target": b"supplied layer"}


def test_capture_requires_an_authored_session(in_memory_session_scope, tmp_path, monkeypatch):
    from astinuslab.lab_packages import verification_service

    monkeypatch.setattr(verification_service.fake_mode, "FAKE_PLAYGROUND", False)
    definition = _definition()
    store = ContentStore(tmp_path / "content", max_bytes=4096)

    with in_memory_session_scope() as session, pytest.raises(VerificationError) as caught:
        _capture_sources(session, _draft(definition), captured_layers=None, content_store=store)

    assert caught.value.code == "session_required"


def test_capture_refuses_a_vm_the_session_does_not_have(in_memory_session_scope, tmp_path, monkeypatch):
    from astinuslab.lab_packages import verification_service

    monkeypatch.setattr(verification_service.fake_mode, "FAKE_PLAYGROUND", False)
    definition = _definition()
    store = ContentStore(tmp_path / "content", max_bytes=4096)

    with in_memory_session_scope() as session:
        session.add(PlaygroundSession(id="pg-1", status=repositories.AUTHORING_CLOSED))
        session.commit()

        with pytest.raises(VerificationError) as caught:
            _capture_sources(
                session,
                _draft(definition),
                captured_layers=None,
                content_store=store,
                authored_session_id="pg-1",
            )

    assert caught.value.code == "vm_unavailable"


def test_fake_mode_returns_synthetic_layers(in_memory_session_scope, tmp_path, monkeypatch):
    from astinuslab.lab_packages import verification_service

    monkeypatch.setattr(verification_service.fake_mode, "FAKE_PLAYGROUND", True)
    definition = _definition()
    store = ContentStore(tmp_path / "content", max_bytes=4096)

    with in_memory_session_scope() as session:
        result = _capture_sources(session, _draft(definition), captured_layers=None, content_store=store)

    assert set(result) == {"target"}
    layer = result["target"]
    assert isinstance(layer, bytes)
    assert b"fake" in layer


# --- typed checkpoint and probe validation -------------------------------------


def test_a_guest_assertion_checkpoint_is_accepted():
    from astinuslab.lab_packages.models import GuestAssertion, validate_checkpoint

    operation = validate_checkpoint(
        {"operation": "assert", "scope": "guest", "vm_id": "target", "path": "flag.json", "expected": 1}
    )

    assert isinstance(operation, GuestAssertion)
    assert operation.expected == 1


def test_an_assertion_expecting_a_non_finite_value_is_refused():
    from astinuslab.lab_packages.models import GuestAssertion

    with pytest.raises(ValueError, match="finite JSON"):
        GuestAssertion(vm_id="target", path="flag.json", expected=float("inf"))


def test_a_checkpoint_that_is_not_an_object_is_refused():
    from astinuslab.lab_packages.models import validate_checkpoint

    with pytest.raises(ValueError, match="checkpoint must be an object"):
        validate_checkpoint(["not", "an", "object"])  # type: ignore[arg-type]


def test_a_legacy_checkpoint_without_an_operation_is_refused():
    from astinuslab.lab_packages.models import validate_checkpoint

    with pytest.raises(ValueError, match="not a typed guest operation"):
        validate_checkpoint({"id": "flag", "answer": "ctf{...}"})


@pytest.mark.parametrize("key", ["host_command", "host_path", "shell", "filesystem", "exec", "command"])
def test_a_checkpoint_reaching_for_the_host_is_refused(key):
    from astinuslab.lab_packages.models import validate_checkpoint

    with pytest.raises(ValueError, match="guest-scoped observations"):
        validate_checkpoint({"operation": "observe", key: "anything"})


def test_a_command_readiness_probe_requires_its_command():
    from astinuslab.lab_packages.models import LabReadinessProbe

    with pytest.raises(ValueError, match="command readiness probe requires a command"):
        LabReadinessProbe(kind="command", timeout_seconds=1)
