"""Journal content-store publication before package ownership.

Revision ID: a031_content_publication_attempts
Revises: a030_package_verifications
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a031_content_publication_attempts"
down_revision = "a030_package_verifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lab_content_publication_attempts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("final_path", sa.Text(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="reserved"),
        sa.Column("error_summary", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime()),
        sa.CheckConstraint("size_bytes > 0", name="ck_lab_content_publications_size_positive"),
        sa.CheckConstraint(
            "state IN ('reserved', 'published', 'completed', 'failed', 'quarantined')",
            name="ck_lab_content_publications_state",
        ),
    )
    op.create_index(
        "ux_lab_content_publications_digest_live",
        "lab_content_publication_attempts",
        ["sha256", "size_bytes"],
        unique=True,
        sqlite_where=sa.text("state IN ('reserved', 'published')"),
    )
    op.create_index("ix_lab_content_publications_state", "lab_content_publication_attempts", ["state"])


def downgrade() -> None:
    op.drop_index("ix_lab_content_publications_state", table_name="lab_content_publication_attempts")
    op.drop_index("ux_lab_content_publications_digest_live", table_name="lab_content_publication_attempts")
    op.drop_table("lab_content_publication_attempts")
