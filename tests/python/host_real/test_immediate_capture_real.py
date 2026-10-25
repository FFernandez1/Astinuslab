"""Real-host smoke for immediate-mode capture wiring.

Opt-in tier: conftest.py skips everything unless ASTINUSLAB_REAL_HOST=1 and a
real libvirt connection opens. Defaults to the prepared ubuntu-24.04 image
(the only image prepared to consume the NoCloud seed); override with
ASTINUSLAB_REAL_HOST_TARGET when the lab box has another prepared image.
"""

from __future__ import annotations

import hashlib
import os
import time
import urllib.request
from pathlib import Path

import pytest

TARGET_ID = os.getenv("ASTINUSLAB_REAL_HOST_TARGET", "ubuntu-24.04")
PLAYGROUND_WORKLOAD_ID = os.getenv("ASTINUSLAB_REAL_HOST_PLAYGROUND_WORKLOAD", "vulnhub-basic-pentesting")
_TERMINAL = {"completed", "failed", "cancelled"}
_CAPTURE_TERMINAL = {"completed", "failed", "cancelled", "error"}


@pytest.fixture
def acquisition_job():
    """Create a capture Job; always release leaked VMs/rows on exit.

    Defaults to immediate/memory. Disk (and `both`) capture is only valid for
    post_script/manual_trigger modes (see job_services validation), so the
    disk test drives a post_script scenario rather than immediate.
    """
    from astinuslab.api.schemas import CaptureType, JobCreate
    from astinuslab.db import session_scope
    from astinuslab.db.repositories import JobRepository
    from astinuslab.jobs.job_services import create_job_service

    created: dict[str, str] = {}

    def _make(
        capture_type: CaptureType = "memory",
        *,
        execution_mode: str = "immediate",
        scenario_id: str = "immediate-capture",
    ) -> str:
        with session_scope() as session:
            job = create_job_service(
                session,
                JobCreate(
                    target_id=TARGET_ID,
                    scenario_id=scenario_id,
                    execution_mode=execution_mode,
                    capture_type=capture_type,
                ),
            )
            created["id"] = job.id
            return job.id

    yield _make

    job_id = created.get("id")
    if not job_id:
        return
    with session_scope() as session:
        job = JobRepository(session).get_job(job_id)
        status = job.status if job else None
    if status is None or status in _TERMINAL:
        return
    # Timeout/assertion-failure path: destroy the domain even if the job is
    # wedged, then force the row terminal so the execution slot is freed.
    from astinuslab.orchestration.lab_lifecycle import teardown_vm

    try:
        teardown_vm(job_id)
    finally:
        with session_scope() as session:
            JobRepository(session).force_transition(job_id, "failed")


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


def _wait_for_status(job_id: str, statuses: set[str], deadline_seconds: float) -> str | None:
    from astinuslab.db import session_scope
    from astinuslab.db.repositories import JobRepository

    deadline = time.monotonic() + deadline_seconds
    status = None
    while time.monotonic() < deadline:
        with session_scope() as session:
            job = JobRepository(session).get_job(job_id)
            assert job is not None
            status = job.status
        if status in statuses:
            break
        time.sleep(5)
    return status


def _assert_dump_on_disk(dump_path: str | None, checksum: str | None, kind: str) -> None:
    assert dump_path, f"no {kind} artifact recorded"
    dump = Path(dump_path)
    assert dump.exists(), f"{kind} dump missing on disk: {dump}"
    size = dump.stat().st_size
    assert size > 10 * 1024 * 1024, f"{kind} dump suspiciously small: {size}B"
    if checksum:
        digest = hashlib.sha256(dump.read_bytes()).hexdigest()
        assert digest == checksum, f"on-disk {kind} dump does not match recorded checksum"


@pytest.mark.host_real
@pytest.mark.timeout(1800)
def test_immediate_memory_job_writes_verifiable_artifact(acquisition_job):
    from astinuslab.db import session_scope
    from astinuslab.db.repositories import JobRepository

    job_id = acquisition_job()
    status = _wait_for_terminal(job_id, deadline_seconds=1500)
    assert status == "completed", f"job ended in {status!r}, expected completed"

    with session_scope() as session:
        job = JobRepository(session).get_job(job_id)
        assert job is not None
        _assert_dump_on_disk(job.dump_path, job.dump_checksum, "memory")


@pytest.mark.host_real
@pytest.mark.timeout(2700)
def test_post_script_both_capture_writes_memory_and_disk_artifacts(acquisition_job):
    from astinuslab.db import session_scope
    from astinuslab.db.repositories import JobRepository

    # Disk capture is only valid for post_script/manual_trigger modes; drive a
    # post_script scenario so `both` is accepted (immediate rejects disk).
    job_id = acquisition_job(
        capture_type="both",
        execution_mode="post_script",
        scenario_id="workload-sample-post-script",
    )
    status = _wait_for_terminal(job_id, deadline_seconds=2400)
    assert status == "completed", f"job ended in {status!r}, expected completed"

    with session_scope() as session:
        job = JobRepository(session).get_job(job_id)
        assert job is not None
        _assert_dump_on_disk(job.dump_path, job.dump_checksum, "memory")
        _assert_dump_on_disk(job.disk_dump_path, job.disk_dump_checksum, "disk")


def _cleanup_job(job_id: str) -> None:
    from astinuslab.db import session_scope
    from astinuslab.db.repositories import JobRepository
    from astinuslab.jobs.job_control import request_job_cancel
    from astinuslab.orchestration.lab_lifecycle import teardown_vm

    with session_scope() as session:
        job = JobRepository(session).get_job(job_id)
        status = job.status if job else None
    if status is None or status in _TERMINAL:
        return
    request_job_cancel(job_id)
    try:
        teardown_vm(job_id)
    finally:
        with session_scope() as session:
            JobRepository(session).force_transition(job_id, "failed")


def _assert_artifact_sidecars(path: str | None, checksum: str | None, kind: str) -> None:
    _assert_dump_on_disk(path, checksum, kind)
    artifact = Path(path or "")
    manifest = artifact.with_suffix(artifact.suffix + ".manifest.json")
    sidecar = artifact.with_suffix(artifact.suffix + ".sha256")
    assert manifest.exists(), f"{kind} manifest missing: {manifest}"
    assert sidecar.exists(), f"{kind} sha256 sidecar missing: {sidecar}"
    if checksum:
        assert checksum in sidecar.read_text()


@pytest.mark.host_real
@pytest.mark.timeout(2700)
def test_manual_trigger_memory_capture_completes_after_operator_request(acquisition_job):
    """Backend path behind release row 4: a manual-trigger job waits, then captures.

    Not row-4 evidence. The playbook requires row 4 to be triggered from the UI; this
    calls ``enqueue_acquire_memory_dump_service`` directly, so it proves the job
    reaches ``waiting_for_capture`` and completes on request, not that the UI can
    drive it.
    """
    from astinuslab.db import session_scope
    from astinuslab.db.repositories import JobRepository
    from astinuslab.forensics.services import enqueue_acquire_memory_dump_service

    job_id = acquisition_job(
        capture_type="memory",
        execution_mode="manual_trigger",
        scenario_id="lab-session",
    )
    status = _wait_for_status(job_id, {"waiting_for_capture", "failed", "cancelled"}, deadline_seconds=1500)
    assert status == "waiting_for_capture", f"job reached {status!r}, expected waiting_for_capture"

    with session_scope() as session:
        enqueue_acquire_memory_dump_service(session, job_id, capture_type="memory")

    status = _wait_for_terminal(job_id, deadline_seconds=900)
    assert status == "completed", f"job ended in {status!r}, expected completed"

    with session_scope() as session:
        job = JobRepository(session).get_job(job_id)
        assert job is not None
        assert job.execution_mode == "manual_trigger"
        _assert_dump_on_disk(job.dump_path, job.dump_checksum, "manual-trigger memory")


def _expected_playground_workload_cache_path() -> Path:
    from astinuslab.catalog import target_by_id, workload_by_id
    from astinuslab.core.compatibility import compatibility_fingerprint
    from astinuslab.core.config import settings
    from astinuslab.workloads import workload_cache_support

    target = target_by_id(TARGET_ID)
    assert target is not None
    workload = workload_by_id(PLAYGROUND_WORKLOAD_ID)
    assert workload is not None
    image_path = settings.images_dir / target["image_ref"]
    fingerprint = compatibility_fingerprint(workload, base_dir=settings.base_dir)
    key = workload_cache_support.workload_cache_key(
        TARGET_ID,
        PLAYGROUND_WORKLOAD_ID,
        image_path,
        workload_fingerprint=fingerprint,
    )
    return workload_cache_support.workload_cache_path(settings, TARGET_ID, PLAYGROUND_WORKLOAD_ID, key)


def _ensure_playground_workload_cache() -> Path:
    from astinuslab.api.schemas import JobCreate
    from astinuslab.db import session_scope
    from astinuslab.db.repositories import JobRepository
    from astinuslab.forensics.services import enqueue_acquire_memory_dump_service
    from astinuslab.jobs.job_services import create_job_service

    cache_path = _expected_playground_workload_cache_path()
    if cache_path.exists():
        return cache_path

    job_id = ""
    try:
        with session_scope() as session:
            job = create_job_service(
                session,
                JobCreate(
                    target_id=TARGET_ID,
                    scenario_id="lab-session",
                    execution_mode="manual_trigger",
                    capture_type="memory",
                    workload_id=PLAYGROUND_WORKLOAD_ID,
                ),
            )
            job_id = job.id

        status = _wait_for_status(job_id, {"waiting_for_capture", "failed", "cancelled"}, deadline_seconds=2400)
        assert status == "waiting_for_capture", f"cache bake job reached {status!r}, expected waiting_for_capture"
        assert cache_path.exists(), f"workload cache was not written: {cache_path}"

        with session_scope() as session:
            enqueue_acquire_memory_dump_service(session, job_id, capture_type="memory")
        status = _wait_for_terminal(job_id, deadline_seconds=900)
        assert status == "completed", f"cache bake cleanup capture ended in {status!r}, expected completed"

        with session_scope() as session:
            baked = JobRepository(session).get_job(job_id)
            assert baked is not None
            assert baked.workload_id == PLAYGROUND_WORKLOAD_ID
    finally:
        if job_id:
            _cleanup_job(job_id)
    return cache_path


def _wait_for_vm(vm_id: str, predicate, *, deadline_seconds: float):
    from astinuslab.db import session_scope
    from astinuslab.db.models import JobVM

    deadline = time.monotonic() + deadline_seconds
    last = None
    while time.monotonic() < deadline:
        with session_scope() as session:
            vm = session.query(JobVM).filter_by(id=vm_id).one_or_none()
            if vm is not None:
                last = {
                    "state": vm.state,
                    "workload_state": vm.workload_state,
                    "workload_error": vm.workload_error,
                    "domain_name": vm.domain_name,
                    "ip": next((nic.ip for nic in vm.nics if nic.ip), None),
                }
        if last is not None and predicate(last):
            return last
        time.sleep(5)
    return last


def _wait_for_acquisition(acquisition_id: str, *, deadline_seconds: float):
    from astinuslab.db import session_scope
    from astinuslab.db.models import Acquisition

    deadline = time.monotonic() + deadline_seconds
    last = None
    while time.monotonic() < deadline:
        with session_scope() as session:
            acq = session.query(Acquisition).filter_by(id=acquisition_id).one_or_none()
            if acq is not None:
                last = {
                    "status": acq.status,
                    "path": acq.path,
                    "sha256": acq.sha256,
                    "kind": acq.kind,
                }
        if last is not None and last["status"] in _CAPTURE_TERMINAL:
            return last
        time.sleep(3)
    return last


def _assert_http_reachable(ip: str, *, path: str = "/", deadline_seconds: float = 60) -> None:
    deadline = time.monotonic() + deadline_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://{ip}{path}", timeout=5) as response:
                assert response.status < 500
                return
        except Exception as exc:  # noqa: BLE001 - retries include connection setup
            last_error = exc
            time.sleep(3)
    raise AssertionError(f"workload not reachable at http://{ip}{path}: {last_error}")


def _domain_is_running(domain_name: str) -> bool:
    import libvirt

    from astinuslab.core.config import settings

    conn = libvirt.open(settings.libvirt_uri)
    try:
        state, _reason = conn.lookupByName(domain_name).state()
        return state == 1
    finally:
        conn.close()


@pytest.mark.host_real
@pytest.mark.timeout(2700)
def test_playground_cached_workload_and_captures_publish_evidence():
    """Backend path behind release rows 5-7: cached workload boots offline, captures
    publish sidecars.

    Not row-6 or row-7 evidence. Both require the Playground UI capture button; this
    calls ``po.capture_memory`` / ``po.capture_disk`` directly. It does cover row 5's
    substance — the baked workload serving on its port from an isolated network — but
    the row still owes its evidence on the release commit.
    """
    from astinuslab.playground import orchestrator as po

    suffix = f"{os.getpid():x}"
    job_id = f"pg-release-{suffix}"
    vm_id = f"vm-release-{suffix}"
    network_id = f"net-release-{suffix}"
    nic_id = f"nic-release-{suffix}"

    try:
        cache_path = _ensure_playground_workload_cache()
        assert cache_path.exists()

        po.create_session.delay(job_id=job_id).get(timeout=60)
        po.create_network.delay(
            job_id=job_id,
            network_id=network_id,
            name="release-default",
            subnet_cidr="10.42.80.0/24",
            isolated=True,
        ).get(timeout=120)
        po.spawn_vm.delay(
            job_id=job_id,
            vm_id=vm_id,
            label="target-release",
            image_id=TARGET_ID,
            nic_specs=[{"network_id": network_id, "nic_id": nic_id}],
            workload_id=PLAYGROUND_WORKLOAD_ID,
        ).get(timeout=900)

        vm = _wait_for_vm(
            vm_id,
            lambda row: row["state"] == "running" and row["workload_state"] == "ready" and bool(row["ip"]),
            deadline_seconds=900,
        )
        assert vm is not None, "VM did not reach running/ready"
        assert vm["workload_error"] is None
        assert vm["ip"]
        _assert_http_reachable(vm["ip"])

        memory_id = po.capture_memory.delay(vm_id=vm_id).get(timeout=900)
        memory = _wait_for_acquisition(memory_id, deadline_seconds=60)
        assert memory is not None
        assert memory["status"] == "completed"
        _assert_artifact_sidecars(memory["path"], memory["sha256"], "playground memory")

        disk_id = po.capture_disk.delay(vm_id=vm_id).get(timeout=1200)
        disk = _wait_for_acquisition(disk_id, deadline_seconds=60)
        assert disk is not None
        assert disk["status"] == "completed"
        _assert_artifact_sidecars(disk["path"], disk["sha256"], "playground disk")
        assert _domain_is_running(vm["domain_name"])
    finally:
        po.destroy_session.delay(job_id=job_id).get(timeout=300)
