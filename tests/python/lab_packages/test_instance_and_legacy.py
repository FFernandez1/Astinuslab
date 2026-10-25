"""Instance state transitions, operation queueing, and legacy scenario import.

An instance's state machine is what keeps a launch, reset, and teardown from
running over each other, so an undeclared transition is refused rather than
applied. Queueing an operation is idempotent for the same operation id: a
retried request must join the existing outbox row, never create a second one
that a worker could run twice.

Legacy scenario packages predate the portable format. They import as unlocked
definitions with a neutral generated runbook, and a package that cannot satisfy
the portable contract is refused rather than half-converted.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from astinuslab.db.models import LabInstance, LabOperationOutbox, LabPackage
from astinuslab.lab_packages.instance_manager import (
    InstanceTransitionError,
    append_action,
    enqueue_operation,
    transition_instance,
)
from astinuslab.lab_packages.legacy import (
    LegacyScenarioPackageError,
    _safe_package_id,
    definition_from_scenario_package,
    import_scenario_package,
    legacy_runbook,
)

pytestmark = pytest.mark.unit


def _package(session) -> LabPackage:
    record = LabPackage(
        id="lp-1",
        package_id="instance-lab",
        version="1.0.0",
        title="Instance lab",
        creator="Researcher",
        license="CC-BY-4.0",
        reproducibility="unlocked",
        definition_json="{}",
        manifest_json="{}",
        manifest_sha256="a" * 64,
        archive_sha256="b" * 64,
        archive_path="/nonexistent/instance.astinuslab",
        archive_size_bytes=1,
    )
    session.add(record)
    return record


def _instance(session, *, state: str = "registered") -> LabInstance:
    _package(session)
    instance = LabInstance(
        id="lpi-1",
        package_record_id="lp-1",
        idempotency_key="k1",
        state=state,
    )
    session.add(instance)
    session.flush()
    return instance


# --- transitions ---------------------------------------------------------------


def test_a_declared_transition_is_applied(in_memory_session_scope):
    with in_memory_session_scope() as session:
        instance = _instance(session)

        transition_instance(session, instance, "creating")

        assert instance.state == "creating"
        assert instance.error_summary is None


def test_a_failing_transition_records_its_reason(in_memory_session_scope):
    with in_memory_session_scope() as session:
        instance = _instance(session, state="creating")

        transition_instance(session, instance, "failed", error="preflight refused the launch")

        assert instance.state == "failed"
        assert instance.error_summary == "preflight refused the launch"


@pytest.mark.parametrize("target", ["ready", "destroyed", "teleported", "registered"])
def test_an_undeclared_transition_is_refused(in_memory_session_scope, target):
    with in_memory_session_scope() as session:
        instance = _instance(session)

        with pytest.raises(InstanceTransitionError, match="invalid instance transition"):
            transition_instance(session, instance, target)

        assert instance.state == "registered"


@pytest.mark.parametrize("source", ["failed", "cleanup_incomplete"])
def test_terminal_cleanup_state_cannot_transition_to_cancel_requested(in_memory_session_scope, source):
    with in_memory_session_scope() as session:
        instance = _instance(session, state=source)

        with pytest.raises(InstanceTransitionError, match="invalid instance transition"):
            transition_instance(session, instance, "cancel_requested")

        assert instance.state == source


# --- actions and outbox --------------------------------------------------------


def test_an_action_records_its_canonical_payload(in_memory_session_scope):
    with in_memory_session_scope() as session:
        _instance(session)

        action = append_action(session, "lpi-1", "launch", {"b": 2, "a": 1})

        assert action.action_type == "launch"
        assert action.payload_json == '{"a":1,"b":2}'


def test_an_action_without_a_payload_records_an_empty_object(in_memory_session_scope):
    with in_memory_session_scope() as session:
        _instance(session)

        assert append_action(session, "lpi-1", "teardown").payload_json == "{}"


def test_queueing_an_operation_creates_one_outbox_row(in_memory_session_scope):
    with in_memory_session_scope() as session:
        event = enqueue_operation(
            session, kind="package_instance_launch", operation_id="lpi-1", payload={"locked": True}
        )

        assert event.state == "pending"
        assert json.loads(event.payload_json) == {"locked": True}


def test_queueing_the_same_operation_twice_joins_the_existing_row(in_memory_session_scope):
    with in_memory_session_scope() as session:
        first = enqueue_operation(session, kind="package_instance_launch", operation_id="lpi-1")

        second = enqueue_operation(
            session, kind="package_instance_launch", operation_id="lpi-1", payload={"ignored": True}
        )

        assert second.id == first.id
        assert session.query(LabOperationOutbox).count() == 1


def test_terminal_operation_can_be_reopened_for_a_new_reset(in_memory_session_scope):
    with in_memory_session_scope() as session:
        first = enqueue_operation(session, kind="package_instance_reset", operation_id="lpi-1")
        first.state = "completed"
        first.completed_at = datetime.now(UTC)
        session.flush()

        reopened = enqueue_operation(
            session,
            kind="package_instance_reset",
            operation_id="lpi-1",
            payload={"generation": 2},
            reopen_terminal=True,
        )

        assert reopened.id == first.id
        assert reopened.state == "pending"
        assert reopened.claimed_at is None
        assert reopened.completed_at is None
        assert json.loads(reopened.payload_json) == {"generation": 2}


def test_a_different_operation_kind_gets_its_own_row(in_memory_session_scope):
    with in_memory_session_scope() as session:
        enqueue_operation(session, kind="package_instance_launch", operation_id="lpi-1")
        enqueue_operation(session, kind="package_instance_teardown", operation_id="lpi-1")

        assert session.query(LabOperationOutbox).count() == 2


# --- legacy identity -----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("simple-lab", "simple-lab"),
        ("Lab With Spaces", "Lab-With-Spaces"),
        ("--trimmed--", "trimmed"),
        ("a/b\\c", "a-b-c"),
    ],
)
def test_a_legacy_name_becomes_a_safe_package_id(value, expected):
    assert _safe_package_id(value) == expected


@pytest.mark.parametrize("value", ["", "ab", "-", "!!"])
def test_a_name_too_short_to_use_gets_a_legacy_prefix(value):
    result = _safe_package_id(value)

    assert result.startswith("legacy-")
    assert len(result) >= 3


def test_a_very_long_name_is_truncated():
    assert len(_safe_package_id("x" * 500)) == 128


def test_the_generated_legacy_runbook_is_stable():
    assert legacy_runbook()
    assert legacy_runbook() == legacy_runbook()


# --- legacy import -------------------------------------------------------------


def test_bytes_that_are_not_a_scenario_package_are_refused():
    with pytest.raises(LegacyScenarioPackageError, match="invalid legacy scenario package"):
        definition_from_scenario_package(b"not a scenario package")


def test_an_empty_payload_is_refused():
    with pytest.raises(LegacyScenarioPackageError):
        definition_from_scenario_package(b"")


def test_importing_bad_bytes_is_refused_through_the_wrapper():
    with pytest.raises(LegacyScenarioPackageError):
        import_scenario_package(b"not a scenario package")


# --- playground adapter guards -------------------------------------------------


def _portable_definition(**overrides):
    from astinuslab.lab_packages.models import (
        HostResources,
        LabDefinition,
        LabExperience,
        LabNetwork,
        LabVM,
        ResourceRecommendations,
    )

    vm_overrides = overrides.pop("vm", {})
    return LabDefinition(
        package_id="adapter-lab",
        version="1.0.0",
        title="Adapter lab",
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
                **vm_overrides,
            ),
        ),
        experience=LabExperience(),
        **overrides,
    )


def test_a_portable_definition_becomes_a_playground_recipe():
    from astinuslab.lab_packages.playground_adapter import recipe_from_definition

    recipe = recipe_from_definition(_portable_definition())

    assert [network.name for network in recipe.networks] == ["isolated"]
    assert [vm.label for vm in recipe.vms] == ["target"]


def test_a_vm_needing_another_architecture_cannot_be_launched_here():
    """Defence in depth: the model pins x86_64, and the adapter checks anyway."""

    from astinuslab.lab_packages.playground_adapter import (
        PackageLaunchError,
        recipe_from_definition,
    )

    base = _portable_definition()
    foreign = base.vms[0].model_construct(**{**base.vms[0].__dict__, "architecture": "aarch64"})
    definition = base.model_construct(**{**base.__dict__, "vms": (foreign,)})

    with pytest.raises(PackageLaunchError, match="unsupported architecture"):
        recipe_from_definition(definition)


def test_a_vm_needing_another_firmware_cannot_be_launched_here():
    from astinuslab.lab_packages.playground_adapter import (
        PackageLaunchError,
        recipe_from_definition,
    )

    definition = _portable_definition(vm={"firmware": "uefi"})

    with pytest.raises(PackageLaunchError, match="unsupported firmware"):
        recipe_from_definition(definition)


def test_authoring_from_an_unknown_session_is_refused(in_memory_session_scope):
    from astinuslab.lab_packages.models import HostResources, LabExperience, ResourceRecommendations
    from astinuslab.lab_packages.playground_adapter import definition_from_session

    resources = ResourceRecommendations(
        minimum=HostResources(cpu_threads=2, memory_mb=4096, free_disk_bytes=40 * 1024**3),
        recommended=HostResources(cpu_threads=4, memory_mb=8192, free_disk_bytes=40 * 1024**3),
    )

    with in_memory_session_scope() as session:
        with pytest.raises(ValueError, match="playground session not found"):
            definition_from_session(
                session,
                session_id="pg-absent",
                package_id="adapter-lab",
                version="1.0.0",
                title="Adapter lab",
                description="",
                creator="Researcher",
                license="CC-BY-4.0",
                resources=resources,
                experience=LabExperience(),
            )
