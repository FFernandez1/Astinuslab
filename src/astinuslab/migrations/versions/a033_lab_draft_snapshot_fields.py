"""Store the complete creator draft snapshot and verification progress.

Revision ID: a033_lab_draft_snapshot_fields
Revises: a032_package_storage_invariants
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a033_lab_draft_snapshot_fields"
down_revision = "a032_package_storage_invariants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("lab_drafts") as batch:
        batch.add_column(sa.Column("runbook", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("supporting_files_json", sa.Text(), nullable=False, server_default="{}"))
        batch.add_column(sa.Column("authored_session_id", sa.String(64), nullable=True))
    with op.batch_alter_table("lab_verification_attempts") as batch:
        batch.add_column(sa.Column("progress_percent", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("lab_verification_attempts") as batch:
        batch.drop_column("cancel_requested")
        batch.drop_column("progress_percent")
    with op.batch_alter_table("lab_drafts") as batch:
        batch.drop_column("authored_session_id")
        batch.drop_column("supporting_files_json")
        batch.drop_column("runbook")
