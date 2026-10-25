"""Allow Playground session identifiers in the historical artifact.job_id field.

Revision ID: a034_polymorphic_artifact_job_reference
Revises: a033_lab_draft_snapshot_fields

``artifacts.job_id`` predates the split between forensic jobs and Playground
sessions.  Playground capture code has always recorded its session identifier
there, but the baseline FK only accepts ``jobs.id``.  With SQLite foreign keys
now correctly enabled this made every valid Playground capture fail.  Keep the
legacy column (and its query/API surface) while removing the over-specific FK;
the publication lifecycle owns cleanup for both kinds of producer.
"""

from __future__ import annotations

from alembic import op

revision = "a034_polymorphic_artifact_job_reference"
down_revision = "a033_lab_draft_snapshot_fields"
branch_labels = None
depends_on = None


_COLUMNS = """
    id VARCHAR(64) NOT NULL PRIMARY KEY,
    case_id VARCHAR(64),
    job_id VARCHAR(64),
    acquisition_id VARCHAR(64),
    producer VARCHAR(64) NOT NULL,
    kind VARCHAR(64) NOT NULL,
    path TEXT NOT NULL,
    filename VARCHAR(255),
    sha256 VARCHAR(128),
    size_bytes BIGINT,
    manifest_path TEXT,
    sha256_sidecar_path TEXT,
    metadata_json TEXT,
    created_at DATETIME NOT NULL,
    FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE SET NULL,
    FOREIGN KEY(acquisition_id) REFERENCES acquisitions(id) ON DELETE SET NULL
"""


def _recreate_artifacts(*, with_job_fk: bool) -> None:
    job_constraint = ",\n    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE" if with_job_fk else ""
    op.execute("PRAGMA foreign_keys=OFF")
    op.execute(f"CREATE TABLE artifacts__new ({_COLUMNS}{job_constraint})")
    op.execute(
        """INSERT INTO artifacts__new
           (id, case_id, job_id, acquisition_id, producer, kind, path, filename,
            sha256, size_bytes, manifest_path, sha256_sidecar_path, metadata_json, created_at)
           SELECT id, case_id, job_id, acquisition_id, producer, kind, path, filename,
                  sha256, size_bytes, manifest_path, sha256_sidecar_path, metadata_json, created_at
           FROM artifacts"""
    )
    op.execute("DROP TABLE artifacts")
    op.rename_table("artifacts__new", "artifacts")
    op.create_index("ix_artifacts_acquisition_id", "artifacts", ["acquisition_id"])
    op.create_index("ix_artifacts_case_id", "artifacts", ["case_id"])
    op.create_index("ix_artifacts_job_id", "artifacts", ["job_id"])
    op.create_index("ix_artifacts_kind", "artifacts", ["kind"])
    op.execute("PRAGMA foreign_keys=ON")


def upgrade() -> None:
    _recreate_artifacts(with_job_fk=False)


def downgrade() -> None:
    _recreate_artifacts(with_job_fk=True)
