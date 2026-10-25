"""Join portable-package and capacity scheduling schema histories.

Revision ID: a038_merge_packages_capacity
Revises: a037_nic_detach_claim, a026_jobs_capacity

The branches modify separate schema objects. Alembic applies the missing
branch before reaching this revision; neither history needs to be rewritten.
"""

from __future__ import annotations

revision = "a038_merge_packages_capacity"
down_revision = ("a037_nic_detach_claim", "a026_jobs_capacity")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
