"""Regression evidence uses real SQLite/publication and fixture tool output."""

import hashlib
import json
from pathlib import Path

import pytest

from astinuslab.db.models import Acquisition, Artifact, ArtifactPublicationAttempt, EvidenceTombstone, Report
from astinuslab.db.repositories import ArtifactRepository, JobRepository
from tests.python._support.vol3_fixtures import completed_child, enqueue_pinned_analysis_action
from tests.python._support.vol3_fixtures import make_completed_batch_job as _make_completed_job
from tests.python._support.vol3_fixtures import make_plugin_job as _make_job_with_action
from tests.python._support.vol3_fixtures import patch_batch_settings as _patch_settings
from tests.python._support.vol3_fixtures import patch_plugin_settings as plugin_settings


def _bytes_below(root):
    return {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_history_clear_preserves_protected_report_and_explicit_delete_is_audited(
    in_memory_session_scope, monkeypatch, tmp_path
):
    from astinuslab.forensics import api
    from astinuslab.storage.deletion import delete_artifact

    runner = plugin_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(api, "settings", runner.settings)
    job_id, action_id = _make_job_with_action(
        in_memory_session_scope, tmp_path, plugin="linux.pslist.PsList", run_id="retained"
    )
    monkeypatch.setattr(
        runner.subprocess, "run", lambda *a, **k: completed_child(a[0], k, returncode=0, stdout="[]", stderr="")
    )
    assert runner.run_vol3_plugin(job_id, action_id)[0]
    with in_memory_session_scope() as session:
        artifact = next(a for a in ArtifactRepository(session).list_artifacts(job_id=job_id) if a.kind == "report")
        session.add(Acquisition(id="protected", name="case evidence", status="sealed"))
        session.flush()
        artifact.acquisition_id = "protected"
        artifact_id = artifact.id
    before = _bytes_below(tmp_path / "storage" / "dumps")
    assert api.volatility_api_queue_clear() == {"deleted": 1}
    assert api.volatility_api_queue()["runs"] == []
    assert all(p.read_bytes() == contents for p, contents in before.items())
    assert api.volatility_api_queue_clear() == {"deleted": 0}
    with in_memory_session_scope() as session:
        artifact = session.get(Artifact, artifact_id)
        denied = delete_artifact(
            session, artifact, actor="test:operator", reason="explicit selection", roots=[tmp_path]
        )
        assert not denied.deleted
        assert not session.query(EvidenceTombstone).count()
        assert all(p.read_bytes() == contents for p, contents in before.items())
        outcome = delete_artifact(
            session, artifact, actor="test:operator", reason="approved disposal", roots=[tmp_path], force=True
        )
        assert outcome.deleted
        tombstone = session.query(EvidenceTombstone).one()
        assert tombstone.forced and tombstone.artifact_id == artifact_id
        report = session.query(Report).filter_by(job_id=job_id).one()
        assert report.status == "deleted"
        assert report.output_artifact_id is None


@pytest.mark.parametrize("second_outcome", ["success", "tool_failure", "publication_failure"])
def test_batch_reruns_keep_every_prior_byte_and_finalize_action(
    in_memory_session_scope, monkeypatch, tmp_path, second_outcome
):
    runner = _patch_settings(monkeypatch, tmp_path, plugins="linux.pslist.PsList")
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    job_id, action_id, _, isf = _make_completed_job(in_memory_session_scope, tmp_path)
    generation = 0

    def fake_run(cmd, **kwargs):
        kwargs["stdout"].write(f"PID\n{generation + 1}\n")
        symbols = Path(cmd[cmd.index("--symbol-dirs") + 1])
        Path(cmd[cmd.index("--save-config") + 1]).write_text(
            json.dumps({"kernel.isf_url": (symbols / "linux" / isf.name).as_uri()})
        )
        return completed_child(cmd, kwargs, returncode=3 if generation and second_outcome == "tool_failure" else 0)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    assert runner.run_vol3_analysis(job_id, action_id)[0]
    before = _bytes_below(tmp_path / "storage" / "dumps" / job_id / "vol3")
    with in_memory_session_scope() as session:
        action_id = enqueue_pinned_analysis_action(JobRepository(session), job_id, "run_vol3_analysis").id
    generation = 1
    if second_outcome == "publication_failure":

        def fail_publish(*args, **kwargs):
            raise OSError("fixture sidecar publication failure")

        monkeypatch.setattr(runner.publication.PublicationAttempt, "publish_sidecars", fail_publish)
    success, summary = runner.run_vol3_analysis(job_id, action_id)
    assert success == (second_outcome == "success")
    assert all(p.read_bytes() == contents for p, contents in before.items())
    with in_memory_session_scope() as session:
        action = JobRepository(session).get_action(action_id)
        assert action is not None
        assert action.status == ("completed" if success else "failed")
        assert action.finished_at is not None
        reports = session.query(Artifact).filter_by(job_id=job_id, kind="report").all()
        assert len(reports) == (2 if success else 1)
        for report in reports:
            assert hashlib.sha256(Path(report.path).read_bytes()).hexdigest() == report.sha256
        assert (
            not session.query(ArtifactPublicationAttempt)
            .filter(ArtifactPublicationAttempt.state.in_(["reserved", "staged", "published"]))
            .count()
        )


def test_batch_reserves_identity_before_tool_writes(in_memory_session_scope, monkeypatch, tmp_path):
    runner = _patch_settings(monkeypatch, tmp_path, plugins="linux.pslist.PsList")
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    job_id, action_id, _, _ = _make_completed_job(in_memory_session_scope, tmp_path)

    def inspect_reservation(cmd, **kwargs):
        with in_memory_session_scope() as session:
            attempts = session.query(ArtifactPublicationAttempt).filter_by(job_id=job_id, state="reserved").all()
            assert len(attempts) == 1
            assert Path(attempts[0].final_path).parent == Path(kwargs["stdout"].name).parent
        return completed_child(cmd, kwargs, returncode=7)

    monkeypatch.setattr(runner.subprocess, "run", inspect_reservation)
    success, result = runner.run_vol3_analysis(job_id, action_id)
    assert not success
    assert "exit code 7" in result["error"]
