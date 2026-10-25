"""Fence package Prepare writes independently of wall-clock claim age.

Revision ID: a044_prepare_execution_token
Revises: a043_host_action_source_artifact

Quiesce all package-mutating API/worker/reconciliation processes for upgrade or
downgrade. Old workers neither hold the new package lock nor check this token.
Existing attempts retain their state and recover with a new UUID under custody.
"""

import sqlalchemy as sa
from alembic import op

revision = "a044_prepare_execution_token"
down_revision = "a043_host_action_source_artifact"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("lab_prepare_attempts", sa.Column("execution_token", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("lab_prepare_attempts", "execution_token")
