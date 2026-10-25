"""Add explicit Playground artifact ownership and immutable-source analysis contexts.

Historical provenance/bytes are never rewritten. Ambiguous rows remain available
by artifact/acquisition ID, with the original owner and reason stored durably.
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "a039_playground_artifact_ownership"
down_revision = "a038_merge_packages_capacity"
branch_labels = None
depends_on = None


def reconcile(connection):
    """Idempotent data step, intentionally independent of current ORM models."""
    jobs = {r[0] for r in connection.execute(sa.text("SELECT id FROM jobs"))}
    sessions = {r[0] for r in connection.execute(sa.text("SELECT id FROM playground_sessions"))}
    acquisitions = {
        r["id"]: r
        for r in connection.execute(sa.text("SELECT id, session_id, job_vm_id, case_id FROM acquisitions")).mappings()
    }
    vms = {r["id"]: r for r in connection.execute(sa.text("SELECT id, session_id FROM job_vms")).mappings()}
    attempts: dict[str, list[sa.RowMapping]] = {}
    for row in connection.execute(
        sa.text(
            "SELECT artifact_id, session_id, job_id FROM artifact_publication_attempts WHERE artifact_id IS NOT NULL"
        )
    ).mappings():
        attempts.setdefault(row["artifact_id"], []).append(row)
    rows = (
        connection.execute(
            sa.text(
                "SELECT id, job_id, session_id, acquisition_id, case_id, producer, ownership_reconciliation_json FROM artifacts"
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        if row["session_id"] or row["ownership_reconciliation_json"]:
            continue
        owner = row["job_id"]
        acq = acquisitions.get(row["acquisition_id"])
        reasons = []
        if row["acquisition_id"] and acq is None:
            reasons.append("missing acquisition")
        if owner in jobs and owner in sessions:
            reasons.append("owner exists as both Job and Playground session")
        pg = owner in sessions and owner not in jobs
        if owner and owner not in jobs and owner not in sessions:
            reasons.append("missing owner")
        if pg:
            if row["producer"] != "playground_capture" or acq is None or acq["session_id"] != owner:
                reasons.append("Playground producer/acquisition ownership not proven")
        elif acq and acq["session_id"]:
            reasons.append("acquisition session conflicts with Job or missing owner")
        if row["producer"] == "playground_capture" and not pg:
            reasons.append("Playground capture has no exclusive session owner")
        if acq:
            if row["case_id"] and row["case_id"] != acq["case_id"]:
                reasons.append("conflicting case association")
            if acq["job_vm_id"]:
                vm = vms.get(acq["job_vm_id"])
                if vm is None or (vm["session_id"] or None) != (owner if pg else None):
                    reasons.append("missing/conflicting acquisition VM")
        for attempt in attempts.get(row["id"], []):
            if pg and (attempt["session_id"] != owner or attempt["job_id"]):
                reasons.append("conflicting publication owner")
            elif not pg and (attempt["session_id"] or (attempt["job_id"] and attempt["job_id"] != owner)):
                reasons.append("conflicting publication owner")
        if reasons:
            snapshot = json.dumps(
                {
                    "legacy_job_id": owner,
                    "acquisition_id": row["acquisition_id"],
                    "reasons": sorted(set(reasons)),
                    "revision": revision,
                },
                sort_keys=True,
            )
            connection.execute(
                sa.text("UPDATE artifacts SET job_id=NULL, ownership_reconciliation_json=:report WHERE id=:id"),
                {"id": row["id"], "report": snapshot},
            )
        elif pg:
            connection.execute(
                sa.text("UPDATE artifacts SET session_id=:owner, job_id=NULL WHERE id=:id"),
                {"owner": owner, "id": row["id"]},
            )


def upgrade():
    # Nullable ADD COLUMN REFERENCES works in SQLite without toggling FK policy
    # or rebuilding tables that existing Report/Acquisition rows reference.
    op.execute(
        "ALTER TABLE artifacts ADD COLUMN session_id VARCHAR(64) REFERENCES playground_sessions(id) ON DELETE RESTRICT"
    )
    op.add_column("artifacts", sa.Column("ownership_reconciliation_json", sa.Text(), nullable=True))
    op.create_index("ix_artifacts_session_id", "artifacts", ["session_id"])
    op.execute("ALTER TABLE jobs ADD COLUMN source_artifact_id VARCHAR(64) REFERENCES artifacts(id) ON DELETE SET NULL")
    op.add_column("jobs", sa.Column("analysis_context_json", sa.Text(), nullable=True))
    op.create_index("ux_jobs_source_artifact_id", "jobs", ["source_artifact_id"], unique=True)
    op.add_column("evidence_tombstones", sa.Column("session_id", sa.String(64), nullable=True))
    reconcile(op.get_bind())


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM jobs WHERE execution_mode='artifact_analysis' LIMIT 1")).first():
        raise RuntimeError(
            "Cannot downgrade while artifact analysis contexts exist; preserve their evidence and restore a pre-migration backup"
        )
    for row in (
        connection.execute(sa.text("SELECT id, session_id, ownership_reconciliation_json FROM artifacts"))
        .mappings()
        .all()
    ):
        owner = row["session_id"]
        if row["ownership_reconciliation_json"]:
            owner = json.loads(row["ownership_reconciliation_json"])["legacy_job_id"]
        if owner:
            connection.execute(
                sa.text("UPDATE artifacts SET job_id=:owner WHERE id=:id"), {"owner": owner, "id": row["id"]}
            )
    op.drop_column("evidence_tombstones", "session_id")
    op.drop_index("ux_jobs_source_artifact_id", table_name="jobs")
    # ORM-created databases declare FKs at table level; SQLite DROP COLUMN
    # cannot remove those. Alembic rebuilds only these two affected tables.
    with op.batch_alter_table("jobs", recreate="always") as batch:
        batch.drop_column("analysis_context_json")
        batch.drop_column("source_artifact_id")
    op.drop_index("ix_artifacts_session_id", table_name="artifacts")
    with op.batch_alter_table("artifacts", recreate="always") as batch:
        batch.drop_column("ownership_reconciliation_json")
        batch.drop_column("session_id")
