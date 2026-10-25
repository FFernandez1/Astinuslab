from __future__ import annotations

from astinuslab.jobs.job_control import ACTION_BY_JOB
from astinuslab.jobs.job_state_support import JobParams
from astinuslab.orchestration.deps import JobRunDeps
from astinuslab.orchestration.runners.boot_capture_runner import acquire_memory_dump


def _failed_acquire_deps(*, keep_failed_vm: bool = False):
    deps = JobRunDeps.default()
    calls: dict[str, list] = {
        "handle_exception": [],
        "teardown_vm": [],
        "teardown_attacker_vm": [],
        "remove_network_isolation": [],
    }

    deps.load_job_params = lambda job_id: JobParams(
        target_id="t1",
        scenario_id="scenario-1",
        image_path=None,
        custom_script=None,
        vm_vcpus=None,
        vm_memory_mb=None,
        vm_disk_gb=None,
        attacker_vm_vcpus=None,
        attacker_vm_memory_mb=None,
        attacker_vm_disk_gb=None,
        capture_type="memory",
    )
    deps.target_by_id = lambda target_id: {
        "distro": "ubuntu",
        "version": "22.04",
        "default_username": "ubuntu",
        "keep_failed_vm": keep_failed_vm,
    }
    deps.load_capture_runtime_state = lambda job_id: {
        "guest_ip": "10.0.0.5",
        "kernel_release": None,
        "attacker_ip": None,
        "network_isolated": False,
        "attacker_vm_present": False,
    }
    deps.runtime_vm_name = lambda job_id, role: f"vm-{job_id}"

    def raise_acquire_error(*args, **kwargs):
        raise RuntimeError("boom")

    deps.acquire_and_publish = raise_acquire_error
    deps.handle_exception = lambda *args: calls["handle_exception"].append(args)
    deps.teardown_vm = lambda *args: calls["teardown_vm"].append(args)
    deps.teardown_attacker_vm = lambda *args: calls["teardown_attacker_vm"].append(args)
    deps.remove_network_isolation = lambda *args: calls["remove_network_isolation"].append(args)

    return deps, calls


def test_failed_acquire_memory_dump_tears_down_target_vm_by_default():
    ACTION_BY_JOB.pop("job-x", None)
    deps, calls = _failed_acquire_deps()

    acquire_memory_dump("job-x", action_id=None, deps=deps)

    assert len(calls["handle_exception"]) == 1
    exc, job_id, action_id = calls["handle_exception"][0]
    assert isinstance(exc, RuntimeError)
    assert str(exc) == "boom"
    assert job_id == "job-x"
    assert action_id is None
    assert calls["teardown_vm"] == [("job-x",)]
    assert calls["teardown_attacker_vm"] == []
    assert calls["remove_network_isolation"] == []
    assert "job-x" not in ACTION_BY_JOB


def test_failed_acquire_memory_dump_preserves_target_vm_when_requested():
    ACTION_BY_JOB.pop("job-x", None)
    deps, calls = _failed_acquire_deps(keep_failed_vm=True)

    acquire_memory_dump("job-x", action_id=None, deps=deps)

    assert len(calls["handle_exception"]) == 1
    assert calls["teardown_vm"] == []
    assert "job-x" not in ACTION_BY_JOB


def test_failed_acquire_memory_dump_finalizes_existing_action_tracking():
    deps, calls = _failed_acquire_deps()
    ACTION_BY_JOB["job-x"] = 1

    try:
        acquire_memory_dump("job-x", action_id=None, deps=deps)

        assert len(calls["handle_exception"]) == 1
        assert "job-x" not in ACTION_BY_JOB
    finally:
        ACTION_BY_JOB.pop("job-x", None)
