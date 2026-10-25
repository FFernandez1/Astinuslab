"""Descriptor-owned downloads for immutable evidence on the supported Linux host.

Callers validate storage containment before constructing a response. We retain
its file identity, open without following links before success headers, and own
that descriptor through streaming. Retention may unlink the name meanwhile.
Starlette continues to own range parsing, conditional ranges and stat headers;
the three streaming hooks deliberately never use its pathname/pathsend branch.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from secrets import token_hex
from threading import Lock

import anyio
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, JSONResponse
from starlette.types import Receive, Scope, Send


class PinnedFileResponse(FileResponse):
    def __init__(
        self,
        path: str | Path,
        *,
        filename: str,
        media_type: str = "application/octet-stream",
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._source = Path(path).absolute()
        try:
            self._expected = self._source.stat(follow_symlinks=False)
            if not stat.S_ISREG(self._expected.st_mode):
                raise OSError("not a regular file")
        except OSError as exc:
            raise HTTPException(404, "artifact file is not available") from exc
        self._descriptor: int | None = None
        self._ownership = Lock()
        self._closed = False
        self._readers = 0
        super().__init__(
            path=self._source, filename=filename, media_type=media_type, headers=headers, stat_result=self._expected
        )

    def _open_verified(self) -> int:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        parent = os.open(self._source.anchor, directory_flags)
        try:
            for component in self._source.parts[1:-1]:
                child = os.open(component, directory_flags, dir_fd=parent)
                os.close(parent)
                parent = child
            descriptor = os.open(
                self._source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent
            )
        finally:
            os.close(parent)
        try:
            opened = os.fstat(descriptor)
            expected = self._expected
            if not stat.S_ISREG(opened.st_mode) or (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
            ) != (expected.st_dev, expected.st_ino, expected.st_size, expected.st_mtime_ns):
                raise HTTPException(409, "artifact file changed before download")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            try:
                await anyio.to_thread.run_sync(self._open_for_transfer)
            except (HTTPException, OSError) as exc:
                code = (
                    exc.status_code
                    if isinstance(exc, HTTPException)
                    else (403 if exc.errno in {errno.EACCES, errno.EPERM} else 404)
                )
                await JSONResponse({"detail": "artifact file is not available"}, status_code=code)(scope, receive, send)
                return
            # Uvicorn's ASGI 2.3 send may silently ignore a disconnected client.
            # Listen as well, so a canceled transfer does not read the whole dump.
            failure: Exception | None = None
            async with anyio.create_task_group() as tasks:

                async def disconnected() -> None:
                    while True:
                        if (await receive())["type"] == "http.disconnect":
                            tasks.cancel_scope.cancel()
                            return
                        await anyio.lowlevel.checkpoint()

                tasks.start_soon(disconnected)
                try:
                    await super().__call__(scope, receive, send)
                except Exception as exc:
                    failure = exc
                finally:
                    tasks.cancel_scope.cancel()
            if failure is not None:
                raise failure
        finally:
            self._close()

    def _open_for_transfer(self) -> None:
        # A raw task cancellation can abandon a thread's returned value. Give
        # ownership to the response from inside the worker, or close locally if
        # the response has already exited. Never hold the lock over filesystem I/O.
        descriptor = self._open_verified()
        with self._ownership:
            closed = self._closed
            if not closed:
                self._descriptor = descriptor
        if closed:
            os.close(descriptor)

    def _close(self) -> None:
        with self._ownership:
            self._closed = True
            descriptor = None
            if not self._readers:
                descriptor, self._descriptor = self._descriptor, None
        if descriptor is not None:
            os.close(descriptor)

    def _read(self, size: int, offset: int) -> bytes:
        # A canceled response must not close and recycle an FD still being used
        # by a worker. A queued read refuses; an admitted read owns a short lease.
        with self._ownership:
            if self._closed or self._descriptor is None:
                raise RuntimeError("download has no open file")
            descriptor = self._descriptor
            self._readers += 1
        try:
            return os.pread(descriptor, size, offset)
        finally:
            with self._ownership:
                self._readers -= 1
                close_descriptor = None
                if self._closed and not self._readers:
                    close_descriptor, self._descriptor = self._descriptor, None
            if close_descriptor is not None:
                os.close(close_descriptor)

    async def _send_bytes(self, send: Send, start: int, end: int) -> None:
        while start < end:
            chunk = await anyio.to_thread.run_sync(self._read, min(self.chunk_size, end - start), start)
            if not chunk:
                raise OSError("artifact was truncated during download")
            start += len(chunk)
            await send({"type": "http.response.body", "body": chunk, "more_body": True})

    async def _handle_simple(self, send: Send, send_header_only: bool, send_pathsend: bool) -> None:
        await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
        if not send_header_only:
            await self._send_bytes(send, 0, self._expected.st_size)
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def _handle_single_range(
        self, send: Send, start: int, end: int, file_size: int, send_header_only: bool
    ) -> None:
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers["content-range"] = f"bytes {start}-{end - 1}/{file_size}"
        headers["content-length"] = str(end - start)
        await send({"type": "http.response.start", "status": 206, "headers": headers.raw})
        if not send_header_only:
            await self._send_bytes(send, start, end)
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def _handle_multiple_ranges(
        self, send: Send, ranges: list[tuple[int, int]], file_size: int, send_header_only: bool
    ) -> None:
        boundary = token_hex(13)
        length, part_header = self.generate_multipart(ranges, boundary, file_size, self.headers["content-type"])
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers["content-type"] = f"multipart/byteranges; boundary={boundary}"
        headers["content-length"] = str(length)
        await send({"type": "http.response.start", "status": 206, "headers": headers.raw})
        if send_header_only:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        for start, end in ranges:
            await send({"type": "http.response.body", "body": part_header(start, end), "more_body": True})
            await self._send_bytes(send, start, end)
            await send({"type": "http.response.body", "body": b"\r\n", "more_body": True})
        await send({"type": "http.response.body", "body": f"--{boundary}--".encode("ascii"), "more_body": False})
