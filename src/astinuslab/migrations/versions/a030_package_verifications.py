"""Persist package verification/prepare attempts, leases and delete tombstones.

Revision ID: a030_package_verifications
Revises: a029_package_instances
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a030_package_verifications"
down_revision = "a029_package_instances"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lab_package_attestations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("input_draft_sha256", sa.String(64), nullable=False),
        sa.Column("locked_definition_sha256", sa.String(64), nullable=False),
        sa.Column("input_digests_json", sa.Text(), nullable=False),
        sa.Column("runtime_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_lab_package_attestations_definition", "lab_package_attestations", ["locked_definition_sha256"])
    op.create_table(
        "lab_verification_attempts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("draft_id", sa.String(64), sa.ForeignKey("lab_drafts.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("definition_sha256", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="reserved"),
        sa.Column("result_json", sa.Text()),
        sa.Column("error_summary", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "state IN ('reserved', 'running', 'verified', 'failed', 'cancelled')",
            name="ck_lab_verification_attempts_state",
        ),
    )
    op.create_index("ix_lab_verification_attempts_draft", "lab_verification_attempts", ["draft_id"])
    op.create_table(
        "lab_prepare_attempts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "package_record_id", sa.String(64), sa.ForeignKey("lab_packages.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("definition_sha256", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="reserved"),
        sa.Column("result_json", sa.Text()),
        sa.Column("expires_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_lab_prepare_attempts_package", "lab_prepare_attempts", ["package_record_id"])
    op.create_table(
        "lab_storage_leases",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("resource_kind", sa.String(32), nullable=False),
        sa.Column("resource_id", sa.String(128), nullable=False),
        sa.Column("operation_kind", sa.String(64), nullable=False),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ux_lab_storage_leases_resource_owner",
        "lab_storage_leases",
        ["resource_kind", "resource_id", "owner_id"],
        unique=True,
    )
    op.create_index("ix_lab_storage_leases_expiry", "lab_storage_leases", ["expires_at"])
    op.create_table(
        "lab_package_tombstones",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("package_record_id", sa.String(64), nullable=False),
        sa.Column("archive_path", sa.Text(), nullable=False),
        sa.Column("archive_sha256", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime()),
    )
    op.create_index("ix_lab_package_tombstones_state", "lab_package_tombstones", ["state"])


def downgrade() -> None:
    op.drop_index("ix_lab_package_tombstones_state", table_name="lab_package_tombstones")
    op.drop_table("lab_package_tombstones")
    op.drop_index("ix_lab_storage_leases_expiry", table_name="lab_storage_leases")
    op.drop_index("ux_lab_storage_leases_resource_owner", table_name="lab_storage_leases")
    op.drop_table("lab_storage_leases")
    op.drop_index("ix_lab_prepare_attempts_package", table_name="lab_prepare_attempts")
    op.drop_table("lab_prepare_attempts")
    op.drop_index("ix_lab_verification_attempts_draft", table_name="lab_verification_attempts")
    op.drop_table("lab_verification_attempts")
    op.drop_index("ix_lab_package_attestations_definition", table_name="lab_package_attestations")
    op.drop_table("lab_package_attestations")
