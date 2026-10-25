"""Journal durable package archive and content ownership.

Revision ID: a028_package_storage
Revises: a027_job_vm_nic_ordinals
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a028_package_storage"
down_revision = "a027_job_vm_nic_ordinals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lab_content_objects",
        sa.Column("sha256", sa.String(64), primary_key=True),
        sa.Column("size_bytes", sa.BigInteger(), primary_key=True),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("filesystem_fingerprint", sa.String(256)),
        sa.Column("last_verified_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime()),
        sa.CheckConstraint("size_bytes > 0", name="ck_lab_content_objects_size_positive"),
    )
    op.create_index("ix_lab_content_objects_last_used_at", "lab_content_objects", ["last_used_at"])
    op.create_table(
        "lab_package_inspections",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("staged_path", sa.Text(), nullable=False),
        sa.Column("archive_sha256", sa.String(64)),
        sa.Column("archive_size_bytes", sa.BigInteger()),
        sa.Column("inspection_json", sa.Text()),
        sa.Column("signature_status", sa.String(32)),
        sa.Column("creator_key_fingerprint", sa.String(64)),
        sa.Column("state", sa.String(32), nullable=False, server_default="reserved"),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("error_summary", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "state IN ('reserved', 'staged', 'committed', 'expired', 'failed')", name="ck_lab_package_inspections_state"
        ),
    )
    op.create_index("ix_lab_package_inspections_expiry", "lab_package_inspections", ["expires_at"])
    op.create_index("ix_lab_package_inspections_state", "lab_package_inspections", ["state"])
    op.create_table(
        "lab_package_publication_attempts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("inspection_id", sa.String(64), sa.ForeignKey("lab_package_inspections.id", ondelete="SET NULL")),
        sa.Column("package_record_id", sa.String(64), sa.ForeignKey("lab_packages.id", ondelete="SET NULL")),
        sa.Column("package_id", sa.String(128), nullable=False),
        sa.Column("version", sa.String(128), nullable=False),
        sa.Column("archive_sha256", sa.String(64), nullable=False),
        sa.Column("staging_path", sa.Text(), nullable=False),
        sa.Column("final_path", sa.Text(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="reserved"),
        sa.Column("error_summary", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime()),
        sa.CheckConstraint(
            "state IN ('reserved', 'published', 'completed', 'failed', 'cleanup_incomplete')",
            name="ck_lab_package_publications_state",
        ),
    )
    op.create_index(
        "ux_lab_package_publications_live_final",
        "lab_package_publication_attempts",
        ["final_path"],
        unique=True,
        sqlite_where=sa.text("state IN ('reserved', 'published', 'completed')"),
    )
    op.create_index("ix_lab_package_publications_state", "lab_package_publication_attempts", ["state"])
    op.create_index(
        "ux_lab_package_publications_identity_nonfailed",
        "lab_package_publication_attempts",
        ["package_id", "version"],
        unique=True,
        sqlite_where=sa.text("state != 'failed'"),
    )
    op.create_table(
        "lab_package_assets",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "package_record_id", sa.String(64), sa.ForeignKey("lab_packages.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("logical_path", sa.String(512), nullable=False),
        sa.Column("role", sa.String(64), nullable=False, server_default="asset"),
        sa.Column("expected_sha256", sa.String(64), nullable=False),
        sa.Column("expected_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(256), nullable=False),
        sa.Column("origin", sa.String(32), nullable=False, server_default="embedded"),
        sa.Column("source_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("content_sha256", sa.String(64)),
        sa.Column("content_size_bytes", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["content_sha256", "content_size_bytes"],
            ["lab_content_objects.sha256", "lab_content_objects.size_bytes"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("expected_size_bytes > 0", name="ck_lab_package_assets_expected_size_positive"),
    )
    op.create_index(
        "ux_lab_package_assets_path_role",
        "lab_package_assets",
        ["package_record_id", "logical_path", "role"],
        unique=True,
    )
    op.create_index("ix_lab_package_assets_content", "lab_package_assets", ["content_sha256", "content_size_bytes"])
    op.create_table(
        "lab_package_export_attempts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("package_id", sa.String(128), nullable=False),
        sa.Column("version", sa.String(128), nullable=False),
        sa.Column("definition_sha256", sa.String(64), nullable=False),
        sa.Column("staging_path", sa.Text()),
        sa.Column("archive_sha256", sa.String(64)),
        sa.Column("state", sa.String(32), nullable=False, server_default="reserved"),
        sa.Column("expires_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime()),
        sa.CheckConstraint(
            "state IN ('reserved', 'published', 'completed', 'failed')", name="ck_lab_package_exports_state"
        ),
        sa.CheckConstraint(
            "state != 'completed' OR archive_sha256 IS NOT NULL", name="ck_lab_package_exports_completed_digest"
        ),
    )
    op.create_index(
        "ux_lab_package_exports_live_identity",
        "lab_package_export_attempts",
        ["package_id", "version"],
        unique=True,
        sqlite_where=sa.text("state IN ('reserved', 'published')"),
    )
    op.create_index(
        "ux_lab_package_exports_final_identity",
        "lab_package_export_attempts",
        ["package_id", "version"],
        unique=True,
        sqlite_where=sa.text("state = 'completed'"),
    )
    op.create_table(
        "lab_drafts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("definition_json", sa.Text(), nullable=False),
        sa.Column("definition_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("lab_drafts")
    op.drop_index("ux_lab_package_exports_final_identity", table_name="lab_package_export_attempts")
    op.drop_index("ux_lab_package_exports_live_identity", table_name="lab_package_export_attempts")
    op.drop_table("lab_package_export_attempts")
    op.drop_index("ix_lab_package_assets_content", table_name="lab_package_assets")
    op.drop_index("ux_lab_package_assets_path_role", table_name="lab_package_assets")
    op.drop_table("lab_package_assets")
    op.drop_index("ix_lab_package_publications_state", table_name="lab_package_publication_attempts")
    op.drop_index("ux_lab_package_publications_identity_nonfailed", table_name="lab_package_publication_attempts")
    op.drop_index("ux_lab_package_publications_live_final", table_name="lab_package_publication_attempts")
    op.drop_table("lab_package_publication_attempts")
    op.drop_index("ix_lab_package_inspections_state", table_name="lab_package_inspections")
    op.drop_index("ix_lab_package_inspections_expiry", table_name="lab_package_inspections")
    op.drop_table("lab_package_inspections")
    op.drop_index("ix_lab_content_objects_last_used_at", table_name="lab_content_objects")
    op.drop_table("lab_content_objects")
