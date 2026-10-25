from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from astinuslab.api.errors import ApiError, api_error_handler
from astinuslab.api.schemas import JobCreate
from astinuslab.db.models import Artifact, ISFArtifact, Report
from astinuslab.db.repositories import JobRepository
from astinuslab.forensics import services as forensic_services
from astinuslab.forensics import vol3_tools
from astinuslab.forensics.vol3_tools import ToolDependencyError, ToolStatus, VerifiedVolatility
from tests.python._support.helpers import autospec_setattr
from tests.python._support.vol3_fixtures import completed_child
from tests.python._support.vol3_fixtures import make_completed_batch_job as _make_completed_job
from tests.python._support.vol3_fixtures import patch_batch_settings as _patch_settings
from tests.python._support.vol3_fixtures import verified_volatility as _verified_volatility


def _ready_volatility_status(path: Path = Path("/verified/vol3")) -> ToolStatus:
    return ToolStatus(
        ready=True,
        code="ready",
        detail="Volatility 3 is ready.",
        version="review-readiness-version",
        resolved_path=path,
    )


@pytest.mark.parametrize(
    ("enabled", "configured", "dependency_code"),
    [
        (False, "/configured/vol3", "feature_disabled"),
        (True, None, "volatility_not_configured"),
    ],
)
def test_enqueue_analysis_readiness_failure_has_dependency_envelope_and_no_action(
    in_memory_session_scope,
    monkeypatch,
    tmp_path,
    enabled,
    configured,
    dependency_code,
):
    job_id, _action_id, _dump_path, _isf_path = _make_completed_job(
        in_memory_session_scope,
        tmp_path,
        with_action=False,
    )
    monkeypatch.setattr(
        forensic_services,
        "settings",
        SimpleNamespace(
            vol3_enabled=enabled,
            vol3_volatility_bin=configured,
            vol3_default_plugins="linux.pslist.PsList",
        ),
    )

    with in_memory_session_scope() as session, pytest.raises(ApiError) as exc_info:
        forensic_services.enqueue_vol3_analysis_service(session, job_id)

    error = exc_info.value
    assert error.status_code == 503
    assert error.code == "dependency_unavailable"
    assert error.context == {"dependency_code": dependency_code}
    response = asyncio.run(api_error_handler(cast(Any, None), error))
    # response.body is `bytes | memoryview[int]`; json.loads takes no memoryview.
    assert json.loads(bytes(response.body)) == {
        "detail": error.detail,
        "code": "dependency_unavailable",
        "context": {"dependency_code": dependency_code},
    }
    with in_memory_session_scope() as session:
        assert JobRepository(session).list_actions(job_id) == []


def test_enqueue_analysis_rejects_unknown_default_plugins_before_action(
    in_memory_session_scope,
    monkeypatch,
    tmp_path,
):
    job_id, _action_id, _dump_path, _isf_path = _make_completed_job(
        in_memory_session_scope,
        tmp_path,
        with_action=False,
    )
    monkeypatch.setattr(
        forensic_services,
        "settings",
        SimpleNamespace(
            vol3_enabled=True,
            vol3_volatility_bin="configured-vol3",
            vol3_default_plugins="linux.pslist.PsList,linux.bogus.Nope,also.Bad",
        ),
    )
    monkeypatch.setattr(
        forensic_services,
        "check_volatility_tool",
        lambda _settings: _ready_volatility_status(),
        raising=False,
    )

    with in_memory_session_scope() as session, pytest.raises(ApiError) as exc_info:
        forensic_services.enqueue_vol3_analysis_service(session, job_id)

    assert exc_info.value.status_code == 400
    assert exc_info.value.code == "invalid_request"
    assert "linux.bogus.Nope" in exc_info.value.detail
    assert "also.Bad" in exc_info.value.detail
    with in_memory_session_scope() as session:
        assert JobRepository(session).list_actions(job_id) == []


def test_enqueue_windows_analysis_ignores_linux_defaults_and_validates_windows_plugins(
    in_memory_session_scope,
    monkeypatch,
    tmp_path,
):
    job_id, _action_id, _dump_path, _isf_path = _make_completed_job(
        in_memory_session_scope,
        tmp_path,
        with_action=False,
        with_isf=False,
        distro="windows-11",
    )
    monkeypatch.setattr(
        forensic_services,
        "settings",
        SimpleNamespace(
            vol3_enabled=True,
            vol3_volatility_bin="configured-vol3",
            vol3_default_plugins="linux.invalid.ConfigDrift",
        ),
    )
    monkeypatch.setattr(
        forensic_services,
        "check_volatility_tool",
        lambda _settings: _ready_volatility_status(),
        raising=False,
    )
    monkeypatch.setattr(forensic_services, "_dispatch_action_after_commit", lambda *_args, **_kwargs: None)

    with in_memory_session_scope() as session:
        action = forensic_services.enqueue_vol3_analysis_service(session, job_id)
        assert action.action_type == "run_vol3_analysis"


def test_enqueue_linux_analysis_rejects_empty_effective_plugins_before_action(
    in_memory_session_scope,
    monkeypatch,
    tmp_path,
):
    job_id, _action_id, _dump_path, _isf_path = _make_completed_job(
        in_memory_session_scope,
        tmp_path,
        with_action=False,
    )
    monkeypatch.setattr(
        forensic_services,
        "settings",
        SimpleNamespace(
            vol3_enabled=True,
            vol3_volatility_bin="configured-vol3",
            vol3_default_plugins="  , \t,  ",
        ),
    )
    monkeypatch.setattr(
        forensic_services,
        "check_volatility_tool",
        lambda _settings: _ready_volatility_status(),
        raising=False,
    )

    with in_memory_session_scope() as session, pytest.raises(ApiError) as exc_info:
        forensic_services.enqueue_vol3_analysis_service(session, job_id)

    assert exc_info.value.status_code == 400
    assert exc_info.value.code == "invalid_request"
    assert "at least one" in exc_info.value.detail
    with in_memory_session_scope() as session:
        assert JobRepository(session).list_actions(job_id) == []


def test_enqueue_analysis_does_not_require_dwarf_for_existing_valid_isf(
    in_memory_session_scope,
    monkeypatch,
    tmp_path,
):
    job_id, _action_id, _dump_path, _isf_path = _make_completed_job(
        in_memory_session_scope,
        tmp_path,
        with_action=False,
    )
    monkeypatch.setattr(
        forensic_services,
        "settings",
        SimpleNamespace(
            vol3_enabled=True,
            vol3_volatility_bin="configured-vol3",
            vol3_default_plugins="linux.pslist.PsList",
        ),
    )
    monkeypatch.setattr(
        forensic_services,
        "check_volatility_tool",
        lambda _settings: _ready_volatility_status(),
        raising=False,
    )
    monkeypatch.setattr(
        vol3_tools,
        "check_dwarf2json_tool",
        lambda *_args: pytest.fail("analysis with a valid ISF must not require dwarf2json"),
    )
    monkeypatch.setattr(forensic_services, "_dispatch_action_after_commit", lambda *_args, **_kwargs: None)

    with in_memory_session_scope() as session:
        action = forensic_services.enqueue_vol3_analysis_service(session, job_id)
        assert action.action_type == "run_vol3_analysis"


def test_enqueue_analysis_duplicate_stays_conflict_after_readiness_passes(
    in_memory_session_scope,
    monkeypatch,
    tmp_path,
):
    job_id, _action_id, _dump_path, _isf_path = _make_completed_job(in_memory_session_scope, tmp_path)
    monkeypatch.setattr(
        forensic_services,
        "settings",
        SimpleNamespace(
            vol3_enabled=True,
            vol3_volatility_bin="configured-vol3",
            vol3_default_plugins="linux.pslist.PsList",
        ),
    )
    monkeypatch.setattr(
        forensic_services,
        "check_volatility_tool",
        lambda _settings: _ready_volatility_status(),
        raising=False,
    )

    with in_memory_session_scope() as session, pytest.raises(ApiError) as exc_info:
        forensic_services.enqueue_vol3_analysis_service(session, job_id)

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "conflict"


def test_run_vol3_analysis_serializes_under_job_lock(in_memory_session_scope, monkeypatch, tmp_path):
    from contextlib import contextmanager
    from pathlib import Path

    analysis = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(analysis, "session_scope", in_memory_session_scope)
    job_id, action_id, _dump_path, _isf_path = _make_completed_job(in_memory_session_scope, tmp_path)

    events: list[tuple[str, str | None]] = []

    @contextmanager
    def fake_lock(path):
        events.append(("lock", Path(path).name))
        yield
        events.append(("unlock", Path(path).name))

    monkeypatch.setattr(analysis, "blocking_file_lock", fake_lock)

    def fake_run(cmd, **kwargs):
        events.append(("vol3", None))
        kwargs["stdout"].write(f"output for {cmd[-1]}\n")
        return completed_child(cmd, kwargs, returncode=0, stderr="")

    monkeypatch.setattr(analysis.subprocess, "run", fake_run)

    success, summary = analysis.run_vol3_analysis(job_id, action_id=action_id)

    assert success is True, summary
    assert events[0] == ("lock", f"{job_id}.lock")
    assert events[-1] == ("unlock", f"{job_id}.lock")
    assert events[1:-1] == [("vol3", None), ("vol3", None)]


def test_missing_dump_rejects_analysis(in_memory_session_scope, monkeypatch, tmp_path):
    analysis = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(analysis, "session_scope", in_memory_session_scope)
    job_id, action_id, _dump_path, _isf_path = _make_completed_job(
        in_memory_session_scope, tmp_path, with_dump=False, with_isf=True
    )

    success, summary = analysis.run_vol3_analysis(job_id, action_id=action_id)

    assert success is False
    assert summary["status"] == "failed"
    assert "memory dump" in summary["error"]
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        action = repo.get_action(action_id)
        artifact = repo.get_latest_isf_artifact(job_id)
        assert action is not None
        assert action.status == "failed"
        assert action.error_summary == summary["error"]
        assert artifact is not None
        assert artifact.validation_status == "valid"


def test_traversal_job_id_fails_without_writing_outside_dumps(in_memory_session_scope, monkeypatch, tmp_path):
    analysis = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(analysis, "session_scope", in_memory_session_scope)

    success, summary = analysis.run_vol3_analysis("../escape")

    assert success is False
    assert summary["status"] == "failed"
    assert "invalid job_id path" in summary["error"]
    assert not (tmp_path / "storage" / "escape" / "vol3" / "analysis-summary.json").exists()


def test_missing_isf_rejects_analysis(in_memory_session_scope, monkeypatch, tmp_path):
    analysis = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(analysis, "session_scope", in_memory_session_scope)
    job_id, action_id, dump_path, _isf_path = _make_completed_job(
        in_memory_session_scope, tmp_path, with_dump=True, with_isf=False
    )

    success, summary = analysis.run_vol3_analysis(job_id, action_id=action_id)

    assert success is False
    assert summary["status"] == "failed"
    assert "valid ISF" in summary["error"]
    assert dump_path.exists()
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        action = repo.get_action(action_id)
        assert action is not None
        assert action.status == "failed"
        assert repo.get_latest_isf_artifact(job_id) is None


def test_isf_built_under_other_job_is_used(in_memory_session_scope, monkeypatch, tmp_path):
    analysis = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(analysis, "session_scope", in_memory_session_scope)
    job_id, action_id, _dump_path, _isf_path = _make_completed_job(
        in_memory_session_scope, tmp_path, with_dump=True, with_isf=False
    )
    # A valid ISF for the same kernel exists under a different (ISF-only) job.
    isf_path = tmp_path / "vol3" / "symbols" / "linux" / "6.8.0-101-generic.json"
    isf_path.parent.mkdir(parents=True, exist_ok=True)
    isf_path.write_text('{"symbols":{}}', encoding="utf-8")
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        other = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
            )
        )
        repo.create_isf_artifact(
            other.id,
            "6.8.0-101-generic",
            str(isf_path),
            "cache",
            distro="ubuntu",
            validation_status="valid",
            architecture="x86_64",
        )

    def fake_run(cmd, **kwargs):
        kwargs["stdout"].write("ok\n")
        return completed_child(cmd, kwargs, returncode=0, stderr="")

    monkeypatch.setattr(analysis.subprocess, "run", fake_run)

    success, summary = analysis.run_vol3_analysis(job_id, action_id=action_id)

    assert success is True
    assert summary["status"] == "completed"
    assert summary["isf_path"] == str(isf_path)


def test_mocked_vol3_success_writes_output_files(in_memory_session_scope, monkeypatch, tmp_path):
    analysis = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(analysis, "session_scope", in_memory_session_scope)
    job_id, action_id, dump_path, isf_path = _make_completed_job(in_memory_session_scope, tmp_path)
    with in_memory_session_scope() as session:
        action = JobRepository(session).get_action(action_id)
        assert action is not None
        input_artifact_id = action.source_artifact_id
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        stdout = kwargs["stdout"]
        stderr = kwargs["stderr"]
        assert stderr is not stdout
        assert kwargs["text"] is True
        assert kwargs["timeout"] == analysis._VOL3_TIMEOUT_SECONDS
        stdout.write(f"output for {cmd[-1]}\n")
        return completed_child(cmd, kwargs, returncode=0, stderr="")

    monkeypatch.setattr(analysis.subprocess, "run", fake_run)

    success, summary = analysis.run_vol3_analysis(job_id, action_id=action_id)

    output_dir = Path(summary["output_dir"])
    pslist = output_dir / "linux.pslist.PsList.txt"
    bash = output_dir / "linux.bash.Bash.txt"
    summary_path = output_dir / "analysis-summary.json"
    summary_manifest_path = output_dir / "analysis-summary.json.manifest.json"
    summary_sidecar_path = output_dir / "analysis-summary.json.sha256"
    staged_isf = output_dir / "symbols" / "linux" / isf_path.name

    assert success is True
    plugin_commands = [call[0] for call in calls]
    assert len(plugin_commands) == 2
    assert all(cmd[0] == str(tmp_path / "fixture-vol3") for cmd in plugin_commands)
    assert all("--version" not in cmd for cmd in plugin_commands)
    assert all(cmd[cmd.index("--symbol-dirs") + 1] == str(output_dir / "symbols") for cmd in plugin_commands)
    assert staged_isf.read_text(encoding="utf-8") == isf_path.read_text(encoding="utf-8")
    assert pslist.read_text(encoding="utf-8") == "output for linux.pslist.PsList\n"
    assert bash.read_text(encoding="utf-8") == "output for linux.bash.Bash\n"
    saved = json.loads(summary_path.read_text(encoding="utf-8"))
    manifest = json.loads(summary_manifest_path.read_text(encoding="utf-8"))
    assert saved["status"] == "completed"
    assert saved["isf_path"] == str(isf_path)
    assert saved["symbols_path"] == str(output_dir / "symbols")
    assert saved["dump_sha256"] == hashlib.sha256(dump_path.read_bytes()).hexdigest()
    assert saved["isf_sha256"] == hashlib.sha256(isf_path.read_bytes()).hexdigest()
    assert saved["vol3_version"] == "review-readiness-version"
    assert [item["status"] for item in saved["plugins"]] == ["completed", "completed"]
    assert manifest["schema_version"] == "artifact-manifest/v1"
    assert manifest["producer"] == "vol3_analysis"
    assert manifest["kind"] == "report"
    assert manifest["job_id"] == job_id
    assert manifest["sha256"] in summary_sidecar_path.read_text(encoding="utf-8")
    assert "  analysis-summary.json\n" in summary_sidecar_path.read_text(encoding="utf-8")
    assert summary == saved
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        action = repo.get_action(action_id)
        artifact = repo.get_latest_isf_artifact(job_id)
        report_artifact = session.query(Artifact).filter_by(job_id=job_id, kind="report").one()
        report = session.query(Report).filter_by(job_id=job_id, report_type="volatility3").one()
        assert action is not None
        assert artifact is not None
        assert action.status == "completed"
        assert artifact.path == str(isf_path)
        assert report_artifact.producer == "vol3_analysis"
        assert report_artifact.path == str(summary_path)
        assert report_artifact.filename == "analysis-summary.json"
        assert report_artifact.manifest_path == str(summary_manifest_path)
        # Task 20: the summary's publication is journalled, and its manifest and
        # sidecar were linked into place as a pair rather than written directly.
        from astinuslab.db.models import ArtifactPublicationAttempt

        attempt = session.query(ArtifactPublicationAttempt).filter_by(producer="vol3_analysis").one()
        assert attempt.state == "completed"
        assert attempt.final_path == str(summary_path)
        assert attempt.artifact_id == report_artifact.id
        assert attempt.sha256 == manifest["sha256"]
        assert not list(output_dir.glob(".publish-*")), "the attempt directory must not survive"
        assert report_artifact.sha256_sidecar_path == str(summary_sidecar_path)
        assert report_artifact.sha256 == manifest["sha256"]
        assert report.status == "completed"
        assert report.path == str(summary_path)
        assert report.input_artifact_id == input_artifact_id
        assert report.output_artifact_id == report_artifact.id


def test_summary_records_isf_source(in_memory_session_scope, monkeypatch, tmp_path):
    analysis = _patch_settings(monkeypatch, tmp_path, plugins="linux.pslist.PsList")
    monkeypatch.setattr(analysis, "session_scope", in_memory_session_scope)
    job_id, action_id, _dump_path, _isf_path = _make_completed_job(in_memory_session_scope, tmp_path)
    with in_memory_session_scope() as session:
        row = session.query(ISFArtifact).one()
        row.source = "live_guest"

    def fake_run(cmd, **kwargs):
        if cmd == ["/bin/vol3", "--version"]:
            return MagicMock(returncode=0, stdout="Volatility 3 Framework 2.8.0\n", stderr="")
        kwargs["stdout"].write("ok\n")
        return completed_child(cmd, kwargs, returncode=0, stderr="")

    monkeypatch.setattr(analysis.subprocess, "run", fake_run)

    success, summary = analysis.run_vol3_analysis(job_id, action_id=action_id)

    output_dir = Path(summary["output_dir"])
    manifest = json.loads((output_dir / "analysis-summary.json.manifest.json").read_text(encoding="utf-8"))
    isf_input = next(item for item in manifest["inputs"] if item["kind"] == "isf")
    assert success is True
    assert summary["status"] == "completed"
    assert summary["isf_source"] == "live_guest"
    assert isf_input["source"] == "live_guest"


def test_analysis_fails_on_dump_hash_mismatch(in_memory_session_scope, monkeypatch, tmp_path):
    analysis = _patch_settings(monkeypatch, tmp_path, plugins="linux.pslist.PsList")
    monkeypatch.setattr(analysis, "session_scope", in_memory_session_scope)
    job_id, action_id, dump_path, _isf_path = _make_completed_job(in_memory_session_scope, tmp_path)
    dump_path.write_bytes(b"BADdump")

    autospec_setattr(
        monkeypatch,
        analysis.subprocess,
        "run",
        lambda *_a, **_k: pytest.fail("vol3 must not run on mismatched evidence"),
    )

    success, summary = analysis.run_vol3_analysis(job_id, action_id=action_id)

    assert success is False
    assert "hash mismatch" in summary["error"]
    with in_memory_session_scope() as session:
        action = JobRepository(session).get_action(action_id)
        assert action is not None
        assert action.status == "failed"


def test_analysis_fails_on_isf_hash_mismatch(in_memory_session_scope, monkeypatch, tmp_path):
    analysis = _patch_settings(monkeypatch, tmp_path, plugins="linux.pslist.PsList")
    monkeypatch.setattr(analysis, "session_scope", in_memory_session_scope)
    job_id, action_id, _dump_path, isf_path = _make_completed_job(in_memory_session_scope, tmp_path)
    isf_path.write_text('{"tampered":true}', encoding="utf-8")

    autospec_setattr(
        monkeypatch,
        analysis.subprocess,
        "run",
        lambda *_a, **_k: pytest.fail("vol3 must not run on mismatched ISF evidence"),
    )

    success, summary = analysis.run_vol3_analysis(job_id, action_id=action_id)

    assert success is False
    assert "ISF hash mismatch" in summary["error"]
    with in_memory_session_scope() as session:
        action = JobRepository(session).get_action(action_id)
        assert action is not None
        assert action.status == "failed"


def test_analysis_summary_records_verification(in_memory_session_scope, monkeypatch, tmp_path):
    analysis = _patch_settings(monkeypatch, tmp_path, plugins="linux.pslist.PsList")
    monkeypatch.setattr(analysis, "session_scope", in_memory_session_scope)
    job_id, action_id, _dump_path, _isf_path = _make_completed_job(in_memory_session_scope, tmp_path)

    def fake_run(cmd, **kwargs):
        if cmd == ["/bin/vol3", "--version"]:
            return MagicMock(returncode=0, stdout="Volatility 3 Framework 2.8.0\n", stderr="")
        kwargs["stdout"].write("ok\n")
        return completed_child(cmd, kwargs, returncode=0, stderr="")

    monkeypatch.setattr(analysis.subprocess, "run", fake_run)

    success, summary = analysis.run_vol3_analysis(job_id, action_id=action_id)

    output_dir = Path(summary["output_dir"])
    manifest = json.loads((output_dir / "analysis-summary.json.manifest.json").read_text(encoding="utf-8"))
    memory_input = next(item for item in manifest["inputs"] if item["kind"] == "memory")
    assert success is True
    assert summary["input_verified"] is True
    assert summary["dump_sha256_at_use"] == summary["dump_sha256"]
    assert memory_input["verified"] is True
    assert memory_input["sha256_at_use"] == summary["dump_sha256_at_use"]


def test_analysis_stages_only_selected_isf(in_memory_session_scope, monkeypatch, tmp_path):
    analysis = _patch_settings(monkeypatch, tmp_path, plugins="linux.pslist.PsList")
    monkeypatch.setattr(analysis, "session_scope", in_memory_session_scope)
    job_id, action_id, _dump_path, isf_path = _make_completed_job(in_memory_session_scope, tmp_path)
    stale_isf = isf_path.parent / "stale-extra.json"
    stale_isf.write_text('{"metadata":{},"symbols":{"stale":{}}}', encoding="utf-8")

    def fake_run(cmd, **kwargs):
        if cmd == ["/bin/vol3", "--version"]:
            return MagicMock(returncode=0, stdout="Volatility 3 Framework 2.8.0\n", stderr="")
        kwargs["stdout"].write("ok\n")
        return completed_child(cmd, kwargs, returncode=0, stderr="")

    monkeypatch.setattr(analysis.subprocess, "run", fake_run)

    success, summary = analysis.run_vol3_analysis(job_id, action_id=action_id)

    staged_linux = Path(summary["output_dir"]) / "symbols" / "linux"
    assert success is True, summary
    assert sorted(path.name for path in staged_linux.iterdir()) == [isf_path.name]
    assert (staged_linux / isf_path.name).read_text(encoding="utf-8") == isf_path.read_text(encoding="utf-8")


def test_analysis_records_pinned_isf(in_memory_session_scope, monkeypatch, tmp_path):
    analysis = _patch_settings(monkeypatch, tmp_path, plugins="linux.pslist.PsList")
    monkeypatch.setattr(analysis, "session_scope", in_memory_session_scope)
    job_id, action_id, _dump_path, isf_path = _make_completed_job(in_memory_session_scope, tmp_path)

    def fake_run(cmd, **kwargs):
        if cmd == ["/bin/vol3", "--version"]:
            return MagicMock(returncode=0, stdout="Volatility 3 Framework 2.8.0\n", stderr="")
        cfg = Path(cmd[cmd.index("--save-config") + 1])
        staged = Path(cmd[cmd.index("--symbol-dirs") + 1])
        cfg.write_text(
            json.dumps({"kernel.symbol_table_name.isf_url": (staged / "linux" / isf_path.name).resolve().as_uri()}),
            encoding="utf-8",
        )
        kwargs["stdout"].write("ok\n")
        return completed_child(cmd, kwargs, returncode=0, stderr="")

    monkeypatch.setattr(analysis.subprocess, "run", fake_run)

    success, summary = analysis.run_vol3_analysis(job_id, action_id=action_id)

    output_dir = Path(summary["output_dir"])
    manifest = json.loads((output_dir / "analysis-summary.json.manifest.json").read_text(encoding="utf-8"))
    isf_input = next(item for item in manifest["inputs"] if item["kind"] == "isf")
    assert success is True, summary
    assert summary["isf_pinned"] is True
    assert summary["plugins"][0]["isf_pinned"] is True
    assert isf_input["pinned"] is True


def test_analysis_selects_staged_isf_over_installed_same_banner(in_memory_session_scope, monkeypatch, tmp_path):
    case_dir = tmp_path / "case with spaces"
    case_dir.mkdir()
    analysis = _patch_settings(monkeypatch, case_dir)
    monkeypatch.setattr(analysis, "session_scope", in_memory_session_scope)
    job_id, action_id, _dump_path, isf_path = _make_completed_job(in_memory_session_scope, case_dir)
    isf = json.loads(isf_path.read_text())
    isf["symbols"]["linux_banner"] = {
        "constant_data": base64.b64encode(b"Linux version 6.8.0-101-generic (fixture)\n\0").decode()
    }
    isf_path.write_text(json.dumps(isf))
    with in_memory_session_scope() as session:
        session.query(ISFArtifact).one().sha256 = hashlib.sha256(isf_path.read_bytes()).hexdigest()
    installed = tmp_path / "installed" / "symbols" / isf_path.name
    installed.parent.mkdir(parents=True)
    installed.write_bytes(isf_path.read_bytes())
    prefixes = {
        "linux.pslist.PsList": "plugins.PsList.kernel.symbol_table_name",
        "linux.bash.Bash": "plugins.Bash.kernel.symbol_table_name",
    }
    symbol_class = "volatility3.framework.symbols.linux.LinuxKernelIntermedSymbols"

    def competing_symbol_child(cmd, **kwargs):
        # Model the observed search collision; explicit CLI config must win.
        extensions = dict(cmd[index + 1].split("=", 1) for index, arg in enumerate(cmd) if arg == "-e")
        prefix = prefixes[cmd[-1]]
        selected = installed.resolve().as_uri()
        if json.loads(extensions.get(f"{prefix}.class", "null")) == symbol_class:
            selected = json.loads(extensions.get(f"{prefix}.isf_url", json.dumps(selected)))
        config = Path(cmd[cmd.index("--save-config") + 1])
        config.write_text(
            json.dumps({"kernel.symbol_table_name.class": symbol_class, "kernel.symbol_table_name.isf_url": selected})
        )
        return completed_child(cmd, kwargs, stdout="PID\tCOMM\n1\tfixture-init\n")

    monkeypatch.setattr(analysis.subprocess, "run", competing_symbol_child)

    success, summary = analysis.run_vol3_analysis(job_id, action_id=action_id)

    assert success is True, summary
    assert summary["isf_pinned"] is True
    output_dir = Path(summary["output_dir"])
    staged = output_dir / "symbols" / "linux" / isf_path.name
    assert staged.read_bytes() == installed.read_bytes() == isf_path.read_bytes()
    assert [result["plugin"] for result in summary["plugins"]] == list(prefixes)
    manifest = json.loads((output_dir / "analysis-summary.json.manifest.json").read_text())
    for result, execution in zip(summary["plugins"], manifest["executions"], strict=True):
        assert result["status"] == "completed"
        assert result["isf_pinned"] is True
        assert execution["argv"] == result["command"]
        config = json.loads(execution["files"]["saved_config"]["content"])
        assert config["kernel.symbol_table_name.isf_url"] == staged.resolve().as_uri()


def test_windows_analysis_uses_cache_without_linux_symbol_selection(in_memory_session_scope, monkeypatch, tmp_path):
    analysis = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(analysis, "session_scope", in_memory_session_scope)
    job_id, action_id, *_ = _make_completed_job(in_memory_session_scope, tmp_path, with_isf=False, distro="windows-11")
    monkeypatch.setattr(
        analysis.subprocess, "run", lambda cmd, **kwargs: completed_child(cmd, kwargs, stdout="Windows fixture table\n")
    )

    success, summary = analysis.run_vol3_analysis(job_id, action_id=action_id)

    assert success is True, summary
    assert [result["plugin"] for result in summary["plugins"]] == [
        "windows.info.Info",
        "windows.pslist.PsList",
        "windows.pstree.PsTree",
    ]
    for result in summary["plugins"]:
        cmd = result["command"]
        assert "-e" not in cmd
        assert cmd[cmd.index("--symbol-dirs") + 1] == str(tmp_path / "vol3" / "symbols" / "windows")


def test_analysis_fails_on_foreign_isf(in_memory_session_scope, monkeypatch, tmp_path):
    analysis = _patch_settings(monkeypatch, tmp_path, plugins="linux.pslist.PsList")
    monkeypatch.setattr(analysis, "session_scope", in_memory_session_scope)
    job_id, action_id, _dump_path, _isf_path = _make_completed_job(in_memory_session_scope, tmp_path)
    foreign = tmp_path / "global" / "evil.json"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("{}", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        if cmd == ["/bin/vol3", "--version"]:
            return MagicMock(returncode=0, stdout="Volatility 3 Framework 2.8.0\n", stderr="")
        cfg = Path(cmd[cmd.index("--save-config") + 1])
        cfg.write_text(json.dumps({"kernel.symbol_table_name.isf_url": foreign.resolve().as_uri()}), encoding="utf-8")
        kwargs["stdout"].write("ok\n")
        return completed_child(cmd, kwargs, returncode=0, stderr="")

    monkeypatch.setattr(analysis.subprocess, "run", fake_run)

    success, summary = analysis.run_vol3_analysis(job_id, action_id=action_id)

    assert success is False
    assert summary["status"] == "failed"
    assert "outside the staged symbols directory" in summary["error"]


def test_plugin_stderr_is_segregated_from_txt_output(in_memory_session_scope, monkeypatch, tmp_path):
    analysis = _patch_settings(monkeypatch, tmp_path, plugins="linux.pslist.PsList")
    monkeypatch.setattr(analysis, "session_scope", in_memory_session_scope)
    job_id, action_id, _dump_path, _isf_path = _make_completed_job(in_memory_session_scope, tmp_path)

    def fake_run(cmd, **kwargs):
        if cmd == ["/bin/vol3", "--version"]:
            return MagicMock(returncode=0, stdout="Volatility 3 Framework 2.8.0\n", stderr="")
        kwargs["stdout"].write("PID\tCOMM\n1\tinit\n")
        kwargs["stderr"].write("Progress: 37.25 Reading Symbol layer\n")
        return completed_child(cmd, kwargs, returncode=0, stderr="")

    monkeypatch.setattr(analysis.subprocess, "run", fake_run)

    success, summary = analysis.run_vol3_analysis(job_id, action_id=action_id)

    vol3_dir = Path(summary["output_dir"])
    txt = (vol3_dir / "linux.pslist.PsList.txt").read_text(encoding="utf-8")
    stderr_log = (vol3_dir / "linux.pslist.PsList.stderr.log").read_text(encoding="utf-8")

    assert success is True, summary
    assert "Progress:" not in txt
    assert txt == "PID\tCOMM\n1\tinit\n"
    assert "Progress: 37.25" in stderr_log
    assert summary["plugins"][0]["stderr_path"] == str(vol3_dir / "linux.pslist.PsList.stderr.log")


def test_failed_plugin_keeps_stderr_log(in_memory_session_scope, monkeypatch, tmp_path):
    analysis = _patch_settings(monkeypatch, tmp_path, plugins="linux.pslist.PsList")
    monkeypatch.setattr(analysis, "session_scope", in_memory_session_scope)
    job_id, action_id, _dump_path, _isf_path = _make_completed_job(in_memory_session_scope, tmp_path)

    def fake_run(cmd, **kwargs):
        if cmd == ["/bin/vol3", "--version"]:
            return MagicMock(returncode=0, stdout="Volatility 3 Framework 2.8.0\n", stderr="")
        kwargs["stderr"].write("boom\n")
        return completed_child(cmd, kwargs, returncode=1, stderr="")

    monkeypatch.setattr(analysis.subprocess, "run", fake_run)

    success, summary = analysis.run_vol3_analysis(job_id, action_id=action_id)

    vol3_dir = Path(summary["output_dir"])
    assert success is False
    assert summary["plugins"][0]["status"] == "failed"
    assert "boom" in (vol3_dir / "linux.pslist.PsList.stderr.log").read_text(encoding="utf-8")


def test_analysis_rechecks_volatility_and_fails_without_spawning(in_memory_session_scope, monkeypatch, tmp_path):
    analysis = _patch_settings(monkeypatch, tmp_path, plugins="linux.pslist.PsList")
    monkeypatch.setattr(analysis, "session_scope", in_memory_session_scope)
    job_id, action_id, _dump_path, isf_path = _make_completed_job(in_memory_session_scope, tmp_path)
    monkeypatch.setattr(
        analysis,
        "require_verified_volatility",
        lambda _settings: (_ for _ in ()).throw(
            ToolDependencyError("volatility_not_found", "The configured Volatility executable was not found.")
        ),
    )
    monkeypatch.setattr(
        analysis.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("Volatility must not spawn when the queued tool was removed"),
    )

    success, summary = analysis.run_vol3_analysis(job_id, action_id=action_id)

    assert success is False
    assert summary["status"] == "failed"
    assert "Volatility executable was not found" in summary["error"]
    assert str(isf_path) == summary["isf_path"]
    assert summary["dump_sha256"] == hashlib.sha256(_dump_path.read_bytes()).hexdigest()
    assert summary["isf_sha256"] == hashlib.sha256(isf_path.read_bytes()).hexdigest()
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        action = repo.get_action(action_id)
        assert action is not None
        assert action.status == "failed"
        assert action.error_summary == summary["error"]


def test_analysis_probes_volatility_once_per_plugin(
    in_memory_session_scope,
    monkeypatch,
    tmp_path,
):
    """Verification runs exactly once per plugin, immediately before the spawn.

    The worker previously probed twice to compare the two resolved paths, but
    resolution is deterministic for a given configuration, so the comparison could
    never fail -- while each probe costs a full Volatility framework load.
    """
    analysis = _patch_settings(monkeypatch, tmp_path, plugins="linux.pslist.PsList,linux.pstree.PsTree")
    monkeypatch.setattr(analysis, "session_scope", in_memory_session_scope)
    job_id, action_id, _dump_path, _isf_path = _make_completed_job(in_memory_session_scope, tmp_path)
    events: list[Any] = []

    def require(_settings):
        events.append("verify")
        return _verified_volatility(tmp_path / "fixture-vol3")

    def fake_run(cmd, **_kwargs):
        events.append("spawn")
        return completed_child(cmd, _kwargs, returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(analysis, "require_verified_volatility", require)
    monkeypatch.setattr(analysis.subprocess, "run", fake_run)

    success, _summary = analysis.run_vol3_analysis(job_id, action_id=action_id)

    assert success is True
    assert events == ["verify", "spawn", "verify", "spawn"]


def _executable_volatility_stub(tmp_path: Path, version: str, *, drift_to: str | None = None) -> SimpleNamespace:
    """A real, executable on-disk stand-in for Volatility that logs every invocation.

    It answers the readiness probe with a parseable framework banner read from a
    sibling file, so the reported version can change between invocations exactly as
    an in-place upgrade would.  `drift_to` makes a plugin run rewrite that file,
    which is what lets a test observe whether the NEXT plugin re-reads the tool.
    """
    log_path = tmp_path / "vol3-invocations.log"
    version_path = tmp_path / "vol3-reported-version"
    version_path.write_text(version, encoding="utf-8")
    drift_path = tmp_path / "vol3-drift-version"
    if drift_to is not None:
        drift_path.write_text(drift_to, encoding="utf-8")
    binary = tmp_path / "vol3-stub"
    binary.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{log_path}"\n'
        'if [ "$1" = "-q" ]; then\n'
        f'  echo "Volatility 3 Framework $(cat "{version_path}")"\n'
        "  exit 0\n"
        "fi\n"
        f'if [ -f "{drift_path}" ]; then cat "{drift_path}" > "{version_path}"; fi\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in --save-config) config="$2"; shift;; --symbol-dirs) symbols="$2"; shift;; esac\n'
        "  shift\n"
        "done\n"
        'for isf in "$symbols"/linux/*.json; do\n'
        '  printf \'{"kernel.isf_url":"file://%s"}\' "$isf" > "$config"\n'
        "  break\n"
        "done\n"
        'echo "stub plugin output"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return SimpleNamespace(binary=binary, log_path=log_path)


def _patch_settings_for_real_readiness(monkeypatch, tmp_path, binary: Path, plugins: str):
    """Same settings seam as `_patch_settings`, but the readiness check is NOT stubbed."""
    import astinuslab.forensics.vol3_analysis as analysis

    monkeypatch.setattr(
        analysis,
        "settings",
        SimpleNamespace(
            storage_dir=tmp_path / "storage",
            vol3_symbols_path=tmp_path / "vol3" / "symbols",
            vol3_enabled=True,
            vol3_volatility_bin=str(binary),
            vol3_default_plugins=plugins,
        ),
    )
    return analysis


def _invocation_kinds(log_path: Path) -> list[str]:
    """Classify each recorded argv as the readiness probe or a named plugin spawn."""
    kinds = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        kinds.append("probe" if line.startswith("-q ") else line.rsplit(" ", 1)[-1])
    return kinds


def test_analysis_reprobes_the_real_volatility_binary_once_per_plugin(
    in_memory_session_scope,
    monkeypatch,
    tmp_path,
):
    """Integration guard for the per-plugin verification seam.

    Every other test in this file stubs `require_verified_volatility`, so a refactor
    that hoisted it above the plugin loop would still satisfy them by construction.
    Here nothing is stubbed: `settings.vol3_volatility_bin` points at a real
    executable that records its own argv, so the recorded order is evidence that the
    tool is genuinely re-resolved and re-executed immediately before each spawn.
    """
    stub = _executable_volatility_stub(tmp_path, next(iter(vol3_tools.TESTED_VOLATILITY_VERSIONS)))
    analysis = _patch_settings_for_real_readiness(
        monkeypatch, tmp_path, stub.binary, "linux.pslist.PsList,linux.pstree.PsTree"
    )
    monkeypatch.setattr(analysis, "session_scope", in_memory_session_scope)
    job_id, action_id, _dump_path, _isf_path = _make_completed_job(in_memory_session_scope, tmp_path)

    success, summary = analysis.run_vol3_analysis(job_id, action_id=action_id)

    assert success is True, summary
    assert _invocation_kinds(stub.log_path) == [
        "probe",
        "linux.pslist.PsList",
        "probe",
        "linux.pstree.PsTree",
    ]


def test_analysis_reprobe_catches_a_volatility_binary_swapped_between_plugins(
    in_memory_session_scope,
    monkeypatch,
    tmp_path,
):
    """The per-plugin probe is load-bearing, not just counted.

    The stub reports a tested version for the first plugin and a below-the-floor one
    afterwards, mimicking an in-place *downgrade* mid-batch.  Only a probe that
    actually re-reads the tool before plugin 2 can catch that, so the second spawn
    must never happen and the stable readiness detail must reach the summary.

    The drift target has to sit below MINIMUM_VOLATILITY_VERSION to make the point:
    drifting *upward* to an untested version is now allowed to keep running, so it
    would prove nothing about re-probing.
    """
    tested = next(iter(vol3_tools.TESTED_VOLATILITY_VERSIONS))
    stub = _executable_volatility_stub(tmp_path, tested, drift_to="2.27.0")
    analysis = _patch_settings_for_real_readiness(
        monkeypatch, tmp_path, stub.binary, "linux.pslist.PsList,linux.pstree.PsTree"
    )
    monkeypatch.setattr(analysis, "session_scope", in_memory_session_scope)
    job_id, action_id, _dump_path, _isf_path = _make_completed_job(in_memory_session_scope, tmp_path)

    success, summary = analysis.run_vol3_analysis(job_id, action_id=action_id)

    assert success is False
    assert _invocation_kinds(stub.log_path) == ["probe", "linux.pslist.PsList", "probe"]
    assert summary["plugins"][0]["status"] == "completed"
    assert summary["plugins"][1]["status"] == "failed"
    assert "Volatility version 2.27.0 is unsupported" in summary["error"]


def test_analysis_fails_without_spawning_when_batch_volatility_path_changes(
    in_memory_session_scope,
    monkeypatch,
    tmp_path,
):
    """A path change between plugins must abort the batch before the next spawn.

    Every plugin in one batch has to run against the same verified binary, or the
    report mixes results from two different tools under one vol3_version.  Hoisting
    the verification above the plugin loop would let plugins 2..N run against a stale
    verification, so this test asserts the per-plugin comparison still bites.
    """
    analysis = _patch_settings(monkeypatch, tmp_path, plugins="linux.pslist.PsList,linux.pstree.PsTree")
    monkeypatch.setattr(analysis, "session_scope", in_memory_session_scope)
    job_id, action_id, _dump_path, _isf_path = _make_completed_job(in_memory_session_scope, tmp_path)
    require_calls = 0
    subprocess_calls = 0

    def require(_settings):
        nonlocal require_calls
        require_calls += 1
        if require_calls == 1:
            return _verified_volatility(tmp_path / "fixture-vol3")
        return _verified_volatility(Path("/verified/swapped-vol3"))

    def fake_run(cmd, **kwargs):
        nonlocal subprocess_calls
        subprocess_calls += 1
        kwargs["stdout"].write(f"output for {cmd[-1]}\n")
        return completed_child(cmd, kwargs, returncode=0, stderr="")

    monkeypatch.setattr(analysis, "require_verified_volatility", require)
    monkeypatch.setattr(analysis.subprocess, "run", fake_run)

    success, summary = analysis.run_vol3_analysis(job_id, action_id=action_id)

    assert success is False
    assert subprocess_calls == 1
    assert "changed during batch execution" in summary["error"]
    assert summary["status"] == "failed"
    assert summary["plugins"][0]["status"] == "completed"
    assert summary["plugins"][1]["status"] == "failed"
    assert "changed during batch execution" in summary["plugins"][1]["error"]
    assert summary["vol3_version"] == "review-readiness-version"


def test_analysis_fails_without_spawning_when_batch_volatility_version_changes(
    in_memory_session_scope,
    monkeypatch,
    tmp_path,
):
    """The same guard covers a version change at an unchanged path.

    An in-place upgrade keeps the resolved path identical while changing what the
    binary actually is, so the probed version is compared too.
    """
    analysis = _patch_settings(monkeypatch, tmp_path, plugins="linux.pslist.PsList,linux.pstree.PsTree")
    monkeypatch.setattr(analysis, "session_scope", in_memory_session_scope)
    job_id, action_id, _dump_path, _isf_path = _make_completed_job(in_memory_session_scope, tmp_path)
    require_calls = 0
    subprocess_calls = 0

    def require(_settings):
        nonlocal require_calls
        require_calls += 1
        if require_calls == 1:
            return _verified_volatility(tmp_path / "fixture-vol3")
        return VerifiedVolatility(path=tmp_path / "fixture-vol3", version="upgraded-in-place-version")

    def fake_run(cmd, **kwargs):
        nonlocal subprocess_calls
        subprocess_calls += 1
        kwargs["stdout"].write(f"output for {cmd[-1]}\n")
        return completed_child(cmd, kwargs, returncode=0, stderr="")

    monkeypatch.setattr(analysis, "require_verified_volatility", require)
    monkeypatch.setattr(analysis.subprocess, "run", fake_run)

    success, summary = analysis.run_vol3_analysis(job_id, action_id=action_id)

    assert success is False
    assert subprocess_calls == 1
    assert "changed during batch execution" in summary["error"]
    assert summary["plugins"][1]["status"] == "failed"
    assert summary["vol3_version"] == "review-readiness-version"


def test_analysis_revalidates_drifted_default_plugins_before_spawning(
    in_memory_session_scope,
    monkeypatch,
    tmp_path,
):
    analysis = _patch_settings(monkeypatch, tmp_path, plugins="linux.invalid.ConfigDrift")
    monkeypatch.setattr(analysis, "session_scope", in_memory_session_scope)
    job_id, action_id, _dump_path, _isf_path = _make_completed_job(in_memory_session_scope, tmp_path)
    monkeypatch.setattr(
        analysis.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("Volatility must not spawn for drifted plugin configuration"),
    )

    success, summary = analysis.run_vol3_analysis(job_id, action_id=action_id)

    assert success is False
    assert "linux.invalid.ConfigDrift" in summary["error"]


def test_analysis_mid_batch_readiness_loss_preserves_full_summary(
    in_memory_session_scope,
    monkeypatch,
    tmp_path,
):
    analysis = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(analysis, "session_scope", in_memory_session_scope)
    job_id, action_id, dump_path, _isf_path = _make_completed_job(in_memory_session_scope, tmp_path)
    require_calls = 0
    subprocess_calls = 0

    def require(_settings):
        nonlocal require_calls
        require_calls += 1
        if require_calls == 1:
            return _verified_volatility(tmp_path / "fixture-vol3")
        raise ToolDependencyError("volatility_not_found", "The configured Volatility executable was removed.")

    def fake_run(cmd, **kwargs):
        nonlocal subprocess_calls
        subprocess_calls += 1
        kwargs["stdout"].write(f"output for {cmd[-1]}\n")
        return completed_child(cmd, kwargs, returncode=0, stderr="")

    monkeypatch.setattr(analysis, "require_verified_volatility", require)
    monkeypatch.setattr(analysis.subprocess, "run", fake_run)

    success, summary = analysis.run_vol3_analysis(job_id, action_id=action_id)

    assert success is False
    assert subprocess_calls == 1
    assert summary["status"] == "failed"
    assert summary["dump_sha256_at_use"] == hashlib.sha256(dump_path.read_bytes()).hexdigest()
    assert summary["input_verified"] is True
    assert summary["isf_source"] == "cache"
    assert summary["isf_pinned"] is False
    assert Path(summary["symbols_path"]) == Path(summary["output_dir"]) / "symbols"
    assert summary["started_at"]
    assert summary["vol3_version"] == "review-readiness-version"
    assert summary["plugins"][0]["status"] == "completed"
    assert summary["plugins"][0]["plugin"] == "linux.pslist.PsList"
    assert "removed" in summary["error"]


def test_non_x86_64_rejects_without_selecting_x86_64_isf(in_memory_session_scope, monkeypatch, tmp_path):
    analysis = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(analysis, "session_scope", in_memory_session_scope)
    job_id, action_id, dump_path, isf_path = _make_completed_job(in_memory_session_scope, tmp_path)

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        repo.force_transition(
            job_id,
            "completed",
            detected_architecture="aarch64",
        )

    success, summary = analysis.run_vol3_analysis(job_id, action_id=action_id)

    assert success is False
    assert "supports x86_64 only" in summary["error"]
    assert summary["dump_sha256"] == hashlib.sha256(dump_path.read_bytes()).hexdigest()
    assert summary["isf_path"] is None
    assert summary["isf_sha256"] is None
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        action = repo.get_action(action_id)
        assert action is not None
        assert action.status == "failed"


def test_mocked_plugin_failure_records_failed_status_and_error_summary(in_memory_session_scope, monkeypatch, tmp_path):
    analysis = _patch_settings(monkeypatch, tmp_path, plugins="linux.pslist.PsList,linux.bash.Bash")
    monkeypatch.setattr(analysis, "session_scope", in_memory_session_scope)
    job_id, action_id, dump_path, isf_path = _make_completed_job(in_memory_session_scope, tmp_path)

    def fake_run(cmd, **kwargs):
        if cmd == ["/bin/vol3", "--version"]:
            return MagicMock(returncode=0, stdout="Volatility 3 Framework 2.8.0\n", stderr="")
        stdout = kwargs["stdout"]
        assert kwargs["stderr"] is not stdout
        stderr = kwargs["stderr"]
        if cmd[-1] == "linux.bash.Bash":
            stderr.write("plugin exploded\n")
            return completed_child(cmd, kwargs, returncode=7)
        stdout.write("ok\n")
        return completed_child(cmd, kwargs, returncode=0)

    monkeypatch.setattr(analysis.subprocess, "run", fake_run)

    success, summary = analysis.run_vol3_analysis(job_id, action_id=action_id)

    assert success is False
    assert summary["status"] == "failed"
    assert "linux.bash.Bash failed with exit code 7" in summary["error"]
    assert dump_path.exists()
    assert isf_path.exists()

    output_dir = Path(summary["output_dir"])
    saved = json.loads((output_dir / "analysis-summary.json").read_text(encoding="utf-8"))
    assert saved["status"] == "failed"
    assert saved["plugins"][0]["status"] == "completed"
    assert saved["plugins"][1]["status"] == "failed"
    assert "plugin exploded" in (output_dir / "linux.bash.Bash.stderr.log").read_text(encoding="utf-8")

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        action = repo.get_action(action_id)
        artifact = repo.get_latest_isf_artifact(job_id)
        assert action is not None
        assert artifact is not None
        assert action.status == "failed"
        assert action.error_summary == summary["error"]
        assert artifact.path == str(isf_path)
        assert artifact.validation_status == "valid"


def test_isf_lookup_prefers_build_id(in_memory_session_scope, tmp_path):
    path_a = tmp_path / "a.json"
    path_b = tmp_path / "b.json"
    path_a.write_text('{"metadata":{},"symbols":{"init_task":{}}}', encoding="utf-8")
    path_b.write_text('{"metadata":{},"symbols":{"init_task":{}}}', encoding="utf-8")

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job_a = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
            )
        )
        job_b = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
            )
        )
        repo.create_isf_artifact(
            job_a.id,
            "6.8.0-same",
            str(path_a),
            "host_fetch",
            build_id="build-a",
            architecture="x86_64",
        )
        repo.create_isf_artifact(
            job_b.id,
            "6.8.0-same",
            str(path_b),
            "host_fetch",
            build_id="build-b",
            architecture="x86_64",
        )

        artifact = repo.find_isf_by_build_id("build-a", "x86_64")

        assert artifact is not None
        assert artifact.path == str(path_a)


def test_analysis_artifact_selection_prefers_build_id():
    import astinuslab.forensics.vol3_analysis as analysis

    class Artifact:
        def __init__(self, path, build_id=None):
            self.path = path
            self.build_id = build_id

    class Repo:
        def __init__(self):
            self.calls = []

        def find_isf_by_build_id(self, build_id, architecture):
            self.calls.append(("build_id", build_id, architecture))
            return Artifact("/by-build-id.json", build_id)

        def get_valid_isf_artifact(self, job_id, kernel_release, *, architecture):
            self.calls.append(("kernel", job_id, kernel_release, architecture))
            return Artifact("/by-kernel.json")

    repo = Repo()

    artifact = analysis._select_isf_artifact(
        cast(JobRepository, repo),
        "job-1",
        "6.8.0-same",
        "x86_64",
        build_id="build-a",
    )

    assert artifact.path == "/by-build-id.json"
    assert repo.calls == [("build_id", "build-a", "x86_64")]
