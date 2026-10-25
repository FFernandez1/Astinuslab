"""A refused or failed backup must preserve source custody and the last copy."""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from astinuslab import db


@pytest.fixture()
def source_db(tmp_path, monkeypatch):
    path = tmp_path / "source.db"
    engine = create_engine(f"sqlite:///{path}", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("create table custody_probe (value text)")
        connection.exec_driver_sql("insert into custody_probe values ('preserved')")
    monkeypatch.setattr(db, "settings", SimpleNamespace(db_path=path))
    monkeypatch.setattr(db, "engine", engine)
    yield path
    engine.dispose()


@pytest.mark.parametrize("alias", ["same", "hardlink", "symlink"])
def test_backup_refuses_source_alias_before_changing_bytes(source_db, tmp_path, monkeypatch, alias):
    destination = source_db if alias == "same" else tmp_path / "alias.db"
    if alias == "hardlink":
        destination.hardlink_to(source_db)
    elif alias == "symlink":
        destination.symlink_to(source_db)
    before = source_db.read_bytes()
    original_connect = sqlite3.connect

    class RefuseUnsafeCopy(sqlite3.Connection):
        def backup(self, target, *args, **kwargs):
            # An unfixed same-inode copy can wait forever on its own SQLite
            # lock. The refusal must occur before reaching this boundary.
            raise AssertionError("source-alias backup reached SQLite copy")

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: original_connect(*a, factory=RefuseUnsafeCopy, **kw))

    with pytest.raises(RuntimeError, match="unsafe|source|same"):
        db.backup_database(destination)

    assert source_db.read_bytes() == before
    with sqlite3.connect(source_db) as connection:
        assert connection.execute("select value from custody_probe").fetchall() == [("preserved",)]


def test_backup_refuses_unrelated_symlink_without_touching_target(source_db, tmp_path):
    outside = tmp_path / "operator-notes.txt"
    outside.write_text("keep operator notes\n")
    destination = tmp_path / "backup.db"
    destination.symlink_to(outside)

    with pytest.raises(RuntimeError, match="unsafe|symlink"):
        db.backup_database(destination)

    assert destination.is_symlink()
    assert outside.read_text() == "keep operator notes\n"


@pytest.mark.parametrize("failure", ["copy", "verify", "publish"])
def test_backup_failure_preserves_previous_copy(source_db, tmp_path, monkeypatch, failure):
    destination = tmp_path / "saved.db"
    destination.write_bytes(b"previous verified backup placeholder")
    before = destination.read_bytes()
    original_entries = set(tmp_path.iterdir())
    original_connect = sqlite3.connect

    class FailingConnection(sqlite3.Connection):
        def backup(self, target, *args, **kwargs):
            if failure == "copy":
                raise sqlite3.OperationalError("copy interrupted")
            return super().backup(target, *args, **kwargs)

        def execute(self, sql, *args, **kwargs):
            if failure == "verify" and sql == "PRAGMA integrity_check":
                raise sqlite3.DatabaseError("verification interrupted")
            return super().execute(sql, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: original_connect(*a, factory=FailingConnection, **kw))
    if failure == "publish":
        monkeypatch.setattr(os, "replace", lambda *a, **kw: (_ for _ in ()).throw(OSError("publish interrupted")))

    with pytest.raises((sqlite3.Error, OSError), match="interrupted"):
        db.backup_database(destination)

    assert destination.read_bytes() == before
    assert set(tmp_path.iterdir()) == original_entries


def test_backup_overwrite_is_private_before_sqlite_writes(source_db, tmp_path, monkeypatch):
    destination = tmp_path / "saved.db"
    destination.write_text("previous backup")
    destination.chmod(0o666)
    original_entries = set(tmp_path.iterdir())
    original_connect = sqlite3.connect
    checked = []

    class InspectingConnection(sqlite3.Connection):
        def backup(self, target, *args, **kwargs):
            staged = Path(target.execute("PRAGMA database_list").fetchone()[2])
            checked.append(staged.stat().st_mode & 0o777)
            assert checked[-1] == 0o600
            assert destination.read_text() == "previous backup"
            return super().backup(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: original_connect(*a, factory=InspectingConnection, **kw))
    record = db.backup_database(destination)

    assert checked == [0o600]
    assert destination.stat().st_mode & 0o777 == 0o600
    assert record["mode"] == "0600"
    with original_connect(destination) as connection:
        assert connection.execute("select value from custody_probe").fetchone() == ("preserved",)
    assert set(tmp_path.iterdir()) == original_entries


def test_backup_refuses_quiesced_destination_wal_without_changing_any_file(source_db, tmp_path):
    donor = tmp_path / "donor.db"
    destination = tmp_path / "saved.db"
    writer = sqlite3.connect(donor)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("create table custody_probe (value text)")
        writer.execute("insert into custody_probe values ('previous destination')")
        writer.commit()
        for suffix in ("", "-wal", "-shm"):
            shutil.copyfile(Path(f"{donor}{suffix}"), Path(f"{destination}{suffix}"))
    finally:
        writer.close()
    # These valid orphan sidecars survive a crash; no destination writer exists.
    retained = {path: path.read_bytes() for path in tmp_path.glob("saved.db*")}
    source_before = source_db.read_bytes()

    with pytest.raises(RuntimeError, match="sidecar"):
        db.backup_database(destination)

    assert {path: path.read_bytes() for path in retained} == retained
    assert source_db.read_bytes() == source_before
    assert not list(tmp_path.glob(".astinuslab-backup-*"))
    with sqlite3.connect(destination) as reader:
        assert reader.execute("select value from custody_probe").fetchall() == [("previous destination",)]


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
@pytest.mark.parametrize("during_copy", [False, True])
def test_backup_refuses_destination_sidecars_before_atomic_replace(
    source_db, tmp_path, monkeypatch, suffix, during_copy
):
    destination = tmp_path / "saved.db"
    destination.write_bytes(b"previous backup")
    sidecar = Path(f"{destination}{suffix}")
    if not during_copy:
        sidecar.write_bytes(b"recoverable SQLite state")
    original_connect = sqlite3.connect

    class SidecarAppears(sqlite3.Connection):
        def backup(self, target, *args, **kwargs):
            result = super().backup(target, *args, **kwargs)
            if during_copy:
                sidecar.write_bytes(b"recoverable SQLite state")
            return result

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: original_connect(*a, factory=SidecarAppears, **kw))
    with pytest.raises(RuntimeError, match="sidecar"):
        db.backup_database(destination)

    assert destination.read_bytes() == b"previous backup"
    assert sidecar.read_bytes() == b"recoverable SQLite state"
    assert not list(tmp_path.glob(".astinuslab-backup-*"))


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
@pytest.mark.parametrize("alias", ["missing", "same", "parent_alias", "hardlink", "symlink"])
def test_backup_refuses_source_sidecar_names_and_aliases_before_opening_sqlite(
    source_db, tmp_path, monkeypatch, suffix, alias
):
    sidecar = Path(f"{source_db}{suffix}")
    if alias != "missing":
        sidecar.write_bytes(b"source SQLite sidecar custody")
    destination = sidecar
    if alias == "parent_alias":
        parent = tmp_path / "parent-alias"
        parent.symlink_to(tmp_path, target_is_directory=True)
        destination = parent / sidecar.name
    elif alias in {"hardlink", "symlink"}:
        destination = tmp_path / "alias.db"
        if alias == "hardlink":
            destination.hardlink_to(sidecar)
        else:
            destination.symlink_to(sidecar)
    source_before = source_db.read_bytes()

    def refuse_sqlite(*args, **kwargs):
        raise AssertionError("unsafe sidecar backup reached SQLite")

    monkeypatch.setattr(db, "current_revision", refuse_sqlite)
    monkeypatch.setattr(sqlite3, "connect", refuse_sqlite)
    with pytest.raises(RuntimeError, match="sidecar|unsafe"):
        db.backup_database(destination)

    assert source_db.read_bytes() == source_before
    if alias == "missing":
        assert not sidecar.exists()
    else:
        assert sidecar.read_bytes() == b"source SQLite sidecar custody"
    assert not list(tmp_path.glob(".astinuslab-backup-*"))
