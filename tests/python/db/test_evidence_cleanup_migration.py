"""Upgrade/downgrade must preserve custody and repair legacy dangling refs."""

import sqlite3

from tests.python._support.helpers import ALEMBIC_UPGRADE_SCRIPT as _UPGRADE_TO
from tests.python._support.helpers import run_with_test_database as _run_with_db


def test_cleanup_intents_upgrade_legacy_repair_and_recoverable_downgrade(tmp_path):
    db = tmp_path / "cleanup.db"
    result = _run_with_db(db, "-c", _UPGRADE_TO.format(target="a039_playground_artifact_ownership"))
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO evidence_tombstones (artifact_id, path, file_set, actor, reason, forced, created_at) VALUES ('removed', '/missing', '[]', 'legacy', 'cleanup', 0, datetime('now'))"
        )
        connection.execute(
            "INSERT INTO reports (id, job_id, report_type, status, path, created_at) VALUES ('dangling-job', 'missing-job', 'analysis', 'completed', '/report', datetime('now'))"
        )
        connection.execute(
            "INSERT INTO reports (id, input_artifact_id, output_artifact_id, report_type, status, path, created_at) VALUES ('survivor', 'missing', 'missing', 'analysis', 'completed', '/report', datetime('now'))"
        )
        connection.execute(
            "INSERT INTO artifact_publication_attempts (id, artifact_id, producer, kind, staging_dir, final_path, state, created_at, updated_at) VALUES ('attempt', 'missing', 'job_capture', 'memory', '/stage', '/final', 'completed', datetime('now'), datetime('now'))"
        )
    result = _run_with_db(db, "-m", "astinuslab.db", "init")
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT cleanup_state FROM evidence_tombstones").fetchall() == [("legacy",)]
        assert connection.execute("SELECT id, input_artifact_id, output_artifact_id FROM reports").fetchall() == [
            ("survivor", None, None)
        ]
        assert connection.execute("SELECT artifact_id FROM artifact_publication_attempts").fetchall() == [(None,)]
        connection.execute("UPDATE evidence_tombstones SET cleanup_state = 'pending', cleanup_metadata = '{}'")
    down_script = _UPGRADE_TO.replace("command.upgrade", "command.downgrade").format(
        target="a039_playground_artifact_ownership"
    )
    refused = _run_with_db(db, "-c", down_script)
    assert refused.returncode != 0
    assert "pending or failed evidence cleanup intents" in refused.stderr
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "a040_evidence_cleanup_intents",
        )
        assert connection.execute("SELECT cleanup_state FROM evidence_tombstones").fetchone() == ("pending",)
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name IN ('admin_browser_sessions', 'ix_jobs_acquisition_id')"
            ).fetchall()
            == []
        )
    recovered = _run_with_db(
        db,
        "-c",
        "from astinuslab.db import SessionLocal; from astinuslab.storage.deletion import cleanup_deletions\nwith SessionLocal() as session: cleanup_deletions(session, roots=())",
    )
    assert recovered.returncode == 0, recovered.stderr
    down = _run_with_db(db, "-c", down_script)
    assert down.returncode == 0, down.stderr
    with sqlite3.connect(db) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(evidence_tombstones)")}
        assert "cleanup_state" not in columns
        assert connection.execute("SELECT artifact_id FROM evidence_tombstones").fetchall() == [("removed",)]
    upgraded = _run_with_db(db, "-m", "astinuslab.db", "init")
    assert upgraded.returncode == 0, upgraded.stderr
