"""Acquisition-policy contract for the asset resolver.

The resolver decides where a declared asset's bytes may legitimately come
from. Everything it refuses is a boundary a crafted package would otherwise
cross: plaintext HTTP, credentials embedded in a URL, a scheme that is not HTTP
at all, a private-network address, or a response that keeps growing past the
configured ceiling. Ordering matters too — embedded bytes, then the local
cache, then declared sources — and each failed candidate has to leave a
diagnostic the operator can read.
"""

from __future__ import annotations

import hashlib
from http.client import IncompleteRead
from pathlib import Path

import pytest

from astinuslab.lab_packages.content_store import ContentStore
from astinuslab.lab_packages.models import AssetReference
from astinuslab.lab_packages.resolver import (
    AssetResolutionError,
    AssetResolver,
    ResolverPolicy,
    _BoundedResponse,
)

pytestmark = pytest.mark.unit

PAYLOAD = b"declared asset bytes"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


@pytest.fixture()
def store(tmp_path: Path) -> ContentStore:
    return ContentStore(tmp_path / "content", max_bytes=4096)


def _reference(*, embedded: bool = False, sources: tuple[str, ...] = ()) -> AssetReference:
    return AssetReference(
        sha256=DIGEST,
        size_bytes=len(PAYLOAD),
        media_type="application/octet-stream",
        embedded=embedded,
        sources=sources,
        license="CC-BY-4.0",
    )


# --- ResolverPolicy validation -------------------------------------------------


def test_policy_defaults_refuse_plaintext_and_private_networks():
    policy = ResolverPolicy()

    assert policy.allow_http is False
    assert policy.allow_private_network is False


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf"), "15"])
def test_policy_refuses_an_unusable_timeout(timeout):
    with pytest.raises(ValueError, match="timeout must be finite and positive"):
        ResolverPolicy(timeout_seconds=timeout)


@pytest.mark.parametrize("maximum", [True, 2.5, "4096", None])
def test_policy_refuses_a_non_integer_response_ceiling(maximum):
    with pytest.raises(ValueError, match="must be an integer"):
        ResolverPolicy(max_response_bytes=maximum)


@pytest.mark.parametrize("maximum", [0, -1])
def test_policy_refuses_a_non_positive_response_ceiling(maximum):
    with pytest.raises(ValueError, match="must be positive"):
        ResolverPolicy(max_response_bytes=maximum)


# --- _BoundedResponse ----------------------------------------------------------


class _Stream:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._payload) - self._offset
        chunk = self._payload[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


def test_bounded_response_passes_through_a_small_body():
    bounded = _BoundedResponse(_Stream(PAYLOAD), 4096)

    assert bounded.read() == PAYLOAD
    assert bounded.read() == b""


def test_bounded_response_refuses_a_body_over_the_ceiling():
    bounded = _BoundedResponse(_Stream(PAYLOAD), 4)

    with pytest.raises(AssetResolutionError, match="too large"):
        bounded.read()


def test_bounded_response_allows_a_body_exactly_at_the_ceiling():
    bounded = _BoundedResponse(_Stream(PAYLOAD), len(PAYLOAD))

    assert bounded.read() == PAYLOAD
    assert bounded.read() == b""


def test_bounded_response_reports_a_truncated_transfer():
    class _Failing:
        def read(self, size: int = -1) -> bytes:
            raise IncompleteRead(b"", 10)

    with pytest.raises(AssetResolutionError, match="timeout or error"):
        _BoundedResponse(_Failing(), 4096).read()


def test_bounded_response_reports_a_socket_failure():
    class _Failing:
        def read(self, size: int = -1) -> bytes:
            raise OSError("connection reset")

    with pytest.raises(AssetResolutionError, match="timeout or error"):
        _BoundedResponse(_Failing(), 4096).read()


# --- resolver construction -----------------------------------------------------


def test_resolver_requires_a_content_store():
    with pytest.raises(TypeError, match="must be a ContentStore"):
        AssetResolver(object())  # type: ignore[arg-type]


def test_resolver_requires_an_asset_reference(store: ContentStore):
    with pytest.raises(TypeError, match="must be an AssetReference"):
        AssetResolver(store).resolve("not-a-reference")  # type: ignore[arg-type]


def test_resolver_refuses_two_embedded_payloads(store: ContentStore):
    with pytest.raises(TypeError, match="only one embedded payload"):
        AssetResolver(store).resolve(_reference(), PAYLOAD, embedded_bytes=PAYLOAD)


# --- embedded and cache ordering -----------------------------------------------


def test_embedded_bytes_are_published_and_reported_as_embedded(store: ContentStore):
    resolved = AssetResolver(store).resolve(_reference(embedded=True), PAYLOAD)

    assert resolved.source_kind == "embedded"
    assert resolved.sha256 == DIGEST
    assert resolved.size_bytes == len(PAYLOAD)
    assert resolved.read() == PAYLOAD
    assert resolved.insecure_transport is False


def test_embedded_bytes_accept_the_keyword_spelling(store: ContentStore):
    resolved = AssetResolver(store).resolve(_reference(embedded=True), embedded_bytes=PAYLOAD)

    assert resolved.source_kind == "embedded"
    assert resolved.content == PAYLOAD
    assert resolved.data == PAYLOAD


def test_a_cached_asset_is_served_without_any_candidate(store: ContentStore):
    store.put(PAYLOAD, sha256=DIGEST, size_bytes=len(PAYLOAD))

    resolved = AssetResolver(store).resolve(_reference())

    assert resolved.source_kind == "cache"
    assert resolved.from_cache is True


def test_a_mismatched_embedded_payload_falls_back_to_the_cache(store: ContentStore):
    store.put(PAYLOAD, sha256=DIGEST, size_bytes=len(PAYLOAD))

    resolved = AssetResolver(store).resolve(_reference(embedded=True), b"different bytes here!")

    assert resolved.source_kind == "cache-fallback"
    assert [d.candidate for d in resolved.diagnostics] == ["embedded"]


def test_a_missing_embedded_payload_is_refused_when_fallback_is_disabled(store: ContentStore):
    resolver = AssetResolver(store, policy=ResolverPolicy(allow_missing_embedded_fallback=False))

    with pytest.raises(AssetResolutionError, match="embedded payload is missing"):
        resolver.resolve(_reference(embedded=True))


def test_all_candidates_failing_reports_every_diagnostic(store: ContentStore):
    resolver = AssetResolver(store, policy=ResolverPolicy(allow_source_network=False))

    with pytest.raises(AssetResolutionError, match="all candidates failed") as caught:
        resolver.resolve(_reference(sources=("https://example.invalid/asset",)))

    assert [d.candidate for d in caught.value.diagnostics] == ["https://example.invalid/asset"]


# --- source policy refusals ----------------------------------------------------


def _diagnostic_reason(store: ContentStore, source: str, policy: ResolverPolicy) -> str:
    resolver = AssetResolver(store, policy=policy)
    with pytest.raises(AssetResolutionError) as caught:
        resolver.resolve(_reference(sources=(source,)))
    return caught.value.diagnostics[0].reason


def test_source_network_access_is_opt_in(store: ContentStore):
    reason = _diagnostic_reason(store, "https://example.invalid/asset", ResolverPolicy(allow_source_network=False))

    assert "explicit opt-in" in reason


def test_plaintext_http_is_refused_unless_allowed(store: ContentStore):
    reason = _diagnostic_reason(store, "http://example.invalid/asset", ResolverPolicy(allow_http=False))

    assert "HTTPS is required" in reason


@pytest.mark.parametrize("source", ["file:///etc/passwd", "ftp://example.invalid/asset", "gopher://x/y"])
def test_non_http_schemes_never_reach_a_reference(source: str):
    """The model refuses the scheme, so no valid package can declare one."""

    with pytest.raises(ValueError, match="must use http or https"):
        AssetReference(
            sha256=DIGEST,
            size_bytes=len(PAYLOAD),
            media_type="application/octet-stream",
            sources=(source,),
            license="CC-BY-4.0",
        )


@pytest.mark.parametrize("source", ["file:///etc/passwd", "ftp://example.invalid/asset"])
def test_the_resolver_refuses_a_non_http_scheme_independently(store: ContentStore, source: str):
    """Defence in depth: the fetch path does not trust the model's validation."""

    resolver = AssetResolver(store)

    with pytest.raises(AssetResolutionError, match="rejects URL scheme"):
        resolver._fetch_source(_reference(), source)


def test_credentials_in_a_source_url_are_refused(store: ContentStore):
    reason = _diagnostic_reason(store, "https://user:secret@example.invalid/asset", ResolverPolicy())

    assert "rejects URL credentials" in reason
