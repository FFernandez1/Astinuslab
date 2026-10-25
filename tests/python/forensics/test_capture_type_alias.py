from __future__ import annotations

from typing import get_args

from astinuslab.api.schemas import CaptureType


def test_capture_type_values_unchanged() -> None:
    assert set(get_args(CaptureType)) == {"memory", "disk", "both", "none"}


def test_validation_constant_matches_alias() -> None:
    from astinuslab.jobs.job_services import _VALID_CAPTURE_TYPES

    assert _VALID_CAPTURE_TYPES == set(get_args(CaptureType))
