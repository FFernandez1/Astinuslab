"""Persist guest-visible VM interface order.

Revision ID: a027_job_vm_nic_ordinals
Revises: a026_lab_packages
Create Date: 2026-09-01 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a027_job_vm_nic_ordinals"
down_revision = "a026_lab_packages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add nullable first so existing databases can be deterministically filled
    # before the uniqueness constraint makes the invariant enforceable.
    with op.batch_alter_table("job_vm_nics") as batch:
        batch.add_column(sa.Column("ordinal", sa.Integer(), nullable=True))

    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, job_vm_id FROM job_vm_nics ORDER BY job_vm_id, created_at, id")).mappings()
    current_vm: str | None = None
    ordinal = -1
    for row in rows:
        if row["job_vm_id"] != current_vm:
            current_vm = row["job_vm_id"]
            ordinal = 0
        else:
            ordinal += 1
        bind.execute(
            sa.text("UPDATE job_vm_nics SET ordinal = :ordinal WHERE id = :id"), {"ordinal": ordinal, "id": row["id"]}
        )

    with op.batch_alter_table("job_vm_nics") as batch:
        batch.alter_column("ordinal", existing_type=sa.Integer(), nullable=False)
        batch.create_index("ux_job_vm_nics_vm_ordinal", ["job_vm_id", "ordinal"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("job_vm_nics") as batch:
        batch.drop_index("ux_job_vm_nics_vm_ordinal")
        batch.drop_column("ordinal")
