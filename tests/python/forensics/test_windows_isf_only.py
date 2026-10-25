"""run_vol3_isf_only must skip the Linux ISF build for Windows targets."""

from __future__ import annotations

import astinuslab.forensics.vol3_support as v3
from astinuslab.api.schemas import JobCreate
from astinuslab.db.repositories import JobRepository
from tests.python._support.helpers import autospec_setattr


def _make_windows_job(scope) -> str:
    with scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(
            JobCreate(
                target_id="windows-server-2022",
                scenario_id="immediate-capture",
                execution_mode="immediate",
            )
        )
        # Windows metadata: a distro slug starting with "win" and no kernel release.
        repo.force_transition(
            job.id,
            "running",
            detected_kernel_release=None,
            detected_distro="windows-server",
            detected_version="2022",
        )
        return job.id


def test_windows_job_skips_linux_isf_build(in_memory_session_scope, monkeypatch):
    monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
    # If the Linux build path were reached it would call this; assert it is not.
    autospec_setattr(
        monkeypatch,
        v3,
        "run_vol3_symbol_build",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Linux ISF build must not run for Windows")),
    )
    job_id = _make_windows_job(in_memory_session_scope)

    success, summary = v3.run_vol3_isf_only(job_id)

    assert success is True
    assert "windows" in summary.lower()
    # Linux jobs with no kernel_release fail; Windows must NOT hit that path.
    assert "kernel_release" not in summary
