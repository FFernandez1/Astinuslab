"""Upgrade old action rows without guessing provenance; enforce the live pin FK."""

import importlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


def test_source_pin_upgrade_preserves_legacy_payload_and_nulls_deleted_reference(monkeypatch):
    migration = importlib.import_module("astinuslab.migrations.versions.a043_host_action_source_artifact")
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.exec_driver_sql("CREATE TABLE artifacts(id VARCHAR(64) PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE host_actions(id INTEGER PRIMARY KEY, payload TEXT)")
        connection.exec_driver_sql('INSERT INTO host_actions VALUES (1, \'{"plugin":"legacy"}\')')
        connection.exec_driver_sql("INSERT INTO artifacts VALUES ('dump-A')")
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        assert connection.execute(text("SELECT payload,source_artifact_id FROM host_actions")).one() == (
            '{"plugin":"legacy"}',
            None,
        )
        assert inspect(connection).get_indexes("host_actions")[0]["column_names"] == ["source_artifact_id"]
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql("UPDATE host_actions SET source_artifact_id='missing'")
        connection.exec_driver_sql("UPDATE host_actions SET source_artifact_id='dump-A'")
        connection.exec_driver_sql("DELETE FROM artifacts WHERE id='dump-A'")
        assert connection.exec_driver_sql("SELECT source_artifact_id FROM host_actions").scalar() is None
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        migration.downgrade()
        assert {column["name"] for column in inspect(connection).get_columns("host_actions")} == {"id", "payload"}
        assert connection.exec_driver_sql("SELECT payload FROM host_actions").scalar() == '{"plugin":"legacy"}'
