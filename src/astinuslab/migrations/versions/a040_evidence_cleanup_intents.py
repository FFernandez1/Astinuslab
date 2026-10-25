"""Persist recoverable evidence cleanup intent separately from its outcome.

Revision ID: a040_evidence_cleanup_intents
Revises: a039_playground_artifact_ownership
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a040_evidence_cleanup_intents"
down_revision = "a039_playground_artifact_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Historical tombstones never authorize new destructive cleanup.
    op.add_column(
        "evidence_tombstones", sa.Column("cleanup_state", sa.String(16), nullable=False, server_default="legacy")
    )
    op.add_column("evidence_tombstones", sa.Column("cleanup_metadata", sa.Text(), nullable=True))
    # Repair references left by the former partial manual cascade implementation.
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "DELETE FROM reports WHERE job_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.id = reports.job_id)"
        )
    )
    for table, field in (
        ("reports", "input_artifact_id"),
        ("reports", "output_artifact_id"),
        ("artifact_publication_attempts", "artifact_id"),
    ):
        connection.execute(
            sa.text(
                f"UPDATE {table} SET {field} = NULL WHERE {field} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM artifacts WHERE artifacts.id = {table}.{field})"  # noqa: S608 — fixed identifiers
            )
        )


def downgrade() -> None:
    # Never discard actionable cleanup records; finish/review them before downgrade.
    pending = (
        op.get_bind()
        .execute(sa.text("SELECT COUNT(*) FROM evidence_tombstones WHERE cleanup_state IN ('pending', 'failed')"))
        .scalar_one()
    )
    if pending:
        raise RuntimeError("cannot downgrade with pending or failed evidence cleanup intents")
    op.drop_column("evidence_tombstones", "cleanup_metadata")
    op.drop_column("evidence_tombstones", "cleanup_state")
