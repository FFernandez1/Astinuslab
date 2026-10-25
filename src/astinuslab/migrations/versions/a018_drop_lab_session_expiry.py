"""drop jobs.lab_session_expires_at (sessions no longer expire)

Revision ID: a018_drop_session_expiry
Revises: a017_session_ids
Create Date: 2026-06-13 00:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a018_drop_session_expiry"
down_revision = "a017_session_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("lab_session_expires_at")


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("lab_session_expires_at", sa.DateTime(), nullable=True))
