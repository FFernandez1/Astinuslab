"""Explicit source ownership and synthetic full interactive execution (not real forensics)."""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from astinuslab.db.models import Acquisition, Artifact, Case, Job, PlaygroundSession, Report
from astinuslab.db.repositories import ArtifactRepository, JobRepository
from astinuslab.forensics import services
from astinuslab.storage.deletion import delete_artifact
from tests.python._support.vol3_fixtures import completed_child, patch_plugin_settings


def capture(scope, tmp_path, kind="memory"):
    path = tmp_path / "captures" / "memory.lime"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"synthetic source capture")
    with scope() as s:
        s.add(Case(id="case-pg", name="Protected"))
        s.add(PlaygroundSession(id="pg-source", status="destroyed"))
        s.flush()
        s.add(
            Acquisition(
                id="acq-source",
                name="Target capture",
                status="completed",
                session_id="pg-source",
                vm_label="target-A",
                case_id="case-pg",
                kind=kind,
            )
        )
        s.flush()
        a = ArtifactRepository(s).create_artifact(
            kind=kind,
            producer="playground_capture",
            path=str(path),
            filename=path.name,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            session_id="pg-source",
            acquisition_id="acq-source",
            case_id="case-pg",
        )
        return a.id, path


def context(s, artifact_id, **overrides):
    from astinuslab.forensics.artifact_context import create_analysis_context

    fields = dict(
        kernel_release="6.8.0-138-generic",
        architecture="x86_64",
        verified=True,
        provenance="Operator recorded uname and capture inventory before capture",
    )
    fields.update(overrides)
    return create_analysis_context(s, artifact_id, **fields)


def test_selected_capture_context_queue_worker_report_custody(in_memory_session_scope, tmp_path, monkeypatch):
    scope = in_memory_session_scope
    artifact_id, path = capture(scope, tmp_path)
    original = path.read_bytes()
    with scope() as s:
        row = next(d for d in services.list_vol3_dumps_service(s) if d.get("artifact_id") == artifact_id)
        assert row["session_id"] == "pg-source" and row["vm_label"] == "target-A"
        assert row["symbols"] == "missing" and not row["job_id"]
        j = context(s, artifact_id)
        job_id = j.id
        assert j.source_artifact_id == artifact_id and j.execution_mode == "artifact_analysis"
        assert j.capture_type == "none" and j.dump_path == str(path)
        repo = JobRepository(s)
        assert not repo.list_runtimes(job_id)
        assert job_id not in [job.id for job in repo.list_jobs()]
        assert context(s, artifact_id).id == job_id
        isf = tmp_path / "matching-isf.json"
        isf.write_text(
            '{"metadata":{"linux":{"kernel_release":"6.8.0-138-generic","architecture":"x86_64"}},"symbols":{"init_task":{}}}'
        )
        repo.create_isf_artifact(
            job_id,
            "6.8.0-138-generic",
            str(isf),
            "manual_upload",
            architecture="x86_64",
            validation_status="valid",
            sha256=hashlib.sha256(isf.read_bytes()).hexdigest(),
        )
    dispatches = []
    monkeypatch.setattr(services, "_require_volatility_readiness", lambda: None)
    monkeypatch.setattr(services, "_dispatch_action_after_commit", lambda *args, **kwargs: dispatches.append(kwargs))
    with scope() as s:
        queued = services.enqueue_vol3_plugin_run_service(s, job_id, "linux.pslist.PsList")
        assert queued["status"] == "queued" and dispatches[0]["job_id"] == job_id
    runner = patch_plugin_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", scope)
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda cmd, **kwargs: completed_child(cmd, kwargs, stdout='[{"PID":42,"COMM":"known-fixture"}]'),
    )
    ok, result = runner.run_vol3_plugin(job_id, queued["action_id"])
    assert ok, result
    assert result["source_artifact_id"] == artifact_id
    assert result["execution"]["inputs"]["memory"]["artifact_id"] == artifact_id
    assert result["rows"] == [[42, "known-fixture"]]
    with scope() as s:
        report = s.scalars(select(Report).where(Report.job_id == job_id)).one()
        assert (report.input_artifact_id, report.acquisition_id, report.case_id) == (
            artifact_id,
            "acq-source",
            "case-pg",
        )
        output = s.get(Artifact, report.output_artifact_id)
        manifest = json.loads(__import__("pathlib").Path(output.manifest_path).read_text())
        assert manifest["inputs"][0]["artifact_id"] == artifact_id
        for artifact in [output, s.get(Artifact, artifact_id)]:
            denied = delete_artifact(s, artifact, actor="test", reason="proof", roots=[tmp_path])
            assert not denied.deleted
            assert denied.protection_reason is not None and "acquisition" in denied.protection_reason
        assert s.get(Artifact, artifact_id).job_id is None
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "overrides",
    [
        {"kernel_release": ""},
        {"architecture": ""},
        {"architecture": "aarch64"},
        {"verified": False},
        {"provenance": ""},
    ],
)
def test_unknown_or_unverified_context_fails(in_memory_session_scope, tmp_path, overrides):
    artifact_id, _ = capture(in_memory_session_scope, tmp_path)
    with in_memory_session_scope() as s, pytest.raises(HTTPException):
        context(s, artifact_id, **overrides)


def test_disk_and_missing_symbols_rejected(in_memory_session_scope, tmp_path, monkeypatch):
    artifact_id, _ = capture(in_memory_session_scope, tmp_path, kind="disk")
    with in_memory_session_scope() as s:
        assert not any(d.get("artifact_id") == artifact_id for d in services.list_vol3_dumps_service(s))
        with pytest.raises(HTTPException):
            context(s, artifact_id)
        s.get(Artifact, artifact_id).kind = "memory"
        s.get(Acquisition, "acq-source").kind = "memory"
        j = context(s, artifact_id)
        monkeypatch.setattr(services, "_require_volatility_readiness", lambda: None)
        with pytest.raises(HTTPException, match="ISF"):
            services.enqueue_vol3_plugin_run_service(s, j.id, "linux.pslist.PsList")


@pytest.mark.parametrize("corruption", ["array", "session", "acquisition", "kernel"])
def test_stored_context_tamper_is_actionable(in_memory_session_scope, tmp_path, corruption):
    from astinuslab.forensics.artifact_context import validate_analysis_context

    artifact_id, _ = capture(in_memory_session_scope, tmp_path)
    with in_memory_session_scope() as s:
        j = context(s, artifact_id)
        data = json.loads(j.analysis_context_json)
        if corruption == "array":
            data = []
        elif corruption == "session":
            data["session_id"] = "pg-other"
        elif corruption == "acquisition":
            data["acquisition_id"] = "other-acquisition"
        else:
            j.detected_kernel_release = "6.8.0-101-generic"
        j.analysis_context_json = json.dumps(data)
        with pytest.raises(HTTPException, match="context"):
            validate_analysis_context(s, j)


def test_forced_source_deletion_invalidates_context_without_fallback(in_memory_session_scope, tmp_path):
    from astinuslab.forensics.artifact_context import validate_analysis_context

    artifact_id, _ = capture(in_memory_session_scope, tmp_path)
    with in_memory_session_scope() as s:
        j = context(s, artifact_id)
        artifact = s.get(Artifact, artifact_id)
        outcome = delete_artifact(
            s, artifact, actor="test", reason="explicit disposal fixture", roots=[tmp_path], force=True
        )
        assert outcome.deleted
        assert j.source_artifact_id is None and j.dump_path is None
        with pytest.raises(HTTPException, match="removed"):
            validate_analysis_context(s, j)


def test_new_ownership_rejects_missing_mixed_and_analysis_capture_owners(in_memory_session_scope, tmp_path):
    artifact_id, _ = capture(in_memory_session_scope, tmp_path)
    with in_memory_session_scope() as s:
        j = context(s, artifact_id)
        repo = ArtifactRepository(s)
        for fields in [
            {"job_id": "missing"},
            {"job_id": j.id, "session_id": "pg-source"},
            {"session_id": "missing"},
            {"job_id": j.id},
        ]:
            with pytest.raises(ValueError):
                repo.create_artifact(kind="memory", producer="fixture", path="unused", **fields)


def test_tool_unavailable_does_not_queue(in_memory_session_scope, tmp_path, monkeypatch):
    from astinuslab.api.errors import ApiError

    artifact_id, _ = capture(in_memory_session_scope, tmp_path)
    with in_memory_session_scope() as s:
        j = context(s, artifact_id)
        isf = tmp_path / "isf.json"
        isf.write_text(
            '{"metadata":{"linux":{"kernel_release":"6.8.0-138-generic","architecture":"x86_64"}},"symbols":{"init_task":{}}}'
        )
        repo = JobRepository(s)
        repo.create_isf_artifact(
            j.id, j.detected_kernel_release, str(isf), "manual_upload", architecture="x86_64", validation_status="valid"
        )

        def unavailable():
            raise ApiError(503, "Operator Volatility tool unavailable", "dependency_unavailable")

        monkeypatch.setattr(services, "_require_volatility_readiness", unavailable)
        with pytest.raises(ApiError, match="tool unavailable"):
            services.enqueue_vol3_plugin_run_service(s, j.id, "linux.pslist.PsList")
        assert not repo.list_actions(j.id)


@pytest.mark.parametrize("status", ["queued", "running"])
def test_analysis_context_cannot_be_deleted_before_first_report(in_memory_session_scope, tmp_path, status):
    from astinuslab.api.errors import ApiError
    from astinuslab.jobs.job_services import delete_job_service

    artifact_id, path = capture(in_memory_session_scope, tmp_path)
    with in_memory_session_scope() as s:
        j = context(s, artifact_id)
        repo = JobRepository(s)
        action = repo.enqueue_action(
            j.id, "run_vol3_plugin", payload={"plugin": "linux.pslist.PsList", "run_id": "retained"}
        )
        action.status = status
        s.flush()
        with pytest.raises(ApiError, match="Retained evidence"):
            delete_job_service(s, j.id)
        assert s.get(Job, j.id) is j and repo.get_action(action.id) is action
        assert path.exists() and s.get(Artifact, artifact_id)


def test_migration_ambiguity_preserves_deletion_and_retention_protection(in_memory_session_scope, tmp_path):
    from astinuslab.migrations.versions.a039_playground_artifact_ownership import reconcile
    from astinuslab.storage.deletion import protection_reason

    artifact_id, path = capture(in_memory_session_scope, tmp_path)
    with in_memory_session_scope() as s:
        artifact = s.get(Artifact, artifact_id)
        artifact.session_id = None
        artifact.job_id = "pg-source"
        artifact.acquisition_id = None
        artifact.case_id = None
        s.add(
            Job(
                id="pg-source", target_id="fixture", scenario_id="fixture", execution_mode="immediate", status="running"
            )
        )
        s.flush()
        reconcile(s.connection())
        s.expire_all()
        artifact = s.get(Artifact, artifact_id)
        assert artifact.job_id is None and artifact.ownership_reconciliation_json
        reason = protection_reason(s, artifact)  # retention shares this function
        assert reason is not None and "reconciliation" in reason
        denied = delete_artifact(s, artifact, actor="test", reason="normal deletion", roots=[tmp_path])
        assert not denied.deleted and path.exists()


@pytest.mark.parametrize(
    "linux",
    [
        {"kernel_release": "6.8.0-101-generic", "architecture": "x86_64"},
        {"kernel_release": "6.8.0-138-generic"},
        {"kernel_release": "6.8.0-138-generic", "architecture": "aarch64"},
        {},
    ],
)
def test_wrong_or_unknown_isf_cannot_be_uploaded_or_selected(in_memory_session_scope, tmp_path, monkeypatch, linux):
    from astinuslab.forensics import manual_upload

    artifact_id, _ = capture(in_memory_session_scope, tmp_path)
    data = json.dumps({"metadata": {"linux": linux}, "symbols": {"init_task": {}}}).encode()
    with in_memory_session_scope() as s:
        j = context(s, artifact_id)
        job_id = j.id
        isf = tmp_path / "mislabeled-isf.json"
        isf.write_bytes(data)
        JobRepository(s).create_isf_artifact(
            job_id, "6.8.0-138-generic", str(isf), "manual_upload", architecture="x86_64", validation_status="valid"
        )
        assert (
            next(d for d in services.list_vol3_dumps_service(s) if d.get("artifact_id") == artifact_id)["symbols"]
            == "missing"
        )
        with pytest.raises(HTTPException, match="ISF kernel/architecture"):
            services.enqueue_vol3_plugin_run_service(s, job_id, "linux.pslist.PsList")
        assert not JobRepository(s).list_actions(job_id)
    monkeypatch.setattr(manual_upload, "session_scope", in_memory_session_scope)
    with pytest.raises(HTTPException, match="ISF kernel/architecture"):
        manual_upload.store_manual_isf(job_id, data)
    assert isf.read_bytes() == data


def test_postqueue_hash_failure_keeps_source_identity(in_memory_session_scope, tmp_path, monkeypatch):
    scope = in_memory_session_scope
    artifact_id, path = capture(scope, tmp_path)
    with scope() as s:
        j = context(s, artifact_id)
        job_id = j.id
        isf = tmp_path / "matching-isf.json"
        isf.write_text(
            '{"metadata":{"linux":{"kernel_release":"6.8.0-138-generic","architecture":"x86_64"}},"symbols":{"init_task":{}}}'
        )
        JobRepository(s).create_isf_artifact(
            j.id, j.detected_kernel_release, str(isf), "manual_upload", architecture="x86_64", validation_status="valid"
        )
        monkeypatch.setattr(services, "_require_volatility_readiness", lambda: None)
        monkeypatch.setattr(services, "_dispatch_action_after_commit", lambda *args, **kwargs: None)
        queued = services.enqueue_vol3_plugin_run_service(s, job_id, "linux.pslist.PsList")
    path.write_bytes(b"corrupted after queue")
    runner = patch_plugin_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", scope)

    def must_not_execute(*args, **kwargs):
        raise AssertionError("hash failure must not execute a child")

    monkeypatch.setattr(runner.subprocess, "run", must_not_execute)
    ok, result = runner.run_vol3_plugin(job_id, queued["action_id"])
    assert not ok and "hash mismatch" in result["error"]
    assert result["source_artifact_id"] == artifact_id and result["rows"] == []


def test_queued_source_cannot_be_rebound_to_another_capture(in_memory_session_scope, tmp_path, monkeypatch):
    artifact_id, path = capture(in_memory_session_scope, tmp_path)
    with in_memory_session_scope() as s:
        j = context(s, artifact_id)
        job_id = j.id
        isf = tmp_path / "matching-isf.json"
        isf.write_text(
            '{"metadata":{"linux":{"kernel_release":"6.8.0-138-generic","architecture":"x86_64"}},"symbols":{"init_task":{}}}'
        )
        repo = JobRepository(s)
        repo.create_isf_artifact(
            j.id, j.detected_kernel_release, str(isf), "manual_upload", architecture="x86_64", validation_status="valid"
        )
        monkeypatch.setattr(services, "_require_volatility_readiness", lambda: None)
        monkeypatch.setattr(services, "_dispatch_action_after_commit", lambda *args, **kwargs: None)
        queued = services.enqueue_vol3_plugin_run_service(s, job_id, "linux.pslist.PsList")
        replacement = ArtifactRepository(s).create_artifact(
            kind="memory",
            producer="playground_capture",
            path=str(path),
            sha256=s.get(Artifact, artifact_id).sha256,
            session_id="pg-source",
            acquisition_id="acq-source",
        )
        j.source_artifact_id = replacement.id
        data = json.loads(j.analysis_context_json)
        data["artifact_id"] = replacement.id
        j.analysis_context_json = json.dumps(data)
    runner = patch_plugin_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    ok, result = runner.run_vol3_plugin(job_id, queued["action_id"])
    assert not ok and "Queued source artifact identity" in result["error"]
    assert result["source_artifact_id"] == artifact_id
