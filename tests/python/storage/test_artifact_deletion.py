"""Task 22 — one custody-aware deletion path for every caller.

The Dump Vault used to unlink a file and drop the Artifact rows with no checks
at all, while retention already refused to touch evidence enrolled in an
Acquisition or an open Case. Two rules for the same bytes, and the weaker one
was reachable from a button in the UI.

These tests describe the single service both go through: what it refuses, what
it records before it destroys anything, and what it leaves behind when a step
fails.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from astinuslab.db import models
from astinuslab.storage import deletion
from tests.python._support.helpers import create_evidence_triad as _evidence
from tests.python._support.helpers import create_terminal_job as _terminal_job


@pytest.mark.parametrize(
    ("status", "force", "deleted"),
    [("queued", False, False), ("running", False, False), ("completed", False, True), ("queued", True, True)],
)
def test_analysis_pin_protects_input_until_terminal_or_audited_override(
    in_memory_session_scope, tmp_path, status, force, deleted
):
    from astinuslab.db.repositories import JobRepository
    from astinuslab.forensics.analysis_input import SOURCE_SNAPSHOT_KEY, SourceSnapshot

    with in_memory_session_scope() as session:
        _terminal_job(session)
        artifact, _paths = _evidence(session, tmp_path, job_id="job-1")
        snapshot = SourceSnapshot.of(artifact).to_payload()
        action = JobRepository(session).enqueue_action(
            "job-1", "run_vol3_plugin", payload={SOURCE_SNAPSHOT_KEY: snapshot}, source_artifact_id=artifact.id
        )
        action.status = status
        session.flush()
        outcome = deletion.delete_artifact(
            session, artifact, actor="test", reason="test pin policy", roots=(tmp_path / "dumps",), force=force
        )
        assert outcome.deleted is deleted
        if deleted:
            session.flush()
            session.refresh(action)
            assert action.source_artifact_id is None
            assert action.payload is not None
            assert json.loads(action.payload)[SOURCE_SNAPSHOT_KEY] == snapshot
            assert session.get(models.EvidenceTombstone, outcome.tombstone_id).forced is force
        else:
            assert outcome.protection_reason is not None
            assert "queued or running analysis" in outcome.protection_reason
            assert session.get(models.Artifact, artifact.id) is not None


def test_deletes_the_whole_triad_and_records_a_tombstone(in_memory_session_scope, tmp_path):
    with in_memory_session_scope() as session:
        _terminal_job(session)
        artifact, paths = _evidence(session, tmp_path, job_id="job-1")

        outcome = deletion.delete_artifact(
            session,
            artifact,
            actor="ui:dumps",
            reason="operator requested",
            roots=(tmp_path / "dumps",),
        )

        assert outcome.deleted is True
        session.commit()
        assert outcome.tombstone_id is not None, "accepted disposal must identify its custody record"
        outcome = deletion.cleanup_deletions(session, [outcome.tombstone_id], roots=(tmp_path / "dumps",))[0]
        assert set(outcome.removed_paths) == {str(p) for p in paths}
        assert all(not p.exists() for p in paths)
        assert session.get(models.Artifact, artifact.id) is None

        tombstone = session.query(models.EvidenceTombstone).one()
        assert tombstone.artifact_id == artifact.id
        assert tombstone.sha256 == "abc"
        assert tombstone.actor == "ui:dumps"
        assert tombstone.reason == "operator requested"
        assert tombstone.forced is False
        assert sorted(json.loads(tombstone.file_set)) == sorted(str(p) for p in paths)


def test_refuses_evidence_enrolled_in_an_acquisition(in_memory_session_scope, tmp_path):
    with in_memory_session_scope() as session:
        _terminal_job(session)
        session.add(models.Acquisition(id="acq-1", name="capture", status="sealed"))
        session.flush()
        artifact, paths = _evidence(session, tmp_path, job_id="job-1", acquisition_id="acq-1")

        outcome = deletion.delete_artifact(
            session, artifact, actor="ui:dumps", reason="cleanup", roots=(tmp_path / "dumps",)
        )

        assert outcome.deleted is False
        assert "acquisition" in (outcome.protection_reason or "")
        assert all(p.exists() for p in paths)
        assert session.get(models.Artifact, artifact.id) is not None
        assert session.query(models.EvidenceTombstone).count() == 0


def test_refuses_evidence_in_an_open_case(in_memory_session_scope, tmp_path):
    with in_memory_session_scope() as session:
        _terminal_job(session)
        session.add(models.Case(id="case-1", name="investigation", status="open"))
        session.flush()
        artifact, paths = _evidence(session, tmp_path, job_id="job-1", case_id="case-1")

        outcome = deletion.delete_artifact(
            session, artifact, actor="ui:dumps", reason="cleanup", roots=(tmp_path / "dumps",)
        )

        assert outcome.deleted is False
        assert "case" in (outcome.protection_reason or "")
        assert all(p.exists() for p in paths)


def test_refuses_evidence_of_a_running_job(in_memory_session_scope, tmp_path):
    with in_memory_session_scope() as session:
        _terminal_job(session, status="acquiring_memory")
        artifact, paths = _evidence(session, tmp_path, job_id="job-1")

        outcome = deletion.delete_artifact(
            session, artifact, actor="ui:dumps", reason="cleanup", roots=(tmp_path / "dumps",)
        )

        assert outcome.deleted is False
        assert "not terminal" in (outcome.protection_reason or "")
        assert all(p.exists() for p in paths)


def test_force_deletes_protected_evidence_and_says_so_in_the_tombstone(in_memory_session_scope, tmp_path):
    with in_memory_session_scope() as session:
        _terminal_job(session)
        session.add(models.Acquisition(id="acq-1", name="capture", status="sealed"))
        session.flush()
        artifact, paths = _evidence(session, tmp_path, job_id="job-1", acquisition_id="acq-1")

        outcome = deletion.delete_artifact(
            session,
            artifact,
            actor="api:admin",
            reason="legal instruction 41",
            force=True,
            roots=(tmp_path / "dumps",),
        )

        assert outcome.deleted is True
        session.commit()
        assert outcome.tombstone_id is not None, "accepted disposal must identify its custody record"
        deletion.cleanup_deletions(session, [outcome.tombstone_id], roots=(tmp_path / "dumps",))
        assert all(not p.exists() for p in paths)
        tombstone = session.query(models.EvidenceTombstone).one()
        assert tombstone.forced is True
        assert "acquisition" in (tombstone.protection_override or "")


def test_refuses_a_path_outside_the_allowed_roots(in_memory_session_scope, tmp_path):
    with in_memory_session_scope() as session:
        _terminal_job(session)
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        stray = outside / "memory.dump"
        stray.write_bytes(b"not ours")
        artifact = models.Artifact(
            id="art-stray", kind="memory", producer="job_capture", path=str(stray), job_id="job-1"
        )
        session.add(artifact)
        session.flush()

        outcome = deletion.delete_artifact(
            session, artifact, actor="ui:dumps", reason="cleanup", roots=(tmp_path / "dumps",)
        )

        assert outcome.deleted is False
        assert "root" in (outcome.protection_reason or "").lower()
        assert stray.exists()


def test_refuses_a_symlink_standing_in_for_the_artifact(in_memory_session_scope, tmp_path):
    """A symlink at the recorded path would delete whatever it points at."""
    with in_memory_session_scope() as session:
        _terminal_job(session)
        dumps = tmp_path / "dumps"
        dumps.mkdir(parents=True, exist_ok=True)
        real = tmp_path / "precious.dump"
        real.write_bytes(b"someone else's evidence")
        link = dumps / "memory.dump"
        link.symlink_to(real)
        artifact = models.Artifact(id="art-link", kind="memory", producer="job_capture", path=str(link), job_id="job-1")
        session.add(artifact)
        session.flush()

        outcome = deletion.delete_artifact(session, artifact, actor="ui:dumps", reason="cleanup", roots=(dumps,))

        assert outcome.deleted is False
        assert "symlink" in (outcome.protection_reason or "").lower()
        assert real.exists(), "the symlink target must never be touched"


def test_refuses_a_sidecar_outside_the_artifact_directory(in_memory_session_scope, tmp_path):
    with in_memory_session_scope() as session:
        _terminal_job(session)
        artifact, paths = _evidence(session, tmp_path, job_id="job-1")
        artifact.manifest_path = str(tmp_path / "elsewhere.manifest.json")
        session.flush()

        outcome = deletion.delete_artifact(
            session, artifact, actor="ui:dumps", reason="cleanup", roots=(tmp_path / "dumps",)
        )

        assert outcome.deleted is False
        assert "sidecar" in (outcome.protection_reason or "").lower()
        assert paths[0].exists()


def test_a_missing_artifact_file_still_clears_the_row(in_memory_session_scope, tmp_path):
    """A row pointing at bytes that are already gone is the misleading state; the
    deletion still records what it found."""
    with in_memory_session_scope() as session:
        _terminal_job(session)
        artifact, paths = _evidence(session, tmp_path, job_id="job-1")
        paths[0].unlink()

        outcome = deletion.delete_artifact(
            session, artifact, actor="retention", reason="expired", roots=(tmp_path / "dumps",)
        )

        assert outcome.deleted is True
        assert session.get(models.Artifact, artifact.id) is None
        tombstone = session.query(models.EvidenceTombstone).one()
        assert tombstone.notes and "missing" in tombstone.notes.lower()


def test_deleting_twice_is_a_no_op(in_memory_session_scope, tmp_path):
    with in_memory_session_scope() as session:
        _terminal_job(session)
        artifact, _ = _evidence(session, tmp_path, job_id="job-1")
        first = deletion.delete_artifact(
            session, artifact, actor="ui:dumps", reason="cleanup", roots=(tmp_path / "dumps",)
        )
        second = deletion.delete_artifact(
            session, artifact, actor="ui:dumps", reason="cleanup", roots=(tmp_path / "dumps",)
        )

        assert first.deleted is True
        assert second.deleted is False
        assert session.query(models.EvidenceTombstone).count() == 1


def test_an_unlink_failure_is_reported_and_the_tombstone_survives(in_memory_session_scope, tmp_path, monkeypatch):
    with in_memory_session_scope() as session:
        _terminal_job(session)
        artifact, paths = _evidence(session, tmp_path, job_id="job-1")

        real_unlink = Path.unlink

        def refuse_sidecar(self, *args, **kwargs):
            if self.name.endswith(".sha256"):
                raise OSError("device busy")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", refuse_sidecar)

        outcome = deletion.delete_artifact(
            session, artifact, actor="ui:dumps", reason="cleanup", roots=(tmp_path / "dumps",)
        )

        session.commit()
        assert outcome.tombstone_id is not None, "accepted disposal must identify its custody record"
        outcome = deletion.cleanup_deletions(session, [outcome.tombstone_id], roots=(tmp_path / "dumps",))[0]
        assert outcome.errors, "a failed unlink must be reported, not swallowed"
        tombstone = session.query(models.EvidenceTombstone).one()
        assert tombstone.notes and "device busy" in tombstone.notes


def test_a_job_event_records_the_deletion(in_memory_session_scope, tmp_path):
    with in_memory_session_scope() as session:
        _terminal_job(session)
        artifact, _ = _evidence(session, tmp_path, job_id="job-1")

        deletion.delete_artifact(
            session, artifact, actor="ui:dumps", reason="operator requested", roots=(tmp_path / "dumps",)
        )

        events = session.query(models.JobEvent).filter_by(job_id="job-1").all()
        assert any("deleted" in event.state for event in events)
        assert any("operator requested" in event.message for event in events)


def test_protection_reason_is_shared_with_retention(in_memory_session_scope, tmp_path):
    """Retention and the UI must agree on what is protected; one function decides."""
    with in_memory_session_scope() as session:
        _terminal_job(session)
        session.add(models.Acquisition(id="acq-1", name="capture", status="sealed"))
        session.flush()
        artifact, _ = _evidence(session, tmp_path, job_id="job-1", acquisition_id="acq-1")

        assert deletion.protection_reason(session, artifact) is not None


@pytest.mark.parametrize("status", ["closed"])
def test_a_closed_case_does_not_protect(in_memory_session_scope, tmp_path, status):
    with in_memory_session_scope() as session:
        _terminal_job(session)
        session.add(models.Case(id="case-1", name="investigation", status=status))
        session.flush()
        artifact, paths = _evidence(session, tmp_path, job_id="job-1", case_id="case-1")

        outcome = deletion.delete_artifact(
            session, artifact, actor="retention", reason="expired", roots=(tmp_path / "dumps",)
        )

        assert outcome.deleted is True
        session.commit()
        assert outcome.tombstone_id is not None, "accepted disposal must identify its custody record"
        deletion.cleanup_deletions(session, [outcome.tombstone_id], roots=(tmp_path / "dumps",))
        assert all(not p.exists() for p in paths)


def test_custody_disposal_commits_before_a_process_can_interrupt_unlink(tmp_path):
    """A child exits after its first unlink; an independent DB must see disposal."""
    import sqlite3
    import subprocess
    import sys

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine(f"sqlite:///{tmp_path / 'custody.db'}")
    models.Base.metadata.create_all(engine)
    with Session(engine) as session:
        artifact, paths = _evidence(session, tmp_path, name="report.json")
        session.add(
            models.Report(
                id="report-record",
                report_type="volatility3",
                status="completed",
                path=artifact.path,
                output_artifact_id=artifact.id,
            )
        )
        session.commit()
    script = """
import os, sys
from pathlib import Path
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from astinuslab.db.models import Artifact
from astinuslab.storage.deletion import cleanup_deletions, delete_artifact
engine = create_engine('sqlite:///' + sys.argv[1])
original = Path.unlink
def interrupted_unlink(path, *args, **kwargs):
    original(path, *args, **kwargs)
    os._exit(73)
Path.unlink = interrupted_unlink
with Session(engine) as session:
    artifact = session.scalars(select(Artifact)).one()
    outcome = delete_artifact(session, artifact, actor='fixture:operator', reason='explicit disposal', roots=[Path(sys.argv[2])])
    session.commit()
    assert outcome.tombstone_id is not None, "accepted disposal must identify its custody record"
    cleanup_deletions(session, [outcome.tombstone_id], roots=[Path(sys.argv[2])])
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "custody.db"), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert child.returncode == 73, child.stderr
    assert not paths[0].exists()
    assert paths[1].exists()  # interrupted cleanup is recoverable from the tombstone
    with sqlite3.connect(tmp_path / "custody.db") as connection:
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
        assert connection.execute("SELECT status, output_artifact_id FROM reports").fetchone() == ("deleted", None)
        row = connection.execute("SELECT actor, reason, file_set FROM evidence_tombstones").fetchone()
        assert row[:2] == ("fixture:operator", "explicit disposal")
        assert set(json.loads(row[2])) == {str(path) for path in paths}


def test_failed_custody_commit_never_unlinks_evidence(in_memory_session_scope, tmp_path, monkeypatch):
    with in_memory_session_scope() as session:
        artifact, paths = _evidence(session, tmp_path)
        session.commit()

        def refuse_commit():
            raise RuntimeError("fixture database commit failure")

        with monkeypatch.context() as patch:
            patch.setattr(session, "commit", refuse_commit)
            with pytest.raises(RuntimeError, match="database commit failure"):
                deletion.delete_artifact(session, artifact, actor="test", reason="explicit", roots=[tmp_path])
                session.commit()
        session.rollback()
        assert all(path.exists() for path in paths)
        assert session.query(models.Artifact).count() == 1
        assert session.query(models.EvidenceTombstone).count() == 0
