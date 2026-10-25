from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import Column, Integer, Table, create_engine, inspect, text
from sqlalchemy.pool import StaticPool

from astinuslab.db import Base, _migrate_schema, main, schema_issues, session_scope

# --- DB Tests ---


def test_sqlite_connection_enables_foreign_key_enforcement(tmp_path):
    from astinuslab.db import set_sqlite_pragma

    database = tmp_path / "foreign-keys.db"
    engine = create_engine(f"sqlite:///{database}", future=True)
    from sqlalchemy import event

    event.listen(engine, "connect", set_sqlite_pragma)
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1


def test_session_scope_exception():
    with pytest.raises(RuntimeError):
        with session_scope() as session:  # noqa: F841
            raise RuntimeError("test error")


def test_migrate_schema_and_issues(monkeypatch):
    test_engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, future=True)
    monkeypatch.setattr("astinuslab.db.engine", test_engine)

    with test_engine.begin() as conn:
        conn.execute(text("CREATE TABLE legacy_table (id INTEGER PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE legacy_col_table (id INTEGER PRIMARY KEY, old_col INTEGER)"))

    monkeypatch.setattr("astinuslab.db._DROPPED_TABLES", ["legacy_table"])
    monkeypatch.setattr("astinuslab.db._DROPPED_COLUMNS", {"legacy_col_table": ["old_col"]})

    issues = schema_issues()
    assert any("legacy table present: legacy_table" in i for i in issues)
    assert any("legacy column present: legacy_col_table.old_col" in i for i in issues)

    _migrate_schema()

    issues = schema_issues()
    assert not any("legacy table present: legacy_table" in i for i in issues)
    assert not any("legacy column present: legacy_col_table.old_col" in i for i in issues)


def test_migrate_schema_add_column(monkeypatch):
    test_engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, future=True)
    monkeypatch.setattr("astinuslab.db.engine", test_engine)
    monkeypatch.setattr(
        "astinuslab.db.BASELINE_SCHEMA",
        {
            "test_add_col": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "nullable": False, "default": None, "primary_key": True},
                    {
                        "name": "new_col",
                        "type": "VARCHAR",
                        "nullable": True,
                        "default": "default_val",
                        "primary_key": False,
                    },
                ],
                "indexes": [],
                "index_sql": [],
            }
        },
    )

    with test_engine.begin() as conn:
        conn.execute(text("CREATE TABLE test_add_col (id INTEGER PRIMARY KEY)"))

    _migrate_schema()

    columns = {col["name"] for col in inspect(test_engine).get_columns("test_add_col")}
    assert "new_col" in columns


def test_migrate_schema_add_column_complex_default(monkeypatch):
    test_engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, future=True)
    monkeypatch.setattr("astinuslab.db.engine", test_engine)
    monkeypatch.setattr(
        "astinuslab.db.BASELINE_SCHEMA",
        {
            "test_complex_default": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "nullable": False, "default": None, "primary_key": True},
                    {
                        "name": "str_col",
                        "type": "VARCHAR",
                        "nullable": True,
                        "default": "it's a default",
                        "primary_key": False,
                    },
                    {"name": "int_col", "type": "INTEGER", "nullable": True, "default": 42, "primary_key": False},
                ],
                "indexes": [],
                "index_sql": [],
            }
        },
    )

    with test_engine.begin() as conn:
        conn.execute(text("CREATE TABLE test_complex_default (id INTEGER PRIMARY KEY)"))
        conn.execute(text("INSERT INTO test_complex_default (id) VALUES (1)"))

    _migrate_schema()

    with test_engine.connect() as conn:
        row = conn.execute(text("SELECT str_col, int_col FROM test_complex_default WHERE id=1")).fetchone()
    assert row == ("it's a default", 42)


def test_db_main(monkeypatch, tmp_path):
    db_file = tmp_path / "test.db"
    with patch("astinuslab.db.settings") as mock_settings:
        mock_settings.db_path = db_file
        assert main(["init"]) == 0
        assert main(["status"]) == 0


def test_db_module_cli_registers_model_metadata(tmp_path):
    db_file = tmp_path / "module-cli.db"
    result = subprocess.run(
        [sys.executable, "-m", "astinuslab.db", "init"],
        cwd=Path(__file__).resolve().parents[3],
        env={**os.environ, "ASTINUSLAB_DB_PATH": str(db_file)},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    with sqlite3.connect(db_file) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"jobs", "playground_sessions"} <= tables


def test_schema_issues_missing_table(monkeypatch):
    test_engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, future=True)
    monkeypatch.setattr("astinuslab.db.engine", test_engine)

    class MissingTable(Base):
        __tablename__ = "missing_table"
        id = Column(Integer, primary_key=True)

    mock_metadata = MagicMock()
    mock_metadata.sorted_tables = [MissingTable.__table__]

    with patch("astinuslab.db.Base.metadata", mock_metadata):
        issues = schema_issues()
        assert any("missing table: missing_table" in i for i in issues)

    # Declarative model registration is process-global; remove this synthetic
    # table so metadata-identity contracts remain order-independent.
    # `__table__` is declared as FromClause on the declarative base; for a mapped
    # class it is always a Table, and remove() accepts nothing else.
    synthetic_table = MissingTable.__table__
    assert isinstance(synthetic_table, Table)
    Base.metadata.remove(synthetic_table)


def test_size_bytes_columns_are_bigint():
    from sqlalchemy import BigInteger

    from astinuslab.db.models import Acquisition, Artifact

    assert isinstance(Artifact.__table__.c.size_bytes.type, BigInteger)
    assert isinstance(Acquisition.__table__.c.size_bytes.type, BigInteger)


def test_job_vm_resource_columns_are_nullable_integers():
    from sqlalchemy import Integer

    from astinuslab.db.models import JobVM

    for name in ("vcpus", "memory_mb", "disk_gb"):
        column = JobVM.__table__.c[name]
        assert isinstance(column.type, Integer)
        assert column.nullable is True
