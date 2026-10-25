"""rename playground job_id columns to session_id

Revision ID: a017_session_ids
Revises: ef55cafe2cea
Create Date: 2026-06-11 00:00:00.000000

"""

from __future__ import annotations

from alembic import op

revision = "a017_session_ids"
down_revision = "ef55cafe2cea"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("job_vms") as batch:
        batch.alter_column("job_id", new_column_name="session_id")
    with op.batch_alter_table("job_networks") as batch:
        batch.alter_column("job_id", new_column_name="session_id")
    with op.batch_alter_table("playground_topology_layout") as batch:
        batch.alter_column("job_id", new_column_name="session_id")

    op.execute("DROP INDEX IF EXISTS ix_job_vms_job_id")
    op.execute("DROP INDEX IF EXISTS ux_job_vms_job_id_label")
    op.execute("DROP INDEX IF EXISTS ix_job_networks_job_id")
    op.execute("DROP INDEX IF EXISTS ux_job_networks_job_id_name")
    op.execute("DROP INDEX IF EXISTS ix_playground_topology_layout_job_id")
    op.create_index("ix_job_vms_session_id", "job_vms", ["session_id"])
    op.create_index("ux_job_vms_session_id_label", "job_vms", ["session_id", "label"], unique=True)
    op.create_index("ix_job_networks_session_id", "job_networks", ["session_id"])
    op.create_index("ux_job_networks_session_id_name", "job_networks", ["session_id", "name"], unique=True)
    op.create_index("ix_playground_topology_layout_session_id", "playground_topology_layout", ["session_id"])


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_job_vms_session_id")
    op.execute("DROP INDEX IF EXISTS ux_job_vms_session_id_label")
    op.execute("DROP INDEX IF EXISTS ix_job_networks_session_id")
    op.execute("DROP INDEX IF EXISTS ux_job_networks_session_id_name")
    op.execute("DROP INDEX IF EXISTS ix_playground_topology_layout_session_id")

    with op.batch_alter_table("job_vms") as batch:
        batch.alter_column("session_id", new_column_name="job_id")
    with op.batch_alter_table("job_networks") as batch:
        batch.alter_column("session_id", new_column_name="job_id")
    with op.batch_alter_table("playground_topology_layout") as batch:
        batch.alter_column("session_id", new_column_name="job_id")

    op.create_index("ix_job_vms_job_id", "job_vms", ["job_id"])
    op.create_index("ux_job_vms_job_id_label", "job_vms", ["job_id", "label"], unique=True)
    op.create_index("ix_job_networks_job_id", "job_networks", ["job_id"])
    op.create_index("ux_job_networks_job_id_name", "job_networks", ["job_id", "name"], unique=True)
    op.create_index("ix_playground_topology_layout_job_id", "playground_topology_layout", ["job_id"])
