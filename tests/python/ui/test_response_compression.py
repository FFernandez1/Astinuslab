"""Response compression for the pages and static assets the operator downloads.

The app serves its own static mount with no reverse proxy in front of it
(`ops/` ships systemd units, not nginx), so nothing else can compress. Without
it every page and stylesheet crosses the wire uncompressed: `components.css` is
~64 KB and `/jobs/new` ~104 KB of highly repetitive HTML and CSS.

The one response that must NOT be compressed is the capture event stream:
buffering a `text/event-stream` inside a gzip window delays the events the
Playground renders live.
"""

from __future__ import annotations

GZIP = {"Accept-Encoding": "gzip"}
IDENTITY = {"Accept-Encoding": "identity"}

# A stylesheet large enough to be worth compressing, linked by every page.
LARGE_SHEET = "/static/css/components.css"


def test_a_large_stylesheet_is_compressed(client):
    r = client.get(LARGE_SHEET, headers=GZIP)
    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"


def test_compression_is_worth_it(client):
    """A stylesheet of mostly repeated selectors should shrink several-fold.

    The static mount streams the file, so the gzip body is chunked and carries
    no `Content-Length`; the bytes that actually cross the wire have to be read
    undecoded.
    """
    with client.stream("GET", LARGE_SHEET, headers=GZIP) as response:
        on_the_wire = len(b"".join(response.iter_raw()))

    plain = client.get(LARGE_SHEET, headers=IDENTITY)
    assert "content-encoding" not in plain.headers
    assert on_the_wire * 3 < len(plain.content)


def test_the_compressed_body_is_the_same_file(client):
    """httpx decodes for us, so this compares what a browser would render."""
    compressed = client.get(LARGE_SHEET, headers=GZIP)
    plain = client.get(LARGE_SHEET, headers=IDENTITY)
    assert compressed.content == plain.content


def test_a_page_is_compressed(client):
    r = client.get("/jobs", headers={**GZIP, "Accept": "text/html"})
    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"


def test_a_client_that_cannot_decompress_still_gets_the_bytes(client):
    r = client.get(LARGE_SHEET, headers=IDENTITY)
    assert r.status_code == 200
    assert "content-encoding" not in r.headers
    assert r.content.startswith(b"/*")


def test_caches_are_told_the_body_varies_by_encoding(client):
    """One URL now has two representations; without Vary a shared cache may
    hand a gzip body to a client that asked for identity."""
    r = client.get(LARGE_SHEET, headers=GZIP)
    assert "accept-encoding" in r.headers["vary"].lower()


def test_security_headers_survive_compression(client):
    """Compression wraps the outermost layer, so it runs after the security
    middleware; the headers that middleware set must still be on the response."""
    r = client.get(LARGE_SHEET, headers=GZIP)
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"


def test_conditional_revalidation_still_returns_304(client):
    """The static mount answers `If-None-Match` itself. A bodyless 304 must not
    acquire a `Content-Encoding` claiming a gzip body that is not there."""
    first = client.get(LARGE_SHEET, headers=GZIP)
    etag = first.headers["etag"]

    second = client.get(LARGE_SHEET, headers={**GZIP, "If-None-Match": etag})
    assert second.status_code == 304
    assert "content-encoding" not in second.headers


def test_cache_policy_survives_compression(client):
    """Compression must not drop the policy `StaticCacheFiles` states."""
    r = client.get(LARGE_SHEET, headers=GZIP)
    assert r.headers["cache-control"] == "no-cache"
