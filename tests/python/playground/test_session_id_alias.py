"""Pin the deprecated playground `job_id` mirror until its removal."""

from __future__ import annotations

from astinuslab.api.schemas import PlaygroundSessionOut


def test_job_id_mirrors_session_id():
    dto = PlaygroundSessionOut.model_validate(
        {
            "session_id": "pg-abc",
            "status": "running",
            "started_at": None,
            "vms": [],
            "networks": [],
        }
    )
    assert dto.job_id == "pg-abc"
    assert dto.session_id == "pg-abc"


def test_session_id_derived_from_job_id_input():
    dto = PlaygroundSessionOut.model_validate(
        {
            "job_id": "pg-xyz",
            "status": "running",
            "started_at": None,
            "vms": [],
            "networks": [],
        }
    )
    assert dto.session_id == "pg-xyz"
