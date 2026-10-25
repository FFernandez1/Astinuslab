"""Cache policy for the /static mount.

Starlette's StaticFiles sends ETag and Last-Modified but no Cache-Control, which
leaves the browser on heuristic freshness — roughly a tenth of the file's age
since it was last modified. Templates cache-bust their own links with
`?v={{ asset_v }}`, but ESM `import` specifiers inside those modules are
deliberately unversioned, so their URLs are identical before and after a deploy
and heuristic freshness can keep serving the old module.

So: revalidate by default, and cache hard only where the URL genuinely names one
immutable version of the bytes.
"""

from __future__ import annotations

from urllib.parse import parse_qs

from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from ..ui import asset_version as _asset_version

# "Store it, but ask me before you use it." The ETag turns that question into a
# 304 with no body, so freshness costs a round trip rather than a download.
REVALIDATE = "no-cache"
IMMUTABLE = "public, max-age=31536000, immutable"


def _requested_version(scope: Scope) -> str | None:
    query = scope.get("query_string") or b""
    if not query:
        return None
    values = parse_qs(query.decode("latin-1")).get("v")
    return values[0] if values else None


def cache_control_for(scope: Scope) -> str:
    """Immutable only for a pinned deploy's own version token."""
    if not _asset_version.ASSET_VERSION_PINNED:
        return REVALIDATE
    return IMMUTABLE if _requested_version(scope) == _asset_version.ASSET_VERSION else REVALIDATE


class StaticCacheFiles(StaticFiles):
    """StaticFiles that states a cache policy instead of leaving it to heuristics."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        # 304s carry the header too: it is what the browser stores for next time.
        if response.status_code < 400:
            response.headers["Cache-Control"] = cache_control_for(scope)
        return response
