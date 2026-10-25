import time

import pytest
from httpx import ASGITransport, AsyncClient

from astinuslab.db import Base, engine, initialize_database, session_scope
from astinuslab.main import app


@pytest.fixture(autouse=True)
def _db():
    initialize_database()
    with engine.begin() as conn:
        for t in reversed(Base.metadata.sorted_tables):
            conn.execute(t.delete())
    yield
    with engine.begin() as conn:
        for t in reversed(Base.metadata.sorted_tables):
            conn.execute(t.delete())


@pytest.mark.asyncio
async def test_F04_sse_terminates_on_timeout(monkeypatch):
    # Insert a placeholder acquisition row.
    from astinuslab.db.models import Acquisition

    with session_scope() as s:
        s.add(Acquisition(id="acq-stream", name="x", status="queued"))

    from astinuslab.core import config as config_mod

    monkeypatch.setattr(
        config_mod,
        "settings",
        config_mod.settings.model_copy(update={"playground_capture_stream_timeout_seconds": 1}),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        start = time.monotonic()
        async with client.stream("GET", "/api/v1/playground/captures/acq-stream/events") as r:
            chunks = []
            async for chunk in r.aiter_text():
                chunks.append(chunk)
                if "stream_timeout" in chunk:
                    break
        elapsed = time.monotonic() - start
        assert elapsed < 5
        assert any("stream_timeout" in c for c in chunks)


@pytest.mark.asyncio
async def test_F04_sse_is_not_compressed(monkeypatch):
    """Compression is registered app-wide, and a gzip window would buffer the
    capture events the Playground renders as they happen. Starlette excludes
    `text/event-stream` by content type; this pins that the route's media type
    actually lands in that exclusion."""
    from astinuslab.db.models import Acquisition

    with session_scope() as s:
        s.add(Acquisition(id="acq-nogzip", name="x", status="queued"))

    from astinuslab.core import config as config_mod

    monkeypatch.setattr(
        config_mod,
        "settings",
        config_mod.settings.model_copy(update={"playground_capture_stream_timeout_seconds": 1}),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream(
            "GET",
            "/api/v1/playground/captures/acq-nogzip/events",
            headers={"Accept-Encoding": "gzip"},
        ) as r:
            assert r.headers["content-type"].startswith("text/event-stream")
            assert "content-encoding" not in r.headers
            async for chunk in r.aiter_text():
                if "stream_timeout" in chunk:
                    break
