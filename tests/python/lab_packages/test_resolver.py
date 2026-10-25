from __future__ import annotations

import hashlib
import io
import socket
import threading
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from astinuslab.lab_packages.content_store import ContentStore
from astinuslab.lab_packages.models import AssetReference
from astinuslab.lab_packages.resolver import (
    AssetResolutionError,
    AssetResolver,
    ResolverPolicy,
    _BoundedResponse,
    resolve_asset,
)


def _reference(payload: bytes, *, sources: tuple[str, ...] = (), embedded: bool = False) -> AssetReference:
    return AssetReference(
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        media_type="application/octet-stream",
        sources=sources,
        embedded=embedded,
        license="CC0-1.0",
        redistribution_confirmed=True,
    )


class _Handler(BaseHTTPRequestHandler):
    response_map: dict[str, tuple[int, bytes, dict[str, str]]] = {}
    requests: list[str] = []
    ranges: list[str | None] = []

    def do_GET(self) -> None:  # noqa: N802
        self.__class__.requests.append(self.path)
        status, body, headers = self.response_map.get(self.path, (404, b"missing", {}))
        range_header = self.headers.get("Range")
        self.__class__.ranges.append(range_header)
        headers = dict(headers)
        if range_header and status == 200 and range_header.startswith("bytes="):
            start = int(range_header.removeprefix("bytes=").removesuffix("-"))
            body = body[start:]
            status = 206
            headers["Content-Range"] = f"bytes {start}-{start + len(body) - 1}/{start + len(body)}"
            headers["Content-Length"] = str(len(body))
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


@pytest.fixture
def http_server():
    _Handler.response_map = {}
    _Handler.requests = []
    _Handler.ranges = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_embedded_bytes_are_verified_and_published_without_network(tmp_path: Path, http_server: str):
    payload = b"embedded forensic image"
    reference = _reference(payload, sources=(f"{http_server}/should-not-fetch",), embedded=True)
    store = ContentStore(tmp_path / "cache")

    result = resolve_asset(reference, embedded=payload, store=store)

    assert result.source_kind == "embedded"
    assert result.insecure_transport is False
    assert result.read() == payload
    assert _Handler.requests == []


def test_cache_is_used_before_sources(tmp_path: Path, http_server: str):
    payload = b"cached forensic image"
    reference = _reference(payload, sources=(f"{http_server}/should-not-fetch",))
    store = ContentStore(tmp_path / "cache")
    store.put(payload, sha256=reference.sha256, size_bytes=reference.size_bytes)

    result = AssetResolver(store).resolve(reference)

    assert result.source_kind == "cache"
    assert result.from_cache is True
    assert _Handler.requests == []


def test_sources_are_tried_in_declared_order_and_fallback_on_failure(tmp_path: Path, http_server: str):
    payload = b"source payload"
    _Handler.response_map["/bad"] = (200, b"wrong", {"Content-Length": "5"})
    _Handler.response_map["/good"] = (200, payload, {"Content-Length": str(len(payload))})
    reference = _reference(payload, sources=(f"{http_server}/bad", f"{http_server}/good"))

    result = AssetResolver(
        ContentStore(tmp_path / "cache"), policy=ResolverPolicy(allow_http=True, allow_private_network=True)
    ).resolve(reference)

    assert result.source_kind == "source"
    assert result.source == f"{http_server}/good"
    assert _Handler.requests == ["/bad", "/good"]


def test_embedded_mismatch_falls_back_with_diagnostic(tmp_path: Path):
    expected = b"expected"
    reference = _reference(expected, embedded=True)
    store = ContentStore(tmp_path / "cache")
    store.put(expected, sha256=reference.sha256, size_bytes=reference.size_bytes)

    result = AssetResolver(store).resolve(reference, embedded=b"wrong")

    assert result.source_kind == "cache-fallback"
    assert result.read() == expected
    assert any(item.candidate == "embedded" for item in result.diagnostics)


def test_missing_embedded_payload_can_fall_back_to_declared_source(tmp_path: Path, http_server: str):
    payload = b"fallback source"
    _Handler.response_map["/asset"] = (200, payload, {"Content-Length": str(len(payload))})
    reference = _reference(payload, sources=(f"{http_server}/asset",), embedded=True)

    result = AssetResolver(
        ContentStore(tmp_path / "cache"), policy=ResolverPolicy(allow_http=True, allow_private_network=True)
    ).resolve(reference)

    assert result.source_kind == "source-fallback"
    assert any(item.candidate == "embedded" for item in result.diagnostics)


def test_missing_embedded_payload_can_be_made_hard_failure_by_policy(tmp_path: Path):
    reference = _reference(b"expected", embedded=True)
    policy = ResolverPolicy(allow_missing_embedded_fallback=False)

    with pytest.raises(AssetResolutionError, match="embedded payload is missing"):
        AssetResolver(ContentStore(tmp_path / "cache"), policy=policy).resolve(reference)


def test_http_requires_explicit_policy_and_is_disclosed(tmp_path: Path, http_server: str):
    payload = b"plain HTTP payload"
    _Handler.response_map["/asset"] = (200, payload, {"Content-Length": str(len(payload))})
    reference = _reference(payload, sources=(f"{http_server}/asset",))
    store = ContentStore(tmp_path / "cache")

    with pytest.raises(AssetResolutionError, match="HTTPS"):
        AssetResolver(store).resolve(reference)

    result = AssetResolver(store, policy=ResolverPolicy(allow_http=True, allow_private_network=True)).resolve(reference)
    assert result.insecure_transport is True


def test_content_length_and_digest_errors_are_aggregated(tmp_path: Path, http_server: str):
    payload = b"expected content"
    _Handler.response_map["/short"] = (200, payload, {"Content-Length": "1"})
    reference = _reference(payload, sources=(f"{http_server}/short",))

    with pytest.raises(AssetResolutionError, match="all candidates failed") as error:
        AssetResolver(
            ContentStore(tmp_path / "cache"),
            policy=ResolverPolicy(allow_http=True, allow_private_network=True),
        ).resolve(reference)
    assert "content-length" in str(error.value).lower()


def test_redirects_are_rejected(tmp_path: Path, http_server: str):
    payload = b"redirected"
    _Handler.response_map["/redirect"] = (302, b"", {"Location": "/asset"})
    _Handler.response_map["/asset"] = (200, payload, {"Content-Length": str(len(payload))})
    reference = _reference(payload, sources=(f"{http_server}/redirect",))

    with pytest.raises(AssetResolutionError, match="redirect"):
        AssetResolver(
            ContentStore(tmp_path / "cache"),
            policy=ResolverPolicy(allow_http=True, allow_private_network=True),
        ).resolve(reference)
    assert _Handler.requests == ["/redirect"]


@pytest.mark.parametrize("source", ["http://user:pass@127.0.0.1/asset"])
def test_unsafe_source_policy_is_rejected(tmp_path: Path, source: str):
    # AssetReference permits some syntactically valid HTTP userinfo so the
    # resolver must enforce the stronger acquisition policy.
    reference = _reference(b"payload", sources=(source,))
    with pytest.raises(AssetResolutionError, match="source policy|credentials|scheme"):
        AssetResolver(ContentStore(tmp_path / "cache"), policy=ResolverPolicy(allow_http=True)).resolve(reference)


def test_oversized_response_is_bounded(tmp_path: Path, http_server: str):
    payload = b"0123456789"
    _Handler.response_map["/large"] = (200, payload, {"Content-Length": str(len(payload))})
    reference = _reference(payload, sources=(f"{http_server}/large",))

    with pytest.raises(AssetResolutionError, match="too large"):
        AssetResolver(
            ContentStore(tmp_path / "cache"),
            policy=ResolverPolicy(allow_http=True, allow_private_network=True, max_response_bytes=4),
        ).resolve(reference)


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf")])
def test_resolver_policy_rejects_non_finite_timeout(timeout: float):
    with pytest.raises(ValueError, match="finite"):
        ResolverPolicy(timeout_seconds=timeout)


def test_private_network_sources_are_refused_without_explicit_opt_in(tmp_path: Path, http_server: str):
    payload = b"private source"
    _Handler.response_map["/private"] = (200, payload, {"Content-Length": str(len(payload))})
    reference = _reference(payload, sources=(f"{http_server}/private",))

    with pytest.raises(AssetResolutionError, match="private|non-global"):
        AssetResolver(ContentStore(tmp_path / "cache"), policy=ResolverPolicy(allow_http=True)).resolve(reference)
    assert _Handler.requests == []


def test_hostname_is_resolved_once_and_connection_uses_pinned_address(
    tmp_path: Path, http_server: str, monkeypatch: pytest.MonkeyPatch
):
    payload = b"pinned source"
    _Handler.response_map["/asset"] = (200, payload, {"Content-Length": str(len(payload))})
    port = int(http_server.rsplit(":", 1)[1])
    calls: list[tuple[str, int]] = []

    def resolve(host: str, service: int, *args: object, **kwargs: object):
        calls.append((host, service))
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", service))]

    monkeypatch.setattr("astinuslab.lab_packages.resolver.socket.getaddrinfo", resolve)
    reference = _reference(payload, sources=(f"http://lab.example:{port}/asset",))

    result = AssetResolver(
        ContentStore(tmp_path / "cache"), policy=ResolverPolicy(allow_http=True, allow_private_network=True)
    ).resolve(reference)

    assert result.read() == payload
    assert calls == [("lab.example", port)]


def test_resolver_resumes_from_a_verified_partial_prefix(tmp_path: Path, http_server: str):
    payload = b"range-resumed payload"
    _Handler.response_map["/range"] = (200, payload, {"Content-Length": str(len(payload))})
    reference = _reference(payload, sources=(f"{http_server}/range",))
    store = ContentStore(tmp_path / "cache")
    prefix = payload[:5]
    assert (
        store.put_resumable(io.BytesIO(prefix), sha256=reference.sha256, size_bytes=reference.size_bytes, offset=0)
        is None
    )

    result = AssetResolver(store, policy=ResolverPolicy(allow_http=True, allow_private_network=True)).resolve(reference)

    assert result.read() == payload
    assert _Handler.ranges == ["bytes=5-"]


def test_incomplete_http_reads_are_normalized_to_package_error():
    class BrokenResponse:
        def read(self, _size: int) -> bytes:
            raise IncompleteRead(b"partial", 4)

    with pytest.raises(AssetResolutionError, match="HTTP read"):
        _BoundedResponse(BrokenResponse(), 64).read(64)
