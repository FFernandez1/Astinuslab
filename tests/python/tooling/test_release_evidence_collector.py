from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import shutil
import stat
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
WRAPPER_PATH = ROOT / "tools/release/collect_release_evidence.sh"
ROWS_SCHEMA = "astinuslab-release-evidence-rows/v1"
PRIVATE_SCHEMA = "astinuslab-release-evidence/v1"
PUBLIC_SCHEMA = "astinuslab-release-evidence-public/v1"
FIXED_NOW = datetime(2026, 8, 1, 1, 2, 3, 456789, tzinfo=UTC)
ROW_KEYS = {
    "artifact_path",
    "command_log_ref",
    "driver",
    "evidence_paths",
    "manifest_path",
    "record_id",
    "record_type",
    "row",
    "sidecar_path",
    "status",
    "ui_proof",
    "waiver",
}

pytestmark = pytest.mark.unit


def _load_collector() -> types.ModuleType:
    module_name = f"astinuslab_release_evidence_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, COLLECTOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


@pytest.fixture
def collector() -> types.ModuleType:
    return _load_collector()


def _run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _run_git(path, "init", "-q", "-b", "main")
    _run_git(path, "config", "user.email", "collector-tests@example.invalid")
    _run_git(path, "config", "user.name", "Collector Tests")
    (path / "tracked.txt").write_text("clean\n", encoding="utf-8")
    _run_git(path, "add", "tracked.txt")
    _run_git(path, "commit", "-q", "-m", "initial")
    return path


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _make_artifact(
    storage: Path,
    *,
    relative_path: str = "captures/memory.dump",
    payload: bytes = b"\x00real artifact bytes\xff",
    kind: str = "memory",
    inputs: list[dict[str, Any]] | None = None,
    record_fields: dict[str, str] | None = None,
) -> dict[str, Any]:
    artifact = storage / relative_path
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    manifest = artifact.with_name(f"{artifact.name}.manifest.json")
    sidecar = artifact.with_name(f"{artifact.name}.sha256")
    manifest_data = {
        "schema_version": "artifact-manifest/v1",
        "artifact_id": "private-artifact-id",
        "producer": "collector-test",
        "kind": kind,
        "path": str(artifact),
        "filename": artifact.name,
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
            "fake_mode_flags": {
                "fake_libvirt": False,
                "fake_playground": False,
                "fake_orchestration": False,
            },
        },
        "inputs": inputs or [],
    }
    manifest_data.update(record_fields or {})
    manifest_bytes = (json.dumps(manifest_data, indent=2, sort_keys=True) + "\n").encode()
    manifest.write_bytes(manifest_bytes)
    sidecar_bytes = f"{digest}  {artifact.name}\n".encode()
    sidecar.write_bytes(sidecar_bytes)
    return {
        "artifact": artifact,
        "digest": digest,
        "manifest": manifest,
        "manifest_bytes": manifest_bytes,
        "manifest_data": manifest_data,
        "payload": payload,
        "sidecar": sidecar,
        "sidecar_bytes": sidecar_bytes,
    }


def _proof(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def scenario(tmp_path: Path, repo: Path) -> dict[str, Any]:
    storage = tmp_path / "storage"
    storage.mkdir()
    artifacts: dict[int, dict[str, Any]] = {}
    for number in (1, 2, 3, 4, 6, 7):
        record_type = "acquisition" if number in {6, 7} else "job"
        kind = "disk" if number in {2, 7} else "memory"
        relative_path = "captures/memory.dump" if number == 1 else f"captures/row-{number}/{kind}-{number}.dump"
        artifacts[number] = _make_artifact(
            storage,
            relative_path=relative_path,
            payload=f"real artifact row {number}\n".encode(),
            kind=kind,
            record_fields={f"{record_type}_id": f"private-record-{number}"},
        )
    proof_root = tmp_path / "source-proofs"
    rows: list[dict[str, Any]] = []
    for number in range(1, 8):
        is_ui = number in {4, 6, 7}
        evidence = _proof(proof_root / f"row-{number}-evidence.log", f"row {number} evidence\n")
        record_type = "workload" if number == 5 else ("acquisition" if number in {6, 7} else "job")
        artifact = artifacts.get(number)
        row: dict[str, Any] = {
            "row": number,
            "driver": "ui" if is_ui else "api",
            "record_type": record_type,
            "record_id": f"private-record-{number}",
            "status": "completed",
            "artifact_path": None if artifact is None else str(artifact["artifact"]),
            "manifest_path": None if artifact is None else str(artifact["manifest"]),
            "sidecar_path": None if artifact is None else str(artifact["sidecar"]),
            "ui_proof": None,
            "evidence_paths": [str(evidence)],
            "command_log_ref": "release-command-1" if number == 1 else None,
            "waiver": None,
        }
        if is_ui:
            trace = _proof(proof_root / f"row-{number}-trace.zip", f"trace {number}\n")
            screenshot = _proof(proof_root / f"row-{number}-screen.png", f"screen {number}\n")
            row["ui_proof"] = {
                "trace_path": str(trace),
                "screenshot_paths": [str(screenshot)],
            }
        rows.append(row)
    rows_file = tmp_path / "rows.json"
    rows_data = {"schema_version": ROWS_SCHEMA, "rows": rows}
    _write_json(rows_file, rows_data)
    command_log = _proof(tmp_path / "source-command.log", "release-command-1: completed\n")
    return {
        "artifact": artifacts[1],
        "artifacts": artifacts,
        "command_log": command_log,
        "evidence": tmp_path / "evidence",
        "proof_root": proof_root,
        "repo": repo,
        "rows_data": rows_data,
        "rows_file": rows_file,
        "storage": storage,
        "tmp_path": tmp_path,
    }


def _save_rows(scenario: dict[str, Any]) -> None:
    _write_json(scenario["rows_file"], scenario["rows_data"])


def _save_manifest(scenario: dict[str, Any]) -> None:
    artifact = scenario["artifact"]
    _write_json(artifact["manifest"], artifact["manifest_data"])


def _collect(
    collector: types.ModuleType,
    scenario: dict[str, Any],
    *,
    command_log: Path | None | object = ...,  # Ellipsis means use the valid fixture log.
    evidence_root: Path | None = None,
    now: datetime = FIXED_NOW,
) -> Path:
    selected_log = scenario["command_log"] if command_log is ... else command_log
    return collector.collect_bundle(
        scenario["repo"],
        scenario["storage"],
        evidence_root or scenario["evidence"],
        scenario["rows_file"],
        command_log=selected_log,
        now=now,
    )


def _assert_rejected(collector: types.ModuleType, scenario: dict[str, Any], match: str) -> None:
    with pytest.raises(collector.CollectionError, match=match):
        _collect(collector, scenario)
    evidence = scenario["evidence"]
    if evidence.exists():
        assert not list(evidence.glob(".tmp-*"))


def test_thin_wrapper_exactly_delegates_and_is_executable(tmp_path: Path) -> None:
    assert WRAPPER_PATH.read_text(encoding="utf-8") == (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"\n'
        'exec "${ASTINUSLAB_PYTHON:-python3}" "$SCRIPT_DIR/collect_release_evidence.py" "$@"\n'
    )
    assert WRAPPER_PATH.stat().st_mode & stat.S_IXUSR

    recorder = tmp_path / "python-recorder"
    output = tmp_path / "argv.json"
    recorder.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "pathlib.Path(os.environ['RECORDED_ARGV']).write_text(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    recorder.chmod(0o755)
    env = os.environ.copy()
    env.update({"ASTINUSLAB_PYTHON": str(recorder), "RECORDED_ARGV": str(output)})
    result = subprocess.run(
        [str(WRAPPER_PATH), "--rows-file", "rows with spaces.json", "--command-log", "commands.log"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8")) == [
        str(COLLECTOR_PATH),
        "--rows-file",
        "rows with spaces.json",
        "--command-log",
        "commands.log",
    ]


def test_help_works_with_system_python_from_arbitrary_cwd(tmp_path: Path) -> None:
    python3 = shutil.which("python3")
    assert python3 is not None
    result = subprocess.run(
        [python3, str(COLLECTOR_PATH), "--help"],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "--rows-file" in result.stdout
    assert "--command-log" in result.stdout
    assert "ASTINUSLAB_STORAGE_DIR" in result.stdout


@pytest.mark.parametrize("state_variable", ["XDG_STATE_HOME", "HOME"])
def test_default_evidence_root_is_outside_repo(
    collector: types.ModuleType,
    repo: Path,
    tmp_path: Path,
    state_variable: str,
) -> None:
    state_home = tmp_path / "operator-state"
    environ = {state_variable: str(state_home)}
    result = collector.default_evidence_root(repo, environ=environ)
    expected_base = state_home if state_variable == "XDG_STATE_HOME" else state_home / ".local/state"
    assert result == (expected_base / "astinuslab/release-evidence").resolve()
    assert not result.is_relative_to(repo.resolve())


def test_collects_private_and_sanitized_public_bundle(
    collector: types.ModuleType,
    scenario: dict[str, Any],
) -> None:
    bundle = _collect(collector, scenario)
    commit = _run_git(scenario["repo"], "rev-parse", "HEAD").stdout.strip()
    expected_name = f"20260801T010203.456789Z-{commit}"
    assert bundle == scenario["evidence"] / expected_name
    assert stat.S_IMODE(scenario["evidence"].stat().st_mode) == 0o700
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o700

    expected_root_files = {
        "HOST_WORKFLOW_CHECKLIST.md",
        "SHA256SUMS.txt",
        "SOURCE_COMMIT.txt",
        "evidence.json",
        "operator-command.log",
    }
    assert expected_root_files <= {path.name for path in bundle.iterdir() if path.is_file()}
    assert {path.name for path in (bundle / "public").iterdir()} == {
        "HOST_WORKFLOW_CHECKLIST.md",
        "SHA256SUMS.txt",
        "evidence-summary.json",
    }
    for path in bundle.rglob("*"):
        if path.is_dir():
            assert stat.S_IMODE(path.stat().st_mode) == 0o700
        else:
            assert stat.S_IMODE(path.stat().st_mode) == 0o600

    source_text = (bundle / "SOURCE_COMMIT.txt").read_text(encoding="utf-8")
    assert f"commit={commit}\n" in source_text
    assert "branch=main\n" in source_text
    assert "detached=false\n" in source_text
    assert "tracked_clean=true\n" in source_text

    private = json.loads((bundle / "evidence.json").read_text(encoding="utf-8"))
    assert private["schema_version"] == PRIVATE_SCHEMA
    assert private["source"] == {
        "branch": "main",
        "commit": commit,
        "detached": False,
        "tracked_clean": True,
    }
    assert private["storage_root"] == str(scenario["storage"].resolve())
    assert [row["row"] for row in private["rows"]] == list(range(1, 8))
    assert len(private["artifacts"]) == 6
    artifact = private["artifacts"][0]
    assert artifact["artifact_path"] == "captures/memory.dump"
    assert artifact["manifest_path"] == "captures/memory.dump.manifest.json"
    assert artifact["sidecar_path"] == "captures/memory.dump.sha256"
    assert artifact["bundle_manifest_path"] == "manifests/captures/memory.dump.manifest.json"
    assert artifact["bundle_sidecar_path"] == "sidecars/captures/memory.dump.sha256"
    assert artifact["sha256"] == scenario["artifact"]["digest"]
    assert artifact["size_bytes"] == len(scenario["artifact"]["payload"])
    assert (bundle / artifact["bundle_manifest_path"]).read_bytes() == scenario["artifact"]["manifest_bytes"]
    assert (bundle / artifact["bundle_sidecar_path"]).read_bytes() == scenario["artifact"]["sidecar_bytes"]
    for source in scenario["artifacts"].values():
        assert not any(path.name == source["artifact"].name for path in bundle.rglob("*"))

    for row in private["rows"]:
        if row["row"] == 5:
            assert row["artifact_path"] is None
        else:
            source = scenario["artifacts"][row["row"]]
            assert row["artifact_path"] == source["artifact"].relative_to(scenario["storage"]).as_posix()
        assert all(not Path(path).is_absolute() for path in row["evidence_paths"])
        for relative in row["evidence_paths"]:
            assert (bundle / relative).is_file()
        if row["ui_proof"] is not None:
            assert not Path(row["ui_proof"]["trace_path"]).is_absolute()
            assert (bundle / row["ui_proof"]["trace_path"]).is_file()
            for screenshot in row["ui_proof"]["screenshot_paths"]:
                assert not Path(screenshot).is_absolute()
                assert (bundle / screenshot).is_file()
    assert (bundle / "operator-command.log").read_bytes() == scenario["command_log"].read_bytes()
    expected_private_sums = "".join(
        f"{item['digest']}  {item['artifact'].relative_to(scenario['storage']).as_posix()}\n"
        for item in sorted(scenario["artifacts"].values(), key=lambda item: str(item["artifact"]))
    )
    assert (bundle / "SHA256SUMS.txt").read_text(encoding="utf-8") == expected_private_sums

    public = json.loads((bundle / "public/evidence-summary.json").read_text(encoding="utf-8"))
    assert public["schema_version"] == PUBLIC_SCHEMA
    assert public["source"] == {"commit": commit}
    assert [row["row"] for row in public["rows"]] == list(range(1, 8))
    assert set(public["rows"][0]) == {"artifact_path", "driver", "row", "sha256", "size_bytes", "status"}
    assert public["rows"][4]["artifact_path"] is None
    assert [row["artifact_path"] for row in public["rows"]] == [
        "row-1/artifact",
        "row-2/artifact",
        "row-3/artifact",
        "row-4/artifact",
        None,
        "row-6/artifact",
        "row-7/artifact",
    ]
    expected_public_sums = "".join(
        f"{scenario['artifacts'][number]['digest']}  row-{number}/artifact\n" for number in (1, 2, 3, 4, 6, 7)
    )
    assert (bundle / "public/SHA256SUMS.txt").read_text(encoding="utf-8") == expected_public_sums
    private_checklist = (bundle / "HOST_WORKFLOW_CHECKLIST.md").read_text(encoding="utf-8")
    for heading in (
        "Manifest",
        "Sidecar",
        "Size bytes",
        "UI/backend evidence",
        "Command log ref",
    ):
        assert heading in private_checklist
    assert private["rows"][0]["manifest_path"] in private_checklist
    assert private["rows"][0]["sidecar_path"] in private_checklist
    assert private["rows"][3]["ui_proof"]["trace_path"] in private_checklist


def test_detached_head_is_recorded(collector: types.ModuleType, scenario: dict[str, Any]) -> None:
    _run_git(scenario["repo"], "checkout", "-q", "--detach")
    bundle = _collect(collector, scenario)
    evidence = json.loads((bundle / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["source"]["branch"] == "DETACHED"
    assert evidence["source"]["detached"] is True
    assert "branch=DETACHED\n" in (bundle / "SOURCE_COMMIT.txt").read_text(encoding="utf-8")


def test_dirty_tracked_checkout_is_rejected(collector: types.ModuleType, scenario: dict[str, Any]) -> None:
    (scenario["repo"] / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    _assert_rejected(collector, scenario, "tracked.*clean")


def test_untracked_files_do_not_block_collection(collector: types.ModuleType, scenario: dict[str, Any]) -> None:
    (scenario["repo"] / "untracked.txt").write_text("allowed\n", encoding="utf-8")
    bundle = _collect(collector, scenario)
    assert bundle.is_dir()


def test_existing_final_bundle_collision_is_refused_without_overwrite(
    collector: types.ModuleType,
    scenario: dict[str, Any],
) -> None:
    bundle = _collect(collector, scenario)
    marker = bundle / "keep-me"
    marker.write_text("original", encoding="utf-8")
    with pytest.raises(collector.CollectionError, match="collision|exists"):
        _collect(collector, scenario)
    assert marker.read_text(encoding="utf-8") == "original"
    assert not list(scenario["evidence"].glob(".tmp-*"))


@pytest.mark.parametrize(
    ("field", "replacement", "match"),
    [
        ("sha256", "f" * 64, "digest|sha256"),
        ("size_bytes", 999, "size"),
        ("filename", "different.dump", "filename"),
        ("path", "/definitely/not/the/artifact", "path"),
    ],
)
def test_manifest_integrity_mismatches_are_rejected(
    collector: types.ModuleType,
    scenario: dict[str, Any],
    field: str,
    replacement: object,
    match: str,
) -> None:
    scenario["artifact"]["manifest_data"][field] = replacement
    _save_manifest(scenario)
    _assert_rejected(collector, scenario, match)


@pytest.mark.parametrize(
    ("sidecar_bytes", "match"),
    [
        (("f" * 64 + "  memory.dump\n").encode(), "digest|sha256"),
        (("0" * 64 + "  other.dump\n").encode(), "filename|name"),
        (("0" * 64 + " memory.dump\n").encode(), "format"),
        (("0" * 64 + "  memory.dump").encode(), "format"),
        (("A" * 64 + "  memory.dump\n").encode(), "format"),
    ],
)
def test_sidecar_digest_name_and_format_mismatches_are_rejected(
    collector: types.ModuleType,
    scenario: dict[str, Any],
    sidecar_bytes: bytes,
    match: str,
) -> None:
    scenario["artifact"]["sidecar"].write_bytes(sidecar_bytes)
    _assert_rejected(collector, scenario, match)


def test_fake_mode_true_is_rejected(collector: types.ModuleType, scenario: dict[str, Any]) -> None:
    scenario["artifact"]["manifest_data"]["integrity"]["fake_mode"] = True
    _save_manifest(scenario)
    _assert_rejected(collector, scenario, "fake_mode")


@pytest.mark.parametrize("flag", ["fake_libvirt", "fake_playground", "fake_orchestration"])
def test_each_truthy_fake_mode_flag_is_rejected(
    collector: types.ModuleType,
    scenario: dict[str, Any],
    flag: str,
) -> None:
    scenario["artifact"]["manifest_data"]["integrity"]["fake_mode_flags"][flag] = True
    _save_manifest(scenario)
    _assert_rejected(collector, scenario, "fake_mode_flags|fake flag")


@pytest.mark.parametrize("component", ["artifact", "manifest", "sidecar"])
def test_symlinked_artifact_triad_component_is_rejected(
    collector: types.ModuleType,
    scenario: dict[str, Any],
    component: str,
) -> None:
    source = scenario["artifact"][component]
    outside = scenario["tmp_path"] / f"outside-{component}"
    outside.write_bytes(source.read_bytes())
    source.unlink()
    source.symlink_to(outside)
    _assert_rejected(collector, scenario, "symlink|outside|regular")


@pytest.mark.parametrize("field", ["artifact_path", "manifest_path", "sidecar_path"])
def test_row_artifact_triad_path_outside_storage_is_rejected(
    collector: types.ModuleType,
    scenario: dict[str, Any],
    field: str,
) -> None:
    outside = _proof(scenario["tmp_path"] / f"outside-{field}", "outside\n")
    scenario["rows_data"]["rows"][0][field] = str(outside)
    _save_rows(scenario)
    _assert_rejected(collector, scenario, "storage|reference|artifact")


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "out-of-range"])
def test_rows_must_be_exactly_one_each_from_one_through_seven(
    collector: types.ModuleType,
    scenario: dict[str, Any],
    mutation: str,
) -> None:
    rows = scenario["rows_data"]["rows"]
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows.append(copy.deepcopy(rows[0]))
    else:
        rows[-1]["row"] = 8
    _save_rows(scenario)
    _assert_rejected(collector, scenario, "rows|duplicate|1.*7")


@pytest.mark.parametrize("mutation", ["artifact", "partial", "no-evidence"])
def test_row_five_requires_null_triad_and_at_least_one_evidence_file(
    collector: types.ModuleType,
    scenario: dict[str, Any],
    mutation: str,
) -> None:
    row = scenario["rows_data"]["rows"][4]
    if mutation == "artifact":
        row["artifact_path"] = str(scenario["artifact"]["artifact"])
        row["manifest_path"] = str(scenario["artifact"]["manifest"])
        row["sidecar_path"] = str(scenario["artifact"]["sidecar"])
    elif mutation == "partial":
        row["manifest_path"] = str(scenario["artifact"]["manifest"])
    else:
        row["evidence_paths"] = []
    _save_rows(scenario)
    _assert_rejected(collector, scenario, "row 5")


def test_required_ui_row_cannot_claim_api_driver(collector: types.ModuleType, scenario: dict[str, Any]) -> None:
    scenario["rows_data"]["rows"][3]["driver"] = "api"
    scenario["rows_data"]["rows"][3]["ui_proof"] = None
    _save_rows(scenario)
    _assert_rejected(collector, scenario, "row 4.*ui")


@pytest.mark.parametrize("mutation", ["missing-trace", "empty-screenshots", "missing-screenshot"])
def test_ui_rows_require_existing_trace_and_nonempty_existing_screenshots(
    collector: types.ModuleType,
    scenario: dict[str, Any],
    mutation: str,
) -> None:
    ui_proof = scenario["rows_data"]["rows"][3]["ui_proof"]
    assert ui_proof is not None
    if mutation == "missing-trace":
        ui_proof["trace_path"] = str(scenario["tmp_path"] / "missing-trace.zip")
    elif mutation == "empty-screenshots":
        ui_proof["screenshot_paths"] = []
    else:
        ui_proof["screenshot_paths"] = [str(scenario["tmp_path"] / "missing-screen.png")]
    _save_rows(scenario)
    _assert_rejected(collector, scenario, "trace|screenshot|proof")


def test_noncompleted_status_is_rejected(collector: types.ModuleType, scenario: dict[str, Any]) -> None:
    scenario["rows_data"]["rows"][0]["status"] = "failed"
    _save_rows(scenario)
    _assert_rejected(collector, scenario, "completed")


def test_command_log_reference_without_supplied_log_is_rejected(
    collector: types.ModuleType,
    scenario: dict[str, Any],
) -> None:
    with pytest.raises(collector.CollectionError, match="command.*log"):
        _collect(collector, scenario, command_log=None)


@pytest.mark.parametrize("mutation", ["unknown-top", "unknown-row", "missing-row-key", "bad-schema"])
def test_rows_input_rejects_unknown_missing_and_malformed_keys(
    collector: types.ModuleType,
    scenario: dict[str, Any],
    mutation: str,
) -> None:
    if mutation == "unknown-top":
        scenario["rows_data"]["unexpected"] = True
    elif mutation == "unknown-row":
        scenario["rows_data"]["rows"][0]["unexpected"] = True
    elif mutation == "missing-row-key":
        del scenario["rows_data"]["rows"][0]["record_id"]
    else:
        scenario["rows_data"]["schema_version"] = "wrong/v1"
    _save_rows(scenario)
    _assert_rejected(collector, scenario, "schema|keys|record_id|unexpected")


def test_waiver_shape_is_strict_but_valid_waiver_is_preserved(
    collector: types.ModuleType,
    scenario: dict[str, Any],
) -> None:
    waiver = {
        "row": 2,
        "reason": "documented exception",
        "impact": "limited confidence",
        "owner": "release-owner",
        "decision_owner": "security-owner",
        "expires_or_retest": "2026-08-15",
    }
    scenario["rows_data"]["rows"][1]["waiver"] = waiver
    _save_rows(scenario)
    bundle = _collect(collector, scenario)
    private = json.loads((bundle / "evidence.json").read_text(encoding="utf-8"))
    assert private["rows"][1]["waiver"] == waiver

    scenario["rows_data"]["rows"][1]["waiver"]["extra"] = "not allowed"
    _save_rows(scenario)
    with pytest.raises(collector.CollectionError, match="waiver"):
        collector.collect_bundle(
            scenario["repo"],
            scenario["storage"],
            scenario["tmp_path"] / "second-evidence",
            scenario["rows_file"],
            command_log=scenario["command_log"],
            now=FIXED_NOW,
        )


@pytest.mark.parametrize("direction", ["evidence-inside-storage", "storage-inside-evidence"])
def test_evidence_and_storage_overlap_is_rejected(
    collector: types.ModuleType,
    scenario: dict[str, Any],
    direction: str,
) -> None:
    evidence = (
        scenario["storage"] / "release-evidence"
        if direction == "evidence-inside-storage"
        else scenario["storage"].parent
    )
    with pytest.raises(collector.CollectionError, match="overlap"):
        _collect(collector, scenario, evidence_root=evidence)


def test_nonignored_in_repo_evidence_root_is_rejected(
    collector: types.ModuleType,
    scenario: dict[str, Any],
) -> None:
    with pytest.raises(collector.CollectionError, match="ignored"):
        _collect(collector, scenario, evidence_root=scenario["repo"] / "release-evidence")


def test_ignored_in_repo_evidence_root_is_allowed(
    collector: types.ModuleType,
    scenario: dict[str, Any],
) -> None:
    (scenario["repo"] / ".gitignore").write_text("/private-evidence/\n", encoding="utf-8")
    _run_git(scenario["repo"], "add", ".gitignore")
    _run_git(scenario["repo"], "commit", "-q", "-m", "ignore private evidence")
    bundle = _collect(collector, scenario, evidence_root=scenario["repo"] / "private-evidence")
    assert bundle.parent == scenario["repo"] / "private-evidence"


def test_public_projection_contains_no_private_identifiers_or_host_paths(
    collector: types.ModuleType,
    scenario: dict[str, Any],
) -> None:
    private_username = "collector-private-user"
    private_record_id = f"{private_username}-record"
    scenario["rows_data"]["rows"][0]["record_id"] = private_record_id
    scenario["artifact"]["manifest_data"]["job_id"] = private_record_id
    _save_manifest(scenario)
    scenario["rows_data"]["rows"][0]["waiver"] = {
        "row": 1,
        "reason": f"private reason for {private_username}",
        "impact": "private impact",
        "owner": private_username,
        "decision_owner": "private-decision-owner",
        "expires_or_retest": "2026-12-31",
    }
    _save_rows(scenario)
    bundle = _collect(collector, scenario)
    public_files = [path for path in (bundle / "public").rglob("*") if path.is_file()]
    public_text = "\n".join(path.read_text(encoding="utf-8") for path in public_files)
    for forbidden in (
        private_username,
        "private-record",
        "private-artifact-id",
        str(scenario["storage"]),
        str(scenario["proof_root"]),
        str(scenario["command_log"]),
        "source-command.log",
        "trace.zip",
        "screen.png",
        "manifest.json",
        ".sha256",
        "fake_mode",
    ):
        assert forbidden not in public_text
    public = json.loads((bundle / "public/evidence-summary.json").read_text(encoding="utf-8"))
    assert set(public) == {"rows", "schema_version", "source"}
    assert set(public["source"]) == {"commit"}
    assert all(
        set(row) == {"artifact_path", "driver", "row", "sha256", "size_bytes", "status"} for row in public["rows"]
    )


def test_isf_and_raw_memory_path_overlap_is_rejected(
    collector: types.ModuleType,
    scenario: dict[str, Any],
) -> None:
    shared = _proof(scenario["tmp_path"] / "inputs/shared.raw", "input\n")
    scenario["artifact"]["manifest_data"]["kind"] = "report"
    scenario["artifact"]["manifest_data"]["inputs"] = [
        {"kind": "memory", "path": str(shared)},
        {"kind": "isf", "path": str(shared)},
    ]
    _save_manifest(scenario)
    _assert_rejected(collector, scenario, "ISF.*memory|memory.*ISF|overlap")


def test_unreferenced_manifest_is_discovered_and_validated(
    collector: types.ModuleType,
    scenario: dict[str, Any],
) -> None:
    unreferenced = _make_artifact(
        scenario["storage"],
        relative_path="unreferenced/report.json",
        payload=b"unreferenced report",
        kind="report",
    )
    unreferenced["manifest_data"]["integrity"]["fake_mode"] = True
    _write_json(unreferenced["manifest"], unreferenced["manifest_data"])
    _assert_rejected(collector, scenario, "fake_mode")


def test_valid_unreferenced_manifest_is_included_in_private_inventory(
    collector: types.ModuleType,
    scenario: dict[str, Any],
) -> None:
    _make_artifact(
        scenario["storage"],
        relative_path="unreferenced/report.json",
        payload=b"unreferenced report",
        kind="report",
    )
    bundle = _collect(collector, scenario)
    private = json.loads((bundle / "evidence.json").read_text(encoding="utf-8"))
    artifact_paths = [artifact["artifact_path"] for artifact in private["artifacts"]]
    assert artifact_paths == sorted(artifact_paths)
    assert "unreferenced/report.json" in artifact_paths
    public_files = b"".join(path.read_bytes() for path in (bundle / "public").iterdir())
    assert b"unreferenced" not in public_files


def test_proof_basename_collision_is_rejected(
    collector: types.ModuleType,
    scenario: dict[str, Any],
) -> None:
    first = _proof(scenario["tmp_path"] / "one/same.log", "one\n")
    second = _proof(scenario["tmp_path"] / "two/same.log", "two\n")
    scenario["rows_data"]["rows"][0]["evidence_paths"] = [str(first), str(second)]
    _save_rows(scenario)
    _assert_rejected(collector, scenario, "collision")


@pytest.mark.parametrize("source_kind", ["symlink", "directory"])
def test_proof_sources_must_be_regular_non_symlink_files(
    collector: types.ModuleType,
    scenario: dict[str, Any],
    source_kind: str,
) -> None:
    source = scenario["tmp_path"] / "bad-proof"
    if source_kind == "symlink":
        target = _proof(scenario["tmp_path"] / "proof-target", "target\n")
        source.symlink_to(target)
    else:
        source.mkdir()
    scenario["rows_data"]["rows"][0]["evidence_paths"] = [str(source)]
    _save_rows(scenario)
    _assert_rejected(collector, scenario, "regular|symlink")


def test_storage_root_and_rows_file_must_exist_and_not_be_symlinks(
    collector: types.ModuleType,
    scenario: dict[str, Any],
) -> None:
    missing_storage = scenario["tmp_path"] / "missing-storage"
    with pytest.raises(collector.CollectionError, match="storage"):
        collector.collect_bundle(
            scenario["repo"],
            missing_storage,
            scenario["evidence"],
            scenario["rows_file"],
            command_log=scenario["command_log"],
            now=FIXED_NOW,
        )

    rows_target = scenario["rows_file"]
    rows_link = scenario["tmp_path"] / "rows-link.json"
    rows_link.symlink_to(rows_target)
    with pytest.raises(collector.CollectionError, match="symlink"):
        collector.collect_bundle(
            scenario["repo"],
            scenario["storage"],
            scenario["evidence"],
            rows_link,
            command_log=scenario["command_log"],
            now=FIXED_NOW,
        )


def test_collector_interface_requires_absolute_row_source_paths(
    collector: types.ModuleType,
    scenario: dict[str, Any],
) -> None:
    scenario["rows_data"]["rows"][0]["artifact_path"] = "captures/memory.dump"
    _save_rows(scenario)
    _assert_rejected(collector, scenario, "absolute")


def test_all_rows_have_the_exact_documented_key_set(scenario: dict[str, Any]) -> None:
    assert all(set(row) == ROW_KEYS for row in scenario["rows_data"]["rows"])


@pytest.mark.parametrize("target", ["rows", "manifest"])
def test_duplicate_json_keys_are_rejected_fail_closed(
    collector: types.ModuleType,
    scenario: dict[str, Any],
    target: str,
) -> None:
    path = scenario["rows_file"] if target == "rows" else scenario["artifact"]["manifest"]
    text = path.read_text(encoding="utf-8")
    if target == "rows":
        needle = '"schema_version": "astinuslab-release-evidence-rows/v1"'
    else:
        needle = '"schema_version": "artifact-manifest/v1"'
    path.write_text(text.replace(needle, f"{needle},\n  {needle}", 1), encoding="utf-8")

    _assert_rejected(collector, scenario, "duplicate JSON key")


def test_row_record_identity_must_match_artifact_manifest(
    collector: types.ModuleType,
    scenario: dict[str, Any],
) -> None:
    scenario["rows_data"]["rows"][0]["record_id"] = "different-job"
    _save_rows(scenario)
    _assert_rejected(collector, scenario, "record.*manifest|manifest.*record")


def test_row_record_identities_and_artifact_references_must_be_unique(
    collector: types.ModuleType,
    scenario: dict[str, Any],
) -> None:
    first = scenario["rows_data"]["rows"][0]
    second = scenario["rows_data"]["rows"][1]
    for key in ("record_type", "record_id", "artifact_path", "manifest_path", "sidecar_path"):
        second[key] = first[key]
    _save_rows(scenario)
    _assert_rejected(collector, scenario, "duplicate.*record|duplicate.*artifact")


def test_existing_evidence_root_permissions_are_rejected_without_chmod(
    collector: types.ModuleType,
    scenario: dict[str, Any],
) -> None:
    scenario["evidence"].mkdir(mode=0o755)
    scenario["evidence"].chmod(0o755)
    with pytest.raises(collector.CollectionError, match="evidence root.*0700|private"):
        _collect(collector, scenario)
    assert stat.S_IMODE(scenario["evidence"].stat().st_mode) == 0o755


def test_source_change_before_publication_is_rejected_and_partial_output_removed(
    collector: types.ModuleType,
    scenario: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = collector._normalize_bundle_modes

    def dirty_source(bundle: Path) -> None:
        original(bundle)
        (scenario["repo"] / "tracked.txt").write_text("changed during collection\n", encoding="utf-8")

    monkeypatch.setattr(collector, "_normalize_bundle_modes", dirty_source)
    with pytest.raises(collector.CollectionError, match="source.*changed|tracked.*clean"):
        _collect(collector, scenario)
    assert not list(scenario["evidence"].glob(".tmp-*"))
    assert not [path for path in scenario["evidence"].iterdir() if not path.name.startswith(".tmp-")]


def test_storage_tree_symlink_directory_is_rejected(
    collector: types.ModuleType,
    scenario: dict[str, Any],
) -> None:
    outside = scenario["tmp_path"] / "outside-storage-directory"
    outside.mkdir()
    (scenario["storage"] / "linked-directory").symlink_to(outside, target_is_directory=True)
    _assert_rejected(collector, scenario, "symlink")


def test_nonboolean_false_fake_mode_flag_is_rejected(
    collector: types.ModuleType,
    scenario: dict[str, Any],
) -> None:
    scenario["artifact"]["manifest_data"]["integrity"]["fake_mode_flags"]["fake_libvirt"] = 0
    _save_manifest(scenario)
    _assert_rejected(collector, scenario, "fake_mode_flags.*boolean|fake flag")


def test_raw_artifact_cannot_be_copied_as_proof(
    collector: types.ModuleType,
    scenario: dict[str, Any],
) -> None:
    scenario["rows_data"]["rows"][0]["evidence_paths"] = [str(scenario["artifact"]["artifact"])]
    _save_rows(scenario)
    _assert_rejected(collector, scenario, "proof.*artifact|raw artifact")


@pytest.mark.parametrize(
    "mutation",
    ["missing-producer", "schema-bool", "integrity-digest", "empty-fake-flags"],
)
def test_manifest_core_schema_and_integrity_are_strict(
    collector: types.ModuleType,
    scenario: dict[str, Any],
    mutation: str,
) -> None:
    manifest = scenario["artifact"]["manifest_data"]
    if mutation == "missing-producer":
        del manifest["producer"]
    elif mutation == "schema-bool":
        manifest["schema_version"] = True
    elif mutation == "integrity-digest":
        manifest["integrity"]["raw_sha256"] = "not-a-sha256"
    else:
        manifest["integrity"]["fake_mode_flags"] = {}
    _save_manifest(scenario)
    _assert_rejected(collector, scenario, "manifest|schema|integrity|fake")


def test_nonfinite_json_constant_is_rejected(
    collector: types.ModuleType,
    scenario: dict[str, Any],
) -> None:
    path = scenario["artifact"]["manifest"]
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("{\n", '{\n  "unexpected_numeric": NaN,\n', 1), encoding="utf-8")
    _assert_rejected(collector, scenario, "JSON|non-finite|constant")


def test_boolean_waiver_row_is_rejected(
    collector: types.ModuleType,
    scenario: dict[str, Any],
) -> None:
    scenario["rows_data"]["rows"][0]["waiver"] = {
        "row": True,
        "reason": "reason",
        "impact": "impact",
        "owner": "owner",
        "decision_owner": "decision owner",
        "expires_or_retest": "2026-08-15",
    }
    _save_rows(scenario)
    _assert_rejected(collector, scenario, "waiver.*row|integer")


def test_row_record_type_must_match_mandatory_workflow(
    collector: types.ModuleType,
    scenario: dict[str, Any],
) -> None:
    scenario["rows_data"]["rows"][0]["record_type"] = "acquisition"
    scenario["artifact"]["manifest_data"]["acquisition_id"] = "private-record-1"
    _save_rows(scenario)
    _save_manifest(scenario)
    _assert_rejected(collector, scenario, "row 1.*record_type|record type")


def test_mandatory_capture_rows_require_expected_artifact_kind(
    collector: types.ModuleType,
    scenario: dict[str, Any],
) -> None:
    second = scenario["artifacts"][2]
    second["manifest_data"]["kind"] = "memory"
    _write_json(second["manifest"], second["manifest_data"])
    _assert_rejected(collector, scenario, "row 2.*disk|artifact kind")


def test_git_source_ignores_inherited_git_directory_override(
    collector: types.ModuleType,
    scenario: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = scenario["tmp_path"] / "other-repo"
    other.mkdir()
    _run_git(other, "init", "-q", "-b", "other")
    _run_git(other, "config", "user.email", "other@example.invalid")
    _run_git(other, "config", "user.name", "Other")
    (other / "tracked").write_text("other\n", encoding="utf-8")
    _run_git(other, "add", "tracked")
    _run_git(other, "commit", "-q", "-m", "other")
    expected = _run_git(scenario["repo"], "rev-parse", "HEAD").stdout.strip()
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    source = collector._git_source(scenario["repo"])
    assert source["commit"] == expected


def test_path_replacement_during_hash_is_rejected(
    collector: types.ModuleType,
    scenario: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = scenario["artifact"]["artifact"]
    moved = artifact.with_name("moved-artifact")
    original_read = collector.os.read
    replaced = False

    def replace_path(fd: int, amount: int) -> bytes:
        nonlocal replaced
        chunk = original_read(fd, amount)
        if chunk and not replaced:
            replaced = True
            artifact.rename(moved)
            artifact.write_bytes(b"x" * len(scenario["artifact"]["payload"]))
        return chunk

    monkeypatch.setattr(collector.os, "read", replace_path)
    with pytest.raises(collector.CollectionError, match="changed|identity"):
        collector._hash_regular_file(artifact, "artifact", root=scenario["storage"].resolve())


def test_preexisting_temp_collision_is_not_removed(
    collector: types.ModuleType,
    scenario: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario["evidence"].mkdir(mode=0o700)
    fixed = types.SimpleNamespace(hex="fixed-temp-name")
    monkeypatch.setattr(collector, "uuid4", lambda: fixed)
    temp = scenario["evidence"] / ".tmp-fixed-temp-name"
    temp.mkdir(mode=0o700)
    marker = temp / "owner-data"
    marker.write_text("preserve\n", encoding="utf-8")
    with pytest.raises(collector.CollectionError, match="temporary|collision|exists"):
        _collect(collector, scenario)
    assert marker.read_text(encoding="utf-8") == "preserve\n"


def test_atomic_no_replace_unavailable_fails_closed(
    collector: types.ModuleType,
    scenario: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(collector.ctypes, "CDLL", lambda *args, **kwargs: types.SimpleNamespace())
    with pytest.raises(collector.CollectionError, match="no-replace|atomic"):
        _collect(collector, scenario)
    assert not list(scenario["evidence"].glob(".tmp-*"))


def test_source_artifact_change_before_publication_is_rejected(
    collector: types.ModuleType,
    scenario: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = collector._normalize_bundle_modes

    def mutate_artifact(bundle: Path) -> None:
        original(bundle)
        scenario["artifact"]["artifact"].write_bytes(b"changed after initial hash\n")

    monkeypatch.setattr(collector, "_normalize_bundle_modes", mutate_artifact)
    with pytest.raises(collector.CollectionError, match="artifact.*changed|source.*changed|digest"):
        _collect(collector, scenario)
    assert not list(scenario["evidence"].glob(".tmp-*"))


def test_special_output_entry_is_rejected_before_publication(
    collector: types.ModuleType,
    scenario: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = collector._copy_proofs

    def inject_fifo(temp_bundle: Path, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        output = original(temp_bundle, rows)
        os.mkfifo(temp_bundle / "proofs" / "unexpected.pipe", mode=0o600)
        return output

    monkeypatch.setattr(collector, "_copy_proofs", inject_fifo)
    with pytest.raises(collector.CollectionError, match="regular|special|unexpected"):
        _collect(collector, scenario)
    assert not list(scenario["evidence"].glob(".tmp-*"))


@pytest.mark.parametrize("number", [1, 2, 3])
def test_rows_without_ui_proof_require_at_least_one_evidence_file(
    collector: types.ModuleType,
    scenario: dict[str, Any],
    number: int,
) -> None:
    scenario["rows_data"]["rows"][number - 1]["evidence_paths"] = []
    _save_rows(scenario)
    _assert_rejected(collector, scenario, f"row {number}.*evidence file")


def test_ui_row_may_rely_on_its_trace_and_screenshot_proof(
    collector: types.ModuleType,
    scenario: dict[str, Any],
) -> None:
    scenario["rows_data"]["rows"][3]["evidence_paths"] = []
    _save_rows(scenario)
    bundle = _collect(collector, scenario)
    evidence = json.loads((bundle / "evidence.json").read_text(encoding="utf-8"))
    row = next(item for item in evidence["rows"] if item["row"] == 4)
    assert row["evidence_paths"] == []
    assert row["ui_proof"]["screenshot_paths"]


def test_hardlinked_artifact_cannot_satisfy_two_mandatory_rows(
    collector: types.ModuleType,
    scenario: dict[str, Any],
) -> None:
    disk = scenario["artifacts"][2]
    hardlink = scenario["storage"] / "captures/row-7/hardlinked-disk.dump"
    hardlink.parent.mkdir(parents=True, exist_ok=True)
    os.link(disk["artifact"], hardlink)
    manifest_data = copy.deepcopy(disk["manifest_data"])
    manifest_data.update(
        {
            "path": str(hardlink),
            "filename": hardlink.name,
            "sha256_sidecar_path": f"{hardlink}.sha256",
        }
    )
    manifest_data.pop("job_id", None)
    manifest_data["acquisition_id"] = "private-record-7"
    _write_json(hardlink.with_name(f"{hardlink.name}.manifest.json"), manifest_data)
    hardlink.with_name(f"{hardlink.name}.sha256").write_text(
        f"{disk['digest']}  {hardlink.name}\n",
        encoding="utf-8",
    )

    seventh = scenario["rows_data"]["rows"][6]
    seventh["artifact_path"] = str(hardlink)
    seventh["manifest_path"] = str(hardlink.with_name(f"{hardlink.name}.manifest.json"))
    seventh["sidecar_path"] = str(hardlink.with_name(f"{hardlink.name}.sha256"))
    _save_rows(scenario)

    assert hardlink.stat().st_ino == disk["artifact"].stat().st_ino
    _assert_rejected(collector, scenario, "duplicate artifact")


def test_owned_cleanup_refuses_an_inode_replaced_tree_without_deleting_it(
    collector: types.ModuleType,
    tmp_path: Path,
) -> None:
    # The collector recorded one inode for its temporary tree; something else now
    # occupies that path, so cleanup must refuse rather than delete a stranger's data.
    created = tmp_path / "collector-created"
    created.mkdir(mode=0o700)
    created_stat = created.lstat()
    recorded_identity = (created_stat.st_dev, created_stat.st_ino)

    replaced = tmp_path / "collector-owned"
    replaced.mkdir(mode=0o700)
    survivor = replaced / "someone-elses-data"
    survivor.write_text("preserve\n", encoding="utf-8")
    assert replaced.lstat().st_ino != created_stat.st_ino

    with pytest.raises(collector.CollectionError, match="changed identity|unsafe cleanup"):
        collector._remove_owned_tree(replaced, recorded_identity)
    assert survivor.read_text(encoding="utf-8") == "preserve\n"


def test_owned_cleanup_removes_a_tree_it_still_owns(
    collector: types.ModuleType,
    tmp_path: Path,
) -> None:
    owned = tmp_path / "still-owned"
    owned.mkdir(mode=0o700)
    (owned / "partial-output").write_text("discard\n", encoding="utf-8")
    identity_stat = owned.lstat()

    collector._remove_owned_tree(owned, (identity_stat.st_dev, identity_stat.st_ino))
    assert not owned.exists()


def test_missing_path_is_rejected_when_existence_is_required(
    collector: types.ModuleType,
    tmp_path: Path,
) -> None:
    absent = tmp_path / "absent-parent" / "absent-leaf"
    with pytest.raises(collector.CollectionError, match="does not exist"):
        collector._reject_symlink_components(absent, "probe path")
    collector._reject_symlink_components(absent, "probe path", allow_missing_leaf=True)


def test_open_file_identity_failure_fails_closed(
    collector: types.ModuleType,
    scenario: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnresolvablePath(Path):
        def resolve(self, strict: bool = False) -> Path:
            raise OSError("procfs unavailable")

    monkeypatch.setattr(collector, "Path", lambda *args: UnresolvablePath(*args))
    with pytest.raises(collector.CollectionError, match="/proc/self/fd"):
        collector._opened_target(0, "probe file")


def _real_producer_manifest(
    artifact: dict[str, Any],
    *,
    number: int,
    record_type: str,
) -> dict[str, Any]:
    """Build a manifest with the backend's real producer, not a test-local shape."""
    from astinuslab.storage.artifacts import build_artifact_manifest

    return build_artifact_manifest(
        artifact_path=artifact["artifact"],
        kind="disk" if number in {2, 7} else "memory",
        producer="astinuslab-capture",
        sha256=artifact["digest"],
        size_bytes=len(artifact["payload"]),
        **{f"{record_type}_id": f"private-record-{number}"},
    )


def test_manifest_contract_matches_the_real_backend_producer(
    collector: types.ModuleType,
    scenario: dict[str, Any],
) -> None:
    manifest = _real_producer_manifest(scenario["artifact"], number=1, record_type="job")

    assert set(manifest["integrity"]) == collector._INTEGRITY_KEYS
    assert set(manifest["integrity"]["fake_mode_flags"]) == collector._FAKE_FLAG_KEYS
    assert collector._MANIFEST_REQUIRED_KEYS <= set(manifest)
    assert manifest["schema_version"] == collector.MANIFEST_SCHEMA_VERSION
    collector._validate_manifest_schema(manifest)


def test_collection_accepts_manifests_written_by_the_real_backend_producer(
    collector: types.ModuleType,
    scenario: dict[str, Any],
) -> None:
    for number, artifact in scenario["artifacts"].items():
        record_type = "acquisition" if number in {6, 7} else "job"
        _write_json(
            artifact["manifest"],
            _real_producer_manifest(artifact, number=number, record_type=record_type),
        )

    bundle = _collect(collector, scenario)

    evidence = json.loads((bundle / "evidence.json").read_text(encoding="utf-8"))
    assert len(evidence["rows"]) == 7
    assert len(evidence["artifacts"]) == len(scenario["artifacts"])


@pytest.mark.parametrize("expiry", ["retest at GA", "2026-13-01", "2026-08-15T10:00:00"])
def test_waiver_expiry_must_be_parseable_iso8601_at_collection_time(
    collector: types.ModuleType,
    scenario: dict[str, Any],
    expiry: str,
) -> None:
    scenario["rows_data"]["rows"][0]["waiver"] = {
        "row": 1,
        "reason": "documented",
        "impact": "limited confidence",
        "owner": "release-owner",
        "decision_owner": "security-owner",
        "expires_or_retest": expiry,
    }
    _save_rows(scenario)
    _assert_rejected(collector, scenario, "expiry/retest")


def test_private_evidence_binds_every_copied_proof_to_a_digest(
    collector: types.ModuleType,
    scenario: dict[str, Any],
) -> None:
    bundle = _collect(collector, scenario)
    evidence = json.loads((bundle / "evidence.json").read_text(encoding="utf-8"))

    recorded = {item["path"]: item for item in evidence["proofs"]}
    assert [item["path"] for item in evidence["proofs"]] == sorted(recorded)
    assert "operator-command.log" in recorded

    copied = {path.relative_to(bundle).as_posix() for path in (bundle / "proofs").rglob("*") if path.is_file()}
    assert copied | {"operator-command.log"} == set(recorded)
    for relative, entry in recorded.items():
        payload = (bundle / relative).read_bytes()
        assert entry["sha256"] == hashlib.sha256(payload).hexdigest()
        assert entry["size_bytes"] == len(payload)
