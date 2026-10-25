"""Paired Jobs must not hand off a target after their requested attacker fails."""

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock

import pytest


@pytest.mark.parametrize("outcome", ["ready", "boot-error", "missing-image", "missing-config", "missing-ip"])
def test_explicit_attacker_without_workload_is_required_for_handoff(tmp_path, monkeypatch, outcome):
    from astinuslab.jobs.job_state_support import JobCleanupManager
    from astinuslab.live_session import runner

    deps = MagicMock()
    deps.load_job_params.return_value = SimpleNamespace(
        target_id="ubuntu",
        scenario_id="manual",
        image_path=None,
        vm_vcpus=2,
        vm_memory_mb=2048,
        vm_disk_gb=20,
        attacker_vm_vcpus=1,
        attacker_vm_memory_mb=2048,
        attacker_vm_disk_gb=20,
    )
    deps.settings.images_dir = tmp_path
    deps.resolve_target_execution_context.return_value = ({"image_ref": "target.img"}, {}, "ubuntu", False)
    deps.load_workload_id.return_value = None
    deps.load_attacker_vm_id.return_value = "selected-attacker"
    deps.boot_and_prepare.return_value = ("192.0.2.10", {})
    deps.boot_simple_vm.return_value = "192.0.2.11" if outcome != "missing-ip" else None
    if outcome == "boot-error":
        deps.boot_simple_vm.side_effect = RuntimeError("attacker source prefix denied")
    if outcome != "missing-image":
        (tmp_path / "attacker.img").write_bytes(b"fixture")
    monkeypatch.setattr(
        runner,
        "attacker_by_id",
        lambda _id: (
            None
            if outcome == "missing-config"
            else {
                "image_ref": "attacker.img",
                "username": "ubuntu",
                "os_variant": "ubuntu24.04",
            }
        ),
    )
    cleanup_calls = []
    deps.job_cleanup_manager.side_effect = lambda job: JobCleanupManager(
        job,
        logger=MagicMock(),
        remove_network_isolation=MagicMock(),
        teardown_vm=lambda *a: cleanup_calls.append("target"),
        teardown_attacker_vm=lambda *a: cleanup_calls.append("attacker"),
        action_by_job={},
    )

    runner.run_live_session_job("job-pair", action_id=31, deps=deps)

    if outcome == "ready":
        deps.boot_simple_vm.assert_called_once_with(
            "job-pair",
            "attacker",
            tmp_path / "attacker.img",
            "ubuntu24.04",
            "ubuntu",
            None,
            vm_vcpus=1,
            vm_memory_mb=2048,
            vm_disk_gb=20,
        )
        deps.complete_action.assert_called_once_with(31)
        deps.job_repository_cls.return_value.transition_job_status.assert_any_call(
            "job-pair",
            "waiting_for_capture",
            capture_wait_deadline=ANY,
        )
        deps.handle_exception.assert_not_called()
        assert cleanup_calls == []
    else:
        deps.complete_action.assert_not_called()
        deps.handle_exception.assert_called_once()
        transitions = deps.job_repository_cls.return_value.transition_job_status.call_args_list
        assert not any(call.args[1] == "waiting_for_capture" for call in transitions)
        assert "target" in cleanup_calls
        if outcome in {"boot-error", "missing-ip"}:
            assert cleanup_calls == ["attacker", "target"]
    deps.provision_workload.assert_not_called()


def test_attacker_boot_passes_configured_images_root(tmp_path):
    from astinuslab.workloads.workload_support import boot_simple_vm

    settings = SimpleNamespace(
        base_dir=tmp_path / "releases" / "candidate",
        images_dir=tmp_path / "images" / "definitions",
        libvirt_uri="qemu:///system",
        libvirt_network="default",
    )
    command = MagicMock()
    result = boot_simple_vm(
        session_scope=lambda: nullcontext(object()),
        repo_cls=MagicMock(),
        get_active_key_paths=lambda: (Path("key"), Path("key.pub")),
        run_command=command,
        parse_guest_ip=lambda *a, **kw: "192.0.2.11",
        wait_for_guest_ssh=MagicMock(),
        sync_runtime_record=MagicMock(),
        settings=settings,
        logger=MagicMock(),
        job_id="job-pair",
        vm_suffix="attacker",
        image_path=settings.images_dir / "ubuntu.img",
        os_variant="ubuntu24.04",
        guest_user="ubuntu",
    )
    assert result == "192.0.2.11"
    assert command.call_args.kwargs["extra_env"]["ASTINUSLAB_IMAGES_DIR"] == str(settings.images_dir)
    assert command.call_args.kwargs["extra_env"]["GUEST_USER"] == "ubuntu"
