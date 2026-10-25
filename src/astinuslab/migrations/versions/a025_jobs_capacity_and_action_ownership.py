"""Durable action identity and capacity scheduling state."""

import sqlalchemy as sa
from alembic import op

revision = "a025_jobs_capacity_and_action_ownership"
down_revision = "a024_job_network_name_excludes_destroyed"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("host_actions", sa.Column("dispatched_at", sa.DateTime(), nullable=True))
    op.add_column("host_actions", sa.Column("dispatch_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("host_actions", sa.Column("owner_token", sa.String(255), nullable=True))
    op.add_column("host_actions", sa.Column("lease_expires_at", sa.DateTime(), nullable=True))
    op.add_column("host_actions", sa.Column("celery_task_id", sa.String(255), nullable=True))


def downgrade():
    for name in ("celery_task_id", "lease_expires_at", "owner_token", "dispatched_at", "dispatch_attempts"):
        op.drop_column("host_actions", name)
