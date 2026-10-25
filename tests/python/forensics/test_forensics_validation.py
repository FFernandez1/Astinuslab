"""Fixture evidence: real validation/publication with a deliberately synthetic child."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import TypedDict

import pytest

from astinuslab.db.repositories import ArtifactRepository, JobRepository
from astinuslab.forensics import vol3_plugin_run as interactive
from astinuslab.forensics.vol3_tools import VerifiedVolatility
from tests.python._support.vol3_fixtures import enqueue_pinned_analysis_action
from tests.python._support.vol3_fixtures import make_completed_batch_job as _make_completed_job
from tests.python._support.vol3_fixtures import make_plugin_job as _make_job_with_action
from tests.python._support.vol3_fixtures import patch_batch_settings as batch_settings
from tests.python._support.vol3_fixtures import patch_plugin_settings as plugin_settings


class CapturedEvidence(TypedDict, total=False):
    path: Path
    manifest: Path
    bytes: bytes


def child(cmd, *, stdout, stderr, text, timeout, output="[]", config="valid"):
    """Write exactly the requested stream/config; never patch production validation."""
    stdout.write(output)
    cfg = Path(cmd[cmd.index("--save-config") + 1])
    staged = Path(cmd[cmd.index("--symbol-dirs") + 1])
    if config == "valid":
        isf = next((staged / "linux").glob("*.json"))
        cfg.write_text(json.dumps({"kernel.symbol_table_name.isf_url": isf.as_uri()}))
    elif config == "unreadable":
        cfg.mkdir()
    elif config == "missing-isf":
        cfg.write_text(json.dumps({"kernel.isf_url": (staged / "linux" / "absent.json").as_uri()}))
    elif config != "missing":
        cfg.write_text(config)
    return SimpleNamespace(returncode=0)


def setup_run(scope, monkeypatch, tmp_path, mode, *, plugin="linux.pslist.PsList", extra_payload=None):
    if mode == "batch":
        runner = batch_settings(monkeypatch, tmp_path, plugins=plugin)
        job_id, action_id, *_ = _make_completed_job(scope, tmp_path)
        run = runner.run_vol3_analysis
    else:
        runner = plugin_settings(monkeypatch, tmp_path)
        job_id, action_id = _make_job_with_action(
            scope, tmp_path, plugin=plugin, run_id="validation", extra_payload=extra_payload
        )
        run = runner.run_vol3_plugin
    monkeypatch.setattr(runner, "session_scope", scope)
    launcher = tmp_path / "fixture-vol3"
    launcher.write_text("#!/bin/sh\n# synthetic fixture launcher; never executed\n")
    monkeypatch.setattr(
        runner, "require_verified_volatility", lambda _: VerifiedVolatility(path=launcher, version="2.28.0")
    )
    return runner, run, job_id, action_id


MALFORMED = [
    "",
    " \n",
    "[",
    "[{}",
    "[{},",
    "[{},]",
    "[,{}]",
    "[{},,{}]",
    "[{}{}]",
    "[{}]junk",
    "[{}][]",
    "{}",
    "null",
    "[NaN]",
    "[Infinity]",
    "[{}]\u00a0",
    "[{}]\x00",
]


@pytest.mark.parametrize("payload", MALFORMED)
def test_stream_rejects_invalid_json_even_past_row_cap(tmp_path, payload):
    path = tmp_path / "output.json"
    path.write_text(payload)
    with pytest.raises(ValueError):
        interactive._parse_json_rows_file(path, max_rows=1)


@pytest.mark.parametrize("payload", ["[]", " [ ] \n", '[{"PID":1},{"PID":2}]', '[{"s":"' + "x" * 70000 + '"}]'])
def test_stream_accepts_valid_json_across_chunks(tmp_path, payload):
    path = tmp_path / "output.json"
    path.write_text(payload)
    _, rows, total, truncated = interactive._parse_json_rows_file(path, max_rows=1)
    assert total == len(json.loads(payload))
    assert len(rows) == min(1, total)
    assert truncated == (total > 1)


@pytest.mark.parametrize("mode", ["interactive", "batch"])
@pytest.mark.parametrize(
    "config", ["missing", "unreadable", "", "{", "[]", "{}", '{"kernel.isf_url":null}', "missing-isf"]
)
def test_linux_rejects_unverifiable_saved_config(in_memory_session_scope, monkeypatch, tmp_path, mode, config):
    runner, run, job_id, action_id = setup_run(in_memory_session_scope, monkeypatch, tmp_path, mode)
    monkeypatch.setattr(runner.subprocess, "run", lambda cmd, **kw: child(cmd, **kw, config=config))
    success, result = run(job_id, action_id)
    assert not success, result
    assert "ISF" in result["error"] or "config" in result["error"] or "symbol" in result["error"]
    with in_memory_session_scope() as session:
        action = JobRepository(session).get_action(action_id)
        assert action is not None
        assert action.status == "failed"
        assert not [a for a in ArtifactRepository(session).list_artifacts(job_id=job_id) if a.kind == "report"]


@pytest.mark.parametrize("mode", ["interactive", "batch"])
@pytest.mark.parametrize("output", ["", " \n\t"])
def test_zero_exit_empty_stdout_fails(in_memory_session_scope, monkeypatch, tmp_path, mode, output):
    runner, run, job_id, action_id = setup_run(in_memory_session_scope, monkeypatch, tmp_path, mode)
    monkeypatch.setattr(runner.subprocess, "run", lambda cmd, **kw: child(cmd, **kw, output=output))
    assert run(job_id, action_id)[0] is False


@pytest.mark.parametrize("mode", ["interactive", "batch"])
def test_legitimate_zero_rows_complete(in_memory_session_scope, monkeypatch, tmp_path, mode):
    runner, run, job_id, action_id = setup_run(in_memory_session_scope, monkeypatch, tmp_path, mode)
    output = "[]" if mode == "interactive" else "PID\tCOMM\n\n"
    monkeypatch.setattr(runner.subprocess, "run", lambda cmd, **kw: child(cmd, **kw, output=output))
    success, result = run(job_id, action_id)
    assert success, result
    assert result["isf_pinned"] is True


@pytest.mark.parametrize(
    "source", ["manual_upload", "cache_soft_hit", "cache", None, "", "unknown", "live_guest", "host_fetch_unverified"]
)
def test_unverified_origin_warns(source):
    result = interactive._result(
        run_id="trust",
        job_id="job",
        plugin="linux.pslist.PsList",
        status="completed",
        started_at=interactive._utc_now(),
        isf_source=source,
        include_isf_provenance=True,
    )
    assert result["isf_low_trust"] is True
    assert result["isf_trust"] == "unverified"


@pytest.mark.parametrize("mode", ["interactive", "batch"])
def test_published_execution_binds_inputs_and_raw_output(in_memory_session_scope, monkeypatch, tmp_path, mode):
    runner, run, job_id, action_id = setup_run(in_memory_session_scope, monkeypatch, tmp_path, mode)
    monkeypatch.setattr(runner.subprocess, "run", child)
    success, result = run(job_id, action_id)
    assert success, result
    record = result if mode == "interactive" else result["plugins"][0]
    execution = record["execution"]
    assert execution["tool"]["version"] == "2.28.0"
    launcher = execution["tool"]["executable"]
    assert launcher["sha256"] == hashlib.sha256(Path(launcher["path"]).read_bytes()).hexdigest()
    assert execution["tool"]["package_identity_verified"] is False
    assert execution["argv"][0] == launcher["path"]
    assert execution["inputs"]["memory"]["sha256_at_use"]
    assert execution["inputs"]["isf"]["sha256_at_use"]
    for key in ("stdout", "stderr", "saved_config"):
        identity = execution["files"][key]
        assert identity["sha256"] == hashlib.sha256(Path(identity["path"]).read_bytes()).hexdigest()
    assert execution["files"]["saved_config"]["content"]
    with in_memory_session_scope() as session:
        artifact = next(a for a in ArtifactRepository(session).list_artifacts(job_id=job_id) if a.kind == "report")
        assert artifact.manifest_path is not None
        manifest = json.loads(Path(artifact.manifest_path).read_text())
        assert execution in manifest["executions"]
        assert hashlib.sha256(Path(artifact.path).read_bytes()).hexdigest() == artifact.sha256


def test_publication_failure_does_not_rewrite_hashed_result(in_memory_session_scope, monkeypatch, tmp_path):
    runner, run, job_id, action_id = setup_run(in_memory_session_scope, monkeypatch, tmp_path, "interactive")
    monkeypatch.setattr(runner.subprocess, "run", child)
    observed: CapturedEvidence = {}

    def reject_report(self, **kw):
        path = Path(kw["path"])
        observed["path"] = path
        observed["bytes"] = path.read_bytes()
        raise RuntimeError("fixture report index failure after hashing")

    monkeypatch.setattr(runner.ReportRepository, "create_report", reject_report)
    success, result = run(job_id, action_id)
    assert not success
    assert result["status"] == "failed"
    assert "publication" in result["error"].lower()
    assert observed["path"].read_bytes() == observed["bytes"]
    with in_memory_session_scope() as session:
        action = JobRepository(session).get_action(action_id)
        assert action is not None
        assert action.status == "failed"
    from astinuslab.forensics import api

    monkeypatch.setattr(api, "settings", runner.settings)
    monkeypatch.setattr(api, "session_scope", in_memory_session_scope)
    assert api.volatility_api_run_result(job_id, "validation")["status"] == "failed"
    assert api.volatility_api_run_latest(job_id, "linux.pslist.PsList")["status"] == "failed"


@pytest.mark.parametrize("status", ["failed", "cancelled", "deleted"])
def test_latest_never_resurrects_previous_success(in_memory_session_scope, monkeypatch, tmp_path, status):
    from astinuslab.forensics import api

    monkeypatch.setattr(api, "settings", SimpleNamespace(storage_dir=tmp_path))
    monkeypatch.setattr(api, "session_scope", in_memory_session_scope)
    root = tmp_path / "dumps" / "job1" / "vol3" / "runs"
    root.mkdir(parents=True)
    for run_id, run_status, date in [("old", "completed", "2026-01-01"), ("new", status, "2026-01-02")]:
        data = {
            "run_id": run_id,
            "job_id": "job1",
            "plugin": "linux.pslist.PsList",
            "status": run_status,
            "finished_at": date,
        }
        (root / f"{run_id}.meta.json").write_text(json.dumps(data))
        if run_status != "deleted":
            (root / f"{run_id}.json").write_text(json.dumps(data))
    result = api.volatility_api_run_latest("job1", "linux.pslist.PsList")
    assert result["status"] == status
    assert result["run_id"] == "new"


def test_failure_sidecar_is_not_a_queue_item(in_memory_session_scope, monkeypatch, tmp_path):
    from astinuslab.forensics import api

    monkeypatch.setattr(api, "settings", SimpleNamespace(storage_dir=tmp_path))
    root = tmp_path / "dumps" / "job1" / "vol3" / "runs"
    root.mkdir(parents=True)
    data = {"run_id": "run1", "job_id": "job1", "plugin": "linux.pslist.PsList", "status": "completed"}
    (root / "run1.json").write_text(json.dumps(data))
    (root / "run1.failure.json").write_text(json.dumps({**data, "status": "failed", "error": "publication"}))
    assert len(api._recent_runs()) == 1
    assert api._recent_runs()[0]["status"] == "failed"
    assert api.volatility_api_queue_clear() == {"deleted": 1}


def test_batch_publication_failure_survives_unavailable_database(in_memory_session_scope, monkeypatch, tmp_path):
    runner, run, job_id, action_id = setup_run(in_memory_session_scope, monkeypatch, tmp_path, "batch")
    monkeypatch.setattr(runner.subprocess, "run", child)
    observed: CapturedEvidence = {}

    def unavailable(*args, **kwargs):
        raise RuntimeError("fixture database is locked")

    def reject(self, **kwargs):
        observed["path"] = Path(kwargs["path"])
        observed["bytes"] = observed["path"].read_bytes()
        monkeypatch.setattr(runner, "_finalise_action", unavailable)
        raise RuntimeError("fixture publication database is locked")

    monkeypatch.setattr(runner.ReportRepository, "create_report", reject)
    success, result = run(job_id, action_id)
    assert not success
    failure = json.loads((observed["path"].parent / "failure.json").read_text())
    assert failure["status"] == "failed"
    assert failure["action_id"] == action_id
    assert failure["recovery"]["action_finalization"] == "pending"
    assert "locked" in failure["recovery"]["error"]
    assert observed["path"].read_bytes() == observed["bytes"]


def test_tombstone_overrides_residual_result_and_reports_cleanup_error(in_memory_session_scope, monkeypatch, tmp_path):
    runner, run, job_id, action_id = setup_run(in_memory_session_scope, monkeypatch, tmp_path, "interactive")
    monkeypatch.setattr(runner.subprocess, "run", child)
    assert run(job_id, action_id)[0]
    from astinuslab.forensics import api

    monkeypatch.setattr(api, "settings", runner.settings)
    monkeypatch.setattr(api, "session_scope", in_memory_session_scope)
    original = Path.unlink

    def denied(path, *args, **kwargs):
        if path.name == "validation.json":
            raise PermissionError("fixture residual bytes")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        api.volatility_api_run_delete(job_id, "validation")
    assert exc.value.status_code == 500
    assert "cleanup" in str(exc.value.detail).lower()
    result = api.volatility_api_run_result(job_id, "validation")
    assert result["status"] == "deleted"
    assert "fixture residual bytes" in result["message"]
    assert "detail" not in result
    assert "rows" not in result
    assert api.volatility_api_run_latest(job_id, "linux.pslist.PsList")["status"] == "deleted"


@pytest.mark.parametrize("payload", MALFORMED)
def test_invalid_interactive_output_preserved_as_failure(in_memory_session_scope, monkeypatch, tmp_path, payload):
    runner, run, job_id, action_id = setup_run(in_memory_session_scope, monkeypatch, tmp_path, "interactive")
    monkeypatch.setattr(runner.subprocess, "run", lambda cmd, **kw: child(cmd, **kw, output=payload))
    success, result = run(job_id, action_id)
    assert not success
    identity = result["execution"]["files"]["stdout"]
    assert Path(identity["path"]).read_text() == payload
    assert identity["sha256"] == hashlib.sha256(payload.encode()).hexdigest()


def test_changed_version_and_rules_have_distinct_immutable_provenance(in_memory_session_scope, monkeypatch, tmp_path):
    rules = "rule one { condition: true }"
    runner, run, job_id, action_id = setup_run(
        in_memory_session_scope,
        monkeypatch,
        tmp_path,
        "interactive",
        plugin="yarascan.YaraScan",
        extra_payload={"yara_rules": rules},
    )
    monkeypatch.setattr(runner.subprocess, "run", child)
    success, first = run(job_id, action_id)
    assert success, first
    root = tmp_path / "storage" / "dumps" / job_id / "vol3" / "runs"
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    replacement_rules = "rule two { condition: false }"
    with in_memory_session_scope() as session:
        action = enqueue_pinned_analysis_action(
            JobRepository(session),
            job_id,
            "run_vol3_plugin",
            payload={"plugin": "yarascan.YaraScan", "run_id": "changed", "yara_rules": replacement_rules},
        )
        next_action_id = action.id
    launcher = tmp_path / "fixture-vol3"
    launcher.write_text("#!/bin/sh\n# fixture changed in place\n")
    monkeypatch.setattr(
        runner, "require_verified_volatility", lambda _: VerifiedVolatility(path=launcher, version="2.29.0")
    )
    success, second = run(job_id, next_action_id)
    assert success, second
    first_exec, second_exec = first["execution"], second["execution"]
    assert first_exec["tool"]["version"] == "2.28.0"
    assert second_exec["tool"]["version"] == "2.29.0"
    assert first_exec["tool"]["executable"]["sha256"] != second_exec["tool"]["executable"]["sha256"]
    assert first_exec["files"]["yara_rules"]["sha256"] != second_exec["files"]["yara_rules"]["sha256"]
    assert second_exec["files"]["yara_rules"]["content"] == replacement_rules
    assert first_exec["files"]["yara_rules"]["content"] == rules
    assert all(path.read_bytes() == data for path, data in before.items())
    # Actual duplicate action delivery and a different action reusing run_id
    # must both preserve the prior JSON, manifest, raw output and staged ISF.
    run(job_id, action_id)
    with in_memory_session_scope() as session:
        duplicate = JobRepository(session).enqueue_action(
            job_id,
            "run_vol3_plugin",
            payload={"plugin": "yarascan.YaraScan", "run_id": "validation", "yara_rules": replacement_rules},
        )
        duplicate_id = duplicate.id
    assert run(job_id, duplicate_id)[0] is False
    assert all(path.read_bytes() == data for path, data in before.items())


@pytest.mark.parametrize("source", ["host_fetch", "live_guest", "manual_upload"])
def test_cache_origin_resolution_reaches_execution_and_manifest(in_memory_session_scope, monkeypatch, tmp_path, source):
    runner, run, job_id, action_id = setup_run(in_memory_session_scope, monkeypatch, tmp_path, "interactive")
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        isf_artifact = repo.get_latest_isf_artifact(job_id)
        assert isf_artifact is not None
        isf_artifact.source = source
        isf_artifact.sha256 = hashlib.sha256(Path(isf_artifact.path).read_bytes()).hexdigest()
        repo.create_isf_artifact(
            job_id,
            isf_artifact.kernel_release,
            isf_artifact.path,
            "cache_soft_hit",
            architecture=isf_artifact.architecture,
            sha256=isf_artifact.sha256,
            validation_status="valid",
        )
    monkeypatch.setattr(runner.subprocess, "run", child)
    success, result = run(job_id, action_id)
    assert success, result
    assert result["isf_source"] == source
    assert result["isf_low_trust"] is (source != "host_fetch")
    assert result["execution"]["inputs"]["isf"]["source"] == source
    with in_memory_session_scope() as session:
        artifact = next(a for a in ArtifactRepository(session).list_artifacts(job_id=job_id) if a.kind == "report")
        assert artifact.manifest_path is not None
        manifest = json.loads(Path(artifact.manifest_path).read_text())
        assert manifest["inputs"][1]["source"] == source
        assert manifest["inputs"][1]["trust"] == result["isf_trust"]


def test_batch_failure_diagnostic_with_real_sqlite_wal_writer(monkeypatch, tmp_path):
    import sqlite3
    from contextlib import contextmanager

    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    from astinuslab.db import Base
    from astinuslab.db.models import ArtifactPublicationAttempt
    from astinuslab.storage import publication

    db_path = tmp_path / "wal.sqlite"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 0.02})

    @event.listens_for(engine, "connect")
    def wal(conn, _):
        conn.execute("PRAGMA journal_mode=WAL")

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    @contextmanager
    def scope():
        with factory.begin() as session:
            yield session

    runner, run, job_id, action_id = setup_run(scope, monkeypatch, tmp_path, "batch")
    monkeypatch.setattr(runner.subprocess, "run", child)
    publish = publication.PublicationAttempt.publish_sidecars
    writers = []
    captured: CapturedEvidence = {}

    def lock_after_sidecars(attempt):
        result = publish(attempt)
        captured["manifest"] = Path(result.manifest_path)
        captured["bytes"] = captured["manifest"].read_bytes()
        conn = sqlite3.connect(db_path)
        conn.execute("BEGIN IMMEDIATE")
        writers.append(conn)
        return result

    monkeypatch.setattr(publication.PublicationAttempt, "publish_sidecars", lock_after_sidecars)
    try:
        success, result = run(job_id, action_id)
        assert not success
        failure = json.loads((Path(result["output_dir"]) / "failure.json").read_text())
        assert failure["recovery"]["action_finalization"] == "pending"
        assert "locked" in failure["recovery"]["error"]
        assert captured["manifest"].read_bytes() == captured["bytes"]
    finally:
        for writer in writers:
            writer.rollback()
            writer.close()
    with scope() as session:
        assert session.query(ArtifactPublicationAttempt).one().state == "published"
        action = JobRepository(session).get_action(action_id)
        assert action is not None
        assert action.status == "running"
    engine.dispose()


def test_poll_does_not_offer_rows_before_publication_finishes(in_memory_session_scope, monkeypatch, tmp_path):
    runner, run, job_id, action_id = setup_run(in_memory_session_scope, monkeypatch, tmp_path, "interactive")
    monkeypatch.setattr(runner.subprocess, "run", child)
    from astinuslab.forensics import api

    monkeypatch.setattr(api, "settings", runner.settings)
    monkeypatch.setattr(api, "session_scope", in_memory_session_scope)
    original = runner._record_run_report_artifact

    def inspect(**kwargs):
        polled = api.volatility_api_run_result(job_id, "validation")
        assert polled["status"] == "running"
        assert "rows" not in polled
        assert api._recent_runs()[0]["status"] == "running"
        return original(**kwargs)

    monkeypatch.setattr(runner, "_record_run_report_artifact", inspect)
    success, result = run(job_id, action_id)
    assert success, result
    assert api.volatility_api_run_result(job_id, "validation")["status"] == "completed"


@pytest.mark.parametrize("mode", ["batch", "interactive"])
def test_execution_hashes_the_staged_symbols(in_memory_session_scope, monkeypatch, tmp_path, mode):
    runner, run, job_id, action_id = setup_run(in_memory_session_scope, monkeypatch, tmp_path, mode)
    monkeypatch.setattr(runner.subprocess, "run", child)
    success, result = run(job_id, action_id)
    assert success, result
    execution = result["execution"] if mode == "interactive" else result["plugins"][0]["execution"]
    staged = execution["inputs"]["isf"]["staged"]
    assert staged["sha256"] == hashlib.sha256(Path(staged["path"]).read_bytes()).hexdigest()
    assert staged["sha256"] == execution["inputs"]["isf"]["sha256_at_use"]


def test_interactive_completion_db_error_retains_failure_diagnostic(in_memory_session_scope, monkeypatch, tmp_path):
    runner, run, job_id, action_id = setup_run(in_memory_session_scope, monkeypatch, tmp_path, "interactive")
    monkeypatch.setattr(runner.subprocess, "run", child)
    finalise = runner._finalise

    def fail_success(*args, **kwargs):
        if kwargs.get("success"):
            raise RuntimeError("fixture completion database error")
        return finalise(*args, **kwargs)

    monkeypatch.setattr(runner, "_finalise", fail_success)
    success, result = run(job_id, action_id)
    assert not success
    path = tmp_path / "storage" / "dumps" / job_id / "vol3" / "runs" / "validation.json"
    manifest = json.loads(path.with_name(path.name + ".manifest.json").read_text())
    assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest["sha256"]
    failure = json.loads(path.with_name("validation.failure.json").read_text())
    assert failure["status"] == "failed"
    assert "completion database error" in failure["error"]


@pytest.mark.parametrize("legacy", [False, True])
def test_review_collision_preserves_original_get_status(in_memory_session_scope, monkeypatch, tmp_path, legacy):
    runner, run, job_id, action_id = setup_run(in_memory_session_scope, monkeypatch, tmp_path, "interactive")
    monkeypatch.setattr(runner.subprocess, "run", child)
    from astinuslab.forensics import api

    monkeypatch.setattr(api, "settings", runner.settings)
    monkeypatch.setattr(api, "session_scope", in_memory_session_scope)
    assert run(job_id, action_id)[0]
    path = tmp_path / "storage" / "dumps" / job_id / "vol3" / "runs" / "validation.json"
    if legacy:
        # Explicit fixture of a pre-owner result. Do this before the preservation
        # snapshot; legacy results have no action_id in their immutable JSON.
        data = json.loads(path.read_text())
        data.pop("action_id", None)
        path.write_text(json.dumps(data))
    before = path.read_bytes()
    with in_memory_session_scope() as session:
        duplicate = JobRepository(session).enqueue_action(
            job_id, "run_vol3_plugin", payload={"plugin": "linux.pslist.PsList", "run_id": "validation"}
        )
        duplicate_id = duplicate.id
    assert not run(job_id, duplicate_id)[0]
    assert path.read_bytes() == before
    assert api.volatility_api_run_result(job_id, "validation")["status"] == "completed"
    latest = api.volatility_api_run_latest(job_id, "linux.pslist.PsList")
    assert latest["status"] == "failed"
    assert latest["action_id"] == duplicate_id
    assert "rows" not in latest


def test_review_queue_tombstone_is_authoritative(in_memory_session_scope, monkeypatch, tmp_path):
    from fastapi import HTTPException

    from astinuslab.forensics import api

    runner, run, job_id, action_id = setup_run(in_memory_session_scope, monkeypatch, tmp_path, "interactive")
    monkeypatch.setattr(runner.subprocess, "run", child)
    monkeypatch.setattr(api, "settings", runner.settings)
    monkeypatch.setattr(api, "session_scope", in_memory_session_scope)
    assert run(job_id, action_id)[0]
    unlink = Path.unlink

    def denied(path, *args, **kwargs):
        if path.name == "validation.json":
            raise PermissionError("fixture queue tombstone residual")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied)
    with pytest.raises(HTTPException):
        api.volatility_api_run_delete(job_id, "validation")
    read = Path.read_text

    def no_result_rows(path, *args, **kwargs):
        if path.name == "validation.json":
            pytest.fail("queue must not load rows when a header exists")
        return read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", no_result_rows)
    assert api._recent_runs()[0]["status"] == "deleted"


def test_review_final_header_failure_recovers_from_owner(in_memory_session_scope, monkeypatch, tmp_path):
    from astinuslab.forensics import api

    runner, run, job_id, action_id = setup_run(in_memory_session_scope, monkeypatch, tmp_path, "interactive")
    monkeypatch.setattr(runner.subprocess, "run", child)
    monkeypatch.setattr(api, "settings", runner.settings)
    monkeypatch.setattr(api, "session_scope", in_memory_session_scope)
    write = Path.write_text
    injections = []

    def denied(path, data, *args, **kwargs):
        is_header = path.name == "validation.meta.json" or path.name.startswith(".validation.meta.json.")
        if is_header and json.loads(data).get("status") == "completed":
            injections.append(path)
            raise OSError("fixture final header write failure")
        return write(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", denied)
    success, result = run(job_id, action_id)
    assert success, result
    assert len(injections) == 1
    assert api.volatility_api_run_result(job_id, "validation")["status"] == "completed"
    assert api._recent_runs()[0]["status"] == "completed"


def test_review_partial_header_write_preserves_visible_completion(in_memory_session_scope, monkeypatch, tmp_path):
    from astinuslab.forensics import api

    runner, run, job_id, action_id = setup_run(in_memory_session_scope, monkeypatch, tmp_path, "interactive")
    monkeypatch.setattr(runner.subprocess, "run", child)
    monkeypatch.setattr(api, "settings", runner.settings)
    monkeypatch.setattr(api, "session_scope", in_memory_session_scope)
    write = Path.write_text
    injections = []

    def partial_header(path, data, *args, **kwargs):
        is_header = path.name == "validation.meta.json" or path.name.startswith(".validation.meta.json.")
        if is_header and json.loads(data).get("status") == "completed":
            write(path, data[:12], *args, **kwargs)
            injections.append(path)
            raise OSError(28, "fixture no space after partial header write")
        return write(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", partial_header)
    success, result = run(job_id, action_id)
    assert success, result
    assert len(injections) == 1
    assert api.volatility_api_run_result(job_id, "validation")["status"] == "completed"
    assert api._recent_runs()[0]["status"] == "completed"
    root = tmp_path / "storage" / "dumps" / job_id / "vol3" / "runs"
    assert not list(root.glob(".validation.meta.json.*.tmp"))
    saved = json.loads((root / "validation.meta.json").read_text())
    assert saved["status"] in {"running", "completed"}
