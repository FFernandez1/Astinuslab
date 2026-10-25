"""Real disposable upgrades join both histories without stamping over either."""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, Table, create_engine, event, inspect, text
from sqlalchemy.engine import Connection, Engine

MERGED_HEAD = "a038_merge_packages_capacity"
PACKAGE_HEAD = "a037_nic_detach_claim"
CAPACITY_HEAD = "a026_jobs_capacity"
MIGRATIONS = Path(__file__).resolve().parents[3] / "src/astinuslab/migrations"
CREATED = datetime(2026, 9, 6, 12, 0, 0)


def _config() -> Config:
    return Config(str(MIGRATIONS / "alembic.ini"))


def _upgrade(engine: Engine, revision: str) -> None:
    with engine.connect() as connection:
        cfg = _config()
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, revision)
        connection.commit()


def _insert(connection: Connection, table: str, **values: Any) -> None:
    reflected = Table(table, MetaData(), autoload_with=connection)
    connection.execute(reflected.insert().values(**values))


def _seed(connection: Connection, parent: str, dump: Path) -> list[str]:
    _insert(
        connection,
        "jobs",
        id="keep-job",
        target_id="fixture",
        scenario_id="fixture",
        execution_mode="manual_trigger",
        status="waiting_for_capture",
        capture_type="memory",
        created_at=CREATED,
    )
    _insert(
        connection,
        "host_actions",
        id=41,
        job_id="keep-job",
        action_type="run_vol3_plugin",
        status="running",
        payload='{"sentinel":"action"}',
        created_at=CREATED,
    )
    _insert(
        connection,
        "artifacts",
        id="keep-artifact",
        job_id="keep-job",
        producer="fixture",
        kind="memory",
        path=str(dump),
        sha256=hashlib.sha256(dump.read_bytes()).hexdigest(),
        created_at=CREATED,
    )
    _insert(
        connection,
        "reports",
        id="keep-report",
        job_id="keep-job",
        input_artifact_id="keep-artifact",
        report_type="vol3_plugin_run",
        status="completed",
        path="fixture-result.json",
        created_at=CREATED,
    )
    _insert(connection, "playground_sessions", id="keep-session", status="destroyed", created_at=CREATED)
    _insert(
        connection,
        "job_vms",
        id="keep-vm",
        session_id="keep-session",
        label="target",
        image_id="fixture",
        state="destroyed",
        role="target",
        domain_name="fixture-domain",
        disk_path="fixture-disk",
        created_at=CREATED,
        updated_at=CREATED,
    )
    _insert(
        connection,
        "job_networks",
        id="keep-network",
        session_id="keep-session",
        name="fixture",
        subnet_cidr="10.0.0.0/24",
        isolated=True,
        libvirt_net_name="fixture-network",
        state="destroyed",
        created_at=CREATED,
        updated_at=CREATED,
    )
    _insert(
        connection,
        "job_vm_nics",
        id="keep-nic",
        job_vm_id="keep-vm",
        job_network_id="keep-network",
        mac="52:54:00:00:00:01",
        state="active",
        created_at=CREATED,
        **({"ordinal": 4} if parent == PACKAGE_HEAD else {}),
    )
    tables = [
        "jobs",
        "host_actions",
        "artifacts",
        "reports",
        "playground_sessions",
        "job_vms",
        "job_networks",
        "job_vm_nics",
    ]
    if parent == PACKAGE_HEAD:
        _insert(
            connection,
            "lab_packages",
            id="keep-package",
            package_id="fixture.package",
            version="1",
            title="Merge sentinel",
            creator="fixture",
            license="fixture",
            reproducibility="locked",
            archive_path="fixture-package.zip",
            archive_sha256="a" * 64,
            archive_size_bytes=2**33,
            manifest_sha256="b" * 64,
            manifest_json='{"sentinel":"manifest"}',
            definition_json='{"sentinel":"definition"}',
            imported_at=CREATED,
        )
        _insert(
            connection,
            "lab_instances",
            id="keep-instance",
            package_record_id="keep-package",
            playground_session_id="keep-session",
            idempotency_key="sentinel",
            state="destroyed",
            created_at=CREATED,
            updated_at=CREATED,
        )
        connection.execute(text("UPDATE playground_sessions SET lab_package_record_id='keep-package'"))
        connection.execute(
            text("UPDATE job_vm_nics SET detach_token='keep-claim', detach_claimed_at=:at, ordinal=4"),
            {"at": CREATED.isoformat(" ")},
        )
        tables += ["lab_packages", "lab_instances"]
    elif parent == CAPACITY_HEAD:
        connection.execute(text("UPDATE jobs SET capacity_reason='keep capacity reason'"))
        connection.execute(
            text(
                "UPDATE host_actions SET owner_token='keep-owner', dispatch_attempts=3, celery_task_id='keep-task', lease_expires_at=:at"
            ),
            {"at": CREATED.isoformat(" ")},
        )
        _insert(
            connection,
            "capacity_reservations",
            job_id="keep-job",
            vm_count=3,
            memory_mb=4096,
            storage_bytes=2**35,
            state="active",
            preserve_failed_vm=True,
            boot_started=True,
            created_at=CREATED,
        )
        tables.append("capacity_reservations")
    return tables


def _rows(connection: Connection, table: str) -> list[dict[str, Any]]:
    reflected = Table(table, MetaData(), autoload_with=connection)
    return [dict(row) for row in connection.execute(reflected.select()).mappings()]


def test_migrations_have_one_head_with_both_original_parents() -> None:
    scripts = ScriptDirectory.from_config(_config())
    assert scripts.get_heads() == ["a044_prepare_execution_token"]
    merge = scripts.get_revision(MERGED_HEAD)
    assert merge is not None
    assert merge.down_revision == (PACKAGE_HEAD, CAPACITY_HEAD)


@pytest.mark.parametrize("parent", ["base", PACKAGE_HEAD, CAPACITY_HEAD])
def test_merge_upgrade_preserves_each_parent_schema_and_sentinels(tmp_path: Path, parent: str) -> None:
    from astinuslab.db import (
        Base,
        models,  # noqa: F401
    )

    engine = create_engine(f"sqlite:///{tmp_path / 'upgrade.db'}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _record):
        # Match the application: configure the driver before SQLAlchemy starts
        # a transaction, leaving Alembic its per-revision transaction boundaries.
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    dump = tmp_path / "sentinel.dump"
    dump.write_bytes(b"synthetic merge evidence sentinel")
    before: dict[str, list[dict[str, Any]]] = {}
    try:
        if parent != "base":
            _upgrade(engine, parent)
            with engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                tables = set(inspect(connection).get_table_names())
                assert ("lab_packages" in tables) == (parent == PACKAGE_HEAD)
                assert ("capacity_reservations" in tables) == (parent == CAPACITY_HEAD)
                seeded_tables = _seed(connection, parent, dump)
                before = {table: _rows(connection, table) for table in seeded_tables}
                connection.commit()
        else:
            assert inspect(engine).get_table_names() == []

        for _ in range(2):
            _upgrade(engine, "head")
            with engine.connect() as connection:
                # A SQLAlchemy Row compares equal to a plain tuple at runtime;
                # mypy models the two as non-overlapping.
                assert connection.execute(text("SELECT version_num FROM alembic_version")).all() == [  # type: ignore[comparison-overlap]
                    ("a044_prepare_execution_token",)
                ]
                schema = inspect(connection)
                assert set(schema.get_table_names()) == set(Base.metadata.tables) | {"alembic_version"}
                for name, model_table in Base.metadata.tables.items():
                    assert {column["name"] for column in schema.get_columns(name)} == set(model_table.columns.keys()), (
                        name
                    )
                for table, expected in before.items():
                    after = [{key: row[key] for key in expected[0]} for row in _rows(connection, table)]
                    assert after == expected, table
                assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
                assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
                indexes = set(connection.execute(text("SELECT name FROM sqlite_master WHERE type='index'")).scalars())
                assert {
                    "ix_capacity_reservations_state",
                    "ux_lab_packages_package_id_version",
                    "ux_job_vm_nics_vm_ordinal",
                    "ux_playground_sessions_one_active",
                } <= indexes
        assert dump.read_bytes() == b"synthetic merge evidence sentinel"
    finally:
        engine.dispose()
