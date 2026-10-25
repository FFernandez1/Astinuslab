"""Persist imported portable lab package records.

Revision ID: a026_lab_packages
Revises: a025_job_vm_resources
Create Date: 2026-08-31 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a026_lab_packages"
down_revision = "a025_job_vm_resources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lab_packages",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("package_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("creator", sa.String(length=160), nullable=False),
        sa.Column("license", sa.String(length=256), nullable=False),
        sa.Column("reproducibility", sa.String(length=16), nullable=False),
        sa.Column("archive_path", sa.Text(), nullable=False),
        sa.Column("archive_sha256", sa.String(length=64), nullable=False),
        sa.Column("archive_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("manifest_json", sa.Text(), nullable=False),
        sa.Column("definition_json", sa.Text(), nullable=False),
        sa.Column("required_capabilities_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("signature_status", sa.String(length=32), server_default="unsigned", nullable=False),
        sa.Column("creator_key_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("imported_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("archive_size_bytes > 0", name="ck_lab_packages_archive_size_positive"),
        sa.CheckConstraint("reproducibility IN ('locked', 'unlocked')", name="ck_lab_packages_reproducibility"),
        sa.CheckConstraint(
            "signature_status IN ('unsigned', 'invalid-signature', 'valid-untrusted-key', 'valid-locally-trusted-key')",
            name="ck_lab_packages_signature_status",
        ),
    )
    op.create_index("ux_lab_packages_package_id_version", "lab_packages", ["package_id", "version"], unique=True)
    op.create_index("ix_lab_packages_title", "lab_packages", ["title"], unique=False)
    op.create_index("ix_lab_packages_signature_status", "lab_packages", ["signature_status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_lab_packages_signature_status", table_name="lab_packages")
    op.drop_index("ix_lab_packages_title", table_name="lab_packages")
    op.drop_index("ux_lab_packages_package_id_version", table_name="lab_packages")
    op.drop_table("lab_packages")
