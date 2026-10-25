from __future__ import annotations

import astinuslab.forensics.vol3_support as v3
from astinuslab.api.schemas import JobCreate
from astinuslab.db.repositories import JobRepository


def test_validated_windows_target_selects_windows_symbols_without_linux_build(in_memory_session_scope, monkeypatch):
    monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(
        v3,
        "target_by_id",
        lambda _target_id: {
            "platform": "windows",
            "reusable": True,
            "family": "windows",
            "distro": "windows-server",
            "version": "2022",
            "default_username": "Administrator",
        },
    )
    monkeypatch.setattr(
        v3,
        "run_vol3_symbol_build",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Linux ISF build must not run")),
    )

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(
            JobCreate(
                target_id="windows-server-2022",
                scenario_id="immediate-capture",
                execution_mode="immediate",
            )
        )
        job_id = job.id
        repo.force_transition(
            job.id,
            "running",
            detected_distro="unknown",
            detected_version="unknown",
            detected_kernel_release=None,
        )

    success, summary = v3.run_vol3_isf_only(job_id)

    assert success is True
    assert "windows" in summary.lower()
    assert "no linux isf build" in summary.lower()
