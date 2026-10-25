from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import pytest

from astinuslab.lab_packages.archive import inspect_archive, write_archive
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

ROOT = Path(__file__).resolve().parents[3]
TOOL = ROOT / "tools/release/collect_lab_package_host_evidence.py"
SCHEMA = "astinuslab-lab-package-host-evidence/v1"

pytestmark = pytest.mark.unit


@pytest.fixture
def evidence_tool() -> ModuleType:
    name = f"lab_package_host_evidence_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def _locked_archive(tmp_path: Path) -> Path:
    layer = b"clean qcow2 layer fixture"
    layer_ref = AssetReference(
        sha256=hashlib.sha256(layer).hexdigest(),
        size_bytes=len(layer),
        media_type="application/x-qcow2",
        embedded=True,
        license="CC-BY-4.0",
        redistribution_confirmed=True,
    )
    resources = ResourceRecommendations(
        minimum=HostResources(cpu_threads=2, memory_mb=4096, free_disk_bytes=40 * 1024**3),
        recommended=HostResources(cpu_threads=4, memory_mb=8192, free_disk_bytes=60 * 1024**3),
    )
    definition = LabDefinition(
        package_id="host-evidence-lab",
        version="1.0.0",
        title="Host evidence lab",
        creator="acceptance",
        license="CC-BY-4.0",
        reproducibility="unlocked",
        resources=resources,
        networks=(LabNetwork(id="isolated", subnet_cidr="192.0.2.0/29", reachability="isolated"),),
        vms=(
            LabVM(
                id="target",
                role="target",
                image_id="ubuntu-24.04",
                vcpus=2,
                memory_mb=2048,
                disk_gb=20,
                network_ids=("isolated",),
                base_image=LabImageSpec(
                    image_id="ubuntu-24.04",
                    sha256="a" * 64,
                    size_bytes=1,
                    media_type="application/x-qcow2",
                ),
            ),
        ),
        experience=LabExperience(
            checkpoints=(
                {
                    "operation": "observe",
                    "scope": "guest",
                    "vm_id": "target",
                    "path": "/etc/hostname",
                    "timeout_seconds": 30,
                },
            )
        ),
    )
    definition = definition.model_copy(
        update={"vms": (definition.vms[0], definition.vms[0].model_copy(update={"id": "peer"}))}
    )
    locked = build_locked_definition(
        definition,
        boot_layers={"target": layer_ref, "peer": layer_ref},
        redistribution_confirmed=True,
    )
    path = tmp_path / "host-evidence.astinuslab"
    path.write_bytes(
        write_archive(locked, "# Acceptance runbook\n", assets={f"assets/sha256/{layer_ref.sha256}": layer})
    )
    return path


def _lifecycle_value():
    digest = hashlib.sha256(b"clean qcow2 layer fixture").hexdigest()
    phase = {
        "state": "ready",
        "boot_layers": [{"vm_id": vm, "sha256": digest, "size_bytes": 25} for vm in ("peer", "target")],
        "topology": {
            "networks": [{"id": "isolated", "subnet_cidr": "192.0.2.0/29", "isolated": True}],
            "nics": [{"vm_id": vm, "network_id": "isolated", "ordinal": 0} for vm in ("peer", "target")],
        },
        "readiness": [{"vm_id": vm, "probes": [], "passed": True} for vm in ("peer", "target")],
        "checkpoints": [
            {
                "operation": {
                    "operation": "observe",
                    "scope": "guest",
                    "vm_id": "target",
                    "path": "/etc/hostname",
                    "timeout_seconds": 30,
                },
                "passed": True,
            }
        ],
    }
    empty: dict[str, list[str]] = {
        key: [] for key in ("domains", "networks", "overlays", "reservations", "temporary_paths")
    }
    return {
        "launch": deepcopy(phase),
        "reset": {**deepcopy(phase), "previous_session_residue": deepcopy(empty)},
        "teardown": {"state": "destroyed", **empty, "retained_asset_digests": [digest]},
    }


def _write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _provenance(archive, lifecycle, identity="a"):
    return {
        "candidate_sha": "1" * 40,
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "host_id_sha256": identity * 64,
        "installation_id_sha256": ("c" if identity == "a" else "d") * 64,
        "identity_receipt_sha256": "e" * 64,
        "installed_source": {"candidate_sha": "1" * 40, "verified": True, "manifest_sha256": "f" * 64},
        "installed_helpers": {"verified": True, "manifest_sha256": "9" * 64},
        "command": {
            "exit_code": 0,
            "passed": 1,
            "failed": 0,
            "skipped": 0,
            "receipt_sha256": "8" * 64,
            "lifecycle_sha256": hashlib.sha256(lifecycle.read_bytes()).hexdigest(),
        },
    }


def _create(tool, tmp_path, *, value=None, identity="a", provenance_change=None):
    archive = _locked_archive(tmp_path)
    lifecycle = _write(tmp_path / f"lifecycle-{identity}.json", value or _lifecycle_value())
    provenance = _provenance(archive, lifecycle, identity)
    if provenance_change:
        provenance_change(provenance)
    path = tmp_path / f"host-{identity}.json"
    tool.create_manifest(
        archive_path=archive,
        lifecycle_path=lifecycle,
        candidate_sha="1" * 40,
        host_label=f"host-{identity}",
        output_path=path,
        provenance_path=_write(tmp_path / f"provenance-{identity}.json", provenance),
    )
    return path


def test_two_host_manifest_binds_package_and_compares_surface(evidence_tool, tmp_path):
    first = _create(evidence_tool, tmp_path)
    second = _create(evidence_tool, tmp_path, identity="b")
    result = evidence_tool.compare_manifests(first, second, candidate_sha="1" * 40)
    assert result["result"] == "PASS"
    assert result["hosts"] == ["host-a", "host-b"]
    assert json.loads(first.read_text())["schema_version"] == SCHEMA


@pytest.mark.parametrize("phase,state", [("launch", "destroyed"), ("reset", "destroyed"), ("teardown", "ready")])
def test_wrong_phase_terminal_state_is_rejected(evidence_tool, tmp_path, phase, state):
    value = _lifecycle_value()
    value[phase]["state"] = state
    with pytest.raises(evidence_tool.EvidenceError, match="state"):
        _create(evidence_tool, tmp_path, value=value)


@pytest.mark.parametrize("phase", ["launch", "reset"])
@pytest.mark.parametrize(
    "damage",
    [
        "missing_layer",
        "changed_layer",
        "zero_size",
        "missing_checkpoint",
        "failed_checkpoint",
        "wrong_checkpoint",
        "failed_readiness",
        "missing_readiness",
        "topology",
    ],
)
def test_every_declared_observation_is_bound_to_archive(evidence_tool, tmp_path, phase, damage):
    value = _lifecycle_value()
    row = value[phase]
    if damage == "missing_layer":
        row["boot_layers"].pop()
    elif damage == "changed_layer":
        row["boot_layers"][1]["sha256"] = "0" * 64
    elif damage == "zero_size":
        row["boot_layers"][1]["size_bytes"] = 0
    elif damage == "missing_checkpoint":
        row["checkpoints"] = []
    elif damage == "failed_checkpoint":
        row["checkpoints"][0]["passed"] = False
    elif damage == "wrong_checkpoint":
        row["checkpoints"][0]["operation"]["path"] = "/different"
    elif damage == "failed_readiness":
        row["readiness"][1]["passed"] = False
    elif damage == "missing_readiness":
        row["readiness"].pop()
    else:
        row["topology"]["nics"][1]["network_id"] = "host-specific-db-id"
    with pytest.raises(evidence_tool.EvidenceError):
        _create(evidence_tool, tmp_path, value=value)


@pytest.mark.parametrize("location", ["reset", "teardown"])
@pytest.mark.parametrize("kind", ["domains", "networks", "overlays", "reservations", "temporary_paths"])
def test_reset_and_teardown_residue_is_rejected(evidence_tool, tmp_path, location, kind):
    value = _lifecycle_value()
    residue = value["reset"]["previous_session_residue"] if location == "reset" else value["teardown"]
    residue[kind] = ["leftover"]
    with pytest.raises(evidence_tool.EvidenceError, match="residue"):
        _create(evidence_tool, tmp_path, value=value)


@pytest.mark.parametrize("field", ["host_id_sha256", "installation_id_sha256"])
def test_relabeling_one_installation_is_not_two_host_proof(evidence_tool, tmp_path, field):
    first = _create(evidence_tool, tmp_path)
    original = json.loads(first.read_text())["provenance"][field]
    second = _create(evidence_tool, tmp_path, identity="b", provenance_change=lambda p: p.update({field: original}))
    with pytest.raises(evidence_tool.EvidenceError, match="distinct"):
        evidence_tool.compare_manifests(first, second)


@pytest.mark.parametrize(
    "damage", ["source", "helper", "candidate", "archive", "command", "skip", "lifecycle", "identity"]
)
def test_incomplete_or_unbound_provenance_is_rejected(evidence_tool, tmp_path, damage):
    def change(p):
        if damage == "source":
            p["installed_source"]["verified"] = False
        elif damage == "helper":
            p["installed_helpers"].pop("manifest_sha256")
        elif damage == "candidate":
            p["installed_source"]["candidate_sha"] = "2" * 40
        elif damage == "archive":
            p["archive_sha256"] = "0" * 64
        elif damage == "command":
            p["command"]["exit_code"] = 1
        elif damage == "skip":
            p["command"]["skipped"] = 1
        elif damage == "lifecycle":
            p["command"]["lifecycle_sha256"] = "0" * 64
        else:
            p.pop("identity_receipt_sha256")

    with pytest.raises(evidence_tool.EvidenceError):
        _create(evidence_tool, tmp_path, provenance_change=change)


def test_compare_revalidates_manifest_instead_of_trusting_stored_surface(evidence_tool, tmp_path):
    first = _create(evidence_tool, tmp_path)
    second = _create(evidence_tool, tmp_path, identity="b")
    tampered = json.loads(second.read_text())
    tampered["lifecycle"]["reset"]["checkpoints"][0]["passed"] = False
    _write(second, tampered)
    with pytest.raises(evidence_tool.EvidenceError):
        evidence_tool.compare_manifests(first, second)


def _p3(tool, tmp_path):
    manifest = _create(tool, tmp_path)
    archive = tmp_path / "host-evidence.astinuslab"
    runs = []
    for mode in ("cold", "cached", "unavailable"):
        names = ("inspect", "import", "prepare", "preflight", "launch", "checkpoints", "reset", "teardown")
        phases = [
            {"name": name, "start_monotonic": 10.0 + i * 10, "end_monotonic": 20.0 + i * 10, "outcome": "passed"}
            for i, name in enumerate(names)
        ]
        if mode != "unavailable":
            lifecycle = _lifecycle_value()
            lifecycle["resource_snapshots"] = [{"session_id": mode + "-session"}]
            manifest = _create(
                tool,
                tmp_path,
                value=lifecycle,
                provenance_change=lambda p: p["command"].update(
                    {"receipt_sha256": ("5" if mode == "cold" else "6") * 64}
                ),
            )
            manifest = manifest.rename(tmp_path / (mode + "-manifest.json"))
        run: dict[str, Any] = {
            "cache_mode": mode,
            "cache_before": [] if mode != "cached" else [hashlib.sha256(b"clean qcow2 layer fixture").hexdigest()],
            "cache_receipt_sha256": "8" * 64,
            "start_monotonic": 10.0,
            "end_monotonic": 90.0,
            "phases": phases,
            "included_work": ["hashing", "queue_waits", "readiness"],
            "excluded_work": [],
            "network": {
                "applied_monotonic": 0.0,
                "restored_monotonic": 100.0,
                "applied_receipt_sha256": "1" * 64,
                "restored_receipt_sha256": "2" * 64,
                "continuous_receipt_sha256": "3" * 64,
                "probes": [
                    {"kind": kind, "stage": stage, "monotonic": stamp, "blocked": True, "receipt_sha256": "4" * 64}
                    for kind in ("dns", "tcp")
                    for stage, stamp in [("before", 5.0), ("after", 95.0)]
                ],
            },
            "command_receipt_sha256": ("6" if mode == "cached" else "5") * 64,
            "manifest": str(manifest),
        }
        if mode == "unavailable":
            run.pop("manifest")
            run["phases"] = phases[:3] + [{**phases[-1], "start_monotonic": 40.0}]
            run["phases"][2]["outcome"] = "expected_failure"
            definition = inspect_archive(archive).definition
            thin = definition.model_copy(
                update={
                    "vms": tuple(
                        vm.model_copy(update={"boot_layer": vm.boot_layer.model_copy(update={"embedded": False})})
                        for vm in definition.vms
                        if vm.boot_layer is not None
                    )
                }
            )
            control = tmp_path / "unavailable-control.astinuslab"
            control.write_bytes(write_archive(thin, "# Acceptance runbook\n"))
            run["control_archive"] = str(control)
            run["control_archive_sha256"] = hashlib.sha256(control.read_bytes()).hexdigest()
            run["reference_archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
            run["unavailable_asset_sha256"] = hashlib.sha256(b"clean qcow2 layer fixture").hexdigest()
            run["command_receipt_sha256"] = "7" * 64
            run["failure_code"] = "required_asset_unavailable"
            run["downloads_attempted"] = 0
            run["residue"] = {key: [] for key in ("domains", "networks", "overlays", "reservations", "temporary_paths")}
            observed = {
                key: run[key]
                for key in (
                    "control_archive_sha256",
                    "reference_archive_sha256",
                    "unavailable_asset_sha256",
                    "failure_code",
                    "downloads_attempted",
                    "residue",
                )
            }
            control_observation = tmp_path / "control-observation.json"
            control_observation.write_text(json.dumps(observed, sort_keys=True, separators=(",", ":")))
            run["provenance"] = _provenance(control, control_observation)
            run["provenance"]["command"]["receipt_sha256"] = "7" * 64
        runs.append(run)
    return {
        "schema_version": SCHEMA,
        "kind": "offline-performance",
        "contract_revision": 2,
        "candidate_sha": "1" * 40,
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "host_id_sha256": "a" * 64,
        "installation_id_sha256": "c" * 64,
        "profile": {
            "os_id": "ubuntu",
            "os_version": "24.04",
            "architecture": "x86_64",
            "cpu_threads": 4,
            "memory_mb": 8192,
            "free_disk_bytes": 40 * 1024**3,
            "guest_memory_mb": 4096,
            "vm_count": 2,
            "compressed_content_bytes": archive.stat().st_size,
            "receipt_sha256": "6" * 64,
        },
        "creator": {
            "candidate_sha": "1" * 40,
            "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "installed_source_manifest_sha256": "f" * 64,
            "installed_helper_manifest_sha256": "9" * 64,
            "source_verified": True,
            "helpers_verified": True,
            "start_monotonic": 0.0,
            "end_monotonic": 300.0,
            "phases": [
                {
                    "name": name,
                    "start_monotonic": float(i * 100),
                    "end_monotonic": float((i + 1) * 100),
                    "outcome": "passed",
                }
                for i, name in enumerate(("build", "verify", "export"))
            ],
            "included_work": ["hashing", "queue_waits", "readiness"],
            "excluded_work": ["initial_asset_downloads", "custom_runbook_authoring"],
            "receipt_sha256": "7" * 64,
        },
        "runs": runs,
    }


def test_p3_validates_complete_receipt(evidence_tool, tmp_path):
    receipt = _write(tmp_path / "p3.json", _p3(evidence_tool, tmp_path))
    assert evidence_tool.validate_p3(receipt, candidate_sha="1" * 40)["result"] == "PASS"


@pytest.mark.parametrize(
    "damage",
    [
        "no_cold",
        "no_negative",
        "network_flag_only",
        "probe_passes",
        "probe_missing",
        "restoration_missing",
        "late_block",
        "early_restore",
        "phase_failed",
        "time_reversed",
        "time_nan",
        "time_gap",
        "creator_slow",
        "recipient_slow",
        "excluded_hashing",
        "no_profile",
        "no_raw_receipt",
        "residue",
        "download",
        "mixed_candidate",
        "mixed_archive",
        "mixed_host",
        "creator_source",
        "creator_archive",
        "false_cold",
        "false_cached",
        "missing_cache_receipt",
        "reused_lifecycle",
    ],
)
def test_p3_rejects_incomplete_offline_or_performance_evidence(evidence_tool, tmp_path, damage):
    value = _p3(evidence_tool, tmp_path)
    run = value["runs"][1]
    if damage == "no_cold":
        value["runs"].pop(0)
    elif damage == "no_negative":
        value["runs"].pop()
    elif damage == "network_flag_only":
        run["network"] = {"network_opt_in": False}
    elif damage == "probe_passes":
        run["network"]["probes"][0]["blocked"] = False
    elif damage == "probe_missing":
        run["network"]["probes"].pop()
    elif damage == "restoration_missing":
        run["network"].pop("restored_receipt_sha256")
    elif damage == "late_block":
        run["network"]["applied_monotonic"] = 11.0
    elif damage == "early_restore":
        run["network"]["restored_monotonic"] = 80.0
    elif damage == "phase_failed":
        run["phases"][3]["outcome"] = "failed"
    elif damage == "time_reversed":
        run["phases"][0]["end_monotonic"] = 9.0
    elif damage == "time_nan":
        run["end_monotonic"] = float("nan")
    elif damage == "time_gap":
        run["phases"][1]["start_monotonic"] += 1.0
    elif damage == "creator_slow":
        value["creator"]["end_monotonic"] = 600.0
    elif damage == "recipient_slow":
        run["phases"][4]["end_monotonic"] = 310.0
    elif damage == "excluded_hashing":
        run["excluded_work"] = ["hashing"]
    elif damage == "no_profile":
        value["profile"].pop("cpu_threads")
    elif damage == "no_raw_receipt":
        run.pop("command_receipt_sha256")
    elif damage == "residue":
        value["runs"][2]["residue"]["overlays"] = ["leftover"]
    elif damage == "download":
        value["runs"][2]["downloads_attempted"] = 1
    elif damage == "mixed_candidate":
        value["candidate_sha"] = "2" * 40
    elif damage == "mixed_archive":
        value["archive_sha256"] = "0" * 64
    elif damage == "mixed_host":
        value["host_id_sha256"] = "0" * 64
    elif damage == "creator_source":
        value["creator"]["source_verified"] = False
    elif damage == "creator_archive":
        value["creator"]["archive_sha256"] = "0" * 64
    elif damage == "false_cold":
        value["runs"][0]["cache_before"] = [hashlib.sha256(b"clean qcow2 layer fixture").hexdigest()]
    elif damage == "false_cached":
        run["cache_before"] = []
    elif damage == "missing_cache_receipt":
        run.pop("cache_receipt_sha256")
    else:
        run["manifest"] = value["runs"][0]["manifest"]
    with pytest.raises(evidence_tool.EvidenceError):
        evidence_tool.validate_p3(_write(tmp_path / "p3.json", value), candidate_sha="1" * 40)


def test_p3_rejects_timing_receipt_from_a_different_lifecycle_run(evidence_tool, tmp_path):
    value = _p3(evidence_tool, tmp_path)
    value["runs"][1]["command_receipt_sha256"] = "0" * 64
    with pytest.raises(evidence_tool.EvidenceError, match="command"):
        evidence_tool.validate_p3(_write(tmp_path / "p3.json", value), candidate_sha="1" * 40)


@pytest.mark.parametrize(
    "damage", ["missing", "candidate", "archive", "host", "installation", "source", "unrelated_asset", "embedded_asset"]
)
def test_p3_unavailable_control_requires_bound_provenance(evidence_tool, tmp_path, damage):
    value = _p3(evidence_tool, tmp_path)
    control = value["runs"][2]
    if damage == "missing":
        control.pop("provenance")
    elif damage == "candidate":
        control["provenance"]["candidate_sha"] = "2" * 40
    elif damage == "archive":
        control["provenance"]["archive_sha256"] = "0" * 64
    elif damage == "host":
        control["provenance"]["host_id_sha256"] = "0" * 64
    elif damage == "installation":
        control["provenance"]["installation_id_sha256"] = "0" * 64
    elif damage == "source":
        control["provenance"]["installed_source"]["verified"] = False
    elif damage == "unrelated_asset":
        control["unavailable_asset_sha256"] = "0" * 64
    else:
        control["control_archive"] = str(tmp_path / "host-evidence.astinuslab")
    with pytest.raises(evidence_tool.EvidenceError):
        evidence_tool.validate_p3(_write(tmp_path / "p3.json", value), candidate_sha="1" * 40)


@pytest.mark.parametrize(
    "workflow,seconds,passes",
    [("creator", 599.0, True), ("creator", 600.0, False), ("recipient", 299.0, True), ("recipient", 300.0, False)],
)
def test_performance_targets_are_strict_with_consistent_phase_times(evidence_tool, tmp_path, workflow, seconds, passes):
    value = _p3(evidence_tool, tmp_path)
    if workflow == "creator":
        value["creator"]["end_monotonic"] = seconds
        value["creator"]["phases"][-1]["end_monotonic"] = seconds
    else:
        run = value["runs"][1]
        delta = 10.0 + seconds - run["phases"][4]["end_monotonic"]
        run["phases"][4]["end_monotonic"] += delta
        for phase in run["phases"][5:]:
            phase["start_monotonic"] += delta
            phase["end_monotonic"] += delta
        run["end_monotonic"] += delta
        run["network"]["restored_monotonic"] += delta
        for probe in run["network"]["probes"]:
            if probe["stage"] == "after":
                probe["monotonic"] += delta
    path = _write(tmp_path / "p3.json", value)
    if passes:
        assert evidence_tool.main(["validate-p3", "--receipt", str(path), "--candidate-sha", "1" * 40]) == 0
    else:
        with pytest.raises(evidence_tool.EvidenceError, match="target"):
            evidence_tool.validate_p3(path, candidate_sha="1" * 40)
        assert evidence_tool.main(["validate-p3", "--receipt", str(path), "--candidate-sha", "1" * 40]) == 2
