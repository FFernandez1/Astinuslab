"""A queued plugin run analyses the dump it was queued against, not the newest one.

Uploading a second memory dump to a completed job used to re-point already-queued
work at the new bytes: the worker read ``Job.dump_path``, which reads through to
the newest ``kind="memory"`` artifact, so the child process opened B while the
published report still claimed to be the analysis the operator asked for. These
tests pin the run to the artifact chosen at enqueue and refuse anything else.
"""

from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import UploadFile

from astinuslab.api.schemas import JobCreate
from astinuslab.db.models import Artifact
from astinuslab.db.repositories import ArtifactRepository, JobRepository, ReportRepository
from astinuslab.forensics import analysis_input
from astinuslab.forensics import manual_upload as manual_upload_mod
from astinuslab.forensics import services as forensic_services
from astinuslab.forensics.vol3_tools import ToolStatus
from tests.python._support.vol3_fixtures import completed_child
from tests.python._support.vol3_fixtures import patch_plugin_settings as _patch_settings

PLUGIN = "linux.pslist.PsList"
KERNEL = "6.8.0-31-generic"


@pytest.mark.parametrize("kind", ["plugin", "batch"])
def test_source_removed_during_analysis_prevents_report_publication(
    in_memory_session_scope, monkeypatch, tmp_path, kind
):
    from tests.python._support.vol3_fixtures import make_completed_batch_job, patch_batch_settings

    scope = in_memory_session_scope
    if kind == "plugin":
        runner = _patch_settings(monkeypatch, tmp_path)
        job_id = _completed_job(scope, tmp_path)
        _capture_memory_artifact(scope, tmp_path, job_id, name="source.lime", body=b"source")
        with scope() as session:
            action_id = forensic_services.enqueue_vol3_plugin_run_service(session, job_id, PLUGIN)["action_id"]
        run_worker = runner.run_vol3_plugin
    else:
        runner = patch_batch_settings(monkeypatch, tmp_path)
        job_id, _, _, _ = make_completed_batch_job(scope, tmp_path, with_action=False)
        with scope() as session:
            action_id = forensic_services.enqueue_vol3_analysis_service(session, job_id).id
        run_worker = runner.run_vol3_analysis
    monkeypatch.setattr(runner, "session_scope", scope)

    def child_then_remove_pin(cmd, **kwargs):
        with scope() as session:
            action = JobRepository(session).get_action(action_id)
            assert action is not None
            action.source_artifact_id = None
        return completed_child(cmd, kwargs, stdout=json.dumps([{"PID": 1, "COMM": "systemd"}]))

    monkeypatch.setattr(runner.subprocess, "run", child_then_remove_pin)
    success, result = run_worker(job_id, action_id=action_id)
    assert success is False, result
    with scope() as session:
        assert ReportRepository(session).list_reports(job_id=job_id) == []
        assert not [
            artifact
            for artifact in ArtifactRepository(session).list_artifacts(job_id=job_id)
            if artifact.producer in {"vol3_plugin_run", "vol3_analysis"}
        ]


def test_queued_batch_uses_original_dump_after_upload(in_memory_session_scope, monkeypatch, tmp_path):
    from tests.python._support.vol3_fixtures import make_completed_batch_job, patch_batch_settings

    scope = in_memory_session_scope
    runner = patch_batch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", scope)
    job_id, _, dump_a, _ = make_completed_batch_job(scope, tmp_path, with_action=False)
    with scope() as session:
        action = forensic_services.enqueue_vol3_analysis_service(session, job_id)
        action_id, artifact_a = action.id, action.source_artifact_id
    _upload_second_dump(monkeypatch, scope, tmp_path, job_id, b"newer dump B")
    spawned = []

    def run(cmd, **kwargs):
        spawned.append(cmd)
        return completed_child(cmd, kwargs, stdout="PID NAME\n1 systemd\n")

    monkeypatch.setattr(runner.subprocess, "run", run)
    success, result = runner.run_vol3_analysis(job_id, action_id=action_id)
    assert success, result
    assert spawned and all(cmd[cmd.index("-f") + 1] == str(dump_a) for cmd in spawned)
    assert result["source_artifact_id"] == artifact_a
    for plugin in result["plugins"]:
        assert plugin["execution"]["inputs"]["memory"]["artifact_id"] == artifact_a
    manifest = json.loads((Path(result["output_dir"]) / "analysis-summary.json.manifest.json").read_text())
    assert manifest["inputs"][0]["artifact_id"] == artifact_a
    with scope() as session:
        assert ReportRepository(session).list_reports(job_id=job_id)[0].input_artifact_id == artifact_a


@pytest.mark.parametrize("first", ["enqueue", "delete"])
def test_enqueue_and_delete_serialize_source_custody(monkeypatch, tmp_path, first):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager

    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    from astinuslab.api.errors import ApiError
    from astinuslab.db import Base
    from astinuslab.storage.deletion import delete_artifact

    engine = create_engine(
        f"sqlite:///{tmp_path / 'race.db'}", connect_args={"check_same_thread": False, "timeout": 10}
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)

    @contextmanager
    def scope():
        with factory() as session:
            try:
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise

    job_id = _completed_job(scope, tmp_path)
    artifact_id, _ = _capture_memory_artifact(scope, tmp_path, job_id, name="source.lime", body=b"source")
    ready, release, contender = threading.Event(), threading.Event(), threading.Event()
    original_select = forensic_services.select_source_artifact

    def select_and_pause(session, job):
        source = original_select(session, job)
        if first == "enqueue":
            ready.set()
            assert release.wait(10)
        return source

    monkeypatch.setattr(forensic_services, "select_source_artifact", select_and_pause)

    def enqueue():
        if first != "enqueue":
            contender.set()
        try:
            with scope() as session:
                forensic_services.enqueue_vol3_plugin_run_service(session, job_id, PLUGIN)
            return True
        except ApiError:
            return False

    def delete():
        if first != "delete":
            contender.set()
        with scope() as session:
            artifact = session.get(Artifact, artifact_id)
            assert artifact is not None
            result = delete_artifact(session, artifact, actor="test", reason="race", roots=(tmp_path / "storage",))
            if first == "delete":
                ready.set()
                assert release.wait(10)
            return result.deleted

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            operations = {"enqueue": enqueue, "delete": delete}
            winner = workers.submit(operations[first])
            try:
                assert ready.wait(10)
                loser = workers.submit(operations["delete" if first == "enqueue" else "enqueue"])
                assert contender.wait(10)
            finally:
                release.set()
            assert winner.result(timeout=10) is True
            assert loser.result(timeout=10) is False
        with scope() as session:
            actions = JobRepository(session).list_actions(job_id)
            assert len(actions) == (1 if first == "enqueue" else 0)
            if actions:
                assert actions[0].source_artifact_id == artifact_id
                assert session.get(Artifact, artifact_id) is not None
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def _ready_enqueue_dependency(monkeypatch):
    """The enqueue service probes Volatility; these tests never spawn it."""
    monkeypatch.setattr(
        forensic_services,
        "check_volatility_tool",
        lambda _settings: ToolStatus(
            ready=True,
            code="ready",
            detail="Volatility 3 is ready.",
            version="2.28.0",
            resolved_path=Path("/verified/vol3"),
        ),
        raising=False,
    )


def _completed_job(scope, tmp_path, *, distro="ubuntu"):
    """A completed immediate-capture job with a valid ISF for its kernel."""
    with scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
            )
        )
        repo.force_transition(
            job.id,
            "completed",
            detected_distro=distro,
            detected_version="22.04",
            detected_kernel_release=KERNEL,
            detected_architecture="x86_64",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
        job_id = job.id

    isf_path = tmp_path / "vol3" / "symbols" / "linux" / f"{KERNEL}.json"
    isf_path.parent.mkdir(parents=True, exist_ok=True)
    isf_path.write_text('{"symbols":{}}', encoding="utf-8")
    with scope() as session:
        JobRepository(session).create_isf_artifact(
            job_id,
            KERNEL,
            str(isf_path),
            "cache",
            distro=distro,
            validation_status="valid",
            architecture="x86_64",
        )
    return job_id


def _capture_memory_artifact(scope, tmp_path, job_id, *, name, body, age_seconds=60):
    """The job's original capture: real bytes, recorded checksum and size."""
    path = tmp_path / "storage" / "dumps" / job_id / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    with scope() as session:
        artifact = ArtifactRepository(session).create_artifact(
            kind="memory",
            producer="job_capture",
            path=str(path),
            filename=name,
            sha256=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
            job_id=job_id,
        )
        # `Job.dump_path` reads the newest memory artifact; make "newest"
        # unambiguous rather than dependent on same-instant tie-breaking.
        artifact.created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=age_seconds)
        session.flush()
        return artifact.id, path


def _upload_second_dump(monkeypatch, scope, tmp_path, job_id, body):
    """Upload a second dump through the real manual-upload service."""
    from types import SimpleNamespace

    monkeypatch.setattr(manual_upload_mod, "session_scope", scope)
    monkeypatch.setattr(manual_upload_mod, "settings", SimpleNamespace(storage_dir=tmp_path / "storage"))
    upload = UploadFile(filename="second.lime", file=io.BytesIO(body))
    return manual_upload_mod.store_manual_dump(job_id, upload)


def _run_file(tmp_path, job_id, run_id):
    return tmp_path / "storage" / "dumps" / job_id / "vol3" / "runs" / f"{run_id}.json"


def _rows_child(spawned):
    def fake_run(cmd, **kwargs):
        spawned.append(cmd)
        return completed_child(cmd, kwargs, returncode=0, stdout=json.dumps([{"PID": 1, "COMM": "systemd"}]), stderr="")

    return fake_run


def test_queued_plugin_run_analyses_its_own_source_not_a_later_upload(in_memory_session_scope, monkeypatch, tmp_path):
    scope = in_memory_session_scope
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", scope)

    job_id = _completed_job(scope, tmp_path)
    artifact_a, dump_a = _capture_memory_artifact(scope, tmp_path, job_id, name="memory-a.lime", body=b"dump-A-bytes")
    sha_a = hashlib.sha256(b"dump-A-bytes").hexdigest()

    with scope() as session:
        queued = forensic_services.enqueue_vol3_plugin_run_service(session, job_id, PLUGIN)
    action_id, run_id = queued["action_id"], queued["run_id"]

    uploaded = _upload_second_dump(monkeypatch, scope, tmp_path, job_id, b"dump-B-bytes-which-is-longer")
    assert uploaded["dump_path"] != str(dump_a)
    with scope() as session:
        # The job itself now presents B: this is exactly what the worker used to read.
        job = JobRepository(session).get_job(job_id)
        assert job is not None and job.dump_path == uploaded["dump_path"]

    spawned: list[list[str]] = []
    monkeypatch.setattr(runner.subprocess, "run", _rows_child(spawned))

    success, result = runner.run_vol3_plugin(job_id, action_id=action_id)
    assert success is True, result.get("error")

    cmd = spawned[0]
    assert cmd[cmd.index("-f") + 1] == str(dump_a)

    memory = result["execution"]["inputs"]["memory"]
    assert memory["artifact_id"] == artifact_a
    assert memory["path"] == str(dump_a)
    assert memory["sha256"] == sha_a
    assert memory["sha256_at_use"] == sha_a
    assert result["source_artifact_id"] == artifact_a
    assert result["dump_file"] == "memory-a.lime"

    manifest = json.loads(_run_file(tmp_path, job_id, run_id).with_suffix(".json.manifest.json").read_text())
    memory_input = next(item for item in manifest["inputs"] if item["kind"] == "memory")
    assert memory_input["artifact_id"] == artifact_a
    assert memory_input["path"] == str(dump_a)
    assert memory_input["sha256"] == sha_a
    assert memory_input["verified"] is True

    with scope() as session:
        report = next(
            r for r in ReportRepository(session).list_reports(job_id=job_id) if r.report_type == "volatility3_plugin"
        )
        assert report.input_artifact_id == artifact_a


def test_truly_unpinned_ordinary_action_fails_closed_without_publishing(in_memory_session_scope, monkeypatch, tmp_path):
    """No pin, no guess: an ordinary action predating pinning asks to be requeued."""
    scope = in_memory_session_scope
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", scope)

    job_id = _completed_job(scope, tmp_path)
    _capture_memory_artifact(scope, tmp_path, job_id, name="memory-a.lime", body=b"dump-A-bytes")

    with scope() as session:
        action = JobRepository(session).enqueue_action(
            job_id, "run_vol3_plugin", payload={"plugin": PLUGIN, "run_id": "unpinned"}
        )
        action_id = action.id

    spawned: list[list[str]] = []
    monkeypatch.setattr(runner.subprocess, "run", _rows_child(spawned))

    success, result = runner.run_vol3_plugin(job_id, action_id=action_id)

    assert success is False
    assert spawned == []
    assert "requeue" in result["error"]
    assert not _run_file(tmp_path, job_id, "unpinned").with_suffix(".json.manifest.json").exists()
    with scope() as session:
        published = [
            a for a in ArtifactRepository(session).list_artifacts(job_id=job_id) if a.producer == "vol3_plugin_run"
        ]
        assert published == []
        assert ReportRepository(session).list_reports(job_id=job_id) == []


def test_legacy_artifact_analysis_pin_is_still_resolvable(in_memory_session_scope, tmp_path):
    """An older artifact-analysis action already names its source; that pin is provable."""
    scope = in_memory_session_scope
    body = b"retained-evidence"
    path = tmp_path / "evidence.lime"
    path.write_bytes(body)

    with scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
            )
        )
        artifact = ArtifactRepository(session).create_artifact(
            kind="memory",
            producer="manual_upload",
            path=str(path),
            filename="evidence.lime",
            sha256=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
        )
        job.execution_mode = "artifact_analysis"
        job.source_artifact_id = artifact.id
        action = repo.enqueue_action(
            job.id,
            "run_vol3_plugin",
            payload={"plugin": PLUGIN, "run_id": "legacy01", "source_artifact_id": artifact.id},
        )
        session.flush()

        resolved = analysis_input.resolve_analysis_input(session, job, action)
        assert resolved.artifact_id == artifact.id
        assert resolved.path == str(path)
        assert resolved.legacy is True
        assert resolved.verified is True


def test_legacy_artifact_analysis_pin_must_match_its_context(in_memory_session_scope, tmp_path):
    """A legacy pin naming something other than the job's source is not a pin."""
    scope = in_memory_session_scope
    path = tmp_path / "evidence.lime"
    path.write_bytes(b"retained-evidence")

    with scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
            )
        )
        artifact = ArtifactRepository(session).create_artifact(
            kind="memory",
            producer="manual_upload",
            path=str(path),
            filename="evidence.lime",
        )
        job.execution_mode = "artifact_analysis"
        job.source_artifact_id = artifact.id
        action = repo.enqueue_action(
            job.id,
            "run_vol3_plugin",
            payload={"plugin": PLUGIN, "run_id": "legacy02", "source_artifact_id": "some-other-artifact"},
        )
        session.flush()

        with pytest.raises(analysis_input.AnalysisInputError):
            analysis_input.resolve_analysis_input(session, job, action)


def test_pinned_source_removed_before_use_fails_closed(in_memory_session_scope, monkeypatch, tmp_path):
    """A cleared live pin is never silently replaced by the job's newest dump."""
    scope = in_memory_session_scope
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", scope)

    job_id = _completed_job(scope, tmp_path)
    artifact_a, _dump_a = _capture_memory_artifact(scope, tmp_path, job_id, name="memory-a.lime", body=b"dump-A-bytes")

    with scope() as session:
        queued = forensic_services.enqueue_vol3_plugin_run_service(session, job_id, PLUGIN)
    action_id = queued["action_id"]

    _upload_second_dump(monkeypatch, scope, tmp_path, job_id, b"dump-B-bytes-which-is-longer")
    with scope() as session:
        # Simulate the live reference being cleared (SET NULL) while the snapshot stays.
        action = JobRepository(session).get_action(action_id)
        assert action is not None
        action.source_artifact_id = None
        session.flush()

    spawned: list[list[str]] = []
    monkeypatch.setattr(runner.subprocess, "run", _rows_child(spawned))

    success, result = runner.run_vol3_plugin(job_id, action_id=action_id)

    assert success is False
    assert spawned == []
    assert artifact_a in result["error"]
    with scope() as session:
        assert [
            a for a in ArtifactRepository(session).list_artifacts(job_id=job_id) if a.producer == "vol3_plugin_run"
        ] == []


@pytest.mark.parametrize("replacement", [b"dump-A-rewritten", b"dump-B-bytes"], ids=["size", "hash"])
def test_pinned_source_bytes_that_changed_fail_closed(in_memory_session_scope, monkeypatch, tmp_path, replacement):
    """Recorded identity is re-proved against the bytes on disk at use time."""
    scope = in_memory_session_scope
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", scope)

    job_id = _completed_job(scope, tmp_path)
    _artifact_a, dump_a = _capture_memory_artifact(scope, tmp_path, job_id, name="memory-a.lime", body=b"dump-A-bytes")

    with scope() as session:
        queued = forensic_services.enqueue_vol3_plugin_run_service(session, job_id, PLUGIN)

    dump_a.write_bytes(replacement)

    spawned: list[list[str]] = []
    monkeypatch.setattr(runner.subprocess, "run", _rows_child(spawned))

    success, result = runner.run_vol3_plugin(job_id, action_id=queued["action_id"])

    assert success is False
    assert spawned == []
    assert "mismatch" in result["error"]


def test_unrelated_action_types_are_not_pinned(in_memory_session_scope):
    """Non-analysis callers keep their existing behaviour: no source, no snapshot."""
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
            )
        )
        action = repo.enqueue_action(job.id, "capture_memory")
        session.flush()
        assert action.source_artifact_id is None
        assert analysis_input.SOURCE_SNAPSHOT_KEY not in json.loads(action.payload or "{}")
        assert session.get(Artifact, "missing") is None
