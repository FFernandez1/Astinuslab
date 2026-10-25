"""A successful artifact response survives eligible retention of its pathname."""

import asyncio
import hashlib
import json
import socket
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from astinuslab.api import routes
from astinuslab.db import Base, models
from astinuslab.jobs import job_services
from astinuslab.storage import retention
from tests.python._support.helpers import create_evidence_triad


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'download-review.db'}")

    @event.listens_for(engine, "connect")
    def pragmas(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")

    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, autoflush=False)

    @contextmanager
    def session_scope():
        with sessions() as session:
            try:
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise

    settings = SimpleNamespace(acl_allowed_storage_roots=(tmp_path,), storage_dir=tmp_path)
    monkeypatch.setattr(retention, "settings", settings)
    monkeypatch.setattr(job_services, "settings", settings)
    monkeypatch.setattr(retention, "session_scope", session_scope)
    payload = b"synthetic review bytes only\n" * 10000
    digest = hashlib.sha256(payload).hexdigest()
    with session_scope() as session:
        session.add(
            models.Job(
                id="job-1",
                target_id="synthetic",
                scenario_id="synthetic",
                execution_mode="immediate",
                status="completed",
            )
        )
        session.flush()
        artifact, paths = create_evidence_triad(session, tmp_path, job_id="job-1")
        paths[0].write_bytes(payload)
        paths[1].write_text(json.dumps({"sha256": digest, "size_bytes": len(payload), "synthetic": True}))
        paths[2].write_text(f"{digest}  {paths[0].name}\n")
        artifact.sha256 = digest
        artifact.size_bytes = len(payload)
        artifact.created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=100)
        artifact_id = artifact.id
    yield SimpleNamespace(
        sessions=sessions,
        session_scope=session_scope,
        artifact_id=artifact_id,
        paths=paths,
        payload=payload,
        digest=digest,
    )
    engine.dispose()


@pytest.mark.parametrize(
    "route_name,use_range",
    [
        ("download_artifact_by_id", False),
        ("download_artifact_manifest", False),
        ("download_artifact_sha256_sidecar", False),
        ("download_artifact_by_id", True),
    ],
)
@pytest.mark.parametrize("schedule", ["headers", "after_open", "no_retention"])
def test_download_remains_complete_during_retention(evidence, route_name, use_range, schedule):
    with evidence.session_scope() as session:
        response = getattr(routes, route_name)(evidence.artifact_id, session)
    expected = Path(response.path).read_bytes()
    headers = []
    if use_range:
        headers = [(b"range", b"bytes=1-180000")]
        expected = expected[1:180001]
    messages = []
    observation = {"route": route_name, "range": use_range, "schedule": schedule}
    triggered = False

    async def send(message):
        nonlocal triggered
        messages.append(dict(message))
        trigger = schedule == "headers" and message["type"] == "http.response.start"
        trigger |= schedule == "after_open" and message["type"] == "http.response.body"
        if trigger and not triggered:
            triggered = True
            observation["retention_result"] = retention._purge_old_evidence_artifacts(90)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    error = None
    try:
        asyncio.run(response({"type": "http", "method": "GET", "headers": headers, "extensions": {}}, receive, send))
    except Exception as exc:
        error = type(exc).__name__
    received = b"".join(m.get("body", b"") for m in messages)
    with evidence.sessions() as session:
        observation["artifact_remains"] = session.get(models.Artifact, evidence.artifact_id) is not None
        observation["tombstones"] = [(t.cleanup_state, t.actor) for t in session.query(models.EvidenceTombstone)]
    observation.update(
        error=error,
        statuses=[m["status"] for m in messages if "status" in m],
        expected_bytes=len(expected),
        received_bytes=len(received),
        triad_remaining=[p.exists() for p in evidence.paths],
        body_complete=any(m["type"] == "http.response.body" and not m.get("more_body", False) for m in messages),
    )
    if schedule != "no_retention":
        assert triggered and observation["retention_result"] == (1, 0)
        assert not observation["artifact_remains"]
        assert observation["triad_remaining"] == [False, False, False]
    assert error is None, "download sent success headers but failed before opening the retained path"
    assert received == expected
    assert observation["body_complete"]


@pytest.mark.parametrize("use_range", [False, True])
def test_real_http_transfer_completes_when_retention_unlinks_at_headers(evidence, use_range):
    """Exercise the socket/server/client path, with a deterministic retention barrier."""
    purges = []

    async def application(scope, receive, send):
        assert scope["type"] == "http"
        with evidence.session_scope() as session:
            response = routes.download_artifact_by_id(evidence.artifact_id, session)

        async def retain_at_headers(message):
            if message["type"] == "http.response.start":
                purges.append(retention._purge_old_evidence_artifacts(90))
            await send(message)

        await response(scope, receive, retain_at_headers)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(application, lifespan="off", access_log=False, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        headers = {"Range": "bytes=1-180000"} if use_range else {}
        response = httpx.get(f"http://127.0.0.1:{port}/download", headers=headers, timeout=5, trust_env=False)
        expected = evidence.payload[1:180001] if use_range else evidence.payload
        assert response.status_code == (206 if use_range else 200)
        assert response.content == expected
        assert int(response.headers["content-length"]) == len(expected)
        assert response.headers["x-astinuslab-sha256"] == evidence.digest
        assert response.headers["x-astinuslab-size"] == str(len(evidence.payload))
        assert purges == [(1, 0)]
        assert not any(path.exists() for path in evidence.paths)
        with evidence.sessions() as session:
            assert session.get(models.Artifact, evidence.artifact_id) is None
            assert session.query(models.EvidenceTombstone).one().cleanup_state == "complete"
    finally:
        server.should_exit = True
        thread.join(5)
        listener.close()
        assert not thread.is_alive()
