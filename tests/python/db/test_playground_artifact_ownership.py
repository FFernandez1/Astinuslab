"""Additive migration proof against representative historical ownership, no payload edits."""

import importlib
import json

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

migration = importlib.import_module("astinuslab.migrations.versions.a039_playground_artifact_ownership")


def database():
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        for statement in [
            "CREATE TABLE jobs(id TEXT PRIMARY KEY, execution_mode TEXT)",
            "CREATE TABLE playground_sessions(id TEXT PRIMARY KEY)",
            "CREATE TABLE acquisitions(id TEXT PRIMARY KEY, session_id TEXT, job_vm_id TEXT, case_id TEXT)",
            "CREATE TABLE job_vms(id TEXT PRIMARY KEY, session_id TEXT)",
            "CREATE TABLE artifact_publication_attempts(artifact_id TEXT, session_id TEXT, job_id TEXT)",
            "CREATE TABLE evidence_tombstones(id INTEGER PRIMARY KEY)",
            "CREATE TABLE artifacts(id TEXT PRIMARY KEY, job_id TEXT, acquisition_id TEXT, case_id TEXT, producer TEXT, path TEXT, sha256 TEXT, metadata_json TEXT)",
            "CREATE TABLE reports(id TEXT PRIMARY KEY, input_artifact_id TEXT, metadata_json TEXT)",
            "INSERT INTO jobs VALUES ('job-old','immediate'), ('collision','immediate')",
            "INSERT INTO playground_sessions VALUES ('pg-good'),('pg-other'),('collision')",
            "INSERT INTO job_vms VALUES ('vm-good','pg-good')",
            "INSERT INTO acquisitions VALUES ('acq-good','pg-good','vm-good','case'),('acq-conflict','pg-other',NULL,NULL)",
        ]:
            c.execute(text(statement))
        rows = [
            ("old", "job-old", None, "job_capture"),
            ("good", "pg-good", "acq-good", "playground_capture"),
            ("collision", "collision", None, "playground_capture"),
            ("missing", "pg-missing", None, "playground_capture"),
            ("conflict", "pg-good", "acq-conflict", "playground_capture"),
            ("missing-acq", "pg-good", "gone", "playground_capture"),
            ("ownerless", None, None, "import"),
        ]
        for id, owner, acq, producer in rows:
            c.execute(
                text(
                    "INSERT INTO artifacts VALUES (:id,:owner,:acq,NULL,:producer,:path,'unchanged-sha','original metadata')"
                ),
                dict(id=id, owner=owner, acq=acq, producer=producer, path=f"retained/{id}"),
            )
        c.execute(text("INSERT INTO reports VALUES ('report','good','retained provenance')"))
    return engine


def test_migration_preserves_provenance_records_ambiguity_and_is_idempotent(monkeypatch):
    engine = database()
    with engine.begin() as c:
        before = c.execute(text("SELECT id,path,sha256,metadata_json FROM artifacts ORDER BY id")).all()
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(c)))
        migration.upgrade()
        assert c.execute(text("PRAGMA foreign_keys")).scalar() == 0
        rows = {r["id"]: r for r in c.execute(text("SELECT * FROM artifacts")).mappings()}
        assert rows["old"]["job_id"] == "job-old" and not rows["old"]["session_id"]
        assert rows["good"]["session_id"] == "pg-good" and rows["good"]["job_id"] is None
        assert rows["ownerless"]["ownership_reconciliation_json"] is None
        for id in ["collision", "missing", "conflict", "missing-acq"]:
            assert rows[id]["job_id"] is None and rows[id]["session_id"] is None
            assert json.loads(rows[id]["ownership_reconciliation_json"])["reasons"]
        snapshot = c.execute(text("SELECT * FROM artifacts ORDER BY id")).all()
        migration.reconcile(c)
        assert c.execute(text("SELECT * FROM artifacts ORDER BY id")).all() == snapshot
        assert c.execute(text("SELECT id,path,sha256,metadata_json FROM artifacts ORDER BY id")).all() == before
        assert c.execute(text("SELECT input_artifact_id,metadata_json FROM reports")).one() == (
            "good",
            "retained provenance",
        )
        assert any(fk["referred_table"] == "playground_sessions" for fk in inspect(c).get_foreign_keys("artifacts"))
        migration.downgrade()
        assert c.execute(text("SELECT job_id FROM artifacts WHERE id='good'")).scalar() == "pg-good"
        assert c.execute(text("SELECT job_id FROM artifacts WHERE id='missing'")).scalar() == "pg-missing"


def test_downgrade_refuses_discarding_analysis_context(monkeypatch):
    engine = database()
    with engine.begin() as c:
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(c)))
        migration.upgrade()
        c.execute(
            text(
                "INSERT INTO jobs(id,execution_mode,source_artifact_id) VALUES ('analysis','artifact_analysis','good')"
            )
        )
        with pytest.raises(RuntimeError, match="Cannot downgrade"):
            migration.downgrade()
        assert c.execute(text("SELECT source_artifact_id FROM jobs WHERE id='analysis'")).scalar() == "good"
