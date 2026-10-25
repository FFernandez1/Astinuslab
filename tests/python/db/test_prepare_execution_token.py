"""The additive Prepare authority migration preserves existing durable intent."""

import sqlite3

from tests.python._support.helpers import ALEMBIC_UPGRADE_SCRIPT, run_with_test_database


def test_prepare_token_upgrade_preserves_legacy_rows_and_round_trips(tmp_path):
    database = tmp_path / "prepare-migration.db"
    result = run_with_test_database(
        database, "-c", ALEMBIC_UPGRADE_SCRIPT.format(target="a043_host_action_source_artifact")
    )
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """INSERT INTO lab_packages
            (id, package_id, version, title, creator, license, reproducibility,
             archive_path, archive_sha256, archive_size_bytes, manifest_sha256, manifest_json,
             definition_json, imported_at)
            VALUES ('lp-migration', 'synthetic', '1', 'Fixture', 'Test', 'CC0', 'unlocked',
                    '/synthetic/archive', ?, 1, ?, '{}', '{}', '2026-01-01')""",
            ("a" * 64, "b" * 64),
        )
        for state in ("reserved", "running", "failed", "completed", "cancelled"):
            connection.execute(
                """INSERT INTO lab_prepare_attempts
                (id, package_record_id, definition_sha256, state, idempotency_key,
                 result_json, created_at, updated_at)
                VALUES (?, 'lp-migration', ?, ?, ?, '{"assets":{}}', '2026-01-01', '2026-01-01')""",
                (f"lpa-{state}", "c" * 64, state, f"legacy-{state}"),
            )
        before = connection.execute("SELECT * FROM lab_prepare_attempts ORDER BY id").fetchall()
        indexes = connection.execute("PRAGMA index_list(lab_prepare_attempts)").fetchall()
    result = run_with_test_database(database, "-m", "astinuslab.db", "init")
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database) as connection:
        after = connection.execute("SELECT * FROM lab_prepare_attempts ORDER BY id").fetchall()
        assert [row[:-1] for row in after] == before
        assert all(row[-1] is None for row in after)
        assert connection.execute("PRAGMA index_list(lab_prepare_attempts)").fetchall() == indexes
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        connection.execute("UPDATE lab_prepare_attempts SET execution_token = ? WHERE state = 'running'", ("d" * 32,))
    result = run_with_test_database(
        database,
        "-c",
        ALEMBIC_UPGRADE_SCRIPT.replace("command.upgrade", "command.downgrade").format(
            target="a043_host_action_source_artifact"
        ),
    )
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM lab_prepare_attempts ORDER BY id").fetchall() == before
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
