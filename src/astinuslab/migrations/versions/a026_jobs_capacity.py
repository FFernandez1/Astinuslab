"""Durable FIFO capacity reservations and public wait reasons."""

import sqlalchemy as sa
from alembic import op

revision = "a026_jobs_capacity"
down_revision = "a025_jobs_capacity_and_action_ownership"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("jobs", sa.Column("capacity_reason", sa.Text(), nullable=True))
    op.create_table(
        "capacity_reservations",
        sa.Column("job_id", sa.String(64), sa.ForeignKey("jobs.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("vm_count", sa.Integer(), nullable=False),
        sa.Column("memory_mb", sa.Integer(), nullable=False),
        sa.Column("storage_bytes", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("preserve_failed_vm", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("boot_started", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("released_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_capacity_reservations_state", "capacity_reservations", ["state"])


def downgrade():
    op.drop_table("capacity_reservations")
    op.drop_column("jobs", "capacity_reason")
