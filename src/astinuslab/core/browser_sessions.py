"""Expiring browser credentials, separate from the permanent admin API token."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete

from .. import db
from ..db.models import AdminBrowserSession

COOKIE_NAME = "astinuslab_admin_token"
SESSION_TTL_SECONDS = 12 * 60 * 60
_TOKEN_PATTERN = re.compile(r"v1\.[A-Za-z0-9_-]{43}\Z")


def _now() -> datetime:
    # SQLite DateTime stores naive timestamps; consistently interpret them as UTC.
    return datetime.now(UTC).replace(tzinfo=None)


def _token_digest(token: str | None) -> str | None:
    if token is None or _TOKEN_PATTERN.fullmatch(token) is None:
        return None
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _generation(admin_token_hash: str) -> str:
    return admin_token_hash.removeprefix("sha256:").lower()


def create_session(admin_token_hash: str, *, previous_token: str | None = None) -> str:
    """Commit a fresh session before allowing the login response to set its cookie."""
    token = "v1." + secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    now = _now()
    with db.session_scope() as session:
        session.execute(delete(AdminBrowserSession).where(AdminBrowserSession.expires_at <= now))
        previous_hash = _token_digest(previous_token)
        if previous_hash is not None:
            session.execute(delete(AdminBrowserSession).where(AdminBrowserSession.token_hash == previous_hash))
        session.add(
            AdminBrowserSession(
                token_hash=token_hash,
                admin_token_hash=_generation(admin_token_hash),
                created_at=now,
                expires_at=now + timedelta(seconds=SESSION_TTL_SECONDS),
            )
        )
    return token


def session_is_valid(token: str | None, admin_token_hash: str | None) -> bool:
    token_hash = _token_digest(token)
    if token_hash is None or not admin_token_hash:
        return False
    with db.session_scope() as session:
        row = session.get(AdminBrowserSession, token_hash)
        return (
            row is not None
            and row.expires_at > _now()
            and hmac.compare_digest(row.admin_token_hash, _generation(admin_token_hash))
        )


def revoke_session(token: str | None) -> None:
    token_hash = _token_digest(token)
    if token_hash is None:
        return
    with db.session_scope() as session:
        session.execute(delete(AdminBrowserSession).where(AdminBrowserSession.token_hash == token_hash))
