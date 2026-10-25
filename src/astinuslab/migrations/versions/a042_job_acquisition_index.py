"""Index the jobs acquisition lookup used by grouped collection counts.

Revision ID: a042_job_acquisition_index
Revises: a041_admin_browser_sessions
Create Date: 2026-09-08 00:00:00.000000
"""

from __future__ import annotations

from alembic import op

revision = "a042_job_acquisition_index"
down_revision = "a041_admin_browser_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_jobs_acquisition_id", "jobs", ["acquisition_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_jobs_acquisition_id", table_name="jobs")
