"""Add expiring, revocable browser sessions without storing raw credentials."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a041_admin_browser_sessions"
down_revision = "a040_evidence_cleanup_intents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_browser_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("admin_token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_admin_browser_sessions_expires_at", "admin_browser_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_admin_browser_sessions_expires_at", table_name="admin_browser_sessions")
    op.drop_table("admin_browser_sessions")
