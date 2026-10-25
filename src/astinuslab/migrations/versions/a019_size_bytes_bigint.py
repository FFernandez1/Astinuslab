"""Promote artifact and acquisition size_bytes columns to BigInteger.

Revision ID: a019_size_bytes_bigint
Revises: a018_drop_session_expiry
Create Date: 2026-07-02 00:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a019_size_bytes_bigint"
down_revision = "a018_drop_session_expiry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("acquisitions") as batch:
        batch.alter_column("size_bytes", existing_type=sa.Integer(), type_=sa.BigInteger(), existing_nullable=True)
    with op.batch_alter_table("artifacts") as batch:
        batch.alter_column("size_bytes", existing_type=sa.Integer(), type_=sa.BigInteger(), existing_nullable=True)


def downgrade() -> None:
    with op.batch_alter_table("artifacts") as batch:
        batch.alter_column("size_bytes", existing_type=sa.BigInteger(), type_=sa.Integer(), existing_nullable=True)
    with op.batch_alter_table("acquisitions") as batch:
        batch.alter_column("size_bytes", existing_type=sa.BigInteger(), type_=sa.Integer(), existing_nullable=True)
