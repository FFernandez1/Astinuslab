"""Add the evidence tombstone table.

Revision ID: a021_evidence_tombstones
Revises: a020_artifact_publication_attempts
Create Date: 2026-08-22 00:00:00.000000

A deleted artifact leaves nothing behind in the artifacts table, so the custody
record needs its own home. Existing installs get the table empty: deletions that
happened before this migration were not recorded and inventing rows for them
would fabricate custody history.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a021_evidence_tombstones"
down_revision = "a020_artifact_publication_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evidence_tombstones",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("artifact_id", sa.String(length=64), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=True),
        sa.Column("producer", sa.String(length=64), nullable=True),
        sa.Column("sha256", sa.String(length=128), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("job_id", sa.String(length=64), nullable=True),
        sa.Column("acquisition_id", sa.String(length=64), nullable=True),
        sa.Column("case_id", sa.String(length=64), nullable=True),
        sa.Column("file_set", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("forced", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("protection_override", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_evidence_tombstones_artifact_id", "evidence_tombstones", ["artifact_id"])
    op.create_index("ix_evidence_tombstones_job_id", "evidence_tombstones", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_evidence_tombstones_job_id", table_name="evidence_tombstones")
    op.drop_index("ix_evidence_tombstones_artifact_id", table_name="evidence_tombstones")
    op.drop_table("evidence_tombstones")
