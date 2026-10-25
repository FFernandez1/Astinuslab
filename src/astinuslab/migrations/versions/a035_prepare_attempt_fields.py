"""Persist resumable prepare state and per-asset resolution ownership.

Revision ID: a035_prepare_attempt_fields
Revises: a034_polymorphic_artifact_job_reference
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a035_prepare_attempt_fields"
down_revision = "a034_polymorphic_artifact_job_reference"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("lab_package_assets") as batch:
        batch.add_column(sa.Column("prepare_state", sa.String(32), nullable=False, server_default="missing"))
        batch.add_column(sa.Column("prepare_attempt_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("prepare_error_summary", sa.Text(), nullable=True))
        batch.add_column(sa.Column("prepared_origin", sa.String(32), nullable=True))
        batch.add_column(sa.Column("prepared_at", sa.DateTime(), nullable=True))
        batch.create_check_constraint(
            "ck_lab_package_assets_prepare_state",
            "prepare_state IN ('missing', 'resolving', 'ready', 'failed', 'cancelled')",
        )

    with op.batch_alter_table("lab_prepare_attempts") as batch:
        batch.add_column(sa.Column("idempotency_key", sa.String(128), nullable=False, server_default="legacy"))
        batch.add_column(sa.Column("progress_percent", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("error_summary", sa.Text(), nullable=True))
        batch.add_column(sa.Column("claimed_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("network_opt_in", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.create_check_constraint(
            "ck_lab_prepare_attempts_state",
            "state IN ('reserved', 'running', 'completed', 'failed', 'cancelled')",
        )
        batch.create_check_constraint(
            "ck_lab_prepare_attempts_progress",
            "progress_percent >= 0 AND progress_percent <= 100",
        )

    # The table was present before prepare requests had client idempotency.
    # Give every historical row a distinct compatibility key before adding the
    # live uniqueness rule; these rows are not safe to merge silently.
    op.execute("UPDATE lab_prepare_attempts SET idempotency_key = 'legacy:' || id WHERE idempotency_key = 'legacy'")

    op.create_index("ix_lab_prepare_attempts_state", "lab_prepare_attempts", ["state"])
    op.create_index(
        "ux_lab_prepare_attempts_package_idempotency",
        "lab_prepare_attempts",
        ["package_record_id", "idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_lab_prepare_attempts_package_idempotency", table_name="lab_prepare_attempts")
    op.drop_index("ix_lab_prepare_attempts_state", table_name="lab_prepare_attempts")
    with op.batch_alter_table("lab_prepare_attempts") as batch:
        batch.drop_constraint("ck_lab_prepare_attempts_progress", type_="check")
        batch.drop_constraint("ck_lab_prepare_attempts_state", type_="check")
        batch.drop_column("network_opt_in")
        batch.drop_column("claimed_at")
        batch.drop_column("error_summary")
        batch.drop_column("cancel_requested")
        batch.drop_column("progress_percent")
        batch.drop_column("idempotency_key")
    with op.batch_alter_table("lab_package_assets") as batch:
        batch.drop_constraint("ck_lab_package_assets_prepare_state", type_="check")
        batch.drop_column("prepared_at")
        batch.drop_column("prepared_origin")
        batch.drop_column("prepare_error_summary")
        batch.drop_column("prepare_attempt_id")
        batch.drop_column("prepare_state")
