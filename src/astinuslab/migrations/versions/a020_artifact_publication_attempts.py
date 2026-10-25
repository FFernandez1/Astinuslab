"""Add the artifact publication journal.

Revision ID: a020_artifact_publication_attempts
Revises: a019_size_bytes_bigint
Create Date: 2026-08-21 00:00:00.000000

A fresh install builds this table from the models via create_all, so this
migration exists for the operator whose database is already stamped: without it
they upgrade into code that reads a table their schema does not have.

Legacy artifacts are deliberately not backfilled. An attempt row records work
that was in flight; inventing one for an artifact that was published long ago
would put a fabricated history in front of reconciliation.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a020_artifact_publication_attempts"
down_revision = "a019_size_bytes_bigint"
branch_labels = None
depends_on = None

_STATES = (
    "reserved",
    "capturing",
    "staged",
    "db_prepared",
    "published",
    "completed",
    "failed",
    "cleanup_incomplete",
)
_LIVE_STATES = (
    "reserved",
    "capturing",
    "staged",
    "db_prepared",
    "published",
    "completed",
)


def _in_list(states: tuple[str, ...]) -> str:
    return ", ".join(f"'{state}'" for state in states)


def upgrade() -> None:
    op.create_table(
        "artifact_publication_attempts",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("artifact_id", sa.String(length=64), nullable=True),
        sa.Column("acquisition_id", sa.String(length=64), nullable=True),
        sa.Column("job_id", sa.String(length=64), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("producer", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("staging_dir", sa.Text(), nullable=False),
        sa.Column("final_path", sa.Text(), nullable=False),
        sa.Column("manifest_path", sa.Text(), nullable=True),
        sa.Column("sha256_sidecar_path", sa.Text(), nullable=True),
        sa.Column("sha256", sa.String(length=128), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="reserved"),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifacts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["acquisition_id"], ["acquisitions.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            f"state IN ({_in_list(_STATES)})",
            name="ck_artifact_publication_attempts_state",
        ),
    )
    # Partial unique index: one live attempt may own a destination path, while a
    # failed or cleanup_incomplete attempt leaves it free for a retry.
    op.create_index(
        "ux_artifact_publication_attempts_live_final_path",
        "artifact_publication_attempts",
        ["final_path"],
        unique=True,
        sqlite_where=sa.text(f"state IN ({_in_list(_LIVE_STATES)})"),
    )
    op.create_index("ix_artifact_publication_attempts_state", "artifact_publication_attempts", ["state"])
    op.create_index("ix_artifact_publication_attempts_job_id", "artifact_publication_attempts", ["job_id"])
    op.create_index(
        "ix_artifact_publication_attempts_acquisition_id",
        "artifact_publication_attempts",
        ["acquisition_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_artifact_publication_attempts_acquisition_id", table_name="artifact_publication_attempts")
    op.drop_index("ix_artifact_publication_attempts_job_id", table_name="artifact_publication_attempts")
    op.drop_index("ix_artifact_publication_attempts_state", table_name="artifact_publication_attempts")
    op.drop_index(
        "ux_artifact_publication_attempts_live_final_path",
        table_name="artifact_publication_attempts",
    )
    op.drop_table("artifact_publication_attempts")
