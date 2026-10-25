"""Pin an analysis action to the memory artifact it was queued against.

Revision ID: a043_host_action_source_artifact
Revises: a042_job_acquisition_index

Queued Volatility work used to read whatever memory artifact its Job presented
at execution time, so a dump uploaded after the action was queued became the
analysed evidence. The new column is the live reference; the action payload
carries a matching identity snapshot that outlives deletion.

Existing rows stay null. A legacy analysis action is therefore not backfilled:
the newest dump is a guess, and the worker refuses an unprovable input instead
of attributing a report to evidence nobody selected. Stop dispatchers and
workers before upgrading and restart them on this revision.
"""

from __future__ import annotations

from alembic import op

revision = "a043_host_action_source_artifact"
down_revision = "a042_job_acquisition_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable ADD COLUMN REFERENCES works in SQLite without toggling FK policy
    # or rebuilding host_actions, matching a039's artifact reference additions.
    op.execute(
        "ALTER TABLE host_actions ADD COLUMN source_artifact_id VARCHAR(64) REFERENCES artifacts(id) ON DELETE SET NULL"
    )
    op.create_index("ix_host_actions_source_artifact_id", "host_actions", ["source_artifact_id"])


def downgrade() -> None:
    op.drop_index("ix_host_actions_source_artifact_id", table_name="host_actions")
    # ORM-created databases declare the FK at table level; SQLite DROP COLUMN
    # cannot remove those, so rebuild this one table as a039's downgrade does.
    with op.batch_alter_table("host_actions", recreate="always") as batch:
        batch.drop_column("source_artifact_id")
