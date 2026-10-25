"""Persist exclusive Playground NIC detach claims.

Revision ID: a037_nic_detach_claim
Revises: a036_playground_authoring_closed
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a037_nic_detach_claim"
down_revision = "a036_playground_authoring_closed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("job_vm_nics") as batch:
        batch.add_column(sa.Column("detach_token", sa.String(64), nullable=True))
        batch.add_column(sa.Column("detach_claimed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    claimed_count = (
        op.get_bind()
        .execute(sa.text("SELECT COUNT(*) FROM job_vm_nics WHERE state IN ('detaching', 'detach_claimed')"))
        .scalar_one()
    )
    if claimed_count:
        raise RuntimeError("cannot downgrade while a NIC detach request is in flight")
    with op.batch_alter_table("job_vm_nics") as batch:
        batch.drop_column("detach_claimed_at")
        batch.drop_column("detach_token")
