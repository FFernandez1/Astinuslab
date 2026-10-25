"""Pinned file lifetime and compatibility with the locked Starlette response."""

import asyncio
import errno
import os
import threading

import pytest

from astinuslab.storage.downloads import PinnedFileResponse


@pytest.fixture
def download(tmp_path, monkeypatch):
    path = tmp_path / "artifact.bin"
    payload = b"synthetic transfer bytes\n" * 10000
    path.write_bytes(payload)
    response = PinnedFileResponse(path, filename="evidence.bin", headers={"X-Astinuslab-SHA256": "synthetic"})
    opened = []
    original = response._open_verified

    def observe():
        descriptor = original()
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(response, "_open_verified", observe)
    return path, payload, response, opened


def assert_closed(opened):
    assert opened
    for descriptor in opened:
        with pytest.raises(OSError) as error:
            os.fstat(descriptor)
        assert error.value.errno == errno.EBADF


def consume(response, *, method="GET", headers=(), on_send=None, extensions=None):
    async def run():
        messages = []

        async def send(message):
            messages.append(dict(message))
            if on_send:
                await on_send(message)

        async def receive():
            await asyncio.Event().wait()

        await response(
            {"type": "http", "method": method, "headers": list(headers), "extensions": extensions or {}}, receive, send
        )
        return messages

    return asyncio.run(run())


@pytest.mark.parametrize(
    "method,range_value,expected_status",
    [
        ("GET", None, 200),
        ("HEAD", None, 200),
        ("HEAD", "bytes=2-19", 206),
        ("GET", "bytes=2-19", 206),
        ("GET", "bytes=-20", 206),
        ("GET", "bytes=2-19,100-120", 206),
        ("GET", "invalid", 400),
        ("GET", "bytes=999999999-", 416),
    ],
)
def test_ranges_headers_head_and_descriptor_lifetime(download, method, range_value, expected_status):
    path, payload, response, opened = download

    async def unlink_after_admission(message):
        if message["type"] == "http.response.start":
            path.unlink()

    messages = consume(
        response,
        method=method,
        headers=() if range_value is None else [(b"range", range_value.encode())],
        on_send=unlink_after_admission,
        extensions={"http.response.pathsend": {}},
    )
    assert messages[0]["status"] == expected_status
    body = b"".join(message.get("body", b"") for message in messages)
    assert not any(message["type"] == "http.response.pathsend" for message in messages)
    assert messages[-1].get("more_body", False) is False
    if method == "HEAD":
        assert not body
    elif range_value is None:
        assert body == payload
    elif range_value == "bytes=2-19":
        assert body == payload[2:20]
    elif range_value == "bytes=-20":
        assert body == payload[-20:]
    elif expected_status == 206:
        assert payload[2:20] in body and payload[100:121] in body
        assert len(body) == int(dict(messages[0]["headers"])[b"content-length"])
    if expected_status in {200, 206}:
        headers = dict(messages[0]["headers"])
        assert headers[b"x-astinuslab-sha256"] == b"synthetic"
        assert headers[b"accept-ranges"] == b"bytes"
        assert b"evidence.bin" in headers[b"content-disposition"]
    assert_closed(opened)


@pytest.mark.parametrize("value,partial", [("match", True), ("different", False)])
def test_if_range_uses_verified_metadata(download, value, partial):
    _path, payload, response, opened = download
    if_range = response.headers["etag"] if value == "match" else '"other"'
    messages = consume(response, headers=[(b"range", b"bytes=0-9"), (b"if-range", if_range.encode())])
    assert messages[0]["status"] == (206 if partial else 200)
    assert b"".join(message.get("body", b"") for message in messages) == (payload[:10] if partial else payload)
    assert_closed(opened)


@pytest.mark.parametrize("replacement", ["missing", "file", "link", "parent-link", "fifo"])
def test_missing_or_replaced_path_refuses_before_success(download, replacement):
    path, _payload, response, _opened = download
    if replacement == "parent-link":
        moved = path.parent.with_name(path.parent.name + "-moved")
        path.parent.rename(moved)
        path.parent.symlink_to(moved, target_is_directory=True)
    else:
        path.unlink()
        if replacement == "file":
            path.write_bytes(b"replacement")
        elif replacement == "link":
            outside = path.parent / "outside"
            outside.write_bytes(b"must not serve")
            path.symlink_to(outside)
        elif replacement == "fifo":
            os.mkfifo(path)
    messages = consume(response)
    assert messages[0]["status"] in {404, 409}
    assert response._descriptor is None
    assert b"replacement" not in b"".join(message.get("body", b"") for message in messages)


def test_send_failure_closes_open_file(download):
    _path, _payload, response, opened = download

    async def broken_client(message):
        if message["type"] == "http.response.body":
            raise OSError("synthetic client disconnect")

    with pytest.raises(OSError, match="synthetic client disconnect"):
        consume(response, on_send=broken_client)
    assert_closed(opened)


def test_asgi_disconnect_stops_reads_and_closes_file(download):
    _path, payload, response, opened = download

    async def run():
        sent = []
        interrupted = asyncio.Event()

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                sent.append(message["body"])
                interrupted.set()
                await asyncio.sleep(0)

        async def receive():
            await interrupted.wait()
            return {"type": "http.disconnect"}

        await response({"type": "http", "method": "GET", "headers": []}, receive, send)
        assert 0 < sum(map(len, sent)) < len(payload)

    asyncio.run(run())
    assert_closed(opened)


def test_task_cancellation_closes_file_during_stream(download):
    _path, _payload, response, opened = download

    async def run():
        streaming = asyncio.Event()

        async def send(message):
            if message["type"] == "http.response.body":
                streaming.set()
                await asyncio.Event().wait()

        async def receive():
            await asyncio.Event().wait()

        task = asyncio.create_task(response({"type": "http", "method": "GET", "headers": []}, receive, send))
        await asyncio.wait_for(streaming.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert_closed(opened)


@pytest.mark.parametrize("operation", ["open", "read"])
def test_task_cancellation_preserves_worker_ownership(download, monkeypatch, operation):
    """Raw asyncio cancellation can abandon a running AnyIO worker's result."""
    _path, payload, response, opened = download
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    read_results = []
    original_open, original_read = response._open_verified, os.pread

    def blocked(*args):
        # Open is blocked at handoff; read is blocked after borrowing the FD.
        descriptor = original_open() if operation == "open" else None
        entered.set()
        assert release.wait(5)
        try:
            if operation == "open":
                return descriptor
            result = original_read(*args)
            read_results.append(result)
            return result
        finally:
            finished.set()

    if operation == "open":
        monkeypatch.setattr(response, "_open_verified", blocked)
    else:
        monkeypatch.setattr(os, "pread", blocked)

    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.001)

    async def run():
        async def send(_message):
            pass

        async def receive():
            await asyncio.Event().wait()

        task = asyncio.create_task(response({"type": "http", "method": "GET", "headers": []}, receive, send))
        try:
            await wait_for(entered.is_set)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # Until the pending operation releases it, the FD cannot be reused.
            assert os.fstat(opened[0]).st_size == len(payload)
        finally:
            release.set()
        await wait_for(finished.is_set)

        def is_closed():
            try:
                os.fstat(opened[0])
            except OSError as exc:
                return exc.errno == errno.EBADF
            return False

        await wait_for(is_closed)
        assert response._descriptor is None

    asyncio.run(run())
    if operation == "read":
        assert read_results == [payload[: response.chunk_size]]
    assert_closed(opened)
