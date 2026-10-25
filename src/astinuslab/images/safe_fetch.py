from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Protocol
from urllib.parse import SplitResult, urljoin, urlsplit

from fastapi import HTTPException

from ..api.schemas import ImageDownloadConfig

OFFICIAL_IMAGE_DOMAINS = (
    "cloud-images.ubuntu.com",
    "cdimage.ubuntu.com",
    "cloud.debian.org",
    "download.fedoraproject.org",
    "dl.fedoraproject.org",
    "download.opensuse.org",
)
BLOCKED_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)
METADATA_IPS = {ipaddress.ip_address("169.254.169.254")}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 5
_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class ValidatedTarget:
    url: str
    scheme: str
    hostname: str
    port: int
    peer_ip: ipaddress.IPv4Address | ipaddress.IPv6Address
    request_target: str


class Response(Protocol):
    status: int

    @property
    def headers(self) -> object: ...

    def read(self, size: int = -1) -> bytes: ...

    def close(self) -> None: ...


class _PinnedResponse:
    def __init__(self, response: http.client.HTTPResponse, sock: socket.socket) -> None:
        self._response = response
        self._sock = sock
        self.status = response.status
        self.headers = response.headers

    def read(self, size: int = -1) -> bytes:
        return self._response.read(size)

    def set_read_timeout(self, timeout: float) -> None:
        self._sock.settimeout(timeout)

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._sock.close()

    def __enter__(self) -> _PinnedResponse:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def _domain_matches(hostname: str, domain: str) -> bool:
    hostname = hostname.lower().rstrip(".")
    domain = domain.lower().rstrip(".")
    return hostname == domain or hostname.endswith(f".{domain}")


def _validate_source(hostname: str, config: ImageDownloadConfig) -> None:
    allowed_domains: tuple[str, ...] = ()
    if config.source_policy == "official_only":
        allowed_domains = OFFICIAL_IMAGE_DOMAINS
    elif config.source_policy == "allowlist":
        allowed_domains = tuple(config.allowed_domains)
        if not allowed_domains:
            raise HTTPException(400, "Custom allowlist policy requires at least one allowed domain")

    if allowed_domains and not any(_domain_matches(hostname, domain) for domain in allowed_domains):
        raise HTTPException(403, f"Domain '{hostname}' is not allowed by the current download policy")


def _parse_url(url: str) -> tuple[SplitResult, str, int, str]:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(400, f"Invalid URL: {exc}") from exc
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(400, "URL must start with http:// or https://")
    if parsed.username is not None or parsed.password is not None:
        raise HTTPException(400, "URL credentials are not allowed")
    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(400, "URL must include a hostname")
    if port == 0:
        raise HTTPException(400, "URL port must be between 1 and 65535")
    port = port if port is not None else (443 if parsed.scheme == "https" else 80)
    request_target = parsed.path or "/"
    if parsed.query:
        request_target += f"?{parsed.query}"
    return parsed, hostname, port, request_target


def _is_blocked(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = ip_obj.ipv4_mapped if isinstance(ip_obj, ipaddress.IPv6Address) else None
    return (
        ip_obj in METADATA_IPS
        or not ip_obj.is_global
        or (mapped is not None and not mapped.is_global)
        or any(ip_obj in network for network in BLOCKED_NETWORKS)
    )


def resolve_and_validate(url: str, config: ImageDownloadConfig) -> list[ValidatedTarget]:
    parsed, hostname, port, request_target = _parse_url(url)
    _validate_source(hostname, config)
    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise HTTPException(400, f"Could not resolve hostname '{hostname}': {exc}") from exc

    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise HTTPException(400, f"Could not resolve hostname '{hostname}'")

    if config.network_policy == "safe":
        for address in addresses:
            if _is_blocked(address):
                raise HTTPException(403, f"Resolved IP '{address}' is blocked by the current network policy")

    return [
        ValidatedTarget(
            url=url,
            scheme=parsed.scheme,
            hostname=hostname,
            port=port,
            peer_ip=address,
            request_target=request_target,
        )
        for address in addresses
    ]


def _host_header(target: ValidatedTarget) -> str:
    host = f"[{target.hostname}]" if ":" in target.hostname else target.hostname
    default_port = 443 if target.scheme == "https" else 80
    return host if target.port == default_port else f"{host}:{target.port}"


def open_pinned_response(
    target: ValidatedTarget, *, connect_timeout: float, read_timeout: float | None = None
) -> _PinnedResponse:
    peer = target.peer_ip
    family = socket.AF_INET6 if peer.version == 6 else socket.AF_INET
    raw_sock = socket.socket(family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    sock: socket.socket = raw_sock
    try:
        raw_sock.settimeout(connect_timeout)
        peer_text = str(target.peer_ip)
        address = (peer_text, target.port, 0, 0) if family == socket.AF_INET6 else (peer_text, target.port)
        raw_sock.connect(address)
        connected_ip = str(ipaddress.ip_address(raw_sock.getpeername()[0]))
        if connected_ip != str(target.peer_ip):
            raise RuntimeError(f"connected peer '{connected_ip}' does not match pinned address '{target.peer_ip}'")
        if target.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(raw_sock, server_hostname=target.hostname)
        sock.settimeout(read_timeout if read_timeout is not None else connect_timeout)

        connection = http.client.HTTPConnection(target.hostname, target.port)
        connection.sock = sock
        connection.putrequest("GET", target.request_target, skip_host=True, skip_accept_encoding=True)
        connection.putheader("Host", _host_header(target))
        connection.putheader("User-Agent", "AstinusLab/1.0")
        connection.putheader("Connection", "close")
        connection.endheaders()
        return _PinnedResponse(connection.getresponse(), sock)
    except BaseException:
        sock.close()
        if sock is not raw_sock:
            raw_sock.close()
        raise


def _header(response: Response, name: str) -> str | None:
    headers = response.headers
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    value = getter(name)
    return str(value) if value is not None else None


def _open_first_response(targets: list[ValidatedTarget], config: ImageDownloadConfig) -> Response:
    last_error: OSError | None = None
    for target in targets:
        try:
            return open_pinned_response(
                target,
                connect_timeout=config.connect_timeout_seconds,
                read_timeout=config.read_timeout_seconds,
            )
        except OSError as exc:
            last_error = exc
    if last_error is None:  # pragma: no cover - resolve_and_validate always returns a target
        raise RuntimeError("Image download hostname resolved to no addresses")
    raise last_error


def stream_download(
    url: str,
    destination: Path,
    config: ImageDownloadConfig,
    progress_callback: Callable[[int, int | None], None],
) -> None:
    current_url = url
    visited: set[str] = set()
    redirects = 0
    max_bytes = config.max_size_gb * (1024**3)
    destination_created = False

    try:
        while True:
            if current_url in visited:
                raise RuntimeError("Download redirect loop detected")
            visited.add(current_url)
            targets = resolve_and_validate(current_url, config)
            response = _open_first_response(targets, config)
            try:
                set_read_timeout = getattr(response, "set_read_timeout", None)
                if set_read_timeout is not None:
                    set_read_timeout(config.read_timeout_seconds)

                if response.status in _REDIRECT_STATUSES:
                    location = _header(response, "Location")
                    if not location:
                        raise RuntimeError("Redirect response did not include a Location header")
                    if redirects >= _MAX_REDIRECTS:
                        raise RuntimeError("Download followed more than 5 redirects")
                    next_url = urljoin(current_url, location)
                    next_scheme = urlsplit(next_url).scheme
                    if (
                        config.network_policy == "safe"
                        and urlsplit(current_url).scheme == "https"
                        and next_scheme == "http"
                    ):
                        raise RuntimeError("HTTPS to HTTP redirect is not allowed by the safe network policy")
                    current_url = next_url
                    redirects += 1
                    continue

                if response.status < 200 or response.status >= 300:
                    raise RuntimeError(f"Image download failed with HTTP status {response.status}")

                content_length_value = _header(response, "Content-Length")
                total: int | None = None
                if content_length_value is not None:
                    if not content_length_value.isascii() or not content_length_value.isdecimal():
                        raise RuntimeError("Image download returned an invalid Content-Length")
                    try:
                        total = int(content_length_value)
                    except ValueError as exc:
                        raise RuntimeError("Image download returned an invalid Content-Length") from exc
                    if total < 0:
                        raise RuntimeError("Image download returned an invalid Content-Length")
                if total is not None and total > max_bytes:
                    raise RuntimeError(f"Download exceeds configured limit of {config.max_size_gb} GB")

                bytes_done = 0
                progress_callback(bytes_done, total)
                with destination.open("xb") as output:
                    destination_created = True
                    while True:
                        try:
                            chunk = response.read(_CHUNK_SIZE)
                        except TimeoutError as exc:
                            raise TimeoutError("Image download read timed out") from exc
                        if not chunk:
                            break
                        bytes_done += len(chunk)
                        if bytes_done > max_bytes:
                            raise RuntimeError(f"Download exceeds configured limit of {config.max_size_gb} GB")
                        output.write(chunk)
                        progress_callback(bytes_done, total)
                return
            finally:
                response.close()
    except BaseException:
        if destination_created:
            destination.unlink(missing_ok=True)
        raise
