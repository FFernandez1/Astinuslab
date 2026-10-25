"""Add a transition timestamp to playground network rows.

Revision ID: a022_job_network_updated_at
Revises: a021_evidence_tombstones
Create Date: 2026-08-23 00:00:00.000000

Create retries need a fresh reconciliation grace window without rewriting the
row's historical creation time. Existing rows inherit their creation timestamp.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a022_job_network_updated_at"
down_revision = "a021_evidence_tombstones"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("job_networks") as batch:
        batch.add_column(sa.Column("updated_at", sa.DateTime(), nullable=True))
    op.execute("UPDATE job_networks SET updated_at = created_at WHERE updated_at IS NULL")
    with op.batch_alter_table("job_networks") as batch:
        batch.alter_column("updated_at", existing_type=sa.DateTime(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("job_networks") as batch:
        batch.drop_column("updated_at")
