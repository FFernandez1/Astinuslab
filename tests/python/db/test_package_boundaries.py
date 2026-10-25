from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


ROOT_DB_MODULES = ("models", "repositories", "db_baseline_snapshot")


def test_root_db_imports_are_removed() -> None:
    """The db family carried 435 of the audit's repathed references.

    Four of the six packages got an inverted ratchet when their shims went; db
    and api had their alias-identity assertions deleted with no replacement, so
    nothing asserted these names were gone. test_no_module_shims.py does not
    close the gap: it matches only a sys.modules reassignment, so a plain
    ``from .db.models import *`` re-export would pass it.
    """
    for name in ROOT_DB_MODULES:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(f"astinuslab.{name}")


def test_database_implementation_is_package_owned() -> None:
    database = importlib.import_module("astinuslab.db")
    importlib.import_module("astinuslab.db.models")
    importlib.import_module("astinuslab.db.repositories")
    importlib.import_module("astinuslab.db.baseline_snapshot")

    assert database.__file__ is not None
    assert Path(database.__file__).parent.name == "db"


def test_database_package_keeps_sqlalchemy_metadata_identity() -> None:
    from astinuslab.db import Base
    from astinuslab.db import models as _models  # noqa: F401

    assert len(Base.metadata.tables) == 36
    assert {
        "admin_browser_sessions",
        "acquisitions",
        "artifacts",
        "jobs",
        "playground_sessions",
        "job_vms",
        "job_networks",
        "job_vm_nics",
        "app_settings",
        "lab_packages",
        "lab_package_assets",
        "lab_content_objects",
        "lab_content_publication_attempts",
        "lab_package_inspections",
        "lab_package_publication_attempts",
        "lab_package_export_attempts",
        "lab_package_tombstones",
        "lab_storage_leases",
        "lab_instances",
        "capacity_reservations",
    } <= set(Base.metadata.tables)
    assert Base.metadata.tables["lab_packages"] is _models.LabPackage.__table__
    assert Base.metadata.tables["capacity_reservations"] is _models.CapacityReservation.__table__


def test_database_package_remains_the_session_factory_monkeypatch_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    import astinuslab.db as database

    events = []

    class FakeSession:
        def commit(self) -> None:
            events.append("commit")

        def rollback(self) -> None:
            events.append("rollback")

        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr(database, "SessionLocal", FakeSession)

    with database.session_scope() as session:
        assert isinstance(session, FakeSession)

    assert events == ["commit", "close"]


def test_database_package_keeps_module_cli() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "astinuslab.db", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "AstinusLab SQLite database utilities" in result.stdout
