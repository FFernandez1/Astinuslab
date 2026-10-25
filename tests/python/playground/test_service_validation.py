from typing import cast

import pytest
from sqlalchemy.orm import Session

from astinuslab.jobs import job_services


def test_valid_modes_includes_playground_for_read_paths():
    assert "playground" in job_services._VALID_EXECUTION_MODES


def test_create_job_service_rejects_playground_mode():
    from fastapi import HTTPException

    from astinuslab.api.schemas import JobCreate

    payload = JobCreate(
        target_id="ubuntu-22.04",
        scenario_id="immediate-default",
        execution_mode="playground",
        capture_type="memory",
    )
    with pytest.raises(HTTPException) as exc_info:
        job_services.create_job_service(session=cast(Session, None), payload=payload)
    assert exc_info.value.status_code == 400
    assert "playground" in exc_info.value.detail.lower()
