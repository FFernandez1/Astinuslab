from typing import get_args

from astinuslab.api.schemas import JobStatus as JobStatusLiteral
from astinuslab.jobs.job_status import JobStatus as JobStatusEnum


def test_enum_and_literal_status_sets_match():
    enum_values = {s.value for s in JobStatusEnum}
    literal_values = set(get_args(JobStatusLiteral))
    assert enum_values == literal_values, (
        f"drift: only in enum={enum_values - literal_values}, only in literal={literal_values - enum_values}"
    )
