"""Contract for the database backup and migration rollback protocol.

Switching the ``current`` symlink back to an earlier release is instant, but the
schema only ever moves forward, so a rollback across a migration would leave old
code running against a newer schema. These primitives make the schema half
recoverable: a checkpointed, verified backup taken before the migration, and a
downgrade that only ever names an explicit revision.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def _run(db: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["ASTINUSLAB_DB_PATH"] = str(db)
    return subprocess.run(
        [sys.executable, "-m", "astinuslab.db", *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _initialised(tmp_path: Path) -> Path:
    db = tmp_path / "storage" / "astinuslab.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    result = _run(db, "init")
    assert result.returncode == 0, result.stderr
    return db


def _fields(stdout: str) -> dict[str, str]:
    line = next(line for line in stdout.splitlines() if line.startswith("backup:"))
    return dict(part.split("=", 1) for part in line.split(maxsplit=1)[1].split())


def test_backup_records_a_verified_copy_with_its_hash_mode_and_revision(tmp_path):
    db = _initialised(tmp_path)
    destination = tmp_path / "backups" / "pre-migration.db"

    result = _run(db, "backup", str(destination))

    assert result.returncode == 0, result.stderr
    fields = _fields(result.stdout)
    assert Path(fields["path"]) == destination
    assert destination.is_file()
    assert fields["sha256"] == hashlib.sha256(destination.read_bytes()).hexdigest()
    assert fields["mode"] == "0600", "a backup of the custody record is not world-readable"
    assert destination.stat().st_mode & 0o777 == 0o600
    assert fields["revision"], "the revision is what a rollback has to target"

    with sqlite3.connect(destination) as copy:
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert copy.execute("select count(*) from alembic_version").fetchone()[0] == 1


def test_backup_captures_writes_still_in_the_write_ahead_log(tmp_path):
    """SQLite runs in WAL mode, so a plain copy of the database file can miss
    committed rows. The backup checkpoints first."""
    db = _initialised(tmp_path)
    with sqlite3.connect(db) as live:
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("create table backup_probe (value text)")
        live.execute("insert into backup_probe values ('committed-into-wal')")
        live.commit()
        destination = tmp_path / "backups" / "wal.db"
        assert _run(db, "backup", str(destination)).returncode == 0

    with sqlite3.connect(destination) as copy:
        rows = copy.execute("select value from backup_probe").fetchall()
    assert rows == [("committed-into-wal",)]
    # The backup is one self-contained file. A WAL source otherwise leaves -wal
    # and -shm siblings beside the copy, and a backup that travels as a single
    # file would silently shed them.
    assert sorted(p.name for p in destination.parent.iterdir()) == [destination.name]


def test_backup_contains_only_the_database_and_never_key_material(tmp_path):
    db = _initialised(tmp_path)
    keys = db.parent / "ssh_keys"
    keys.mkdir()
    secret = b"-----BEGIN OPENSSH PRIVATE KEY-----\nnot-a-real-key\n"
    (keys / "astinuslab-default").write_bytes(secret)
    destination = tmp_path / "backups" / "only-db.db"

    assert _run(db, "backup", str(destination)).returncode == 0

    assert secret not in destination.read_bytes()
    assert sorted(p.name for p in destination.parent.iterdir()) == [destination.name]


def test_restore_puts_the_backup_back_in_place(tmp_path):
    db = _initialised(tmp_path)
    destination = tmp_path / "backups" / "before.db"
    assert _run(db, "backup", str(destination)).returncode == 0
    with sqlite3.connect(db) as live:
        live.execute("create table after_backup (value text)")
        live.commit()

    result = _run(db, "restore", str(destination))

    assert result.returncode == 0, result.stderr
    with sqlite3.connect(db) as live:
        tables = {row[0] for row in live.execute("select name from sqlite_master where type='table'")}
    assert "after_backup" not in tables, "restore must return the schema the backup captured"


def test_restore_refuses_a_backup_that_does_not_verify(tmp_path):
    db = _initialised(tmp_path)
    destination = tmp_path / "backups" / "corrupt.db"
    assert _run(db, "backup", str(destination)).returncode == 0
    with sqlite3.connect(db) as live:
        live.execute("create table survivor (value text)")
        live.commit()
    data = bytearray(destination.read_bytes())
    data[4096:4200] = b"\x00" * 104
    destination.write_bytes(bytes(data))

    result = _run(db, "restore", str(destination))

    assert result.returncode != 0
    with sqlite3.connect(db) as live:
        tables = {row[0] for row in live.execute("select name from sqlite_master where type='table'")}
    assert "survivor" in tables, "a refused restore must leave the live database alone"


def test_revision_reports_what_a_rollback_would_target(tmp_path):
    db = _initialised(tmp_path)

    result = _run(db, "revision")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip(), "a rollback needs a concrete revision to pin"


def test_downgrade_refuses_a_relative_target(tmp_path):
    """`downgrade -1` is relative to whatever head happens to be. Adding a
    revision or a merge point later silently changes which transition runs."""
    db = _initialised(tmp_path)

    for relative in ("-1", "+1", "head", "heads", "base"):
        result = _run(db, "downgrade", relative)
        assert result.returncode != 0, relative
        assert "explicit" in (result.stdout + result.stderr).lower(), relative
