"""Ordered, local-first resolution of content-addressed lab assets."""

from __future__ import annotations

import hashlib
import ipaddress
import math
import socket
import ssl
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from http.client import HTTPConnection, HTTPException, HTTPSConnection, IncompleteRead
from pathlib import Path
from typing import Any, BinaryIO, cast
from urllib.parse import SplitResult, urlsplit

from .content_store import AssetSource, ContentStore, ContentStoreError, StoredAsset
from .models import AssetReference


class AssetResolutionError(ValueError):
    """Raised when no safe candidate satisfies an asset identity."""

    def __init__(self, message: str, diagnostics: tuple[ResolutionDiagnostic, ...] = ()) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


@dataclass(frozen=True, slots=True)
class ResolutionDiagnostic:
    """Stable explanation for one unavailable or mismatching candidate."""

    candidate: str
    reason: str


@dataclass(frozen=True, slots=True)
class ResolverPolicy:
    """Explicit local acquisition policy; HTTP is opt-in."""

    allow_http: bool = False
    # The primitive remains network-capable by default for existing direct
    # callers.  Package preparation overrides this to False unless the caller
    # explicitly opted into fetching declared sources.
    allow_source_network: bool = True
    allow_private_network: bool = False
    allow_missing_embedded_fallback: bool = True
    timeout_seconds: float = 15.0
    max_response_bytes: int = 2 * 1024**3

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout must be finite and positive")
        if isinstance(self.max_response_bytes, bool) or not isinstance(self.max_response_bytes, int):
            raise ValueError("max response size must be an integer")
        if self.max_response_bytes < 1:
            raise ValueError("max response size must be positive")


@dataclass(frozen=True, slots=True)
class ResolvedAsset:
    """Verified asset metadata plus a bounded local read handle."""

    reference: AssetReference
    path: Path
    source_kind: str
    source: str | None
    insecure_transport: bool
    _store: ContentStore = field(repr=False, compare=False, hash=False)
    _cache_hit: bool = field(default=False, repr=False, compare=False, hash=False)
    diagnostics: tuple[ResolutionDiagnostic, ...] = ()

    @property
    def sha256(self) -> str:
        return self.reference.sha256

    @property
    def size_bytes(self) -> int:
        return self.reference.size_bytes

    @property
    def media_type(self) -> str:
        return self.reference.media_type

    @property
    def from_cache(self) -> bool:
        return self._cache_hit

    def open(self) -> BinaryIO:
        return self._store.open(self.sha256, size_bytes=self.size_bytes)

    def read(self) -> bytes:
        return self._store.get(self.sha256, size_bytes=self.size_bytes)

    @property
    def content(self) -> bytes:
        return self.read()

    @property
    def data(self) -> bytes:
        return self.read()


@dataclass(frozen=True, slots=True)
class _ResolvedAddress:
    family: int
    socktype: int
    protocol: int
    sockaddr: tuple[Any, ...]


class _PinnedHTTPConnection(HTTPConnection):
    def __init__(self, host: str, port: int, address: _ResolvedAddress, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        sock = socket.socket(self._address.family, self._address.socktype, self._address.protocol)
        try:
            sock.settimeout(self.timeout)
            sock.connect(self._address.sockaddr)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


class _PinnedHTTPSConnection(HTTPSConnection):
    def __init__(self, host: str, port: int, address: _ResolvedAddress, timeout: float) -> None:
        self._ssl_context = ssl.create_default_context()
        super().__init__(host, port, timeout=timeout, context=self._ssl_context)
        self._address = address

    def connect(self) -> None:
        sock = socket.socket(self._address.family, self._address.socktype, self._address.protocol)
        try:
            sock.settimeout(self.timeout)
            sock.connect(self._address.sockaddr)
            self.sock = self._ssl_context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class _BoundedResponse:
    def __init__(self, response: Any, maximum: int) -> None:
        self._response = response
        self._maximum = maximum
        self._read = 0

    def read(self, requested: int = -1) -> bytes:
        remaining = self._maximum - self._read + 1
        if remaining <= 0:
            raise AssetResolutionError("HTTP response is too large")
        if requested < 0:
            requested = remaining
        try:
            chunk = self._response.read(min(requested, remaining))
        except (OSError, TimeoutError, IncompleteRead, HTTPException) as exc:
            raise AssetResolutionError("HTTP read timeout or error") from exc
        if chunk:
            self._read += len(chunk)
            if self._read > self._maximum:
                raise AssetResolutionError("HTTP response is too large")
        return chunk


class AssetResolver:
    """Resolve an asset in embedded, cache, then declared-source order."""

    def __init__(self, store: ContentStore, *, policy: ResolverPolicy | None = None) -> None:
        if not isinstance(store, ContentStore):
            raise TypeError("store must be a ContentStore")
        self.store = store
        self.policy = policy or ResolverPolicy()

    def resolve(
        self,
        reference: AssetReference,
        embedded: AssetSource | None = None,
        *,
        embedded_bytes: AssetSource | None = None,
    ) -> ResolvedAsset:
        if not isinstance(reference, AssetReference):
            raise TypeError("reference must be an AssetReference")
        if embedded is not None and embedded_bytes is not None:
            raise TypeError("provide only one embedded payload")
        payload = embedded if embedded is not None else embedded_bytes
        diagnostics: list[ResolutionDiagnostic] = []
        attempted_embedded = False

        if payload is not None:
            attempted_embedded = True
            try:
                if isinstance(payload, (bytes, bytearray, memoryview)):
                    actual_size = len(payload)
                    actual_digest = hashlib.sha256(bytes(payload)).hexdigest()
                    if actual_size != reference.size_bytes:
                        raise ContentStoreError("asset size mismatch")
                    if actual_digest != reference.sha256:
                        raise ContentStoreError("asset digest mismatch")
                    if self.store.has(reference.sha256, size_bytes=reference.size_bytes):
                        stored = StoredAsset(
                            reference.sha256,
                            reference.size_bytes,
                            self.store.path_for(reference.sha256),
                            from_cache=True,
                        )
                    else:
                        stored = self.store.put(
                            payload,
                            sha256=reference.sha256,
                            size_bytes=reference.size_bytes,
                        )
                else:
                    stored = self.store.put(
                        payload,
                        sha256=reference.sha256,
                        size_bytes=reference.size_bytes,
                        reuse_existing=False,
                    )
            except (ContentStoreError, OSError, ValueError) as exc:
                diagnostics.append(ResolutionDiagnostic("embedded", _reason(exc)))
            else:
                return self._result(reference, stored, "embedded", None, diagnostics)
        elif reference.embedded:
            attempted_embedded = True
            diagnostics.append(ResolutionDiagnostic("embedded", "embedded payload is missing"))
            if not self.policy.allow_missing_embedded_fallback:
                raise AssetResolutionError("embedded payload is missing", tuple(diagnostics))

        try:
            if self.store.has(reference.sha256, size_bytes=reference.size_bytes):
                stored = StoredAsset(
                    reference.sha256,
                    reference.size_bytes,
                    self.store.path_for(reference.sha256),
                    from_cache=True,
                )
                kind = "cache-fallback" if attempted_embedded else "cache"
                return self._result(reference, stored, kind, None, diagnostics)
        except ContentStoreError as exc:
            diagnostics.append(ResolutionDiagnostic("cache", _reason(exc)))

        for source in reference.sources:
            try:
                stored = self._fetch_source(reference, source)
            except (AssetResolutionError, ContentStoreError, OSError, ValueError) as exc:
                diagnostics.append(ResolutionDiagnostic(source, _reason(exc)))
                continue
            kind = "source-fallback" if attempted_embedded else "source"
            return self._result(reference, stored, kind, source, diagnostics)

        if reference.embedded and not diagnostics:
            diagnostics.append(ResolutionDiagnostic("embedded", "embedded payload is missing"))
        summary = "; ".join(f"{item.candidate}: {item.reason}" for item in diagnostics)
        detail = f": {summary}" if summary else ""
        raise AssetResolutionError(f"all candidates failed for asset {reference.sha256}{detail}", tuple(diagnostics))

    def _result(
        self,
        reference: AssetReference,
        stored: StoredAsset,
        kind: str,
        source: str | None,
        diagnostics: list[ResolutionDiagnostic],
    ) -> ResolvedAsset:
        return ResolvedAsset(
            reference=reference,
            path=stored.path,
            source_kind=kind,
            source=source,
            insecure_transport=source is not None and source.lower().startswith("http://"),
            _cache_hit=stored.from_cache,
            diagnostics=tuple(diagnostics),
            _store=self.store,
        )

    def _fetch_source(self, reference: AssetReference, source: str) -> StoredAsset:
        if not self.policy.allow_source_network:
            raise AssetResolutionError("source network access requires explicit opt-in")
        try:
            parsed = urlsplit(source)
        except ValueError as exc:
            raise AssetResolutionError("malformed source URL") from exc
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"}:
            raise AssetResolutionError("source policy rejects URL scheme")
        if scheme == "http" and not self.policy.allow_http:
            raise AssetResolutionError("HTTPS is required unless HTTP is explicitly allowed")
        if parsed.username is not None or parsed.password is not None:
            raise AssetResolutionError("source policy rejects URL credentials")
        hostname = parsed.hostname
        if hostname is None or not parsed.netloc:
            raise AssetResolutionError("malformed source URL")
        try:
            parsed_port = parsed.port
        except ValueError as exc:
            raise AssetResolutionError("malformed source URL port") from exc
        if reference.size_bytes > self.policy.max_response_bytes:
            raise AssetResolutionError("HTTP response is too large")
        try:
            offset = self.store.partial_size(reference.sha256, size_bytes=reference.size_bytes)
        except ContentStoreError as exc:
            raise AssetResolutionError(_reason(exc)) from exc
        try:
            response, connection = self._open_pinned_response(parsed, parsed_port, range_start=offset)
        except (OSError, TimeoutError, IncompleteRead, HTTPException) as exc:
            raise AssetResolutionError("HTTP connection timeout or error") from exc

        try:
            status = response.status
            if status < 200 or status >= 300:
                if 300 <= status < 400:
                    raise AssetResolutionError("redirects are not allowed")
                raise AssetResolutionError(f"HTTP error {status}")
            effective_offset = offset
            reset_partial = False
            if offset:
                if status == 206:
                    range_start, range_total = _parse_content_range(response.headers.get("Content-Range"))
                    if range_start != offset or range_total not in {None, reference.size_bytes}:
                        raise AssetResolutionError("HTTP range response does not match the partial asset")
                elif status == 200:
                    # The source does not support ranges; safely restart from
                    # zero rather than appending a second full representation.
                    effective_offset = 0
                    reset_partial = True
                else:
                    raise AssetResolutionError("HTTP range response is unavailable")
            elif status == 206:
                raise AssetResolutionError("unexpected HTTP range response")
            encoding = response.headers.get("Content-Encoding", "").strip().lower()
            if encoding not in {"", "identity"}:
                raise AssetResolutionError("compressed HTTP responses are not allowed")
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except ValueError as exc:
                    raise AssetResolutionError("invalid content-length") from exc
                if declared_length != reference.size_bytes - effective_offset:
                    raise AssetResolutionError("content-length mismatch")
            bounded = _BoundedResponse(response, self.policy.max_response_bytes - effective_offset)
            try:
                stored = self.store.put_resumable(
                    cast(Any, bounded),
                    sha256=reference.sha256,
                    size_bytes=reference.size_bytes,
                    offset=effective_offset,
                    reset=reset_partial,
                )
            except ContentStoreError as exc:
                raise AssetResolutionError(_reason(exc)) from exc
            if stored is None:
                raise AssetResolutionError("HTTP response ended before the expected asset size")
            return stored
        finally:
            response.close()
            connection.close()

    def _open_pinned_response(self, parsed: SplitResult, parsed_port: int | None, *, range_start: int = 0):
        hostname = parsed.hostname
        if hostname is None:
            raise AssetResolutionError("malformed source URL")
        port = parsed_port or (443 if parsed.scheme.lower() == "https" else 80)
        address = self._resolve_network_address(hostname, port)
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        host_header = hostname
        if ":" in host_header and not host_header.startswith("["):
            host_header = f"[{host_header}]"
        if parsed.port is not None and parsed.port != (443 if parsed.scheme.lower() == "https" else 80):
            host_header = f"{host_header}:{parsed.port}"
        if parsed.scheme.lower() == "https":
            connection: HTTPConnection = _PinnedHTTPSConnection(hostname, port, address, self.policy.timeout_seconds)
        else:
            connection = _PinnedHTTPConnection(hostname, port, address, self.policy.timeout_seconds)
        try:
            headers = {"Accept-Encoding": "identity", "Host": host_header}
            if range_start:
                headers["Range"] = f"bytes={range_start}-"
            connection.request("GET", target, headers=headers)
            return connection.getresponse(), connection
        except BaseException:
            connection.close()
            raise

    def _resolve_network_address(self, hostname: str, port: int) -> _ResolvedAddress:
        try:
            literal = ipaddress.ip_address(hostname)
        except ValueError:
            resolved = self._resolve_dns(hostname, port)
        else:
            resolved = [
                (
                    socket.AF_INET6 if isinstance(literal, ipaddress.IPv6Address) else socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (str(literal), port, 0, 0) if isinstance(literal, ipaddress.IPv6Address) else (str(literal), port),
                )
            ]
        addresses = {ipaddress.ip_address(item[4][0]) for item in resolved}
        if not addresses:
            raise AssetResolutionError("DNS resolution returned no addresses")
        if not self.policy.allow_private_network and any(not address.is_global for address in addresses):
            raise AssetResolutionError("source resolves to a private or non-global address")
        family, socktype, protocol, _canonname, sockaddr = resolved[0]
        return _ResolvedAddress(family, socktype, protocol, sockaddr)

    def _resolve_dns(self, hostname: str, port: int) -> list[tuple[Any, ...]]:
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="astinuslab-dns")
        future = executor.submit(socket.getaddrinfo, hostname, port, type=socket.SOCK_STREAM)
        try:
            try:
                resolved = future.result(timeout=self.policy.timeout_seconds)
            except FutureTimeoutError as exc:
                future.cancel()
                raise AssetResolutionError("DNS resolution timed out") from exc
            except OSError as exc:
                raise AssetResolutionError("DNS resolution failed") from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return resolved


def _reason(exc: BaseException) -> str:
    message = str(exc).strip()
    return message or exc.__class__.__name__.lower()


def _parse_content_range(value: str | None) -> tuple[int, int | None]:
    if not value or not value.startswith("bytes "):
        raise AssetResolutionError("HTTP range response is missing Content-Range")
    try:
        span, total = value[6:].split("/", 1)
        start_text, end_text = span.split("-", 1)
        start = int(start_text)
        end = int(end_text)
        total_value = None if total == "*" else int(total)
    except (AttributeError, TypeError, ValueError) as exc:
        raise AssetResolutionError("invalid HTTP Content-Range") from exc
    if start < 0 or end < start or total_value is not None and total_value <= end:
        raise AssetResolutionError("invalid HTTP Content-Range")
    return start, total_value


def resolve_asset(
    reference: AssetReference,
    embedded: AssetSource | None = None,
    *,
    embedded_bytes: AssetSource | None = None,
    store: ContentStore,
    policy: ResolverPolicy | None = None,
) -> ResolvedAsset:
    """Functional entry point for local callers."""

    return AssetResolver(store, policy=policy).resolve(reference, embedded, embedded_bytes=embedded_bytes)
