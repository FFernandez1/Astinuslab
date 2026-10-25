"""Single source of truth for the static-asset cache-busting token.

Resolution order: AL_ASSET_VERSION env override → short git SHA (auto-changes
per commit/deploy) → "dev". Computed once at import.

The token also decides how long a browser may keep an asset (see
`web.static_cache`). Only an explicit AL_ASSET_VERSION counts as *pinned* — a
deploy declaring "these bytes belong to this identity". A git SHA does not:
it stays put while a working tree is edited, so treating it as pinned would let
a development checkout hand out a year of caching for a token it then reuses
for different bytes.
"""

from __future__ import annotations

import os
import subprocess


def _git_short_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        sha = out.stdout.strip()
        return sha or None
    except Exception:
        return None


def resolve_asset_version() -> tuple[str, bool]:
    """Return (token, pinned). Pinned means the deploy chose the token itself."""
    override = os.environ.get("AL_ASSET_VERSION")
    if override:
        return override, True
    return _git_short_sha() or "dev", False


ASSET_VERSION, ASSET_VERSION_PINNED = resolve_asset_version()


def register_asset_version(templates) -> None:
    """Expose `asset_v` to a Jinja2Templates env so templates use ?v={{ asset_v }}."""
    templates.env.globals["asset_v"] = ASSET_VERSION
