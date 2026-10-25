"""Worker-claim fencing for clean-run verification.

Only one worker may own a verification at a time, and the ``claimed_at``
timestamp is the token that proves it. Every renewal and every terminal write
compares the token it last saw, so a worker whose lease was reclaimed — by a
restart, a reconciliation sweep, or a second worker — cannot go on to publish
a result or keep renewing someone else's claim. That is what stops two runs
from attesting to the same package.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from astinuslab.db.models import LabOperationOutbox, LabVerificationAttempt
from astinuslab.lab_packages.verification_service import (
    _LostOutboxClaim,
    _OutboxClaimHeartbeat,
)

pytestmark = pytest.mark.unit

TOKEN = datetime(2026, 9, 4, 12, 0, 0)


def _seed(session, *, state: str = "claimed", claimed_at: datetime | None = TOKEN, attempt_state: str = "running"):
    session.add(LabVerificationAttempt(id="lpv-1", draft_id="ld-1", definition_sha256="a" * 64, state=attempt_state))
    session.add(
        LabOperationOutbox(
            id="outbox-lpv-1",
            operation_kind="lab_verify_clean_run",
            operation_id="lpv-1",
            state=state,
            claimed_at=claimed_at,
        )
    )
    session.commit()


def _heartbeat(scope, **kwargs) -> _OutboxClaimHeartbeat:
    return _OutboxClaimHeartbeat(
        attempt_id="lpv-1", token=TOKEN, session_scope_factory=scope, interval_seconds=0.01, **kwargs
    )


# --- token generation ----------------------------------------------------------


def test_a_renewed_token_always_moves_forward():
    future = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)

    assert _OutboxClaimHeartbeat._next_token(future) > future


def test_a_renewed_token_is_current_when_the_previous_one_is_old():
    past = datetime(2020, 1, 1)

    refreshed = _OutboxClaimHeartbeat._next_token(past)

    assert refreshed > past
    assert refreshed.year >= 2026


# --- owns_claim ----------------------------------------------------------------


def test_a_worker_owns_a_claim_carrying_its_token(in_memory_session_scope):
    with in_memory_session_scope() as session:
        _seed(session)

        assert _heartbeat(in_memory_session_scope).owns_claim(session) is True


def test_a_worker_does_not_own_a_claim_that_moved_on(in_memory_session_scope):
    with in_memory_session_scope() as session:
        _seed(session, claimed_at=TOKEN + timedelta(seconds=5))

        assert _heartbeat(in_memory_session_scope).owns_claim(session) is False


def test_a_worker_does_not_own_a_terminal_claim(in_memory_session_scope):
    with in_memory_session_scope() as session:
        _seed(session, state="completed")

        assert _heartbeat(in_memory_session_scope).owns_claim(session) is False


# --- renewal -------------------------------------------------------------------


def test_renewal_advances_the_stored_token(in_memory_session_scope):
    with in_memory_session_scope() as session:
        _seed(session)
        heartbeat = _heartbeat(in_memory_session_scope)

        heartbeat.commit_progress(session)

        assert session.get(LabOperationOutbox, "outbox-lpv-1").claimed_at > TOKEN
        assert heartbeat.owns_claim(session) is True


def test_a_reclaimed_worker_cannot_renew(in_memory_session_scope):
    with in_memory_session_scope() as session:
        _seed(session, claimed_at=TOKEN + timedelta(seconds=5))
        heartbeat = _heartbeat(in_memory_session_scope)

        with pytest.raises(_LostOutboxClaim, match="lost its worker claim"):
            heartbeat.commit_progress(session)


def test_a_worker_whose_attempt_is_no_longer_active_loses_its_claim(in_memory_session_scope):
    with in_memory_session_scope() as session:
        _seed(session, attempt_state="cancelled")
        heartbeat = _heartbeat(in_memory_session_scope)

        with pytest.raises(_LostOutboxClaim, match="no longer active"):
            heartbeat.commit_progress(session)


def test_a_lost_claim_stays_lost(in_memory_session_scope):
    with in_memory_session_scope() as session:
        _seed(session, claimed_at=TOKEN + timedelta(seconds=5))
        heartbeat = _heartbeat(in_memory_session_scope)
        with pytest.raises(_LostOutboxClaim):
            heartbeat.commit_progress(session)

        # Even after the row is restored, a worker that already lost custody
        # must not renew: another worker may have run in the meantime.
        session.get(LabOperationOutbox, "outbox-lpv-1").claimed_at = TOKEN
        session.commit()

        with pytest.raises(_LostOutboxClaim):
            heartbeat.commit_progress(session)


def test_a_terminal_attempt_adopts_its_cleanup_marker(in_memory_session_scope):
    with in_memory_session_scope() as session:
        _seed(session, state="completed", attempt_state="verified")
        heartbeat = _heartbeat(in_memory_session_scope)

        heartbeat.commit_progress(session)

        assert session.get(LabOperationOutbox, "outbox-lpv-1").claimed_at > TOKEN


# --- terminal writes -----------------------------------------------------------


@pytest.mark.parametrize("state", ["completed", "failed"])
def test_a_terminal_state_is_published_under_the_current_token(in_memory_session_scope, state):
    with in_memory_session_scope() as session:
        _seed(session)
        heartbeat = _heartbeat(in_memory_session_scope)

        heartbeat.complete(session, terminal_state=state)

        event = session.get(LabOperationOutbox, "outbox-lpv-1")
        assert event.state == state
        assert event.completed_at is not None


@pytest.mark.parametrize("state", ["running", "claimed", "cancelled", ""])
def test_a_non_terminal_state_is_refused(in_memory_session_scope, state):
    with in_memory_session_scope() as session:
        _seed(session)

        with pytest.raises(ValueError, match="invalid terminal outbox state"):
            _heartbeat(in_memory_session_scope).complete(session, terminal_state=state)


def test_a_reclaimed_worker_cannot_publish_a_terminal_state(in_memory_session_scope):
    with in_memory_session_scope() as session:
        _seed(session, claimed_at=TOKEN + timedelta(seconds=5))

        with pytest.raises(_LostOutboxClaim):
            _heartbeat(in_memory_session_scope).complete(session, terminal_state="completed")


def test_a_worker_that_already_lost_its_claim_cannot_publish(in_memory_session_scope):
    with in_memory_session_scope() as session:
        _seed(session, claimed_at=TOKEN + timedelta(seconds=5))
        heartbeat = _heartbeat(in_memory_session_scope)
        with pytest.raises(_LostOutboxClaim):
            heartbeat.commit_progress(session)

        with pytest.raises(_LostOutboxClaim):
            heartbeat.complete(session, terminal_state="completed")


# --- background renewal --------------------------------------------------------


def test_the_background_heartbeat_renews_and_stops_cleanly(in_memory_session_scope):
    with in_memory_session_scope() as session:
        _seed(session)

    heartbeat = _heartbeat(in_memory_session_scope)
    heartbeat.start()
    try:
        with in_memory_session_scope() as session:
            assert session.get(LabOperationOutbox, "outbox-lpv-1").claimed_at > TOKEN
    finally:
        heartbeat.stop()


def test_starting_a_heartbeat_whose_claim_is_gone_is_refused(in_memory_session_scope):
    with in_memory_session_scope() as session:
        _seed(session, claimed_at=TOKEN + timedelta(seconds=5))

    heartbeat = _heartbeat(in_memory_session_scope)

    with pytest.raises(_LostOutboxClaim):
        heartbeat.start()
