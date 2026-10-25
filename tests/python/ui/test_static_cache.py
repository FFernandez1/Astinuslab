"""Cache policy for the /static mount.

Templates version their own links with `?v={{ asset_v }}`, but ESM `import`
specifiers inside the modules are deliberately unversioned, so those URLs are
stable across deploys. Without an explicit policy the browser falls back to
heuristic freshness (roughly a tenth of the file's age) and can keep serving a
module from before a deploy. These tests pin the policy that prevents that.
"""

from __future__ import annotations

import pytest

from astinuslab.ui import asset_version as asset_version_mod

REVALIDATE = "no-cache"
IMMUTABLE = "public, max-age=31536000, immutable"

# One ESM module the page imports without a version token, and one asset the
# templates do link with `?v=`.
UNVERSIONED_MODULE = "/static/playground/render-map.js"
VERSIONED_SHEET = "/static/css/pages/playground.css"


@pytest.fixture
def pinned_version(monkeypatch):
    """Stand in for a deploy that set AL_ASSET_VERSION explicitly."""
    monkeypatch.setattr(asset_version_mod, "ASSET_VERSION", "deadbee")
    monkeypatch.setattr(asset_version_mod, "ASSET_VERSION_PINNED", True)
    return "deadbee"


def test_unversioned_module_must_be_revalidated(client):
    """An ESM import URL never changes, so it may never be served from cache blind."""
    r = client.get(UNVERSIONED_MODULE)
    assert r.status_code == 200
    assert r.headers["cache-control"] == REVALIDATE


def test_revalidation_is_cheap_because_the_validator_survives(client):
    """no-cache means "ask first", not "send it again" — the 304 path must work."""
    first = client.get(UNVERSIONED_MODULE)
    etag = first.headers["etag"]
    assert etag

    second = client.get(UNVERSIONED_MODULE, headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.headers["cache-control"] == REVALIDATE


def test_pinned_deploy_caches_a_matching_version_token_forever(client, pinned_version):
    r = client.get(f"{VERSIONED_SHEET}?v={pinned_version}")
    assert r.status_code == 200
    assert r.headers["cache-control"] == IMMUTABLE


def test_pinned_deploy_still_revalidates_a_stale_token(client, pinned_version):
    """A token from an older deploy names content this server no longer has."""
    r = client.get(f"{VERSIONED_SHEET}?v=0ldc0de")
    assert r.status_code == 200
    assert r.headers["cache-control"] == REVALIDATE


def test_pinned_deploy_still_revalidates_an_unversioned_request(client, pinned_version):
    r = client.get(UNVERSIONED_MODULE)
    assert r.status_code == 200
    assert r.headers["cache-control"] == REVALIDATE


def test_a_development_checkout_never_caches_immutably(client, monkeypatch):
    """The git SHA does not move when a file is edited without committing, so a
    dev checkout must not hand out year-long caching for a token it will reuse."""
    monkeypatch.setattr(asset_version_mod, "ASSET_VERSION", "abc1234")
    monkeypatch.setattr(asset_version_mod, "ASSET_VERSION_PINNED", False)

    r = client.get(f"{VERSIONED_SHEET}?v=abc1234")
    assert r.status_code == 200
    assert r.headers["cache-control"] == REVALIDATE


def test_asset_version_is_pinned_only_when_the_environment_says_so(monkeypatch):
    monkeypatch.setenv("AL_ASSET_VERSION", "release-9")
    reloaded = asset_version_mod.resolve_asset_version()
    assert reloaded == ("release-9", True)

    monkeypatch.delenv("AL_ASSET_VERSION", raising=False)
    version, pinned = asset_version_mod.resolve_asset_version()
    assert pinned is False
    assert version
