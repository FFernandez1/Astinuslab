from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from astinuslab import app_settings
from astinuslab.db.repositories import AppSettingsRepository


def test_settings_merge_reserves_writer_before_reading(tmp_path, monkeypatch):
    from sqlalchemy import Table, create_engine
    from sqlalchemy.orm import Session

    from astinuslab.db.models import AppSetting

    path = tmp_path / "settings.db"
    engine = create_engine(f"sqlite:///{path}")
    table = AppSetting.__table__
    assert isinstance(table, Table)
    table.create(engine)

    @contextmanager
    def scope():
        with Session(engine) as session, session.begin():
            yield session

    monkeypatch.setattr(app_settings, "session_scope", scope)
    app_settings.save_app_settings({"retention": {"auto_enabled": True, "dumps_days": 7}})
    original_get = AppSettingsRepository.get
    competed = False

    def read_with_competing_writer(self, key):
        nonlocal competed
        result = original_get(self, key)
        if not competed:
            competed = True
            with sqlite3.connect(path, timeout=0) as writer:
                # Another policy writer must wait until this read/merge commits.
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    writer.execute("BEGIN IMMEDIATE")
        return result

    monkeypatch.setattr(AppSettingsRepository, "get", read_with_competing_writer)
    try:
        app_settings.save_app_settings({"retention": {"last_run_at": "2026-10-04T00:00:00+00:00"}})
        app_settings.save_app_settings({"retention": {"auto_enabled": False, "dumps_days": 365}})
        result = app_settings.load_app_settings()["retention"]
        assert competed
        assert result["auto_enabled"] is False
        assert result["dumps_days"] == 365
        assert result["last_run_at"] == "2026-10-04T00:00:00+00:00"
    finally:
        engine.dispose()


@pytest.fixture()
def app_settings_db(in_memory_session_scope, monkeypatch):
    monkeypatch.setattr(app_settings, "session_scope", in_memory_session_scope)
    return in_memory_session_scope


def test_load_returns_defaults_when_setting_missing(app_settings_db):
    data = app_settings.load_app_settings()
    assert data == {
        "retention": {"dumps_days": 90, "logs_days": 180, "auto_enabled": False},
        "disk_guardrail": {"min_free_gib": 0},
    }


def test_save_then_load_round_trip(app_settings_db):
    app_settings.save_app_settings({"retention": {"dumps_days": 7, "logs_days": 30}})

    with app_settings_db() as session:
        stored = AppSettingsRepository(session).get(app_settings._SETTINGS_KEY)
    assert stored is not None
    assert json.loads(stored) == {
        "retention": {"dumps_days": 7, "logs_days": 30, "auto_enabled": False},
        "disk_guardrail": {"min_free_gib": 0},
    }

    data = app_settings.load_app_settings()
    assert data["retention"]["dumps_days"] == 7
    assert data["retention"]["logs_days"] == 30


def test_save_deep_merges_existing(app_settings_db):
    app_settings.save_app_settings({"retention": {"dumps_days": 14, "logs_days": 60}})
    app_settings.save_app_settings({"retention": {"dumps_days": 21}})
    data = app_settings.load_app_settings()
    assert data["retention"] == {"dumps_days": 21, "logs_days": 60, "auto_enabled": False}


def test_disk_guardrail_min_free_gib_normalized(app_settings_db):
    saved = app_settings.save_app_settings({"disk_guardrail": {"min_free_gib": "30"}})
    assert saved["disk_guardrail"]["min_free_gib"] == 30
    assert app_settings.load_app_settings()["disk_guardrail"]["min_free_gib"] == 30


def test_disk_guardrail_rejects_non_integer(app_settings_db):
    with pytest.raises(ValueError):
        app_settings.save_app_settings({"disk_guardrail": {"min_free_gib": "lots"}})


def test_disk_guardrail_rejects_negative(app_settings_db):
    with pytest.raises(ValueError):
        app_settings.save_app_settings({"disk_guardrail": {"min_free_gib": -5}})


def test_save_persists_auto_enabled(app_settings_db):
    app_settings.save_app_settings({"retention": {"auto_enabled": True}})

    data = app_settings.load_app_settings()

    assert data["retention"]["auto_enabled"] is True
    assert data["retention"]["dumps_days"] == 90
    assert data["retention"]["logs_days"] == 180


def test_save_parses_auto_enabled_false_string(app_settings_db):
    app_settings.save_app_settings({"retention": {"auto_enabled": "false"}})

    assert app_settings.load_app_settings()["retention"]["auto_enabled"] is False


def test_load_falls_back_on_malformed_json(app_settings_db, caplog):
    with app_settings_db() as session:
        AppSettingsRepository(session).set(app_settings._SETTINGS_KEY, "{ not json")
    with caplog.at_level("WARNING"):
        data = app_settings.load_app_settings()
    assert data["retention"]["dumps_days"] == 90
    assert "app_settings" in caplog.text.lower()


def test_save_rejects_non_integer_retention_days(app_settings_db):
    with pytest.raises(ValueError, match="dumps_days must be an integer"):
        app_settings.save_app_settings({"retention": {"dumps_days": "abc"}})


def test_save_rejects_out_of_range_retention_days(app_settings_db):
    with pytest.raises(ValueError, match="logs_days out of range"):
        app_settings.save_app_settings({"retention": {"logs_days": "0"}})


def test_run_retention_uses_saved_days(app_settings_db, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from astinuslab.storage import retention

    app_settings.save_app_settings({"retention": {"dumps_days": 3, "logs_days": 9}})

    captured_logs: list[tuple[Path, int, str]] = []
    captured_evidence: list[int] = []

    def fake_purge(directory, older_than_days, suffix="", *, dry_run=False):
        captured_logs.append((directory, older_than_days, suffix))
        return 0

    def fake_purge_evidence(older_than_days, *, dry_run=False):
        captured_evidence.append(older_than_days)
        return 0, 0

    monkeypatch.setattr(retention, "_purge_old_files", fake_purge)
    monkeypatch.setattr(retention, "_purge_old_evidence_artifacts", fake_purge_evidence)

    fake_storage = tmp_path / "store"
    fake_storage.mkdir()
    monkeypatch.setattr(
        retention,
        "settings",
        SimpleNamespace(
            base_dir=fake_storage,
            data_dir=fake_storage,
            storage_dir=fake_storage / "storage",
            serial_log_dir=fake_storage / "storage" / "logs" / "serial",
        ),
    )

    retention.run_retention()
    suffixes = {(d.name, days, suf) for d, days, suf in captured_logs}
    assert captured_evidence == [3]
    assert ("logs", 9, "") in suffixes
    # The serial sweep is a second pass over its own directory, on the same window.
    assert ("serial", 9, "") in suffixes
