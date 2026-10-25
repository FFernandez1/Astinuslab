from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[3]
COLLECTOR_PATH = ROOT / "tools/release/collect_release_evidence.py"
VALIDATOR_PATH = ROOT / "tools/release/validate_release_evidence.py"
FIXED_NOW = datetime(2026, 8, 1, 1, 2, 3, 456789, tzinfo=UTC)

pytestmark = pytest.mark.unit


def _load(path: Path, prefix: str) -> types.ModuleType:
    name = f"{prefix}_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


@pytest.fixture
def collector() -> types.ModuleType:
    return _load(COLLECTOR_PATH, "release_collector")


@pytest.fixture
def validator() -> types.ModuleType:
    return _load(VALIDATOR_PATH, "release_validator")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _proof(path: Path, value: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return path


def _artifact(storage: Path, number: int, record_type: str) -> dict[str, Any]:
    kind = "disk" if number in {2, 7} else "memory"
    relative = "captures/memory.dump" if number == 1 else f"captures/row-{number}/{kind}-{number}.dump"
    path = storage / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = f"release candidate memory row {number}\n".encode()
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    manifest = path.with_name(f"{path.name}.manifest.json")
    sidecar = path.with_name(f"{path.name}.sha256")
    manifest_data = {
        "schema_version": "artifact-manifest/v1",
        "artifact_id": "private-artifact-id",
        "producer": "validator-test",
        "kind": kind,
        "path": str(path),
        "filename": path.name,
        "sha256": digest,
        "sha256_sidecar_path": str(sidecar),
        "size_bytes": len(payload),
        "created_at_utc": "2026-08-01T00:00:00+00:00",
        "target": None,
        "capture": None,
        "network": None,
        "integrity": {
            "raw_sha256": None,
            "domain_xml_sha256": None,
            "domain_xml_excerpt": None,
            "fake_mode": False,
            "fake_mode_flags": {"fake_libvirt": False, "fake_playground": False, "fake_orchestration": False},
        },
        "inputs": [],
        f"{record_type}_id": f"record-{number}",
    }
    _write_json(manifest, manifest_data)
    sidecar.write_text(f"{digest}  {path.name}\n", encoding="ascii")
    return {
        "digest": digest,
        "manifest": manifest,
        "manifest_data": manifest_data,
        "path": path,
        "sidecar": sidecar,
    }


@pytest.fixture
def bundle_fixture(tmp_path: Path, collector: types.ModuleType) -> dict[str, Any]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "validator-tests@example.invalid")
    _git(repo, "config", "user.name", "Validator Tests")
    (repo / "tracked.txt").write_text("clean\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "candidate")
    commit = _git(repo, "rev-parse", "HEAD")

    storage = tmp_path / "storage"
    storage.mkdir()
    artifacts: dict[int, dict[str, Any]] = {}
    for number in (1, 2, 3, 4, 6, 7):
        record_type = "acquisition" if number in {6, 7} else "job"
        artifacts[number] = _artifact(storage, number, record_type)
    proofs = tmp_path / "proof-sources"
    rows: list[dict[str, Any]] = []
    for number in range(1, 8):
        is_ui = number in {4, 6, 7}
        evidence_path = _proof(proofs / f"row-{number}.log", f"row {number}\n")
        record_type = "workload" if number == 5 else ("acquisition" if number in {6, 7} else "job")
        artifact = artifacts.get(number)
        row: dict[str, Any] = {
            "row": number,
            "driver": "ui" if is_ui else "api",
            "record_type": record_type,
            "record_id": f"record-{number}",
            "status": "completed",
            "artifact_path": None if artifact is None else str(artifact["path"]),
            "manifest_path": None if artifact is None else str(artifact["manifest"]),
            "sidecar_path": None if artifact is None else str(artifact["sidecar"]),
            "ui_proof": None,
            "evidence_paths": [str(evidence_path)],
            "command_log_ref": "row-1-command" if number == 1 else None,
            "waiver": None,
        }
        if is_ui:
            trace = _proof(proofs / f"row-{number}.trace.zip", f"trace {number}\n")
            screenshot = _proof(proofs / f"row-{number}.png", f"screen {number}\n")
            row["ui_proof"] = {
                "trace_path": str(trace),
                "screenshot_paths": [str(screenshot)],
            }
        rows.append(row)
    rows_file = tmp_path / "rows.json"
    _write_json(
        rows_file,
        {"schema_version": "astinuslab-release-evidence-rows/v1", "rows": rows},
    )
    command_log = _proof(tmp_path / "commands.log", "row-1-command: pass\n")
    evidence_root = tmp_path / "evidence"
    bundle = collector.collect_bundle(
        repo,
        storage,
        evidence_root,
        rows_file,
        command_log=command_log,
        now=FIXED_NOW,
    )
    return {
        "artifact": artifacts[1],
        "artifacts": artifacts,
        "bundle": bundle,
        "commit": commit,
        "repo": repo,
        "storage": storage,
        "tmp_path": tmp_path,
    }


def _mutate_evidence(fixture: dict[str, Any], mutator) -> dict[str, Any]:
    path = fixture["bundle"] / "evidence.json"
    evidence = _read_json(path)
    mutator(evidence)
    _write_json(path, evidence)
    return evidence


def _rejected(
    validator: types.ModuleType,
    fixture: dict[str, Any],
    match: str,
    *,
    now: datetime = FIXED_NOW,
) -> None:
    with pytest.raises(validator.ValidationError, match=match):
        validator.validate_bundle(
            fixture["bundle"],
            candidate_sha=fixture["commit"],
            now=now,
        )


def test_collector_and_validator_duplicate_contract_constants_remain_equal(
    collector: types.ModuleType,
    validator: types.ModuleType,
) -> None:
    assert validator.ROWS_SCHEMA_VERSION == collector.ROWS_SCHEMA_VERSION
    assert validator.EVIDENCE_SCHEMA_VERSION == collector.EVIDENCE_SCHEMA_VERSION
    assert validator.PUBLIC_SCHEMA_VERSION == collector.PUBLIC_SCHEMA_VERSION
    assert validator.MANIFEST_SCHEMA_VERSION == collector.MANIFEST_SCHEMA_VERSION
    assert validator._ROW_KEYS == collector._ROW_KEYS
    assert validator._WAIVER_KEYS == collector._WAIVER_KEYS
    assert validator._UI_PROOF_KEYS == collector._UI_PROOF_KEYS
    assert validator._REQUIRED_UI_ROWS == collector._REQUIRED_UI_ROWS
    assert validator._RECORD_TYPE.pattern == collector._RECORD_TYPE.pattern
    assert validator._RECORD_ID.pattern == collector._RECORD_ID.pattern
    assert validator._RECORD_FIELD_ALIASES == collector._RECORD_FIELD_ALIASES
    assert validator._EXPECTED_RECORD_TYPES == collector._EXPECTED_RECORD_TYPES
    assert validator._EXPECTED_ARTIFACT_KINDS == collector._EXPECTED_ARTIFACT_KINDS
    assert validator._MANIFEST_REQUIRED_KEYS == collector._MANIFEST_REQUIRED_KEYS
    assert validator._INTEGRITY_KEYS == collector._INTEGRITY_KEYS
    assert validator._FAKE_FLAG_KEYS == collector._FAKE_FLAG_KEYS


def test_help_works_with_system_python_from_arbitrary_cwd(tmp_path: Path) -> None:
    python3 = shutil.which("python3")
    assert python3 is not None
    result = subprocess.run(
        [python3, str(VALIDATOR_PATH), "--help"],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "EVIDENCE_BUNDLE" in result.stdout
    assert "--candidate-sha" in result.stdout


def test_generated_private_bundle_validates(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
) -> None:
    result = validator.validate_bundle(
        bundle_fixture["bundle"],
        candidate_sha=bundle_fixture["commit"],
        now=FIXED_NOW,
    )
    assert result["verdict"] == "PASS"
    assert result["commit"] == bundle_fixture["commit"]
    assert result["rows"] == 7
    assert result["artifacts"] == 6
    public = _read_json(bundle_fixture["bundle"] / "public/evidence-summary.json")
    assert [row["artifact_path"] for row in public["rows"]] == [
        "row-1/artifact",
        "row-2/artifact",
        "row-3/artifact",
        "row-4/artifact",
        None,
        "row-6/artifact",
        "row-7/artifact",
    ]


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "out-of-range"])
def test_missing_duplicate_or_out_of_range_rows_are_rejected(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
    mutation: str,
) -> None:
    def mutate(evidence: dict[str, Any]) -> None:
        if mutation == "missing":
            evidence["rows"].pop()
        elif mutation == "duplicate":
            evidence["rows"].append(copy.deepcopy(evidence["rows"][0]))
        else:
            evidence["rows"][-1]["row"] = 8

    _mutate_evidence(bundle_fixture, mutate)
    _rejected(validator, bundle_fixture, "rows|duplicate|1.*7")


@pytest.mark.parametrize("mutation", ["driver", "proof", "trace", "screenshot"])
def test_ui_rows_require_ui_driver_and_existing_trace_and_screenshot(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
    mutation: str,
) -> None:
    evidence = _read_json(bundle_fixture["bundle"] / "evidence.json")
    row = evidence["rows"][3]
    if mutation == "driver":
        row["driver"] = "api"
    elif mutation == "proof":
        row["ui_proof"] = None
    elif mutation == "trace":
        (bundle_fixture["bundle"] / row["ui_proof"]["trace_path"]).unlink()
    else:
        (bundle_fixture["bundle"] / row["ui_proof"]["screenshot_paths"][0]).unlink()
    if mutation in {"driver", "proof"}:
        _write_json(bundle_fixture["bundle"] / "evidence.json", evidence)
    _rejected(validator, bundle_fixture, "row 4|ui|trace|screenshot|proof")


@pytest.mark.parametrize("mutation", ["missing", "short", "bundle-name", "candidate"])
def test_source_commit_must_be_present_full_and_match_candidate(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
    mutation: str,
) -> None:
    bundle = bundle_fixture["bundle"]
    candidate = bundle_fixture["commit"]
    if mutation == "missing":
        (bundle / "SOURCE_COMMIT.txt").unlink()
    elif mutation == "short":
        source = (bundle / "SOURCE_COMMIT.txt").read_text(encoding="utf-8")
        (bundle / "SOURCE_COMMIT.txt").write_text(
            source.replace(candidate, candidate[:7]),
            encoding="utf-8",
        )
    elif mutation == "bundle-name":
        renamed = bundle.with_name(bundle.name.rsplit("-", 1)[0] + "-" + "f" * 40)
        bundle.rename(renamed)
        bundle_fixture["bundle"] = renamed
    else:
        bundle_fixture["commit"] = "f" * 40
    _rejected(validator, bundle_fixture, "SOURCE_COMMIT|40|candidate|bundle|commit|SHA")


@pytest.mark.parametrize(
    "field",
    ["row", "reason", "impact", "owner", "decision_owner", "expires_or_retest"],
)
def test_incomplete_waiver_is_rejected(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
    field: str,
) -> None:
    waiver = {
        "row": 2,
        "reason": "documented",
        "impact": "limited confidence",
        "owner": "release-owner",
        "decision_owner": "security-owner",
        "expires_or_retest": "2026-08-15",
    }
    del waiver[field]
    _mutate_evidence(bundle_fixture, lambda evidence: evidence["rows"][1].update(waiver=waiver))
    _rejected(validator, bundle_fixture, "waiver")


def test_expired_waiver_is_rejected(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
) -> None:
    waiver = {
        "row": 2,
        "reason": "documented",
        "impact": "limited confidence",
        "owner": "release-owner",
        "decision_owner": "security-owner",
        "expires_or_retest": "2026-07-31",
    }
    _mutate_evidence(bundle_fixture, lambda evidence: evidence["rows"][1].update(waiver=waiver))
    _rejected(validator, bundle_fixture, "expired|retest|waiver")


def test_complete_unexpired_waiver_is_accepted_after_rendered_views_are_regenerated(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
) -> None:
    evidence = _read_json(bundle_fixture["bundle"] / "evidence.json")
    evidence["rows"][1]["waiver"] = {
        "row": 2,
        "reason": "documented",
        "impact": "limited confidence",
        "owner": "release-owner",
        "decision_owner": "security-owner",
        "expires_or_retest": "2026-08-15",
    }
    _write_json(bundle_fixture["bundle"] / "evidence.json", evidence)
    (bundle_fixture["bundle"] / "HOST_WORKFLOW_CHECKLIST.md").write_text(
        validator._private_checklist(evidence["rows"]),
        encoding="utf-8",
    )
    result = validator.validate_bundle(
        bundle_fixture["bundle"],
        candidate_sha=bundle_fixture["commit"],
        now=FIXED_NOW,
    )
    assert result["verdict"] == "PASS"


def test_noncompleted_status_is_rejected(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
) -> None:
    _mutate_evidence(
        bundle_fixture,
        lambda evidence: evidence["rows"][0].update(status="failed"),
    )
    _rejected(validator, bundle_fixture, "completed|status")


def test_artifact_path_escape_is_rejected(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
) -> None:
    _mutate_evidence(
        bundle_fixture,
        lambda evidence: evidence["artifacts"][0].update(artifact_path="../outside.dump"),
    )
    _rejected(validator, bundle_fixture, "relative|parent|storage|outside")


@pytest.mark.parametrize("mutation", ["fake-mode", "fake-flag", "manifest-copy", "digest"])
def test_fake_or_manifest_disagreement_is_rejected(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
    mutation: str,
) -> None:
    bundle = bundle_fixture["bundle"]
    manifest_path = bundle / "manifests/captures/memory.dump.manifest.json"
    manifest = _read_json(manifest_path)
    if mutation == "fake-mode":
        manifest["integrity"]["fake_mode"] = True
        _write_json(manifest_path, manifest)
    elif mutation == "fake-flag":
        manifest["integrity"]["fake_mode_flags"]["fake_libvirt"] = True
        _write_json(manifest_path, manifest)
    elif mutation == "manifest-copy":
        manifest["producer"] = "tampered"
        _write_json(manifest_path, manifest)
    else:
        bundle_fixture["artifact"]["path"].write_bytes(b"tampered raw bytes")
    _rejected(validator, bundle_fixture, "fake|manifest|digest|sha256|size|source")


def test_isf_and_raw_memory_overlap_is_rejected(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
) -> None:
    artifact_path = bundle_fixture["artifact"]["path"]
    source_manifest = bundle_fixture["artifact"]["manifest"]
    manifest = _read_json(source_manifest)
    manifest["inputs"] = [{"kind": "isf", "path": str(artifact_path)}]
    _write_json(source_manifest, manifest)
    _write_json(
        bundle_fixture["bundle"] / "manifests/captures/memory.dump.manifest.json",
        manifest,
    )
    _rejected(validator, bundle_fixture, "ISF.*memory|memory.*ISF|overlap")


@pytest.mark.parametrize("missing", ["command-log", "evidence", "trace", "screenshot"])
def test_missing_referenced_log_trace_or_screenshot_is_rejected(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
    missing: str,
) -> None:
    evidence = _read_json(bundle_fixture["bundle"] / "evidence.json")
    if missing == "command-log":
        path = bundle_fixture["bundle"] / "operator-command.log"
    elif missing == "evidence":
        path = bundle_fixture["bundle"] / evidence["rows"][0]["evidence_paths"][0]
    elif missing == "trace":
        path = bundle_fixture["bundle"] / evidence["rows"][3]["ui_proof"]["trace_path"]
    else:
        path = bundle_fixture["bundle"] / evidence["rows"][3]["ui_proof"]["screenshot_paths"][0]
    path.unlink()
    _rejected(validator, bundle_fixture, "command|evidence|trace|screenshot|proof|missing")


@pytest.mark.parametrize("target", ["public-json", "public-checklist", "private-checklist", "sums", "extra"])
def test_rendered_and_public_outputs_cannot_disagree_with_evidence_json(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
    target: str,
) -> None:
    bundle = bundle_fixture["bundle"]
    if target == "public-json":
        public = _read_json(bundle / "public/evidence-summary.json")
        public["rows"][0]["status"] = "failed"
        _write_json(bundle / "public/evidence-summary.json", public)
    elif target == "public-checklist":
        (bundle / "public/HOST_WORKFLOW_CHECKLIST.md").write_text("edited\n", encoding="utf-8")
    elif target == "private-checklist":
        (bundle / "HOST_WORKFLOW_CHECKLIST.md").write_text("edited\n", encoding="utf-8")
    elif target == "sums":
        (bundle / "SHA256SUMS.txt").write_text("0" * 64 + "  captures/memory.dump\n", encoding="utf-8")
    else:
        (bundle / "public/private-leak.txt").write_text("secret\n", encoding="utf-8")
    _rejected(validator, bundle_fixture, "public|checklist|SHA256SUMS|rendered|unexpected|summary")


def test_command_log_reference_must_exist_in_copied_log(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
) -> None:
    (bundle_fixture["bundle"] / "operator-command.log").write_text(
        "different-command: pass\n",
        encoding="utf-8",
    )
    _rejected(validator, bundle_fixture, "command.*reference|command.*log")


def test_validator_rediscovers_unreferenced_source_manifest(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
) -> None:
    _write_json(bundle_fixture["storage"] / "unreferenced.manifest.json", {})
    _rejected(validator, bundle_fixture, "manifest discovery|artifact inventory")


@pytest.mark.parametrize("kind", ["manifest", "sidecar"])
def test_validator_rejects_unexpected_copied_integrity_files(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
    kind: str,
) -> None:
    directory = bundle_fixture["bundle"] / ("manifests" if kind == "manifest" else "sidecars")
    extra = directory / ("extra.manifest.json" if kind == "manifest" else "extra.sha256")
    extra.write_text("{}\n", encoding="utf-8")
    extra.chmod(0o600)
    _rejected(validator, bundle_fixture, f"{kind} inventory|unexpected")


def test_validator_rejects_duplicate_json_keys(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
) -> None:
    path = bundle_fixture["bundle"] / "evidence.json"
    text = path.read_text(encoding="utf-8")
    needle = '"schema_version": "astinuslab-release-evidence/v1"'
    path.write_text(text.replace(needle, f"{needle},\n  {needle}", 1), encoding="utf-8")
    _rejected(validator, bundle_fixture, "duplicate JSON key")


def test_validator_rejects_nonboolean_fake_flags(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
) -> None:
    source = bundle_fixture["artifact"]["manifest"]
    manifest = _read_json(source)
    manifest["integrity"]["fake_mode_flags"]["fake_libvirt"] = 0
    _write_json(source, manifest)
    _write_json(
        bundle_fixture["bundle"] / "manifests/captures/memory.dump.manifest.json",
        manifest,
    )
    _rejected(validator, bundle_fixture, "fake_mode_flags.*boolean")


def test_validator_rejects_symlinked_bundle_ancestor(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
) -> None:
    link = bundle_fixture["tmp_path"] / "bundle-parent-link"
    link.symlink_to(bundle_fixture["bundle"].parent, target_is_directory=True)
    bundle_fixture["bundle"] = link / bundle_fixture["bundle"].name
    _rejected(validator, bundle_fixture, "symlink")


def test_validator_rejects_row_record_identity_not_backed_by_manifest(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
) -> None:
    _mutate_evidence(
        bundle_fixture,
        lambda evidence: evidence["rows"][0].update(record_id="different-job"),
    )
    _rejected(validator, bundle_fixture, "record.*manifest|manifest.*record")


def test_validator_rejects_duplicate_record_and_artifact_rows(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
) -> None:
    def duplicate(evidence: dict[str, Any]) -> None:
        first = evidence["rows"][0]
        second = evidence["rows"][1]
        for key in (
            "record_type",
            "record_id",
            "artifact_path",
            "manifest_path",
            "sidecar_path",
            "sha256",
            "size_bytes",
        ):
            second[key] = first[key]

    _mutate_evidence(bundle_fixture, duplicate)
    _rejected(validator, bundle_fixture, "duplicate.*record|duplicate.*artifact")


def test_validator_rejects_raw_artifact_bytes_copied_as_proof(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
) -> None:
    evidence = _read_json(bundle_fixture["bundle"] / "evidence.json")
    proof = bundle_fixture["bundle"] / evidence["rows"][0]["evidence_paths"][0]
    proof.write_bytes(bundle_fixture["artifact"]["path"].read_bytes())
    proof.chmod(0o600)
    _rejected(validator, bundle_fixture, "proof.*raw artifact|raw artifact.*proof")


def test_validator_rejects_symlink_directory_in_source_storage(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
) -> None:
    outside = bundle_fixture["tmp_path"] / "outside-storage-directory"
    outside.mkdir()
    (bundle_fixture["storage"] / "linked-directory").symlink_to(outside, target_is_directory=True)
    _rejected(validator, bundle_fixture, "symlink")


def test_current_candidate_helper_rejects_dirty_tracked_checkout(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
) -> None:
    assert validator._current_head(bundle_fixture["repo"]) == bundle_fixture["commit"]
    (bundle_fixture["repo"] / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(validator.ValidationError, match="dirty tracked"):
        validator._current_head(bundle_fixture["repo"])


@pytest.mark.parametrize(
    "mutation",
    ["missing-producer", "integrity-digest", "empty-flags", "bad-created", "bad-target"],
)
def test_validator_enforces_complete_producer_manifest_schema(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
    mutation: str,
) -> None:
    source = bundle_fixture["artifact"]["manifest"]
    manifest = _read_json(source)
    if mutation == "missing-producer":
        del manifest["producer"]
    elif mutation == "integrity-digest":
        manifest["integrity"]["raw_sha256"] = "not-a-sha256"
    elif mutation == "empty-flags":
        manifest["integrity"]["fake_mode_flags"] = {}
    elif mutation == "bad-created":
        manifest["created_at_utc"] = "not-a-date"
    else:
        manifest["target"] = []
    _write_json(source, manifest)
    _write_json(bundle_fixture["bundle"] / "manifests/captures/memory.dump.manifest.json", manifest)
    _rejected(validator, bundle_fixture, "manifest|integrity|producer|fake|created|target")


def test_validator_rejects_nonfinite_manifest_json(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
) -> None:
    paths = [
        bundle_fixture["artifact"]["manifest"],
        bundle_fixture["bundle"] / "manifests/captures/memory.dump.manifest.json",
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("{\n", '{\n  "unexpected_numeric": NaN,\n', 1), encoding="utf-8")
    _rejected(validator, bundle_fixture, "non-finite|JSON constant")


def test_validator_rejects_boolean_waiver_row(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
) -> None:
    _mutate_evidence(
        bundle_fixture,
        lambda evidence: evidence["rows"][0].update(
            waiver={
                "row": True,
                "reason": "reason",
                "impact": "impact",
                "owner": "owner",
                "decision_owner": "decision owner",
                "expires_or_retest": "2026-08-15",
            }
        ),
    )
    _rejected(validator, bundle_fixture, "waiver.*row|integer")


def test_validator_rejects_wrong_record_type_for_mandatory_row(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
) -> None:
    _mutate_evidence(
        bundle_fixture,
        lambda evidence: evidence["rows"][0].update(record_type="acquisition"),
    )
    _rejected(validator, bundle_fixture, "row 1.*record_type")


def test_validator_rejects_wrong_artifact_kind_for_mandatory_row(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
) -> None:
    artifact = bundle_fixture["artifacts"][2]
    manifest = _read_json(artifact["manifest"])
    manifest["kind"] = "memory"
    _write_json(artifact["manifest"], manifest)
    evidence = _read_json(bundle_fixture["bundle"] / "evidence.json")
    inventory = next(item for item in evidence["artifacts"] if item["artifact_path"].endswith("disk-2.dump"))
    inventory["kind"] = "memory"
    _write_json(bundle_fixture["bundle"] / "evidence.json", evidence)
    _write_json(bundle_fixture["bundle"] / inventory["bundle_manifest_path"], manifest)
    _rejected(validator, bundle_fixture, "row 2.*disk|artifact kind")


def test_validator_requires_semantic_bundle_timestamp(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
) -> None:
    invalid = bundle_fixture["bundle"].with_name(f"20261301T010203.456789Z-{bundle_fixture['commit']}")
    bundle_fixture["bundle"].rename(invalid)
    bundle_fixture["bundle"] = invalid
    _rejected(validator, bundle_fixture, "invalid UTC timestamp")


def test_source_commit_text_must_be_canonical(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
) -> None:
    source = bundle_fixture["bundle"] / "SOURCE_COMMIT.txt"
    source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    _rejected(validator, bundle_fixture, "SOURCE_COMMIT.*disagrees")


def test_validator_rejects_unexpected_empty_integrity_directory(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
) -> None:
    extra = bundle_fixture["bundle"] / "manifests/unexpected-empty"
    extra.mkdir(mode=0o700)
    _rejected(validator, bundle_fixture, "manifest inventory|unexpected")


def test_validator_rejects_special_bundle_entry(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
) -> None:
    os.mkfifo(bundle_fixture["bundle"] / "proofs/unexpected.pipe", mode=0o600)
    _rejected(validator, bundle_fixture, "regular files|directories")


def test_validator_rejects_duplicate_proof_reference(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
) -> None:
    def duplicate(evidence: dict[str, Any]) -> None:
        evidence["rows"][1]["evidence_paths"] = list(evidence["rows"][0]["evidence_paths"])

    _mutate_evidence(bundle_fixture, duplicate)
    _rejected(validator, bundle_fixture, "duplicate proof")


def test_validator_rechecks_source_artifact_before_pass(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = validator._validate_rendered_outputs

    def mutate_source(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        bundle_fixture["artifact"]["path"].write_bytes(b"changed after initial validation\n")

    monkeypatch.setattr(validator, "_validate_rendered_outputs", mutate_source)
    _rejected(validator, bundle_fixture, "source artifact.*changed|digest|size")


def test_current_candidate_helper_ignores_inherited_git_directory(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = bundle_fixture["commit"]
    other = bundle_fixture["tmp_path"] / "other-repo"
    other.mkdir()
    _git(other, "init", "-q", "-b", "other")
    _git(other, "config", "user.email", "other@example.invalid")
    _git(other, "config", "user.name", "Other")
    (other / "tracked").write_text("other\n", encoding="utf-8")
    _git(other, "add", "tracked")
    _git(other, "commit", "-q", "-m", "other")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    assert validator._current_head(bundle_fixture["repo"]) == expected


def test_hash_rejects_path_replacement_during_read(
    validator: types.ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    artifact = storage / "artifact.bin"
    artifact.write_bytes(b"original artifact bytes\n")
    moved = storage / "opened-inode.bin"
    original_read = validator.os.read
    replaced = False

    def replacing_read(fd: int, size: int) -> bytes:
        nonlocal replaced
        chunk = original_read(fd, size)
        if chunk and not replaced:
            replaced = True
            artifact.rename(moved)
            artifact.write_bytes(b"replacement bytes same\n")
        return chunk

    monkeypatch.setattr(validator.os, "read", replacing_read)
    with pytest.raises(validator.ValidationError, match="changed.*identity"):
        validator._hash_storage_file(storage, "artifact.bin", "artifact")


@pytest.mark.parametrize("number", [1, 2, 3])
def test_rows_without_ui_proof_require_at_least_one_evidence_file(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
    number: int,
) -> None:
    _mutate_evidence(
        bundle_fixture,
        lambda evidence: evidence["rows"][number - 1].update(evidence_paths=[]),
    )
    _rejected(validator, bundle_fixture, f"row {number}.*evidence file")


def test_ui_row_may_rely_on_its_trace_and_screenshot_proof(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
) -> None:
    bundle = bundle_fixture["bundle"]
    evidence = _read_json(bundle / "evidence.json")
    fourth = next(row for row in evidence["rows"] if row["row"] == 4)
    dropped = set(fourth["evidence_paths"])
    for relative in dropped:
        (bundle / relative).unlink()
    fourth["evidence_paths"] = []
    evidence["proofs"] = [item for item in evidence["proofs"] if item["path"] not in dropped]
    _write_json(bundle / "evidence.json", evidence)
    (bundle / "HOST_WORKFLOW_CHECKLIST.md").write_text(
        validator._private_checklist(evidence["rows"]),
        encoding="utf-8",
    )

    result = validator.validate_bundle(
        bundle,
        candidate_sha=bundle_fixture["commit"],
        now=FIXED_NOW,
    )
    assert result["verdict"] == "PASS"


def test_validator_rejects_hardlinked_artifact_reused_across_rows(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
) -> None:
    bundle = bundle_fixture["bundle"]
    storage = bundle_fixture["storage"]
    disk = bundle_fixture["artifacts"][2]

    hardlink = storage / "captures/row-7/hardlinked-disk.dump"
    os.link(disk["path"], hardlink)
    assert hardlink.stat().st_ino == disk["path"].stat().st_ino
    manifest_data = copy.deepcopy(disk["manifest_data"])
    manifest_data.update(
        {
            "path": str(hardlink),
            "filename": hardlink.name,
            "sha256_sidecar_path": f"{hardlink}.sha256",
        }
    )
    manifest_data.pop("job_id", None)
    manifest_data["acquisition_id"] = "record-7"
    manifest_path = hardlink.with_name(f"{hardlink.name}.manifest.json")
    sidecar_path = hardlink.with_name(f"{hardlink.name}.sha256")
    _write_json(manifest_path, manifest_data)
    sidecar_path.write_text(f"{disk['digest']}  {hardlink.name}\n", encoding="ascii")

    relative_artifact = hardlink.relative_to(storage).as_posix()
    relative_manifest = manifest_path.relative_to(storage).as_posix()
    relative_sidecar = sidecar_path.relative_to(storage).as_posix()
    for source, destination in (
        (manifest_path, bundle / "manifests" / relative_manifest),
        (sidecar_path, bundle / "sidecars" / relative_sidecar),
    ):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        destination.chmod(0o600)

    evidence = _read_json(bundle / "evidence.json")
    evidence["artifacts"].append(
        {
            "artifact_path": relative_artifact,
            "bundle_manifest_path": f"manifests/{relative_manifest}",
            "bundle_sidecar_path": f"sidecars/{relative_sidecar}",
            "kind": "disk",
            "manifest_path": relative_manifest,
            "sha256": disk["digest"],
            "sidecar_path": relative_sidecar,
            "size_bytes": manifest_data["size_bytes"],
        }
    )
    evidence["artifacts"].sort(key=lambda item: item["artifact_path"])
    seventh = next(row for row in evidence["rows"] if row["row"] == 7)
    seventh.update(
        artifact_path=relative_artifact,
        manifest_path=relative_manifest,
        sidecar_path=relative_sidecar,
        sha256=disk["digest"],
        size_bytes=manifest_data["size_bytes"],
    )
    _write_json(bundle / "evidence.json", evidence)

    _rejected(validator, bundle_fixture, "duplicate artifact")


def test_validator_rejects_bundle_mutation_during_validation(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = bundle_fixture["bundle"]
    evidence = _read_json(bundle / "evidence.json")
    target = bundle / evidence["rows"][0]["evidence_paths"][0]
    original = validator._revalidate_artifact_snapshot

    def mutate_bundle(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        target.write_text("swapped after the bundle was read\n", encoding="utf-8")
        target.chmod(0o600)

    monkeypatch.setattr(validator, "_revalidate_artifact_snapshot", mutate_bundle)
    _rejected(validator, bundle_fixture, "bundle changed during validation")


def test_open_file_identity_failure_fails_closed(
    validator: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnresolvablePath(Path):
        def resolve(self, strict: bool = False) -> Path:
            raise OSError("procfs unavailable")

    monkeypatch.setattr(validator, "Path", lambda *args: UnresolvablePath(*args))
    with pytest.raises(validator.ValidationError, match="/proc/self/fd"):
        validator._opened_target(0, "probe file")


def test_validator_cli_passes_for_a_clean_matching_candidate(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    commit = bundle_fixture["commit"]
    monkeypatch.setattr(validator, "_current_head", lambda repo_root: commit)

    exit_code = validator.main([str(bundle_fixture["bundle"]), "--candidate-sha", commit])

    assert exit_code == 0
    assert capsys.readouterr().out.startswith(f"PASS commit={commit} rows=7 artifacts=")


def test_validator_cli_rejects_repository_change_after_validation(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    commit = bundle_fixture["commit"]
    heads = iter([commit, "b" * 40])
    monkeypatch.setattr(validator, "_current_head", lambda repo_root: next(heads))

    exit_code = validator.main([str(bundle_fixture["bundle"]), "--candidate-sha", commit])

    assert exit_code == 2
    assert "source changed during evidence validation" in capsys.readouterr().err


def test_validator_manifest_contract_matches_the_real_backend_producer(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
) -> None:
    from astinuslab.storage.artifacts import build_artifact_manifest

    artifact = bundle_fixture["artifact"]
    manifest = build_artifact_manifest(
        artifact_path=artifact["path"],
        kind="memory",
        producer="astinuslab-capture",
        sha256=artifact["digest"],
        size_bytes=artifact["manifest_data"]["size_bytes"],
        job_id="record-1",
    )

    assert set(manifest["integrity"]) == validator._INTEGRITY_KEYS
    assert set(manifest["integrity"]["fake_mode_flags"]) == validator._FAKE_FLAG_KEYS
    assert validator._MANIFEST_REQUIRED_KEYS <= set(manifest)
    assert manifest["schema_version"] == validator.MANIFEST_SCHEMA_VERSION

    validator._validate_artifact_manifest(
        manifest,
        artifact_path=artifact["path"],
        sidecar_path=artifact["sidecar"],
        digest=artifact["digest"],
        size_bytes=artifact["manifest_data"]["size_bytes"],
        inventory_kind="memory",
    )


@pytest.mark.parametrize("target", ["evidence", "trace", "screenshot", "command-log"])
def test_validator_rejects_proof_bytes_swapped_after_collection(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
    target: str,
) -> None:
    bundle = bundle_fixture["bundle"]
    evidence = _read_json(bundle / "evidence.json")
    if target == "command-log":
        relative = "operator-command.log"
    else:
        fourth = next(row for row in evidence["rows"] if row["row"] == 4)
        if target == "evidence":
            relative = fourth["evidence_paths"][0]
        elif target == "trace":
            relative = fourth["ui_proof"]["trace_path"]
        else:
            relative = fourth["ui_proof"]["screenshot_paths"][0]

    swapped = bundle / relative
    swapped.write_bytes(b"forged proof bytes substituted after collection\n")
    swapped.chmod(0o600)

    _rejected(validator, bundle_fixture, "disagree with the recorded proof digest")


@pytest.mark.parametrize("mutation", ["missing", "extra", "unsorted", "bad-digest"])
def test_validator_rejects_a_malformed_or_incomplete_proof_inventory(
    validator: types.ModuleType,
    bundle_fixture: dict[str, Any],
    mutation: str,
) -> None:
    def mutate(evidence: dict[str, Any]) -> None:
        if mutation == "missing":
            evidence["proofs"] = evidence["proofs"][1:]
        elif mutation == "extra":
            evidence["proofs"].append({"path": "proofs/row-1/never-copied.log", "sha256": "a" * 64, "size_bytes": 1})
        elif mutation == "unsorted":
            evidence["proofs"] = list(reversed(evidence["proofs"]))
        else:
            evidence["proofs"][0]["sha256"] = "not-a-digest"

    _mutate_evidence(bundle_fixture, mutate)
    _rejected(validator, bundle_fixture, "proof inventory|recorded proof")
