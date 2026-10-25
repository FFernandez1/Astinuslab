"""Freeze an authored Playground session instead of destroying it.

Adds the durable ``authoring_closed`` state to the one-active-session
exclusion index and binds a verification attempt to the exact frozen session
it owns.

Revision ID: a036_playground_authoring_closed
Revises: a035_prepare_attempt_fields
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a036_playground_authoring_closed"
down_revision = "a035_prepare_attempt_fields"
branch_labels = None
depends_on = None

_ACTIVE_BEFORE = (
    "'pending', 'queued', 'running', 'cancel_requested', "
    "'destroy_requested', 'destroying', 'destroy_failed', 'failure_cleanup'"
)
_ACTIVE_AFTER = f"{_ACTIVE_BEFORE}, 'authoring_closed'"


def _recreate_exclusion_index(states: str) -> None:
    op.execute("DROP INDEX IF EXISTS ux_playground_sessions_one_active")
    op.execute(
        f"CREATE UNIQUE INDEX ux_playground_sessions_one_active ON playground_sessions ((1)) WHERE status IN ({states})"
    )


def upgrade() -> None:
    # Only the exclusion predicate changes. Historical terminal rows keep their
    # recorded status; a frozen session is created by the verification flow
    # from now on, never by rewriting the past.
    _recreate_exclusion_index(_ACTIVE_AFTER)
    with op.batch_alter_table("lab_verification_attempts") as batch:
        batch.add_column(sa.Column("authored_session_id", sa.String(64), nullable=True))
    op.create_index(
        "ux_lab_verification_attempts_authored_session",
        "lab_verification_attempts",
        ["authored_session_id"],
        unique=True,
    )
    op.create_index(
        "ux_lab_verification_attempts_draft_live",
        "lab_verification_attempts",
        ["draft_id"],
        unique=True,
        sqlite_where=sa.text("state IN ('reserved', 'running')"),
    )


def downgrade() -> None:
    frozen_count = (
        op.get_bind()
        .execute(sa.text("SELECT COUNT(*) FROM playground_sessions WHERE status = 'authoring_closed'"))
        .scalar_one()
    )
    if frozen_count:
        raise RuntimeError("cannot downgrade while an authored Playground session is frozen")
    cleanup_count = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT COUNT(*) FROM lab_verification_attempts a "
                "JOIN playground_sessions p ON p.id = a.authored_session_id "
                "WHERE p.status = 'destroying'"
            )
        )
        .scalar_one()
    )
    if cleanup_count:
        raise RuntimeError("verification-owned Playground cleanup is in progress")
    op.drop_index("ux_lab_verification_attempts_draft_live", table_name="lab_verification_attempts")
    op.drop_index("ux_lab_verification_attempts_authored_session", table_name="lab_verification_attempts")
    with op.batch_alter_table("lab_verification_attempts") as batch:
        batch.drop_column("authored_session_id")
    # Frozen rows are rejected above, so restoring the previous predicate
    # cannot silently release a still-live lab from global exclusion.
    _recreate_exclusion_index(_ACTIVE_BEFORE)
