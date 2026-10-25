from astinuslab.jobs.job_status import (
    ALLOWED_TRANSITIONS,
    FORENSIC_LIVE_STATUSES,
    TERMINAL_STATUSES,
    JobStatus,
)


def test_member_set_equals_transition_vocabulary():
    vocab = set(ALLOWED_TRANSITIONS.keys())
    for successors in ALLOWED_TRANSITIONS.values():
        vocab |= set(successors)
    assert set(JobStatus) == vocab


def test_strenum_string_equality():
    # mypy's strict_equality does not model StrEnum members as strings, so it
    # calls these non-overlapping. They are true at runtime, and asserting
    # exactly that is the point of this test.
    assert JobStatus.CANCELLED == "cancelled"  # type: ignore[comparison-overlap]
    assert JobStatus.WAITING_FOR_CAPTURE == "waiting_for_capture"  # type: ignore[comparison-overlap]


def test_terminal_statuses_content():
    assert TERMINAL_STATUSES == {"completed", "failed", "cancelled"}


def test_post_script_can_enter_capture_directly():
    assert {"acquiring_memory", "acquiring_disk"} <= ALLOWED_TRANSITIONS["running_scenario_script"]


def test_forensic_live_statuses_disjoint_from_terminal():
    assert not (FORENSIC_LIVE_STATUSES & TERMINAL_STATUSES)
