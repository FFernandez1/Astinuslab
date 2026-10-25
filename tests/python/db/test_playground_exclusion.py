"""Forensic admission must account for durable Playground cleanup/reset intent."""

import pytest

from astinuslab.db.models import LabInstance, LabPackage, PlaygroundSession
from astinuslab.db.repositories import has_active_playground_session


@pytest.mark.parametrize(
    ("status", "blocked"),
    [
        ("destroy_cleanup_pending", True),
        ("failure_cleanup_pending", True),
        ("running", True),
        ("destroyed", False),
        ("failed", False),
    ],
)
def test_playground_cleanup_backlog_blocks_until_terminal(in_memory_session_scope, status, blocked):
    with in_memory_session_scope() as session:
        session.add(PlaygroundSession(id="pg-boundary", status=status))
        session.flush()
        assert has_active_playground_session(session) is blocked


@pytest.mark.parametrize(("state", "blocked"), [("resetting", True), ("destroyed", False), ("failed", False)])
def test_package_reset_blocks_between_old_session_cleanup_and_new_registration(in_memory_session_scope, state, blocked):
    with in_memory_session_scope() as session:
        session.add(
            LabPackage(
                id="package-boundary",
                package_id="fixture.package",
                version="1",
                title="Boundary fixture",
                creator="fixture",
                license="fixture",
                reproducibility="locked",
                archive_path="fixture.zip",
                archive_sha256="a" * 64,
                archive_size_bytes=1,
                manifest_sha256="b" * 64,
                manifest_json="{}",
                definition_json="{}",
            )
        )
        session.flush()
        session.add(
            LabInstance(
                id="instance-boundary",
                package_record_id="package-boundary",
                playground_session_id=None,
                idempotency_key="reset-sentinel",
                state=state,
            )
        )
        session.flush()
        assert has_active_playground_session(session) is blocked
