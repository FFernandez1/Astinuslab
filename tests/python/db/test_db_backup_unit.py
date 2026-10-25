"""In-process cover for the backup primitives.

test_db_backup.py drives the same code through `python -m astinuslab.db`, which
proves the CLI contract an installer depends on but runs in a subprocess where
the failure branches cannot be asserted precisely.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from astinuslab import db


@pytest.fixture()
def live_db(tmp_path, monkeypatch):
    """A stamped database with the module pointed at it."""
    path = tmp_path / "storage" / "astinuslab.db"
    path.parent.mkdir(parents=True)
    engine = create_engine(f"sqlite:///{path}", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("create table alembic_version (version_num varchar(32) not null)")
        connection.exec_driver_sql("insert into alembic_version values ('a021_evidence_tombstones')")
        connection.exec_driver_sql("create table evidence (value text)")
        connection.exec_driver_sql("insert into evidence values ('custody')")
    monkeypatch.setattr(db, "settings", SimpleNamespace(db_path=path))
    monkeypatch.setattr(db, "engine", engine)
    return path


def test_backup_reports_the_hash_mode_and_revision_it_wrote(live_db, tmp_path):
    record = db.backup_database(tmp_path / "backups" / "one.db")

    copy = Path(record["path"])
    assert copy.is_file()
    assert copy.stat().st_mode & 0o777 == 0o600
    assert record["mode"] == "0600"
    assert record["revision"] == "a021_evidence_tombstones"
    with sqlite3.connect(copy) as handle:
        assert handle.execute("select value from evidence").fetchone() == ("custody",)


def test_current_revision_is_none_for_an_unstamped_database(tmp_path, monkeypatch):
    path = tmp_path / "unstamped.db"
    engine = create_engine(f"sqlite:///{path}", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("create table only_data (value text)")
    monkeypatch.setattr(db, "settings", SimpleNamespace(db_path=path))
    monkeypatch.setattr(db, "engine", engine)

    assert db.current_revision() is None


def test_backup_of_an_unstamped_database_records_an_empty_revision(tmp_path, monkeypatch):
    path = tmp_path / "unstamped.db"
    engine = create_engine(f"sqlite:///{path}", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("create table only_data (value text)")
    monkeypatch.setattr(db, "settings", SimpleNamespace(db_path=path))
    monkeypatch.setattr(db, "engine", engine)

    record = db.backup_database(tmp_path / "backups" / "unstamped-copy.db")

    assert record["revision"] == ""


def test_restore_rejects_a_missing_backup(live_db, tmp_path):
    with pytest.raises(RuntimeError, match="missing"):
        db.restore_database(tmp_path / "nope.db")


def test_restore_rejects_a_file_that_is_not_a_database(live_db, tmp_path):
    junk = tmp_path / "not-a-db"
    junk.write_bytes(b"just some bytes, definitely not SQLite\n")

    with pytest.raises(RuntimeError, match="unverified|unreadable"):
        db.restore_database(junk)

    with sqlite3.connect(live_db) as handle:
        assert handle.execute("select value from evidence").fetchone() == ("custody",)


def test_restore_returns_the_revision_it_put_back(live_db, tmp_path):
    record = db.backup_database(tmp_path / "backups" / "before.db")
    with sqlite3.connect(live_db) as handle:
        handle.execute("delete from evidence")
        handle.commit()

    restored = db.restore_database(Path(record["path"]))

    assert restored["revision"] == "a021_evidence_tombstones"
    with sqlite3.connect(live_db) as handle:
        assert handle.execute("select value from evidence").fetchone() == ("custody",)


def test_restore_never_follows_a_preplanted_staging_symlink(live_db, tmp_path):
    record = db.backup_database(tmp_path / "backups" / "before.db")
    outside = tmp_path / "outside.txt"
    outside.write_text("operator data\n", encoding="utf-8")
    outside.chmod(0o666)
    planted = live_db.with_name(live_db.name + ".restore")
    planted.symlink_to(outside)
    with sqlite3.connect(live_db) as handle:
        handle.execute("delete from evidence")
        handle.commit()

    db.restore_database(Path(record["path"]))

    assert outside.read_text(encoding="utf-8") == "operator data\n"
    assert outside.stat().st_mode & 0o777 == 0o666
    assert planted.is_symlink(), "restore must not reuse the attacker-chosen staging name"
    with sqlite3.connect(live_db) as handle:
        assert handle.execute("select value from evidence").fetchone() == ("custody",)


def test_restore_rejects_a_symlinked_live_database_without_touching_outside(live_db, tmp_path):
    record = db.backup_database(tmp_path / "backups" / "before.db")
    saved = live_db.with_name("saved.db")
    live_db.rename(saved)
    outside = tmp_path / "outside.db"
    outside.write_text("operator data\n", encoding="utf-8")
    outside.chmod(0o666)
    live_db.symlink_to(outside)

    with pytest.raises(RuntimeError, match="unsafe live database"):
        db.restore_database(Path(record["path"]))

    assert outside.read_text(encoding="utf-8") == "operator data\n"
    assert outside.stat().st_mode & 0o777 == 0o666


def test_database_restore_preserves_exact_acl_snapshot_custody(live_db, tmp_path):
    install_script = Path(__file__).resolve().parents[3] / "ops" / "deploy" / "install.sh"
    record = db.backup_database(tmp_path / "backups" / "before.db")
    live_db.chmod(0o660)
    subprocess.run(["setfacl", "-m", "u:65534:r--", live_db], check=True)
    before = live_db.stat()
    before_acl = subprocess.run(["getfacl", "-cp", live_db], check=True, capture_output=True, text=True).stdout
    acl_snapshot = tmp_path / "storage.acl"
    captured = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; snapshot_acl_state "$2" "$3"',
            "astinuslab-acl-test",
            str(install_script),
            str(live_db.parent),
            str(acl_snapshot),
        ],
        capture_output=True,
        text=True,
    )
    assert captured.returncode == 0, captured.stderr
    with sqlite3.connect(live_db) as handle:
        handle.execute("delete from evidence")
        handle.commit()
    subprocess.run(["setfacl", "-b", live_db], check=True)
    live_db.chmod(0o600)

    db.restore_database(Path(record["path"]))
    restored = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; restore_acl_snapshot "$2" "$3"',
            "astinuslab-acl-test",
            str(install_script),
            str(acl_snapshot),
            str(live_db.parent),
        ],
        capture_output=True,
        text=True,
    )

    assert restored.returncode == 0, restored.stderr
    after = live_db.stat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    assert after.st_mode & 0o777 == before.st_mode & 0o777
    assert subprocess.run(["getfacl", "-cp", live_db], check=True, capture_output=True, text=True).stdout == before_acl


@pytest.mark.parametrize("target", ["-1", "+2", "head", "heads", "base", "current", "  ", ""])
def test_downgrade_refuses_targets_that_are_not_an_explicit_revision(target):
    with pytest.raises(RuntimeError, match="explicit revision"):
        db.downgrade_to(target)


def test_downgrade_passes_an_explicit_revision_through(monkeypatch):
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(db, "_run_alembic", lambda command, target: seen.append((command, target)))

    db.downgrade_to("  a020_artifact_publication_attempts  ")

    assert seen == [("downgrade", "a020_artifact_publication_attempts")]


def test_main_backup_prints_one_machine_readable_line(live_db, tmp_path, capsys):
    destination = tmp_path / "backups" / "cli.db"

    assert db.main(["backup", str(destination)]) == 0

    line = capsys.readouterr().out.strip()
    assert line.startswith("backup: path=")
    fields = dict(part.split("=", 1) for part in line.split(maxsplit=1)[1].split())
    assert fields["mode"] == "0600"
    assert fields["revision"] == "a021_evidence_tombstones"


def test_main_restore_and_revision_round_trip(live_db, tmp_path, capsys):
    assert db.main(["backup", str(tmp_path / "backups" / "rt.db")]) == 0
    capsys.readouterr()

    assert db.main(["revision"]) == 0
    assert capsys.readouterr().out.strip() == "a021_evidence_tombstones"

    assert db.main(["restore", str(tmp_path / "backups" / "rt.db")]) == 0
    assert "restore: path=" in capsys.readouterr().out


def test_main_reports_a_failed_backup_without_raising(live_db, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "backup_database", lambda _destination: (_ for _ in ()).throw(RuntimeError("disk full")))

    assert db.main(["backup", str(tmp_path / "x.db")]) == 1


def test_main_reports_a_failed_restore_without_raising(live_db, tmp_path):
    assert db.main(["restore", str(tmp_path / "absent.db")]) == 1


def test_main_reports_a_refused_downgrade_without_raising(live_db):
    assert db.main(["downgrade", "-1"]) == 1


def test_main_downgrade_reports_the_revision_it_reached(live_db, monkeypatch, capsys):
    monkeypatch.setattr(db, "_run_alembic", lambda command, target: None)

    assert db.main(["downgrade", "a020_artifact_publication_attempts"]) == 0
    assert "downgrade: revision=" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["backup", "restore", "downgrade"])
def test_main_requires_a_target(live_db, command):
    assert db.main([command]) == 2


def test_restore_accepts_a_backup_of_an_unstamped_database(tmp_path, monkeypatch):
    """A clean install backs up before the first migration, so the backup has no
    alembic_version table. That is an unstamped database, not a corrupt one."""
    path = tmp_path / "unstamped.db"
    engine = create_engine(f"sqlite:///{path}", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("create table only_data (value text)")
    monkeypatch.setattr(db, "settings", SimpleNamespace(db_path=path))
    monkeypatch.setattr(db, "engine", engine)
    record = db.backup_database(tmp_path / "backups" / "unstamped.db")

    restored = db.restore_database(Path(record["path"]))

    assert restored["revision"] == ""


def test_restore_keeps_the_live_database_owner_and_mode(live_db, tmp_path, monkeypatch):
    """The restore runs as root but the service identity has to keep writing the
    database afterwards, so the replacement carries the original owner and mode."""
    import os

    live_db.chmod(0o660)
    before = live_db.stat()
    record = db.backup_database(tmp_path / "backups" / "owned.db")
    chowned: list[tuple[int, int]] = []
    real_fchown = os.fchown
    monkeypatch.setattr(os, "fchown", lambda descriptor, uid, gid: chowned.append((uid, gid)))

    db.restore_database(Path(record["path"]))

    monkeypatch.setattr(os, "fchown", real_fchown)
    assert live_db.stat().st_mode & 0o777 == 0o660, "mode must survive the replacement"
    assert chowned == [(before.st_uid, before.st_gid)], "owner must survive the replacement"
