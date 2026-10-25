"""Enforce paired, manifest-matching package content ownership.

Revision ID: a032_package_storage_invariants
Revises: a031_content_publication_attempts
"""

from __future__ import annotations

from alembic import op

revision = "a032_package_storage_invariants"
down_revision = "a031_content_publication_attempts"
branch_labels = None
depends_on = None

_CONTENT_PAIR = (
    "(content_sha256 IS NULL AND content_size_bytes IS NULL) OR "
    "(content_sha256 IS NOT NULL AND content_size_bytes IS NOT NULL)"
)
_CONTENT_MATCHES_EXPECTED = (
    "content_sha256 IS NULL OR (content_sha256 = expected_sha256 AND content_size_bytes = expected_size_bytes)"
)


def upgrade() -> None:
    # Old provenance rows that stored only half a composite identity, or whose
    # content points at a different declared payload, cannot be safely adopted.
    # Leave them unresolved for preparation rather than falsely claiming bytes.
    op.execute(
        "UPDATE lab_package_assets SET content_sha256 = NULL, content_size_bytes = NULL "
        "WHERE (content_sha256 IS NULL) != (content_size_bytes IS NULL) "
        "OR (content_sha256 IS NOT NULL AND "
        "(content_sha256 != expected_sha256 OR content_size_bytes != expected_size_bytes))"
    )
    with op.batch_alter_table("lab_package_assets", recreate="always") as batch:
        batch.create_check_constraint("ck_lab_package_assets_content_pair", _CONTENT_PAIR)
        batch.create_check_constraint("ck_lab_package_assets_content_matches_expected", _CONTENT_MATCHES_EXPECTED)


def downgrade() -> None:
    with op.batch_alter_table("lab_package_assets", recreate="always") as batch:
        batch.drop_constraint("ck_lab_package_assets_content_matches_expected", type_="check")
        batch.drop_constraint("ck_lab_package_assets_content_pair", type_="check")
