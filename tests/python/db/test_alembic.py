from __future__ import annotations

from pathlib import Path

import pytest

from tests.python._support.helpers import ALEMBIC_UPGRADE_SCRIPT as _UPGRADE_TO
from tests.python._support.helpers import run_with_test_database as _run_with_db


def test_inline_schema_migration_initializes_empty_db(tmp_path):
    db = tmp_path / "schema-smoke.db"

    init_result = _run_with_db(db, "-m", "astinuslab.db", "init")
    status_result = _run_with_db(db, "-m", "astinuslab.db", "status")

    assert init_result.returncode == 0, init_result.stderr
    assert status_result.returncode == 0, status_result.stderr
    assert db.exists()


def test_db_module_no_longer_exposes_alembic_stamp_hook():
    import astinuslab.db as db

    assert not hasattr(db, "stamp_if_unstamped")


def test_alembic_ini_removed_from_runtime_surface():
    assert not Path("alembic.ini").exists()


def _sqlite_names(db: Path, kind: str) -> set[str]:
    import sqlite3

    with sqlite3.connect(db) as connection:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))}


def test_publication_journal_migrates_onto_an_existing_database(tmp_path):
    """The journal has to arrive on a live operator database, not only a fresh one.

    A fresh install runs create_all from the models, so it would show the table
    even with no migration at all. This walks the path an upgrading operator
    takes: stop at the previous head, put a row in artifacts, then upgrade.
    """
    import sqlite3

    db = tmp_path / "upgrade.db"

    seeded = _run_with_db(db, "-c", _UPGRADE_TO.format(target="a019_size_bytes_bigint"))
    assert seeded.returncode == 0, seeded.stderr
    assert "artifact_publication_attempts" not in _sqlite_names(db, "table")

    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO artifacts (id, producer, kind, path, created_at) VALUES (?, ?, ?, ?, datetime('now'))",
            ("legacy-artifact", "job_capture", "memory_dump", "/srv/storage/dumps/legacy/memory.dump"),
        )

    upgraded = _run_with_db(db, "-m", "astinuslab.db", "init")
    assert upgraded.returncode == 0, upgraded.stderr

    assert "artifact_publication_attempts" in _sqlite_names(db, "table")
    assert "ux_artifact_publication_attempts_live_final_path" in _sqlite_names(db, "index")
    with sqlite3.connect(db) as connection:
        rows = connection.execute("SELECT id FROM artifacts").fetchall()
    assert rows == [("legacy-artifact",)], "existing artifacts must survive the upgrade"


def test_publication_journal_downgrade_removes_only_its_own_table(tmp_path):
    db = tmp_path / "downgrade.db"

    seeded = _run_with_db(db, "-m", "astinuslab.db", "init")
    assert seeded.returncode == 0, seeded.stderr
    assert "artifact_publication_attempts" in _sqlite_names(db, "table")

    # Pinned to a revision rather than "-1": relative targets silently retarget
    # every time a migration is added after this one.
    down = _run_with_db(
        db,
        "-c",
        _UPGRADE_TO.replace("command.upgrade", "command.downgrade").format(target="a019_size_bytes_bigint"),
    )
    assert down.returncode == 0, down.stderr

    tables = _sqlite_names(db, "table")
    assert "artifact_publication_attempts" not in tables
    assert "evidence_tombstones" not in tables
    assert "artifacts" in tables and "acquisitions" in tables


def test_content_publication_journal_round_trips_onto_an_existing_package_database(tmp_path):
    db = tmp_path / "content-publications.db"

    seeded = _run_with_db(db, "-c", _UPGRADE_TO.format(target="a030_package_verifications"))
    assert seeded.returncode == 0, seeded.stderr
    assert "lab_content_publication_attempts" not in _sqlite_names(db, "table")

    upgraded = _run_with_db(db, "-m", "astinuslab.db", "init")
    assert upgraded.returncode == 0, upgraded.stderr
    assert "lab_content_publication_attempts" in _sqlite_names(db, "table")
    assert "ux_lab_content_publications_digest_live" in _sqlite_names(db, "index")

    downgraded = _run_with_db(
        db,
        "-c",
        _UPGRADE_TO.replace("command.upgrade", "command.downgrade").format(target="a030_package_verifications"),
    )
    assert downgraded.returncode == 0, downgraded.stderr
    assert "lab_content_publication_attempts" not in _sqlite_names(db, "table")
    assert "lab_packages" in _sqlite_names(db, "table")


def test_package_content_identity_constraints_upgrade_and_downgrade(tmp_path):
    import sqlite3

    db = tmp_path / "package-content-invariants.db"
    seeded = _run_with_db(db, "-c", _UPGRADE_TO.format(target="a031_content_publication_attempts"))
    assert seeded.returncode == 0, seeded.stderr

    upgraded = _run_with_db(db, "-m", "astinuslab.db", "init")
    assert upgraded.returncode == 0, upgraded.stderr
    with sqlite3.connect(db) as connection:
        ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'lab_package_assets'"
        ).fetchone()[0]
    assert "ck_lab_package_assets_content_pair" in ddl
    assert "ck_lab_package_assets_content_matches_expected" in ddl

    downgraded = _run_with_db(
        db,
        "-c",
        _UPGRADE_TO.replace("command.upgrade", "command.downgrade").format(target="a031_content_publication_attempts"),
    )
    assert downgraded.returncode == 0, downgraded.stderr


def test_evidence_tombstones_migrate_onto_an_existing_database(tmp_path):
    """Deletions before this migration were never recorded; the table arrives empty."""
    db = tmp_path / "tombstones.db"

    seeded = _run_with_db(db, "-c", _UPGRADE_TO.format(target="a020_artifact_publication_attempts"))
    assert seeded.returncode == 0, seeded.stderr
    assert "evidence_tombstones" not in _sqlite_names(db, "table")

    upgraded = _run_with_db(db, "-m", "astinuslab.db", "init")
    assert upgraded.returncode == 0, upgraded.stderr

    assert "evidence_tombstones" in _sqlite_names(db, "table")
    assert "ix_evidence_tombstones_artifact_id" in _sqlite_names(db, "index")


def test_nic_ordinals_are_backfilled_deterministically_on_upgrade(tmp_path):
    """v1 exports must preserve guest interface order after upgrading a live DB."""
    import sqlite3

    db = tmp_path / "nic-ordinals.db"
    seeded = _run_with_db(db, "-c", _UPGRADE_TO.format(target="a026_lab_packages"))
    assert seeded.returncode == 0, seeded.stderr
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO playground_sessions (id, status, created_at) VALUES (?, ?, datetime('now'))",
            ("pg-nic-order", "running"),
        )
        connection.execute(
            """INSERT INTO job_vms
               (id, session_id, label, image_id, state, role, domain_name, disk_path, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("vm-nic-order", "pg-nic-order", "target", "ubuntu-24.04", "running", "target", "domain", "/disk"),
        )
        connection.executemany(
            """INSERT INTO job_networks
               (id, session_id, name, subnet_cidr, isolated, libvirt_net_name, state, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                ("net-z", "pg-nic-order", "zeta", "10.42.2.0/24", 1, "net-z", "active", "2026-01-01", "2026-01-01"),
                ("net-a", "pg-nic-order", "alpha", "10.42.1.0/24", 1, "net-a", "active", "2026-01-01", "2026-01-01"),
            ],
        )
        connection.executemany(
            """INSERT INTO job_vm_nics
               (id, job_vm_id, job_network_id, mac, state, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                ("nic-first", "vm-nic-order", "net-z", "52:54:00:00:00:01", "active", "2026-01-01 00:00:00"),
                ("nic-second", "vm-nic-order", "net-a", "52:54:00:00:00:02", "active", "2026-01-01 00:00:01"),
            ],
        )

    upgraded = _run_with_db(db, "-m", "astinuslab.db", "init")
    assert upgraded.returncode == 0, upgraded.stderr
    with sqlite3.connect(db) as connection:
        rows = connection.execute("SELECT id, ordinal FROM job_vm_nics ORDER BY ordinal").fetchall()
        assert rows == [("nic-first", 0), ("nic-second", 1)]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO job_vm_nics (id, job_vm_id, job_network_id, mac, ordinal, state, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
                ("nic-duplicate", "vm-nic-order", "net-a", "52:54:00:00:00:03", 0, "active"),
            )


def test_playground_cleanup_sessions_remain_exclusive_after_upgrade(tmp_path):
    import sqlite3

    db = tmp_path / "cleanup-exclusion.db"
    seeded = _run_with_db(db, "-c", _UPGRADE_TO.format(target="a022_job_network_updated_at"))
    assert seeded.returncode == 0, seeded.stderr
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO playground_sessions (id, status, created_at) VALUES (?, ?, datetime('now'))",
            ("pg-cleanup", "destroy_failed"),
        )
        connection.execute(
            """INSERT INTO job_networks
               (id, session_id, name, subnet_cidr, isolated, libvirt_net_name, state, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("net-cleanup", "pg-cleanup", "dirty", "10.42.1.0/24", 1, "astinuslab-pg-cleanup", "failed"),
        )

    upgraded = _run_with_db(db, "-m", "astinuslab.db", "init")
    assert upgraded.returncode == 0, upgraded.stderr

    with sqlite3.connect(db) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO playground_sessions (id, status, created_at) VALUES (?, ?, datetime('now'))",
            ("pg-new", "running"),
        )


def test_authoring_closed_migration_preserves_exclusion_and_downgrades(tmp_path):
    import sqlite3

    db = tmp_path / "authoring-closed.db"
    seeded = _run_with_db(db, "-c", _UPGRADE_TO.format(target="a035_prepare_attempt_fields"))
    assert seeded.returncode == 0, seeded.stderr

    upgraded = _run_with_db(db, "-m", "astinuslab.db", "init")
    assert upgraded.returncode == 0, upgraded.stderr

    with sqlite3.connect(db) as connection:
        verification_columns = {row[1] for row in connection.execute("PRAGMA table_info(lab_verification_attempts)")}
        assert "authored_session_id" in verification_columns
        indexes = {row[1]: row[2] for row in connection.execute("PRAGMA index_list(lab_verification_attempts)")}
        assert indexes["ux_lab_verification_attempts_authored_session"] == 1
        assert indexes["ux_lab_verification_attempts_draft_live"] == 1
        index_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            ("ux_playground_sessions_one_active",),
        ).fetchone()[0]
        assert "authoring_closed" in index_sql
        connection.execute(
            "INSERT INTO playground_sessions (id, status, created_at) VALUES (?, ?, datetime('now'))",
            ("pg-frozen", "authoring_closed"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO playground_sessions (id, status, created_at) VALUES (?, ?, datetime('now'))",
                ("pg-new", "running"),
            )
        connection.execute("DELETE FROM playground_sessions WHERE id = ?", ("pg-frozen",))

    downgraded = _run_with_db(
        db,
        "-c",
        _UPGRADE_TO.replace("command.upgrade", "command.downgrade").format(target="a035_prepare_attempt_fields"),
    )
    assert downgraded.returncode == 0, downgraded.stderr
    with sqlite3.connect(db) as connection:
        verification_columns = {row[1] for row in connection.execute("PRAGMA table_info(lab_verification_attempts)")}
        assert "authored_session_id" not in verification_columns
        index_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            ("ux_playground_sessions_one_active",),
        ).fetchone()[0]
        assert "authoring_closed" not in index_sql


def test_authoring_closed_migration_refuses_downgrade_during_bound_cleanup(tmp_path):
    import sqlite3

    db = tmp_path / "authoring-cleanup-downgrade.db"
    upgraded = _run_with_db(db, "-m", "astinuslab.db", "init")
    assert upgraded.returncode == 0, upgraded.stderr

    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO playground_sessions (id, status, created_at) VALUES (?, ?, datetime('now'))",
            ("pg-destroying", "destroying"),
        )
        connection.execute(
            """INSERT INTO lab_drafts
               (id, definition_json, definition_sha256, runbook, supporting_files_json, authored_session_id, created_at)
               VALUES (?, '{}', ?, '', '{}', ?, datetime('now'))""",
            ("draft-cleanup", "a" * 64, "pg-destroying"),
        )
        connection.execute(
            """INSERT INTO lab_verification_attempts
               (id, draft_id, authored_session_id, definition_sha256, state, progress_percent,
                cancel_requested, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'verified', 100, 0, datetime('now'), datetime('now'))""",
            ("attempt-cleanup", "draft-cleanup", "pg-destroying", "a" * 64),
        )

    downgraded = _run_with_db(
        db,
        "-c",
        _UPGRADE_TO.replace("command.upgrade", "command.downgrade").format(target="a035_prepare_attempt_fields"),
    )

    assert downgraded.returncode != 0
    assert "verification-owned Playground cleanup is in progress" in downgraded.stderr


def test_cleanup_exclusion_migration_serializes_multiple_legacy_backlogs(tmp_path):
    import sqlite3

    db = tmp_path / "cleanup-backlog.db"
    seeded = _run_with_db(db, "-c", _UPGRADE_TO.format(target="a022_job_network_updated_at"))
    assert seeded.returncode == 0, seeded.stderr
    sessions = (
        ("pg-destroy-old", "destroy_failed", "2026-01-01 00:00:00"),
        ("pg-destroy-new", "destroy_failed", "2026-01-02 00:00:00"),
        ("pg-failed", "failed", "2026-01-03 00:00:00"),
    )
    with sqlite3.connect(db) as connection:
        connection.executemany(
            "INSERT INTO playground_sessions (id, status, created_at) VALUES (?, ?, ?)",
            sessions,
        )
        for index, (session_id, _status, created_at) in enumerate(sessions, start=1):
            connection.execute(
                """INSERT INTO job_networks
                   (id, session_id, name, subnet_cidr, isolated, libvirt_net_name, state, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"net-{index}",
                    session_id,
                    f"dirty-{index}",
                    f"10.42.{index}.0/24",
                    1,
                    f"astinuslab-{session_id}",
                    "failed",
                    created_at,
                    created_at,
                ),
            )

    upgraded = _run_with_db(db, "-m", "astinuslab.db", "init")
    assert upgraded.returncode == 0, upgraded.stderr

    with sqlite3.connect(db) as connection:
        rows = connection.execute("SELECT id, status FROM playground_sessions ORDER BY created_at, id").fetchall()
        assert rows == [
            ("pg-destroy-old", "destroy_failed"),
            ("pg-destroy-new", "destroy_cleanup_pending"),
            ("pg-failed", "failure_cleanup_pending"),
        ]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO playground_sessions (id, status, created_at) VALUES (?, ?, datetime('now'))",
                ("pg-new", "running"),
            )


_INSERT_NETWORK = """INSERT INTO job_networks
    (id, session_id, name, subnet_cidr, isolated, libvirt_net_name, state, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"""


def test_destroyed_network_name_is_reusable_after_upgrade(tmp_path):
    """A torn-down network must stop reserving its name on a live database.

    The pre-a024 index was unique over (session_id, name) with no state
    predicate, so a session that destroyed its "default" network could never
    create another by that name - create_network compared the request against
    non-destroyed rows, admitted it, and the insert then died on the index.
    """
    import sqlite3

    db = tmp_path / "network-names.db"
    seeded = _run_with_db(db, "-c", _UPGRADE_TO.format(target="a023_playground_cleanup_exclusion"))
    assert seeded.returncode == 0, seeded.stderr
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO playground_sessions (id, status, created_at) VALUES (?, ?, datetime('now'))",
            ("pg-names", "running"),
        )
        connection.execute(
            _INSERT_NETWORK,
            ("net-gone", "pg-names", "default", "10.42.1.0/24", 1, "lv-gone", "destroyed", "2026-01-01", "2026-01-01"),
        )

    upgraded = _run_with_db(db, "-m", "astinuslab.db", "init")
    assert upgraded.returncode == 0, upgraded.stderr

    with sqlite3.connect(db) as connection:
        connection.execute(
            _INSERT_NETWORK,
            ("net-live", "pg-names", "default", "10.42.1.0/24", 1, "lv-live", "pending", "2026-01-02", "2026-01-02"),
        )
        # The name is released by teardown, not surrendered: two live networks
        # in one session still may not share it.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                _INSERT_NETWORK,
                ("net-dup", "pg-names", "default", "10.42.3.0/24", 1, "lv-dup", "pending", "2026-01-03", "2026-01-03"),
            )


def test_network_name_downgrade_renames_duplicates_instead_of_aborting(tmp_path):
    """Downgrade has to survive the reuse the partial index permitted.

    Rows that were legal under a024 collide under the total index a023 restores,
    so recreating it directly would fail the downgrade partway. The later
    duplicate is suffixed with its own primary key and both rows survive.
    """
    import sqlite3

    db = tmp_path / "network-names-downgrade.db"
    seeded = _run_with_db(db, "-m", "astinuslab.db", "init")
    assert seeded.returncode == 0, seeded.stderr
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO playground_sessions (id, status, created_at) VALUES (?, ?, datetime('now'))",
            ("pg-down", "running"),
        )
        for network_id, created in (("net-first", "2026-01-01"), ("net-second", "2026-01-02")):
            connection.execute(
                _INSERT_NETWORK,
                (
                    network_id,
                    "pg-down",
                    "default",
                    "10.42.1.0/24",
                    1,
                    f"lv-{network_id}",
                    "destroyed",
                    created,
                    created,
                ),
            )

    down = _run_with_db(
        db,
        "-c",
        _UPGRADE_TO.replace("command.upgrade", "command.downgrade").format(target="a023_playground_cleanup_exclusion"),
    )
    assert down.returncode == 0, down.stderr

    with sqlite3.connect(db) as connection:
        names = dict(connection.execute("SELECT id, name FROM job_networks ORDER BY created_at, id").fetchall())
    assert names == {"net-first": "default", "net-second": "default-net-second"}


def test_vm_resource_columns_migrate_from_a024(tmp_path):
    db = tmp_path / "vm-resources.db"
    seeded = _run_with_db(db, "-c", _UPGRADE_TO.format(target="a024_job_network_name_excludes_destroyed"))
    assert seeded.returncode == 0, seeded.stderr

    upgraded = _run_with_db(db, "-m", "astinuslab.db", "init")
    assert upgraded.returncode == 0, upgraded.stderr

    import sqlite3

    with sqlite3.connect(db) as connection:
        columns = {row[1]: (row[2], row[3]) for row in connection.execute("PRAGMA table_info(job_vms)")}
    assert columns["vcpus"] == ("INTEGER", 0)
    assert columns["memory_mb"] == ("INTEGER", 0)
    assert columns["disk_gb"] == ("INTEGER", 0)


def test_vm_resource_migration_round_trip_preserves_existing_rows(tmp_path):
    db = tmp_path / "vm-resources-round-trip.db"
    seeded = _run_with_db(db, "-c", _UPGRADE_TO.format(target="a024_job_network_name_excludes_destroyed"))
    assert seeded.returncode == 0, seeded.stderr

    import sqlite3

    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO playground_sessions (id, status, created_at) VALUES (?, ?, datetime('now'))",
            ("pg-resources", "running"),
        )
        connection.execute(
            """INSERT INTO job_vms
               (id, session_id, label, image_id, state, role, domain_name, disk_path, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("vm-resources", "pg-resources", "web", "img", "pending", "target", "domain-web", "/tmp/web.qcow2"),
        )

    upgraded = _run_with_db(db, "-m", "astinuslab.db", "init")
    assert upgraded.returncode == 0, upgraded.stderr
    with sqlite3.connect(db) as connection:
        row = connection.execute("SELECT id, vcpus, memory_mb, disk_gb FROM job_vms").fetchone()
        assert row == ("vm-resources", None, None, None)

    downgraded = _run_with_db(
        db,
        "-c",
        _UPGRADE_TO.replace("command.upgrade", "command.downgrade").format(
            target="a024_job_network_name_excludes_destroyed"
        ),
    )
    assert downgraded.returncode == 0, downgraded.stderr
    with sqlite3.connect(db) as connection:
        row = connection.execute("SELECT id FROM job_vms").fetchone()
        columns = {row[1] for row in connection.execute("PRAGMA table_info(job_vms)")}
    assert row == ("vm-resources",)
    assert {"vcpus", "memory_mb", "disk_gb"}.isdisjoint(columns)


def test_job_acquisition_index_upgrades_and_downgrades_cleanly(tmp_path):
    db = tmp_path / "job-acquisition-index.db"
    seeded = _run_with_db(db, "-c", _UPGRADE_TO.format(target="a024_job_network_name_excludes_destroyed"))
    assert seeded.returncode == 0, seeded.stderr
    assert "ix_jobs_acquisition_id" not in _sqlite_names(db, "index")

    upgraded = _run_with_db(db, "-m", "astinuslab.db", "init")
    assert upgraded.returncode == 0, upgraded.stderr
    assert "ix_jobs_acquisition_id" in _sqlite_names(db, "index")

    down = _run_with_db(
        db,
        "-c",
        _UPGRADE_TO.replace("command.upgrade", "command.downgrade").format(
            target="a024_job_network_name_excludes_destroyed"
        ),
    )
    assert down.returncode == 0, down.stderr
    assert "ix_jobs_acquisition_id" not in _sqlite_names(db, "index")
