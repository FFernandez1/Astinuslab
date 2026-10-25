"""Tests for the inline SQLite schema migration in db.py."""

from __future__ import annotations

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import StaticPool


def _make_engine():
    return create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )


def test_migrate_schema_adds_column_with_escaped_string_default(monkeypatch):
    """_migrate_schema must add string defaults that contain single quotes."""
    import astinuslab.db as db

    engine = _make_engine()
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(
        db,
        "BASELINE_SCHEMA",
        {
            "migration_test": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "nullable": False, "default": None, "primary_key": True},
                    {
                        "name": "label",
                        "type": "VARCHAR",
                        "nullable": True,
                        "default": "it's a default",
                        "primary_key": False,
                    },
                ],
                "indexes": [],
                "index_sql": [],
            }
        },
    )

    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE migration_test (id INTEGER PRIMARY KEY)"))
        conn.execute(text("INSERT INTO migration_test (id) VALUES (1)"))

    db._migrate_schema()

    with engine.connect() as conn:
        row = conn.execute(text("SELECT label FROM migration_test WHERE id=1")).fetchone()
    assert row == ("it's a default",)


def test_migrate_schema_adds_column_with_integer_default(monkeypatch):
    """_migrate_schema must add integer defaults without quoting them as strings."""
    import astinuslab.db as db

    engine = _make_engine()
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(
        db,
        "BASELINE_SCHEMA",
        {
            "migration_test2": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "nullable": False, "default": None, "primary_key": True},
                    {"name": "count", "type": "INTEGER", "nullable": True, "default": 42, "primary_key": False},
                ],
                "indexes": [],
                "index_sql": [],
            }
        },
    )

    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE migration_test2 (id INTEGER PRIMARY KEY)"))
        conn.execute(text("INSERT INTO migration_test2 (id) VALUES (1)"))

    db._migrate_schema()

    with engine.connect() as conn:
        row = conn.execute(text("SELECT count FROM migration_test2 WHERE id=1")).fetchone()
    assert row == (42,)


def test_migrate_schema_removes_dropped_tables_and_columns(monkeypatch):
    import astinuslab.db as db

    engine = _make_engine()
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "_DROPPED_TABLES", ["legacy_table"])
    monkeypatch.setattr(db, "_DROPPED_COLUMNS", {"legacy_col_table": ["old_col"]})
    monkeypatch.setattr(
        db,
        "BASELINE_SCHEMA",
        {
            "legacy_col_table": {
                "columns": [{"name": "id", "type": "INTEGER", "nullable": False, "default": None, "primary_key": True}],
                "indexes": [],
                "index_sql": [],
            }
        },
    )

    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE legacy_table (id INTEGER PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE legacy_col_table (id INTEGER PRIMARY KEY, old_col INTEGER)"))

    issues = "\n".join(db.schema_issues())
    assert "legacy table present: legacy_table" in issues
    assert "legacy column present: legacy_col_table.old_col" in issues

    db._migrate_schema()

    issues = "\n".join(db.schema_issues())
    assert "legacy table present: legacy_table" not in issues
    assert "legacy column present: legacy_col_table.old_col" not in issues
    assert "legacy_table" not in inspect(engine).get_table_names()
    assert {col["name"] for col in inspect(engine).get_columns("legacy_col_table")} == {"id"}


def test_migrate_schema_adds_isf_artifact_build_id(monkeypatch):
    """Old DB without build_id column gets the column added idempotently."""
    import astinuslab.db as db
    import astinuslab.db.models  # noqa: F401 - registers ORM tables with Base.metadata

    engine = _make_engine()
    old_engine = db.engine

    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE jobs (id VARCHAR(64) PRIMARY KEY)"))
        conn.execute(
            text(
                """
                CREATE TABLE isf_artifacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id VARCHAR(64) NOT NULL,
                    kernel_release VARCHAR(255) NOT NULL,
                    distro VARCHAR(64),
                    version VARCHAR(64),
                    architecture VARCHAR(16) NOT NULL DEFAULT 'x86_64',
                    path TEXT NOT NULL,
                    sha256 VARCHAR(64),
                    source VARCHAR(32) NOT NULL,
                    validation_status VARCHAR(32) NOT NULL DEFAULT 'valid',
                    created_at DATETIME NOT NULL
                )
                """
            )
        )

    monkeypatch.setattr(db, "engine", engine)
    try:
        db._migrate_schema()
        db._migrate_schema()
    finally:
        monkeypatch.setattr(db, "engine", old_engine)

    inspector = inspect(engine)
    cols = {col["name"] for col in inspector.get_columns("isf_artifacts")}

    assert "build_id" in cols


def test_migrate_schema_uses_frozen_snapshot_not_live_metadata():
    import inspect

    import astinuslab.db as db
    import astinuslab.db.baseline_snapshot as snapshot

    source = inspect.getsource(db._migrate_schema)
    assert "BASELINE_SCHEMA" in source
    assert "Base.metadata" not in source
    assert snapshot.BASELINE_REVISION == "ef55cafe2cea"


def test_migrate_data_splits_playground_sessions_and_backfills_artifacts(tmp_path):
    """Upgrade smoke test: an old-schema DB (playground sessions stored as jobs
    rows, dump metadata denormalized on jobs) is migrated in a subprocess
    running initialize_database(). Playground rows must land in
    playground_sessions, dump columns must be backfilled into artifacts, and
    the legacy columns/index must be gone.
    """
    import os
    import sqlite3
    import subprocess
    import sys

    db_path = tmp_path / "old-schema.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE jobs (
            id VARCHAR(64) PRIMARY KEY,
            target_id VARCHAR(128) NOT NULL,
            scenario_id VARCHAR(128) NOT NULL,
            execution_mode VARCHAR(64) NOT NULL,
            status VARCHAR(64) NOT NULL,
            capture_type VARCHAR(32) NOT NULL DEFAULT 'memory',
            dump_path TEXT, dump_filename VARCHAR(255),
            dump_checksum VARCHAR(128), dump_size_bytes INTEGER,
            disk_dump_path TEXT, disk_dump_filename VARCHAR(255),
            disk_dump_checksum VARCHAR(128), disk_dump_size_bytes INTEGER,
            error_summary TEXT,
            created_at DATETIME NOT NULL, started_at DATETIME, finished_at DATETIME
        );
        CREATE UNIQUE INDEX ux_jobs_one_active_playground_session ON jobs (execution_mode)
            WHERE execution_mode = 'playground' AND status IN ('pending', 'queued', 'running');
        INSERT INTO jobs (id, target_id, scenario_id, execution_mode, status,
                          dump_path, dump_filename, dump_checksum, dump_size_bytes,
                          disk_dump_path, disk_dump_filename, disk_dump_checksum, disk_dump_size_bytes,
                          created_at, finished_at)
            VALUES ('job-1', 'ubuntu-22-04-default', 'immediate-capture', 'immediate', 'completed',
                    '/storage/dumps/job-1/memory.dump', 'memory.dump', 'aa11', 1024,
                    '/storage/dumps/job-1/job-1-disk.img.gz', 'job-1-disk.img.gz', 'bb22', 2048,
                    '2026-01-01 00:00:00', '2026-01-01 01:00:00');
        INSERT INTO jobs (id, target_id, scenario_id, execution_mode, status, created_at, started_at)
            VALUES ('pg-active', '__playground__', '__playground__', 'playground', 'running',
                    '2026-01-02 00:00:00', '2026-01-02 00:00:01');
        INSERT INTO jobs (id, target_id, scenario_id, execution_mode, status, error_summary,
                          created_at, started_at, finished_at)
            VALUES ('pg-old', '__playground__', '__playground__', 'playground', 'failed', 'boom',
                    '2026-01-01 00:00:00', '2026-01-01 00:00:01', '2026-01-01 02:00:00');
        """
    )
    con.commit()
    con.close()

    env = os.environ.copy()
    env["ASTINUSLAB_DB_PATH"] = str(db_path)
    for _ in range(2):  # init twice: the migration must be idempotent
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from astinuslab.db import initialize_database; initialize_database()",
            ],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        assert result.returncode == 0, result.stderr

    con = sqlite3.connect(db_path)
    try:
        sessions = dict(con.execute("SELECT id, status FROM playground_sessions ORDER BY id").fetchall())
        assert sessions == {"pg-active": "running", "pg-old": "failed"}
        assert con.execute("SELECT error_summary FROM playground_sessions WHERE id='pg-old'").fetchone() == ("boom",)

        # jobs table no longer holds playground rows
        assert con.execute("SELECT id FROM jobs").fetchall() == [("job-1",)]

        # legacy dump columns backfilled into artifacts, then dropped
        artifacts = {
            kind: (path, filename, sha256, size_bytes)
            for kind, path, filename, sha256, size_bytes in con.execute(
                "SELECT kind, path, filename, sha256, size_bytes FROM artifacts WHERE job_id='job-1'"
            )
        }
        assert artifacts["memory"] == ("/storage/dumps/job-1/memory.dump", "memory.dump", "aa11", 1024)
        assert artifacts["disk"] == (
            "/storage/dumps/job-1/job-1-disk.img.gz",
            "job-1-disk.img.gz",
            "bb22",
            2048,
        )
        job_columns = {row[1] for row in con.execute("PRAGMA table_info(jobs)")}
        assert not any(
            col.endswith(("dump_path", "dump_filename", "dump_checksum", "dump_size_bytes")) for col in job_columns
        )

        # one-active invariant moved to playground_sessions
        index_rows = {
            row[0]: row[1] for row in con.execute("SELECT name, tbl_name FROM sqlite_master WHERE type='index'")
        }
        assert "ux_jobs_one_active_playground_session" not in index_rows
        assert index_rows.get("ux_playground_sessions_one_active") == "playground_sessions"

        # the legacy bootstrap ends by stamping the Alembic baseline, so the
        # second initialize_database() run above took the upgrade-only path
        version = con.execute("SELECT version_num FROM alembic_version").fetchone()
        assert version is not None and version[0]
    finally:
        con.close()
