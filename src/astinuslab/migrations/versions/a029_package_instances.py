"""Add durable package instance intent and operation outbox.

Revision ID: a029_package_instances
Revises: a028_package_storage
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a029_package_instances"
down_revision = "a028_package_storage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lab_instances",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "package_record_id", sa.String(64), sa.ForeignKey("lab_packages.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("playground_session_id", sa.String(64), sa.ForeignKey("playground_sessions.id", ondelete="SET NULL")),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="registered"),
        sa.Column("error_summary", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "state IN ('registered', 'creating', 'ready', 'failed', 'cancel_requested', 'rollback_pending', 'cleanup_incomplete', 'resetting', 'destroying', 'destroyed')",
            name="ck_lab_instances_state",
        ),
    )
    op.create_index(
        "ux_lab_instances_package_idempotency", "lab_instances", ["package_record_id", "idempotency_key"], unique=True
    )
    op.create_index("ix_lab_instances_package_state", "lab_instances", ["package_record_id", "state"])
    op.create_table(
        "lab_instance_actions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("instance_id", sa.String(64), sa.ForeignKey("lab_instances.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("payload_json", sa.Text()),
        sa.Column("error_summary", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_lab_instance_actions_instance", "lab_instance_actions", ["instance_id"])
    op.create_table(
        "lab_operation_outbox",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("operation_kind", sa.String(64), nullable=False),
        sa.Column("operation_id", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("state", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("claimed_at", sa.DateTime()),
        sa.Column("completed_at", sa.DateTime()),
        sa.CheckConstraint(
            "state IN ('pending', 'claimed', 'completed', 'failed')", name="ck_lab_operation_outbox_state"
        ),
    )
    op.create_index(
        "ux_lab_operation_outbox_kind_operation",
        "lab_operation_outbox",
        ["operation_kind", "operation_id"],
        unique=True,
    )
    op.create_index("ix_lab_operation_outbox_state", "lab_operation_outbox", ["state"])
    # SQLite needs table recreation to add an FK.  Its reflection skips the
    # expression index below, so recreate that invariant explicitly after the
    # batch operation on both upgrade and downgrade.
    op.execute("PRAGMA foreign_keys=OFF")
    with op.batch_alter_table("playground_sessions", recreate="always") as batch:
        batch.add_column(sa.Column("lab_package_record_id", sa.String(64), nullable=True))
        batch.create_foreign_key(
            "fk_playground_sessions_lab_package", "lab_packages", ["lab_package_record_id"], ["id"], ondelete="SET NULL"
        )
    op.execute(
        "CREATE UNIQUE INDEX ux_playground_sessions_one_active ON playground_sessions ((1)) WHERE status IN ('pending', 'queued', 'running', 'cancel_requested', 'destroy_requested', 'destroying', 'destroy_failed', 'failure_cleanup')"
    )
    op.execute("PRAGMA foreign_keys=ON")


def downgrade() -> None:
    op.execute("PRAGMA foreign_keys=OFF")
    with op.batch_alter_table("playground_sessions", recreate="always") as batch:
        batch.drop_constraint("fk_playground_sessions_lab_package", type_="foreignkey")
        batch.drop_column("lab_package_record_id")
    op.execute(
        "CREATE UNIQUE INDEX ux_playground_sessions_one_active ON playground_sessions ((1)) WHERE status IN ('pending', 'queued', 'running', 'cancel_requested', 'destroy_requested', 'destroying', 'destroy_failed', 'failure_cleanup')"
    )
    op.execute("PRAGMA foreign_keys=ON")
    op.drop_index("ix_lab_operation_outbox_state", table_name="lab_operation_outbox")
    op.drop_index("ux_lab_operation_outbox_kind_operation", table_name="lab_operation_outbox")
    op.drop_table("lab_operation_outbox")
    op.drop_index("ix_lab_instance_actions_instance", table_name="lab_instance_actions")
    op.drop_table("lab_instance_actions")
    op.drop_index("ix_lab_instances_package_state", table_name="lab_instances")
    op.drop_index("ux_lab_instances_package_idempotency", table_name="lab_instances")
    op.drop_table("lab_instances")
