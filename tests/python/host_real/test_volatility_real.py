"""Real-host Volatility 3 rows against the operator-supplied tools.

Opt-in tier: ``conftest._require_real_host`` gates this whole directory on a real
libvirt host. These rows additionally require operator-supplied Volatility 3 and
``dwarf2json`` through the ``operator_volatility`` fixture, which **raises**
rather than skipping when the tools are absent — an unprovisioned runner is a
failure, not an inapplicable test.

Nothing here downloads, installs, vendors, or imports either tool. Both are read
from the operator's configured paths only; the one row that mutates a
``dwarf2json`` file mutates a private copy under ``tmp_path`` and never touches
the operator's own binary.

Defaults to the prepared ubuntu-24.04 image like the capture tier; override with
ASTINUSLAB_REAL_HOST_TARGET when the lab box has another prepared image.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

TARGET_ID = os.getenv("ASTINUSLAB_REAL_HOST_TARGET", "ubuntu-24.04")
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+.*")
# A cheap, universally present Linux plugin: it needs a valid ISF, so a parseable
# result proves the whole symbol + execution path, not just that vol3 starts.
PLUGIN = "linux.pslist.PsList"
_TERMINAL = {"completed", "failed", "cancelled"}
_RUN_TERMINAL = {"completed", "failed"}


# ── shared helpers ───────────────────────────────────────────────────────────


def _wait_for_terminal(job_id: str, deadline_seconds: float) -> str | None:
    from astinuslab.db import session_scope
    from astinuslab.db.repositories import JobRepository

    deadline = time.monotonic() + deadline_seconds
    status = None
    while time.monotonic() < deadline:
        with session_scope() as session:
            job = JobRepository(session).get_job(job_id)
            assert job is not None
            status = job.status
        if status in _TERMINAL:
            break
        time.sleep(10)
    return status


def _release_job(job_id: str) -> None:
    """Destroy any domain still bound to the job and free its execution slot."""
    from astinuslab.db import session_scope
    from astinuslab.db.repositories import JobRepository

    with session_scope() as session:
        job = JobRepository(session).get_job(job_id)
        status = job.status if job else None
    if status is None or status in _TERMINAL:
        return
    from astinuslab.orchestration.lab_lifecycle import teardown_vm

    try:
        teardown_vm(job_id)
    finally:
        with session_scope() as session:
            JobRepository(session).force_transition(job_id, "failed")


@pytest.fixture
def vol3_job():
    """Create immediate-mode Jobs; always release leaked VMs/rows on exit.

    Same shape as ``test_immediate_capture_real.acquisition_job``, widened to
    create more than one job per test (a Volatility run needs both a dump and an
    ISF, and the ISF comes from its own ISF-only job).
    """
    from astinuslab.api.schemas import CaptureType, JobCreate
    from astinuslab.db import session_scope
    from astinuslab.jobs.job_services import create_job_service

    created: list[str] = []

    def _make(capture_type: CaptureType = "memory", *, build_vol3_isf: bool = False) -> str:
        with session_scope() as session:
            job = create_job_service(
                session,
                JobCreate(
                    target_id=TARGET_ID,
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                    capture_type=capture_type,
                    build_vol3_isf=build_vol3_isf,
                ),
            )
            created.append(job.id)
            return job.id

    yield _make

    for job_id in reversed(created):
        _release_job(job_id)


# ── readiness ────────────────────────────────────────────────────────────────


@pytest.mark.host_real
@pytest.mark.timeout(300)
def test_readiness_reports_both_capabilities_available(operator_volatility):
    """Both operator-supplied capabilities probe ready on the provisioned runner."""
    from astinuslab.core.config import settings
    from astinuslab.forensics.vol3_tools import get_vol3_readiness

    # Re-probe rather than trusting the session fixture's cached result: the
    # readiness contract is that a probe reflects the tools as they stand now.
    readiness = get_vol3_readiness(settings)

    assert readiness.enabled, "vol3 is disabled; the nightly runner must enable it"
    assert readiness.analysis.ready, (
        f"Volatility analysis not ready: {readiness.analysis.code} — {readiness.analysis.detail}"
    )
    assert readiness.analysis.code == "ready"
    # The version gate lives in check_volatility_tool: an unacceptable version
    # yields volatility_version_unsupported, never `ready`. Assert the probe
    # reported a concrete version rather than pinning a specific one here.
    assert _VERSION_RE.fullmatch(readiness.analysis.version or ""), (
        f"Volatility readiness reported no usable version: {readiness.analysis.version!r}"
    )
    assert readiness.isf_build.ready, (
        f"dwarf2json ISF build not ready: {readiness.isf_build.code} — {readiness.isf_build.detail}"
    )
    assert readiness.isf_build.code == "ready"

    # Same facts over the public projection the API and UI consume. Checked
    # field by field so an added provenance field cannot fail this row.
    public = readiness.public_dict()
    assert public["enabled"] is True
    analysis = public["analysis"]
    isf_build = public["isf_build"]
    assert isinstance(analysis, dict) and isinstance(isf_build, dict)
    assert analysis["ready"] is True
    assert analysis["code"] == "ready"
    assert analysis["version"] == readiness.analysis.version
    assert isf_build["ready"] is True
    assert isf_build["code"] == "ready"


@pytest.mark.host_real
@pytest.mark.timeout(300)
def test_operator_tools_resolve_outside_the_astinuslab_tree(operator_volatility):
    """The licensing boundary, checked on the machine that actually runs the tools.

    ``docs/user/VOLATILITY3.md`` requires both tools to live in operator-owned
    paths outside the AstinusLab tree; AstinusLab never ships, fetches, or
    installs them. A runner that satisfies readiness from a binary sitting inside
    the checkout has quietly turned the tools into vendored dependencies.
    """
    from astinuslab.core.config import settings

    tree = settings.base_dir.resolve()
    for label, status in (
        ("Volatility", operator_volatility.analysis),
        ("dwarf2json", operator_volatility.isf_build),
    ):
        resolved = status.resolved_path
        assert resolved is not None, f"{label} reported ready without a resolved path"
        assert not resolved.resolve().is_relative_to(tree), (
            f"{label} resolves to {resolved} which is inside the AstinusLab tree {tree}; "
            "operator-supplied tools must be installed outside it"
        )


# ── the pin is re-checked at use, not cached ─────────────────────────────────


@pytest.mark.host_real
@pytest.mark.timeout(300)
def test_dwarf2json_pin_is_reverified_after_the_file_is_replaced(operator_volatility, tmp_path):
    """Readiness is not execution authorization: replacing the file breaks the next check.

    Operates on a private copy of the operator's ``dwarf2json`` under ``tmp_path``
    — same bytes, therefore the same configured SHA-256 pin — so the row proves
    the re-verification behaviour without ever mutating the operator's binary.
    """
    from astinuslab.core.config import settings
    from astinuslab.forensics.vol3_tools import (
        ToolDependencyError,
        check_dwarf2json_tool,
        require_dwarf2json_tool,
    )

    operator_binary = operator_volatility.isf_build.resolved_path
    assert operator_binary is not None
    sandbox_binary = tmp_path / operator_binary.name
    shutil.copy2(operator_binary, sandbox_binary)  # copy2 keeps the executable bit
    probe_settings = settings.model_copy(update={"vol3_dwarf2json_path": sandbox_binary})

    # 1. The copy is byte-identical, so the operator's real pin accepts it.
    before = check_dwarf2json_tool(probe_settings)
    assert before.ready, f"copied dwarf2json did not pass readiness: {before.code} — {before.detail}"
    assert before.code == "ready"
    assert require_dwarf2json_tool(probe_settings) == sandbox_binary

    # 2. Replace the file at that same configured path, keeping it executable.
    sandbox_binary.write_bytes(b"#!/bin/sh\nexit 0\n")
    sandbox_binary.chmod(0o755)

    # 3. The very next check must fail. Nothing may be carried over from step 1.
    after = check_dwarf2json_tool(probe_settings)
    assert after.ready is False, "readiness still reports ready after the pinned file was replaced"
    assert after.code == "dwarf2json_hash_mismatch", f"expected dwarf2json_hash_mismatch, got {after.code}"

    with pytest.raises(ToolDependencyError) as excinfo:
        require_dwarf2json_tool(probe_settings)
    assert excinfo.value.code == "dwarf2json_hash_mismatch"

    # 4. The operator's real configuration is untouched by any of the above.
    assert check_dwarf2json_tool(settings).code == "ready"


# ── a real plugin run against a real dump ────────────────────────────────────


def _valid_isf_path(kernel_release: str, architecture: str) -> Path | None:
    from astinuslab.db import session_scope
    from astinuslab.db.repositories import JobRepository

    with session_scope() as session:
        artifact = JobRepository(session).find_valid_isf_by_kernel(kernel_release, architecture=architecture)
        path = artifact.path if artifact is not None else None
    if path and Path(path).is_file():
        return Path(path)
    return None


def _wait_for_plugin_run(job_id: str, run_id: str, deadline_seconds: float) -> dict[str, Any] | None:
    """Poll the same endpoint the workbench polls until the run is terminal."""
    from fastapi import HTTPException

    from astinuslab.forensics.api import volatility_api_run_result

    deadline = time.monotonic() + deadline_seconds
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        try:
            last = dict(volatility_api_run_result(job_id, run_id))
        except HTTPException:
            last = None
        if last is not None and last.get("status") in _RUN_TERMINAL:
            return last
        time.sleep(5)
    return last


@pytest.mark.host_real
@pytest.mark.timeout(7200)
def test_single_plugin_run_against_a_real_dump_produces_parseable_output(operator_volatility, vol3_job):
    """Capture a real dump, build a real ISF, run one real plugin, parse the result."""
    from astinuslab.db import session_scope
    from astinuslab.db.repositories import JobRepository
    from astinuslab.forensics.services import enqueue_vol3_plugin_run_service
    from astinuslab.forensics.vol3_support import os_family_for_distro

    # 1. A real memory capture from a real VM.
    dump_job_id = vol3_job("memory")
    status = _wait_for_terminal(dump_job_id, deadline_seconds=2400)
    assert status == "completed", f"capture job ended in {status!r}, expected completed"

    with session_scope() as session:
        job = JobRepository(session).get_job(dump_job_id)
        assert job is not None
        dump_path = job.dump_path
        kernel_release = job.detected_kernel_release or ""
        architecture = job.detected_architecture or "x86_64"
        os_family = os_family_for_distro(job.detected_distro)

    assert dump_path and Path(dump_path).is_file(), f"no memory dump on disk for {dump_job_id}"
    assert os_family == "linux", f"{PLUGIN} needs a Linux target, got os_family={os_family!r}"
    assert kernel_release, "capture recorded no kernel release; the ISF cannot be keyed"

    # 2. A real ISF for that exact kernel. Reuse a cached one when the runner
    #    already has it; otherwise drive the production ISF-only job, which is
    #    the path that actually executes the operator's dwarf2json.
    isf_path = _valid_isf_path(kernel_release, architecture)
    if isf_path is None:
        isf_job_id = vol3_job("none", build_vol3_isf=True)
        isf_status = _wait_for_terminal(isf_job_id, deadline_seconds=2400)
        assert isf_status == "completed", f"ISF-only job ended in {isf_status!r}, expected completed"
        isf_path = _valid_isf_path(kernel_release, architecture)
    assert isf_path is not None, f"no valid ISF on disk for kernel {kernel_release} ({architecture})"

    # 3. One real single-plugin run through the production enqueue service.
    with session_scope() as session:
        queued = enqueue_vol3_plugin_run_service(session, dump_job_id, PLUGIN)
    run_id = queued["run_id"]

    result = _wait_for_plugin_run(dump_job_id, run_id, deadline_seconds=1800)
    assert result is not None, f"{PLUGIN} run {run_id} never produced a result"
    assert result["status"] == "completed", f"{PLUGIN} run ended in {result['status']!r}: {result.get('error')}"

    # 4. The output is genuinely parsed, not merely produced.
    columns = result["columns"]
    rows = result["rows"]
    assert columns, f"{PLUGIN} produced no columns"
    assert rows, f"{PLUGIN} produced no rows"
    assert "PID" in columns, f"{PLUGIN} columns lack PID: {columns}"
    assert all(len(row) == len(columns) for row in rows), "parsed rows do not match the column count"
    assert result["total_rows"] >= len(rows) >= 1
    pid_index = columns.index("PID")
    assert any(str(row[pid_index]).strip() for row in rows), "every parsed PID cell is empty"

    # Evidence provenance: the dump was re-hashed and matched at use time.
    assert result["input_verified"] is True, "plugin run did not verify the dump checksum at use"
    assert result["dump_file"], "plugin run recorded no dump file"
