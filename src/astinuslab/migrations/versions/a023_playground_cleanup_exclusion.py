"""Keep cleanup-bound playground sessions globally exclusive.

Revision ID: a023_playground_cleanup_exclusion
Revises: a022_job_network_updated_at
Create Date: 2026-08-24 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a023_playground_cleanup_exclusion"
down_revision = "a022_job_network_updated_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ux_playground_sessions_one_active", table_name="playground_sessions")
    connection = op.get_bind()
    # a022 excluded cleanup states from the unique index. More than one failed
    # teardown could therefore accumulate, and creating the widened index
    # directly would abort a real upgrade. Preserve one oldest cleanup owner
    # and serialize the rest in non-active backlog states. Terminal repository
    # transitions promote the next backlog row in the same transaction, so
    # there is never a committed gap in the global exclusion.
    connection.execute(
        sa.text(
            """UPDATE playground_sessions
                SET status = 'cancelled',
                    finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP)
                WHERE status IN ('cancel_requested', 'destroy_requested', 'destroying', 'destroy_failed')
                  AND NOT (
                    EXISTS (SELECT 1 FROM job_vms
                            WHERE job_vms.session_id = playground_sessions.id
                              AND job_vms.state != 'destroyed')
                    OR EXISTS (SELECT 1 FROM job_networks
                               WHERE job_networks.session_id = playground_sessions.id
                                 AND job_networks.state != 'destroyed')
                  )"""
        )
    )
    connection.execute(
        sa.text(
            """UPDATE playground_sessions
                SET status = 'failure_cleanup_pending', finished_at = NULL
                WHERE status = 'failed' AND (
                    EXISTS (SELECT 1 FROM job_vms
                            WHERE job_vms.session_id = playground_sessions.id
                              AND job_vms.state != 'destroyed')
                    OR EXISTS (SELECT 1 FROM job_networks
                               WHERE job_networks.session_id = playground_sessions.id
                                 AND job_networks.state != 'destroyed')
                )"""
        )
    )
    connection.execute(
        sa.text(
            """UPDATE playground_sessions
                SET status = 'destroy_cleanup_pending', finished_at = NULL
                WHERE status IN ('cancel_requested', 'destroy_requested', 'destroying', 'destroy_failed')
                  AND (
                    EXISTS (SELECT 1 FROM job_vms
                            WHERE job_vms.session_id = playground_sessions.id
                              AND job_vms.state != 'destroyed')
                    OR EXISTS (SELECT 1 FROM job_networks
                               WHERE job_networks.session_id = playground_sessions.id
                                 AND job_networks.state != 'destroyed')
                  )"""
        )
    )
    connection.execute(
        sa.text(
            """UPDATE playground_sessions
               SET status = CASE status
                   WHEN 'failure_cleanup_pending' THEN 'failure_cleanup'
                   ELSE 'destroy_failed'
               END
               WHERE id = (
                   SELECT id FROM playground_sessions
                   WHERE status IN ('destroy_cleanup_pending', 'failure_cleanup_pending')
                   ORDER BY created_at, id
                   LIMIT 1
               )
               AND NOT EXISTS (
                   SELECT 1 FROM playground_sessions
                   WHERE status IN ('pending', 'queued', 'running')
               )"""
        )
    )
    op.create_index(
        "ux_playground_sessions_one_active",
        "playground_sessions",
        [sa.text("(1)")],
        unique=True,
        sqlite_where=sa.text(
            "status IN ('pending', 'queued', 'running', 'cancel_requested', "
            "'destroy_requested', 'destroying', 'destroy_failed', 'failure_cleanup')"
        ),
    )


def downgrade() -> None:
    op.drop_index("ux_playground_sessions_one_active", table_name="playground_sessions")
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """UPDATE playground_sessions
               SET status = 'destroy_failed'
               WHERE status = 'destroy_cleanup_pending'"""
        )
    )
    connection.execute(
        sa.text(
            """UPDATE playground_sessions
               SET status = 'failed'
               WHERE status IN ('failure_cleanup', 'failure_cleanup_pending')"""
        )
    )
    op.create_index(
        "ux_playground_sessions_one_active",
        "playground_sessions",
        [sa.text("(1)")],
        unique=True,
        sqlite_where=sa.text("status IN ('pending', 'queued', 'running')"),
    )
