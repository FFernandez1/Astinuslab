"""Let a destroyed network release its name.

Revision ID: a024_job_network_name_excludes_destroyed
Revises: a023_playground_cleanup_exclusion
Create Date: 2026-08-28 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a024_job_network_name_excludes_destroyed"
down_revision = "a023_playground_cleanup_exclusion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Narrowing a unique index can never conflict with data already in the
    # table, so this needs no reconciliation pass: every row the widened index
    # admitted is still admitted, and destroyed rows simply leave its scope.
    op.drop_index("ux_job_networks_session_id_name", table_name="job_networks")
    op.create_index(
        "ux_job_networks_session_id_name",
        "job_networks",
        ["session_id", "name"],
        unique=True,
        sqlite_where=sa.text("state != 'destroyed'"),
    )


def downgrade() -> None:
    op.drop_index("ux_job_networks_session_id_name", table_name="job_networks")
    connection = op.get_bind()
    # While the partial index was in force a session could legitimately reuse a
    # destroyed network's name, so recreating the total index directly would
    # abort on rows the schema had allowed. Suffix the later duplicates with
    # their own primary key, which is unique per table, and keep the name
    # readable by trimming it to fit the column rather than truncating the id.
    #
    # A row is a duplicate when it has a live namesake or a destroyed one that
    # came first; ordering by (created_at, id) keeps the survivor stable. The
    # survivor never matches the predicate itself, and each loser matches on the
    # survivor alone, so the result does not depend on the order SQLite walks
    # the table in.
    connection.execute(
        sa.text(
            """UPDATE job_networks
                  SET name = substr(name, 1, max(1, 63 - length(id))) || '-' || id
                WHERE state = 'destroyed'
                  AND EXISTS (
                      SELECT 1 FROM job_networks AS other
                       WHERE other.session_id = job_networks.session_id
                         AND other.name = job_networks.name
                         AND other.id <> job_networks.id
                         AND (
                              other.state <> 'destroyed'
                           OR other.created_at < job_networks.created_at
                           OR (other.created_at = job_networks.created_at AND other.id < job_networks.id)
                         )
                  )"""
        )
    )
    op.create_index(
        "ux_job_networks_session_id_name",
        "job_networks",
        ["session_id", "name"],
        unique=True,
    )
