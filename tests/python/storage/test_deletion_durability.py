"""Deletion intent must survive before any evidence bytes can disappear."""

import json
from pathlib import Path

import pytest
from sqlalchemy import event

from astinuslab.api.errors import ApiError
from astinuslab.db import models
from astinuslab.db.repositories import JobRepository
from astinuslab.jobs.job_services import delete_job_service
from astinuslab.storage import deletion
from tests.python._support.helpers import create_evidence_triad as _evidence
from tests.python._support.helpers import create_terminal_job as _terminal_job


def test_rollback_preserves_bytes_and_metadata(in_memory_session_scope, tmp_path):
    with in_memory_session_scope() as session:
        artifact, paths = _evidence(session, tmp_path)
        artifact_id = artifact.id
        session.commit()
        deletion.delete_artifact(session, artifact, actor="test", reason="rollback", roots=(tmp_path / "dumps",))
        session.rollback()
        assert all(path.exists() for path in paths)
        assert session.get(models.Artifact, artifact_id) is not None
        assert session.query(models.EvidenceTombstone).count() == 0


def test_failed_commit_never_unlinks(in_memory_session_scope, tmp_path):
    with in_memory_session_scope() as session:
        artifact, paths = _evidence(session, tmp_path)
        session.commit()
        deletion.delete_artifact(session, artifact, actor="test", reason="failed commit", roots=(tmp_path / "dumps",))

        def fail_commit(session):
            raise RuntimeError("commit failed")

        event.listen(session, "before_commit", fail_commit)
        with pytest.raises(RuntimeError, match="commit failed"):
            session.commit()
        event.remove(session, "before_commit", fail_commit)
        session.rollback()
        assert all(path.exists() for path in paths)
        assert session.query(models.EvidenceTombstone).count() == 0


@pytest.mark.parametrize("delete_files", [False, True])
@pytest.mark.parametrize("protection", ["open", "sealed", "acquisition"])
def test_job_deletion_refuses_every_protected_artifact(
    in_memory_session_scope, tmp_path, monkeypatch, delete_files, protection
):
    with in_memory_session_scope() as session:
        _terminal_job(session)
        _, first_paths = _evidence(session, tmp_path, job_id="job-1", name="first.dump")
        artifact, paths = _evidence(session, tmp_path, job_id="job-1", name="protected.dump")
        if protection == "acquisition":
            session.add(models.Acquisition(id="acq", name="case capture", status="sealed"))
            artifact.acquisition_id = "acq"
        else:
            session.add(models.Case(id="case", name="custody", status=protection))
            artifact.case_id = "case"
        session.commit()
        monkeypatch.setattr(deletion, "evidence_roots", lambda settings: (tmp_path / "dumps",))
        with pytest.raises(ApiError) as exc:
            delete_job_service(session, "job-1", delete_files=delete_files)
        assert exc.value.status_code == 409
        assert session.query(models.Artifact).count() == 2
        assert session.query(models.EvidenceTombstone).count() == 0
        assert all(path.exists() for path in (*first_paths, *paths))


def test_repository_cannot_bypass_custody(in_memory_session_scope, tmp_path):
    with in_memory_session_scope() as session:
        _terminal_job(session)
        session.add(models.Case(id="case", name="custody", status="sealed"))
        _evidence(session, tmp_path, job_id="job-1", case_id="case")
        session.commit()
        assert JobRepository(session).delete_job("job-1") is False
        assert session.query(models.Artifact).count() == 1


def test_individual_deletion_nulls_surviving_references(in_memory_session_scope, tmp_path):
    with in_memory_session_scope() as session:
        artifact, _ = _evidence(session, tmp_path)
        report = models.Report(
            id="report",
            report_type="analysis",
            path="report.html",
            input_artifact_id=artifact.id,
            output_artifact_id=artifact.id,
        )
        attempt = models.ArtifactPublicationAttempt(
            id="attempt",
            artifact_id=artifact.id,
            producer="job_capture",
            kind="memory",
            staging_dir="stage",
            final_path=artifact.path,
            state="completed",
        )
        session.add_all([report, attempt])
        session.commit()
        deletion.delete_artifact(session, artifact, actor="test", reason="cleanup", roots=(tmp_path / "dumps",))
        session.flush()
        session.expire_all()
        assert report.input_artifact_id is None
        assert report.output_artifact_id is None
        assert attempt.artifact_id is None


def test_job_delete_retains_all_evidence_and_custody_metadata(in_memory_session_scope, tmp_path):
    with in_memory_session_scope() as session:
        _terminal_job(session)
        artifacts = [
            _evidence(session, tmp_path, job_id="job-1", name=name)
            for name in ("old.dump", "new.dump", "analysis.json")
        ]
        session.add(models.Report(id="report", job_id="job-1", report_type="analysis", path="report.html"))
        session.commit()
        for delete_files in (False, True):
            with pytest.raises(ApiError) as exc:
                delete_job_service(session, "job-1", delete_files=delete_files)
            assert exc.value.status_code == 409
        assert session.query(models.Report).count() == 1
        assert session.query(models.Artifact).count() == 3
        assert session.query(models.EvidenceTombstone).count() == 0
        assert all(path.exists() for _, paths in artifacts for path in paths)


def test_explicit_metadata_only_disposal_retains_and_audits_every_file(in_memory_session_scope, tmp_path):
    with in_memory_session_scope() as session:
        artifacts = [_evidence(session, tmp_path, name=name) for name in ("old.dump", "new.dump")]
        session.commit()
        for artifact, _ in artifacts:
            assert deletion.delete_artifact(
                session,
                artifact,
                actor="test",
                reason="metadata disposal",
                roots=(),
                delete_files=False,
            ).deleted
        session.commit()
        assert deletion.cleanup_deletions(session, roots=(tmp_path / "dumps",)) == []
        tombstones = session.query(models.EvidenceTombstone).all()
        assert len(tombstones) == 2
        assert {t.cleanup_state for t in tombstones} == {"retained"}
        assert all(path.exists() for _, paths in artifacts for path in paths)
        assert {p for t in tombstones for p in json.loads(t.file_set)} == {
            str(p) for _, paths in artifacts for p in paths
        }


def test_cleanup_requires_completed_caller_transaction(in_memory_session_scope, tmp_path):
    with in_memory_session_scope() as session:
        artifact, paths = _evidence(session, tmp_path)
        session.commit()
        session.add(models.Case(id="unrelated", name="not committed", status="open"))
        deletion.delete_artifact(session, artifact, actor="test", reason="cleanup", roots=(tmp_path / "dumps",))
        with pytest.raises(RuntimeError, match="commit or roll back"):
            deletion.cleanup_deletions(session, roots=(tmp_path / "dumps",))
        session.rollback()
        assert session.get(models.Case, "unrelated") is None
        assert all(path.exists() for path in paths)


def test_cleanup_failure_and_retry_preserve_committed_intent(in_memory_session_scope, tmp_path, monkeypatch):
    with in_memory_session_scope() as session:
        artifact, paths = _evidence(session, tmp_path)
        outcome = deletion.delete_artifact(
            session, artifact, actor="test", reason="cleanup", roots=(tmp_path / "dumps",)
        )
        session.commit()
        unlink = Path.unlink

        def fail_one(path, *args, **kwargs):
            if path == paths[2]:
                raise OSError("device busy")
            return unlink(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "unlink", fail_one)
            assert outcome.tombstone_id is not None, "accepted disposal must identify its custody record"
            result = deletion.cleanup_deletions(session, [outcome.tombstone_id], roots=(tmp_path / "dumps",))[0]
        assert result.errors
        tombstone = session.get(models.EvidenceTombstone, outcome.tombstone_id)
        assert tombstone.cleanup_state == "failed"
        assert "device busy" in tombstone.notes
        session.rollback()
        deletion.cleanup_deletions(session, roots=(tmp_path / "dumps",))
        assert all(not path.exists() for path in paths)
        assert session.get(models.EvidenceTombstone, outcome.tombstone_id).cleanup_state == "complete"
        session.rollback()
        assert deletion.cleanup_deletions(session, roots=(tmp_path / "dumps",)) == []


def test_recovery_never_unlinks_replacement_files(in_memory_session_scope, tmp_path):
    with in_memory_session_scope() as session:
        artifact, paths = _evidence(session, tmp_path)
        outcome = deletion.delete_artifact(
            session, artifact, actor="test", reason="cleanup", roots=(tmp_path / "dumps",)
        )
        session.commit()
        replacement = paths[0].with_name("replacement")
        replacement.write_bytes(b"new evidence")
        replacement.replace(paths[0])
        result = deletion.cleanup_deletions(session, roots=(tmp_path / "dumps",))[0]
        assert any("changed" in error for error in result.errors)
        assert paths[0].read_bytes() == b"new evidence"
        assert session.get(models.EvidenceTombstone, outcome.tombstone_id).cleanup_state == "failed"


def test_every_unlink_observes_durable_tombstone_from_another_connection(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from astinuslab.db import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'custody.sqlite'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        artifact, paths = _evidence(session, tmp_path)
        session.commit()
        outcome = deletion.delete_artifact(
            session, artifact, actor="test", reason="cleanup", roots=(tmp_path / "dumps",)
        )
        session.commit()
        unlink = Path.unlink
        observed = []

        def verify_then_unlink(path, *args, **kwargs):
            with engine.connect() as connection:
                assert connection.scalar(select(models.EvidenceTombstone.id)) == outcome.tombstone_id
                assert connection.scalar(select(models.Artifact.id)) is None
            observed.append(path)
            return unlink(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "unlink", verify_then_unlink)
            deletion.cleanup_deletions(session, roots=(tmp_path / "dumps",))
        assert set(observed) == set(paths)
    engine.dispose()


def test_later_artifact_exception_preserves_whole_batch(in_memory_session_scope, tmp_path, monkeypatch):
    with in_memory_session_scope() as session:
        _terminal_job(session)
        artifacts = [_evidence(session, tmp_path, job_id="job-1", name=name) for name in ("one.dump", "two.dump")]
        session.commit()
        delete = deletion.delete_artifact
        calls = []

        def fail_second(*args, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                raise RuntimeError("later artifact failed")
            return delete(*args, **kwargs)

        monkeypatch.setattr(deletion, "delete_artifact", fail_second)
        with pytest.raises(RuntimeError, match="later artifact failed"):
            for artifact, _ in artifacts:
                deletion.delete_artifact(
                    session, artifact, actor="test", reason="batch disposal", roots=(tmp_path / "dumps",)
                )
        session.rollback()
        assert all(path.exists() for _, paths in artifacts for path in paths)
        assert session.query(models.Artifact).count() == 2
        assert session.query(models.EvidenceTombstone).count() == 0


def test_explicit_cleanup_inventories_every_artifact_and_sidecar(in_memory_session_scope, tmp_path):
    with in_memory_session_scope() as session:
        _terminal_job(session)
        artifacts = [
            _evidence(session, tmp_path, job_id="job-1", name=name) for name in ("old.dump", "new.dump", "report.json")
        ]
        session.commit()
        ids = []
        for artifact, _ in artifacts:
            outcome = deletion.delete_artifact(
                session, artifact, actor="test", reason="batch disposal", roots=(tmp_path / "dumps",)
            )
            assert outcome.deleted
            assert outcome.tombstone_id is not None, "every batch item must identify its custody record"
            ids.append(outcome.tombstone_id)
        assert all(path.exists() for _, paths in artifacts for path in paths)
        session.commit()
        deletion.cleanup_deletions(session, ids, roots=(tmp_path / "dumps",))
        assert all(not path.exists() for _, paths in artifacts for path in paths)
        assert len(ids) == 3


def test_orphan_directory_deletion_cannot_bypass_protection(in_memory_session_scope, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from astinuslab.ui import artifact_routes

    (tmp_path / "storage" / "dumps" / "orphan").mkdir(parents=True)
    with in_memory_session_scope() as session:
        session.add(models.Case(id="case", name="custody", status="sealed"))
        _, paths = _evidence(session, tmp_path / "storage", case_id="case", name="orphan/memory.dump")
    monkeypatch.setattr(artifact_routes, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(
        artifact_routes, "settings", SimpleNamespace(base_dir=tmp_path, storage_dir=tmp_path / "storage")
    )
    assert artifact_routes._remove_orphan_dir("orphan") is False
    assert all(path.exists() for path in paths)


def test_orphan_directory_deletion_records_every_file(in_memory_session_scope, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from astinuslab.ui import artifact_routes

    orphan = tmp_path / "storage" / "dumps" / "orphan"
    orphan.mkdir(parents=True)
    paths = [orphan / "memory.dump", orphan / "manifest.json"]
    for path in paths:
        path.write_bytes(b"untracked evidence")
    monkeypatch.setattr(artifact_routes, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(
        artifact_routes, "settings", SimpleNamespace(base_dir=tmp_path, storage_dir=tmp_path / "storage")
    )
    assert artifact_routes._remove_orphan_dir("orphan") is True
    assert not orphan.exists()
    with in_memory_session_scope() as session:
        tombstones = session.query(models.EvidenceTombstone).all()
        assert len(tombstones) == 2
        assert {t.cleanup_state for t in tombstones} == {"complete"}


def test_cleanup_outcome_commit_failure_is_recoverable(in_memory_session_scope, tmp_path):
    from sqlalchemy.orm import Session

    with in_memory_session_scope() as owner:
        artifact, paths = _evidence(owner, tmp_path)
        outcome = deletion.delete_artifact(owner, artifact, actor="test", reason="cleanup", roots=(tmp_path / "dumps",))
        owner.commit()

        def fail_outcome_commit(session):
            if session is not owner:
                raise RuntimeError("outcome commit failed")

        event.listen(Session, "before_commit", fail_outcome_commit)
        try:
            with pytest.raises(RuntimeError, match="outcome commit failed"):
                deletion.cleanup_deletions(owner, roots=(tmp_path / "dumps",))
        finally:
            event.remove(Session, "before_commit", fail_outcome_commit)
        assert all(not path.exists() for path in paths)
        assert owner.get(models.EvidenceTombstone, outcome.tombstone_id).cleanup_state == "pending"
        owner.rollback()
        deletion.cleanup_deletions(owner, roots=(tmp_path / "dumps",))
        assert owner.get(models.EvidenceTombstone, outcome.tombstone_id).cleanup_state == "complete"


def test_deletion_refreshes_custody_loaded_before_write_reservation(tmp_path):
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from astinuslab.db import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'stale.sqlite'}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        case = models.Case(id="case", name="custody", status="closed")
        session.add(case)
        artifact, paths = _evidence(session, tmp_path, case_id="case")
        session.commit()
        assert deletion.protection_reason(session, artifact) is None
        with engine.begin() as connection:
            connection.execute(text("UPDATE cases SET status = 'sealed' WHERE id = 'case'"))
        result = deletion.delete_artifact(
            session, artifact, actor="test", reason="cleanup", roots=(tmp_path / "dumps",)
        )
        assert result.deleted is False
        assert result.protection_reason is not None, "custody refusal must explain its protection"
        assert "sealed" in result.protection_reason
        assert all(path.exists() for path in paths)
    engine.dispose()


def test_custody_check_holds_sqlite_write_reservation(tmp_path, monkeypatch):
    import sqlite3

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from astinuslab.db import Base

    db_path = tmp_path / "reservation.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        artifact, _ = _evidence(session, tmp_path)
        session.commit()
        protection_reason = deletion.protection_reason
        checked = []

        def verify_reservation(*args, **kwargs):
            with sqlite3.connect(db_path, timeout=0.01) as other:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    other.execute("BEGIN IMMEDIATE")
            checked.append(True)
            return protection_reason(*args, **kwargs)

        monkeypatch.setattr(deletion, "protection_reason", verify_reservation)
        assert deletion.delete_artifact(
            session, artifact, actor="test", reason="cleanup", roots=(tmp_path / "dumps",)
        ).deleted
        assert checked
        session.rollback()
    engine.dispose()


def test_cleanup_preserves_files_reenrolled_as_live_evidence(in_memory_session_scope, tmp_path):
    with in_memory_session_scope() as session:
        artifact, paths = _evidence(session, tmp_path)
        deletion.delete_artifact(session, artifact, actor="test", reason="cleanup", roots=(tmp_path / "dumps",))
        session.commit()
        session.add(models.Case(id="case", name="custody", status="sealed"))
        session.add(
            models.Artifact(
                id="reenrolled",
                case_id="case",
                path=str(paths[0]),
                kind="memory",
                producer="import",
                manifest_path=str(paths[1]),
                sha256_sidecar_path=str(paths[2]),
            )
        )
        session.commit()
        outcome = deletion.cleanup_deletions(session, roots=(tmp_path / "dumps",))[0]
        assert outcome.errors
        assert all(path.exists() for path in paths)


def test_package_custody_guards_survive_durable_cleanup_merge(in_memory_session_scope, tmp_path):
    with in_memory_session_scope() as session:
        session.add(models.PlaygroundSession(id="pg-custody", status="destroyed"))
        artifact, paths = _evidence(session, tmp_path)
        artifact.session_id = "pg-custody"
        artifact.ownership_reconciliation_json = '{"historical_owner":"unknown"}'
        session.commit()
        denied = deletion.delete_artifact(
            session, artifact, actor="test", reason="cleanup", roots=(tmp_path / "dumps",)
        )
        assert denied.protection_reason is not None, "custody refusal must explain its protection"
        assert not denied.deleted and "reconciliation" in denied.protection_reason
        artifact.ownership_reconciliation_json = None
        analysis = models.Job(
            id="analysis",
            target_id="fixture",
            scenario_id="fixture",
            execution_mode="artifact_analysis",
            status="completed",
            source_artifact_id=artifact.id,
        )
        session.add(analysis)
        session.flush()
        action = models.HostAction(job_id=analysis.id, action_type="run_vol3_plugin", status="queued")
        session.add(action)
        session.commit()
        denied = deletion.delete_artifact(
            session, artifact, actor="test", reason="cleanup", roots=(tmp_path / "dumps",)
        )
        assert denied.protection_reason is not None, "custody refusal must explain its protection"
        assert not denied.deleted and "analysis" in denied.protection_reason
        assert all(path.exists() for path in paths)
        assert session.query(models.EvidenceTombstone).count() == 0


def test_playground_disposal_rolls_back_report_and_analysis_context_together(in_memory_session_scope, tmp_path):
    with in_memory_session_scope() as session:
        session.add(models.PlaygroundSession(id="pg-custody", status="destroyed"))
        artifact, paths = _evidence(session, tmp_path)
        artifact.session_id = "pg-custody"
        analysis = models.Job(
            id="analysis",
            target_id="fixture",
            scenario_id="fixture",
            execution_mode="artifact_analysis",
            status="completed",
            source_artifact_id=artifact.id,
        )
        report = models.Report(
            id="report",
            report_type="volatility3",
            path=str(paths[0]),
            status="completed",
            input_artifact_id=artifact.id,
            output_artifact_id=artifact.id,
        )
        session.add_all([analysis, report])
        session.commit()
        outcome = deletion.delete_artifact(
            session, artifact, actor="test", reason="cleanup", roots=(tmp_path / "dumps",)
        )
        assert outcome.deleted
        assert analysis.source_artifact_id is None
        assert report.status == "deleted"
        assert report.input_artifact_id is None and report.output_artifact_id is None
        assert session.get(models.EvidenceTombstone, outcome.tombstone_id).session_id == "pg-custody"
        session.rollback()
        assert analysis.source_artifact_id == artifact.id
        assert report.status == "completed" and report.output_artifact_id == artifact.id
        assert all(path.exists() for path in paths)
        outcome = deletion.delete_artifact(
            session, artifact, actor="test", reason="cleanup", roots=(tmp_path / "dumps",)
        )
        session.commit()
        assert outcome.tombstone_id is not None, "accepted disposal must identify its custody record"
        deletion.cleanup_deletions(session, [outcome.tombstone_id], roots=(tmp_path / "dumps",))
        assert all(not path.exists() for path in paths)
        assert report.status == "deleted" and analysis.source_artifact_id is None
