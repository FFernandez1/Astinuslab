"""Persist effective VM resource allocations.

Revision ID: a025_job_vm_resources
Revises: a024_job_network_name_excludes_destroyed
Create Date: 2026-08-30 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a025_job_vm_resources"
down_revision = "a024_job_network_name_excludes_destroyed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("job_vms", sa.Column("vcpus", sa.Integer(), nullable=True))
    op.add_column("job_vms", sa.Column("memory_mb", sa.Integer(), nullable=True))
    op.add_column("job_vms", sa.Column("disk_gb", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("job_vms", "disk_gb")
    op.drop_column("job_vms", "memory_mb")
    op.drop_column("job_vms", "vcpus")
