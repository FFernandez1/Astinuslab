from __future__ import annotations

import io
import socket
import ssl
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from astinuslab.api.schemas import ImageDownloadConfig
from astinuslab.images import safe_fetch


def _config(**updates: Any) -> ImageDownloadConfig:
    values: dict[str, Any] = {
        "source_policy": "unrestricted",
        "network_policy": "safe",
        "connect_timeout_seconds": 3,
        "read_timeout_seconds": 7,
        "max_size_gb": 1,
    }
    values.update(updates)
    return ImageDownloadConfig(**values)


def _dns(monkeypatch: pytest.MonkeyPatch, answers: dict[str, list[str]]) -> list[tuple[str, int]]:
    calls: list[tuple[str, int]] = []

    def fake_getaddrinfo(host: str, port: int, **_kwargs: Any):
        calls.append((host, port))
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (address, port, 0, 0) if ":" in address else (address, port),
            )
            for address in answers[host]
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    return calls


class FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        chunks: list[bytes | BaseException] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._chunks = list(chunks or [b""])
        self.closed = False

    def read(self, _size: int = -1) -> bytes:
        item = self._chunks.pop(0) if self._chunks else b""
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


def test_public_redirect_to_loopback_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _dns(monkeypatch, {"public.example": ["93.184.216.34"], "localhost": ["127.0.0.1"]})
    monkeypatch.setattr(
        safe_fetch,
        "open_pinned_response",
        lambda target, **_kwargs: FakeResponse(status=302, headers={"Location": "http://localhost/image.qcow2"}),
    )

    with pytest.raises(HTTPException, match="blocked"):
        safe_fetch.stream_download("http://public.example/start", tmp_path / "image", _config(), lambda *_: None)


@pytest.mark.parametrize("blocked_address", ["10.0.0.8", "169.254.169.254"])
def test_redirect_to_blocked_network_is_revalidated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, blocked_address: str
) -> None:
    _dns(monkeypatch, {"public.example": ["93.184.216.34"], "blocked.example": [blocked_address]})
    monkeypatch.setattr(
        safe_fetch,
        "open_pinned_response",
        lambda target, **_kwargs: FakeResponse(status=302, headers={"Location": "http://blocked.example/image.qcow2"}),
    )

    with pytest.raises(HTTPException, match="blocked"):
        safe_fetch.stream_download("http://public.example/start", tmp_path / "image", _config(), lambda *_: None)


@pytest.mark.parametrize("address", ["10.0.0.8", "172.20.1.2", "192.168.1.2"])
def test_rfc1918_targets_are_rejected(monkeypatch: pytest.MonkeyPatch, address: str) -> None:
    _dns(monkeypatch, {"images.example": [address]})

    with pytest.raises(HTTPException, match="blocked"):
        safe_fetch.resolve_and_validate("https://images.example/a.qcow2", _config())


def test_metadata_target_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"metadata.example": ["169.254.169.254"]})

    with pytest.raises(HTTPException, match="blocked"):
        safe_fetch.resolve_and_validate("http://metadata.example/latest/meta-data", _config())


def test_dns_public_to_private_rebinding_cannot_change_pinned_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _dns(monkeypatch, {"images.example": ["93.184.216.34"]})
    target = safe_fetch.resolve_and_validate("http://images.example/a.qcow2", _config())[0]
    sent: list[bytes] = []
    connections: list[tuple[tuple[str, int], float]] = []

    class FakeSocket:
        def settimeout(self, timeout: float) -> None:
            assert timeout == 3

        def connect(self, address: tuple[str, int]) -> None:
            connections.append((address, 3))

        def getpeername(self):
            return ("93.184.216.34", 80)

        def makefile(self, *_args: Any, **_kwargs: Any):
            return io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")

        def sendall(self, data: bytes) -> None:
            sent.append(data)

        def close(self) -> None:
            pass

    def forbidden_create_connection(*_args: Any, **_kwargs: Any):
        raise AssertionError("pinned transport must use an explicit socket without resolving again")

    monkeypatch.setattr(socket, "create_connection", forbidden_create_connection)
    monkeypatch.setattr(socket, "socket", lambda *_args: FakeSocket())
    response = safe_fetch.open_pinned_response(target, connect_timeout=3)
    response.close()

    assert calls == [("images.example", 80)]
    assert connections == [(("93.184.216.34", 80), 3)]
    assert b"Host: images.example\r\n" in b"".join(sent)


def test_response_headers_use_read_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    target = safe_fetch.ValidatedTarget(
        url="http://images.example/a",
        scheme="http",
        hostname="images.example",
        port=80,
        peer_ip=safe_fetch.ipaddress.ip_address("93.184.216.34"),
        request_target="/a",
    )

    class FakeSocket:
        timeout = 0.0

        def settimeout(self, timeout: float) -> None:
            self.timeout = timeout

        def connect(self, _address: tuple[str, int]) -> None:
            assert self.timeout == 3

        def getpeername(self):
            return ("93.184.216.34", 80)

        def makefile(self, *_args: Any, **_kwargs: Any):
            assert self.timeout == 7
            return io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")

        def sendall(self, _data: bytes) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(socket, "socket", lambda *_args: FakeSocket())

    response = safe_fetch.open_pinned_response(target, connect_timeout=3, read_timeout=7)
    response.close()


def test_mixed_public_private_answers_reject_entire_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _dns(monkeypatch, {"images.example": ["93.184.216.34", "fd00::12"]})

    with pytest.raises(HTTPException, match="fd00::12.*blocked"):
        safe_fetch.resolve_and_validate("https://images.example/a.qcow2", _config())

    assert calls == [("images.example", 443)]


def test_connection_falls_back_to_next_validated_address(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _dns(monkeypatch, {"images.example": ["2001:4860:4860::8888", "93.184.216.34"]})
    attempts: list[str] = []

    def connect(target, **_kwargs):
        attempts.append(str(target.peer_ip))
        if target.peer_ip.version == 6:
            raise OSError("IPv6 route unavailable")
        return FakeResponse(chunks=[b"image", b""])

    monkeypatch.setattr(safe_fetch, "open_pinned_response", connect)

    safe_fetch.stream_download("http://images.example/a", tmp_path / "image", _config(), lambda *_: None)

    assert attempts == ["2001:4860:4860::8888", "93.184.216.34"]
    assert (tmp_path / "image").read_bytes() == b"image"


def test_stream_download_exclusively_creates_destination_without_truncating_existing_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _dns(monkeypatch, {"images.example": ["93.184.216.34"]})
    destination = tmp_path / "payload.part"
    destination.write_bytes(b"other-operation")
    response = FakeResponse(chunks=[b"new-image", b""])
    monkeypatch.setattr(safe_fetch, "open_pinned_response", lambda target, **_kwargs: response)

    with pytest.raises(FileExistsError):
        safe_fetch.stream_download("http://images.example/a", destination, _config(), lambda *_: None)

    assert destination.read_bytes() == b"other-operation"


def test_resolved_peer_is_typed_ip_address(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"images.example": ["93.184.216.34"]})

    target = safe_fetch.resolve_and_validate("https://images.example/a.qcow2", _config())[0]

    assert isinstance(target.peer_ip, safe_fetch.ipaddress.IPv4Address)


def test_redirect_loop_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _dns(monkeypatch, {"images.example": ["93.184.216.34"]})
    monkeypatch.setattr(
        safe_fetch,
        "open_pinned_response",
        lambda target, **_kwargs: FakeResponse(status=302, headers={"Location": "/loop"}),
    )

    with pytest.raises(RuntimeError, match="redirect loop"):
        safe_fetch.stream_download("http://images.example/loop", tmp_path / "image", _config(), lambda *_: None)


def test_more_than_five_redirect_hops_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _dns(monkeypatch, {"images.example": ["93.184.216.34"]})

    def redirect(target, **_kwargs):
        hop = int(target.request_target.removeprefix("/hop/"))
        return FakeResponse(status=302, headers={"Location": f"/hop/{hop + 1}"})

    monkeypatch.setattr(safe_fetch, "open_pinned_response", redirect)

    with pytest.raises(RuntimeError, match="more than 5 redirects"):
        safe_fetch.stream_download("http://images.example/hop/0", tmp_path / "image", _config(), lambda *_: None)


def test_https_hostname_mismatch_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    target = safe_fetch.ValidatedTarget(
        url="https://images.example/a",
        scheme="https",
        hostname="images.example",
        port=443,
        peer_ip=safe_fetch.ipaddress.ip_address("93.184.216.34"),
        request_target="/a",
    )

    class FakeSocket:
        def settimeout(self, _timeout: float) -> None:
            pass

        def connect(self, _address: tuple[str, int]) -> None:
            pass

        def getpeername(self):
            return ("93.184.216.34", 443)

        def close(self) -> None:
            pass

    class FakeContext:
        def wrap_socket(self, sock, *, server_hostname: str):
            assert server_hostname == "images.example"
            raise ssl.SSLCertVerificationError("hostname mismatch")

    monkeypatch.setattr(socket, "socket", lambda *_args: FakeSocket())
    monkeypatch.setattr(ssl, "create_default_context", lambda: FakeContext())

    with pytest.raises(ssl.SSLCertVerificationError, match="hostname mismatch"):
        safe_fetch.open_pinned_response(target, connect_timeout=3)


def test_connected_peer_different_from_pinned_address_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    target = safe_fetch.ValidatedTarget(
        url="http://images.example/a",
        scheme="http",
        hostname="images.example",
        port=80,
        peer_ip=safe_fetch.ipaddress.ip_address("93.184.216.34"),
        request_target="/a",
    )

    class FakeSocket:
        def settimeout(self, _timeout: float) -> None:
            pass

        def connect(self, _address: tuple[str, int]) -> None:
            pass

        def getpeername(self):
            return ("93.184.216.35", 80)

        def close(self) -> None:
            pass

    monkeypatch.setattr(socket, "socket", lambda *_args: FakeSocket())

    with pytest.raises(RuntimeError, match="connected peer.*pinned"):
        safe_fetch.open_pinned_response(target, connect_timeout=3)


def test_oversized_content_length_is_rejected_before_body_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _dns(monkeypatch, {"images.example": ["93.184.216.34"]})
    response = FakeResponse(headers={"Content-Length": str(2 * 1024**3)}, chunks=[AssertionError("body read")])
    monkeypatch.setattr(safe_fetch, "open_pinned_response", lambda target, **_kwargs: response)

    with pytest.raises(RuntimeError, match="configured limit"):
        safe_fetch.stream_download("http://images.example/a", tmp_path / "image", _config(), lambda *_: None)

    assert response.closed


@pytest.mark.parametrize("content_length", ["-1", "+1", "1_0", "not-a-number"])
def test_invalid_content_length_is_rejected_before_body_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content_length: str
) -> None:
    _dns(monkeypatch, {"images.example": ["93.184.216.34"]})
    response = FakeResponse(headers={"Content-Length": content_length}, chunks=[AssertionError("body read")])
    monkeypatch.setattr(safe_fetch, "open_pinned_response", lambda target, **_kwargs: response)

    with pytest.raises(RuntimeError, match="invalid Content-Length"):
        safe_fetch.stream_download("http://images.example/a", tmp_path / "image", _config(), lambda *_: None)

    assert response.closed


def test_stream_without_content_length_cannot_exceed_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _dns(monkeypatch, {"images.example": ["93.184.216.34"]})
    response = FakeResponse(chunks=[b"12345", b"67890", b""])
    monkeypatch.setattr(safe_fetch, "open_pinned_response", lambda target, **_kwargs: response)
    config = _config(max_size_gb=0)

    with pytest.raises(RuntimeError, match="configured limit"):
        safe_fetch.stream_download("http://images.example/a", tmp_path / "image", config, lambda *_: None)


def test_read_timeout_uses_independent_deadline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _dns(monkeypatch, {"images.example": ["93.184.216.34"]})
    read_timeouts: list[float] = []

    class TimedOutResponse(FakeResponse):
        def set_read_timeout(self, timeout: float) -> None:
            read_timeouts.append(timeout)

    response = TimedOutResponse(chunks=[TimeoutError("read timed out")])
    monkeypatch.setattr(safe_fetch, "open_pinned_response", lambda target, **_kwargs: response)

    with pytest.raises(TimeoutError, match="read timed out"):
        safe_fetch.stream_download("http://images.example/a", tmp_path / "image", _config(), lambda *_: None)

    assert read_timeouts == [7]


def test_url_credentials_are_rejected_before_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _dns(monkeypatch, {"images.example": ["93.184.216.34"]})

    with pytest.raises(HTTPException, match="credentials"):
        safe_fetch.resolve_and_validate("https://user:secret@images.example/a", _config())

    assert calls == []


def test_explicit_zero_port_is_rejected_before_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _dns(monkeypatch, {"images.example": ["93.184.216.34"]})

    with pytest.raises(HTTPException, match="port"):
        safe_fetch.resolve_and_validate("http://images.example:0/a", _config())

    assert calls == []


def test_download_image_bg_preserves_state_and_post_download_hooks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from astinuslab import catalog
    from astinuslab.images import image_admin_support, image_inspection

    destination = tmp_path / "image.qcow2"
    state = {
        "status": "running",
        "bytes_done": 0,
        "total": None,
        "error": None,
    }
    operation_id = "11111111111111111111111111111111"
    monkeypatch.setitem(image_admin_support.URL_DOWNLOADS, operation_id, state)
    hooks: list[object] = []

    def fake_stream_download(url, destination, config, progress_callback):
        assert url == "https://images.example/image.qcow2"
        destination.write_bytes(b"image-data")
        progress_callback(10, 10)

    monkeypatch.setattr(image_admin_support, "stream_download", fake_stream_download)
    monkeypatch.setattr(catalog, "invalidate_catalog_caches", lambda: hooks.append("invalidate"))
    monkeypatch.setattr(image_inspection, "inspect_image_bg", lambda path, *, force: hooks.append((path, force)))

    image_admin_support.download_image_bg(operation_id, "https://images.example/image.qcow2", destination, _config())

    assert destination.read_bytes() == b"image-data"
    assert state == {
        "status": "done",
        "bytes_done": 10,
        "total": 10,
        "error": None,
        "filename": destination.name,
    }
    assert hooks == ["invalidate", (destination, True)]


def test_upload_and_download_racing_for_same_final_name_publish_only_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from astinuslab.images import image_admin_support

    destination = tmp_path / "same.qcow2"
    upload = image_admin_support.ImageStagingOperation.create(tmp_path)
    with upload.payload_path.open("xb") as output:
        output.write(b"upload-image")
    state = {"status": "running", "bytes_done": 0, "total": None, "error": None}
    operation_id = "22222222222222222222222222222222"
    monkeypatch.setitem(image_admin_support.URL_DOWNLOADS, operation_id, state)
    barrier = threading.Barrier(2)

    def fake_stream_download(_url, staged, _config, progress_callback):
        with staged.open("xb") as output:
            output.write(b"download-image")
        progress_callback(14, 14)
        barrier.wait(timeout=5)

    monkeypatch.setattr(image_admin_support, "stream_download", fake_stream_download)
    monkeypatch.setattr("astinuslab.catalog.invalidate_catalog_caches", lambda: None)
    monkeypatch.setattr("astinuslab.images.image_inspection.inspect_image_bg", lambda *_args, **_kwargs: None)
    thread = threading.Thread(
        target=image_admin_support.download_image_bg,
        args=(operation_id, "https://images.example/same.qcow2", destination, _config()),
    )
    thread.start()
    barrier.wait(timeout=5)
    upload_status = 303
    try:
        upload.publish(upload.payload_path, destination)
    except HTTPException as exc:
        upload_status = exc.status_code
    finally:
        upload.cleanup()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert sorted([upload_status, 303 if state["status"] == "done" else 409]) == [303, 409]
    assert destination.read_bytes() in {b"upload-image", b"download-image"}
    assert list((tmp_path / ".staging").iterdir()) == []


def test_background_download_duplicate_enters_error_state_without_changing_existing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from astinuslab.images import image_admin_support

    destination = tmp_path / "same.qcow2"
    destination.write_bytes(b"existing-image")
    state = {"status": "running", "bytes_done": 0, "total": None, "error": None}
    operation_id = "33333333333333333333333333333333"
    monkeypatch.setitem(image_admin_support.URL_DOWNLOADS, operation_id, state)

    def fake_stream_download(_url, staged, _config, progress_callback):
        with staged.open("xb") as output:
            output.write(b"download-image")
        progress_callback(14, 14)

    monkeypatch.setattr(image_admin_support, "stream_download", fake_stream_download)

    image_admin_support.download_image_bg(operation_id, "https://images.example/same.qcow2", destination, _config())

    assert state["status"] == "error"
    assert "already exists" in str(state["error"])
    assert destination.read_bytes() == b"existing-image"
    assert list((tmp_path / ".staging").iterdir()) == []


def test_safe_policy_rejects_https_to_http_downgrade(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _dns(monkeypatch, {"images.example": ["93.184.216.34"]})
    monkeypatch.setattr(
        safe_fetch,
        "open_pinned_response",
        lambda target, **_kwargs: FakeResponse(status=302, headers={"Location": "http://images.example/a"}),
    )

    with pytest.raises(RuntimeError, match="HTTPS to HTTP"):
        safe_fetch.stream_download("https://images.example/start", tmp_path / "image", _config(), lambda *_: None)
