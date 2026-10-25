from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from astinuslab.lab_packages.archive import write_archive
from astinuslab.lab_packages.content_store import ContentStore
from astinuslab.lab_packages.package_service import LabPackageService
from tests.python._support.lab_packages import prepare_definition


@pytest.fixture()
def prepare_package_service(tmp_path: Path) -> LabPackageService:
    return LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))


@pytest.fixture()
def prepare_gc_fixture(tmp_path, prepare_package_service, monkeypatch):
    import socket
    import subprocess

    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    from astinuslab.db import Base
    from astinuslab.lab_packages import package_service, prepare_service
    from astinuslab.lab_packages.models import AssetReference
    from astinuslab.lab_packages.resolver import AssetResolver

    service = prepare_package_service

    def refuse(*args, **kwargs):
        raise AssertionError("prepare fixture forbids network, subprocess and task dispatch")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(prepare_service.prepare_assets_task, "delay", refuse)
    clock = [datetime.now(UTC).replace(tzinfo=None)]

    class FixtureDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0] if tz is None else clock[0].replace(tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(package_service, "datetime", FixtureDateTime)
    monkeypatch.setattr(prepare_service, "_now", lambda: clock[0])
    engine = create_engine(f"sqlite:///{tmp_path / 'prepare-gc.db'}", connect_args={"timeout": 3})

    @event.listens_for(engine, "connect")
    def pragmas(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")

    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, autoflush=False)
    payload = b"synthetic prepare ownership bytes"
    digest = hashlib.sha256(payload).hexdigest()
    base = prepare_definition()
    reference = AssetReference(
        sha256=digest, size_bytes=len(payload), media_type="application/x-qcow2", license="CC-BY-4.0"
    )
    definition = base.model_copy(update={"vms": (base.vms[0].model_copy(update={"clean_layer": reference}),)})
    source = tmp_path / "prepare-gc.astinuslab"
    source.write_bytes(write_archive(definition, "# Fixture\n"))
    with sessions() as session:
        record = service.import_path(source, session).record
        attempt = prepare_service.create_prepare(session, record=record, service=service, idempotency_key="gc-boundary")
        session.commit()
    original_resolve = AssetResolver.resolve

    def resolve(resolver, asset, *args, **kwargs):
        # Use the actual resolver/content-store publication with synthetic bytes.
        return original_resolve(resolver, asset, embedded=payload)

    monkeypatch.setattr(AssetResolver, "resolve", resolve)
    try:
        yield service, sessions, attempt.id, digest, payload, clock
    finally:
        engine.dispose()


@pytest.fixture()
def package_client(in_memory_session_scope, monkeypatch):
    """Exercise the preview router without enabling it in the default app."""
    from astinuslab import db as db_mod
    from astinuslab.api import get_session
    from astinuslab.lab_packages.api import router

    app = FastAPI()
    monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)

    def _override_get_session():
        with in_memory_session_scope() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    app.include_router(router)
    with TestClient(app, raise_server_exceptions=True) as client:
        yield client
