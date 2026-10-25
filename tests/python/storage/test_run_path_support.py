"""Unit tests for run_path_support — boot context resolution and post-boot script execution."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from astinuslab.api.schemas import JobCreate
from astinuslab.db.repositories import JobRepository
from astinuslab.storage.run_path_support import (
    prepare_boot_context,
    resolve_target_execution_context,
    run_post_boot_script,
)


class FakeSettings:
    def __init__(self, images_dir: Path):
        self.images_dir = images_dir


class TestResolveTargetExecutionContext:
    def _target_by_id(self, tid):
        return {
            "id": tid,
            "default_username": "ubuntu",
            "keep_failed_vm": False,
            "image_ref": "ubuntu.qcow2",
        }

    def _scenario_by_id(self, sid):
        return {"id": sid, "mode": "immediate"}

    def test_returns_target_guest_user_and_flag(self):
        target, scenario, guest_user, keep_failed_vm = resolve_target_execution_context(
            self._target_by_id,
            self._scenario_by_id,
            "ubuntu-22",
            "immediate-capture",
        )
        assert guest_user == "ubuntu"
        assert keep_failed_vm is False

    def test_no_scenario_id_returns_none_scenario(self):
        target, scenario, _, _ = resolve_target_execution_context(
            self._target_by_id,
            self._scenario_by_id,
            "ubuntu-22",
            None,
        )
        assert scenario is None

    def test_keep_failed_vm_respected(self):
        def target_with_keep(tid):
            return {"id": tid, "default_username": "root", "keep_failed_vm": True, "image_ref": "x.qcow2"}

        _, _, _, keep = resolve_target_execution_context(target_with_keep, self._scenario_by_id, "t", "s")
        assert keep is True

    def test_default_username_fallback(self):
        def target_no_user(tid):
            return {"id": tid, "image_ref": "x.qcow2"}

        _, _, guest_user, _ = resolve_target_execution_context(target_no_user, self._scenario_by_id, "t", None)
        assert guest_user == "ubuntu"


class TestPrepareBootContext:
    def test_uses_job_image_path_when_provided(self, tmp_path):
        image = tmp_path / "custom" / "my.qcow2"
        image.parent.mkdir()
        image.write_bytes(b"")
        target = {"image_ref": "default.qcow2"}
        settings = FakeSettings(tmp_path)
        image_path, _, _, _ = prepare_boot_context(target, settings, str(image), None, None, None)
        assert image_path == image.resolve()

    def test_falls_back_to_images_dir_when_no_job_path(self, tmp_path):
        target = {"image_ref": "ubuntu.qcow2"}
        settings = FakeSettings(tmp_path)
        image_path, _, _, _ = prepare_boot_context(target, settings, None, None, None, None)
        assert image_path == tmp_path.resolve() / "ubuntu.qcow2"

    def test_job_vcpus_override_target_vcpus(self, tmp_path):
        target = {"image_ref": "x.qcow2", "vcpu": 2}
        settings = FakeSettings(tmp_path)
        _, vcpus, _, _ = prepare_boot_context(target, settings, None, 4, None, None)
        assert vcpus == 4

    def test_target_vcpus_used_when_no_job_override(self, tmp_path):
        target = {"image_ref": "x.qcow2", "vcpu": 2}
        settings = FakeSettings(tmp_path)
        _, vcpus, _, _ = prepare_boot_context(target, settings, None, None, None, None)
        assert vcpus == 2

    def test_all_resource_overrides_applied(self, tmp_path):
        target = {"image_ref": "x.qcow2", "vcpu": 1, "memory_mb": 1024, "disk_gb": 10}
        settings = FakeSettings(tmp_path)
        _, vcpus, mem, disk = prepare_boot_context(target, settings, None, 4, 8192, 50)
        assert vcpus == 4
        assert mem == 8192
        assert disk == 50


class TestPrepareBootContextImagePathContainment:
    def test_rejects_image_path_outside_images_dir(self, tmp_path):
        images_dir = tmp_path / "images"
        images_dir.mkdir()
        outside = tmp_path / "outside.qcow2"
        outside.write_bytes(b"")

        settings = FakeSettings(images_dir=images_dir)
        target = {"image_ref": "ubuntu.qcow2"}

        with pytest.raises(ValueError, match="image_path must be inside"):
            prepare_boot_context(
                target=target,
                settings=settings,
                job_image_path=str(outside),
                vm_vcpus=None,
                vm_memory_mb=None,
                vm_disk_gb=None,
            )

    def test_accepts_image_path_inside_images_dir(self, tmp_path):
        images_dir = tmp_path / "images"
        images_dir.mkdir()
        good = images_dir / "ubuntu.qcow2"
        good.write_bytes(b"")

        settings = FakeSettings(images_dir=images_dir)
        target = {"image_ref": "ubuntu.qcow2"}

        path, *_ = prepare_boot_context(
            target=target,
            settings=settings,
            job_image_path=str(good),
            vm_vcpus=None,
            vm_memory_mb=None,
            vm_disk_gb=None,
        )

        assert path == good.resolve()


class TestRunPostBootScriptTransitions:
    def _seed_mounted_job(self, in_memory_session_scope) -> str:
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job = repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="workload-sample-post-script",
                    execution_mode="post_script",
                )
            )
            repo.transition_job_status(job.id, "preparing_image")
            repo.transition_job_status(job.id, "waiting_for_guest_access")
            repo.transition_job_status(job.id, "mounting_output_share")
            return job.id

    def _run_script_for_job(self, in_memory_session_scope, tmp_path, job_id: str) -> list[dict]:
        private_key = tmp_path / "id_ed25519"
        public_key = tmp_path / "id_ed25519.pub"
        private_key.write_text("key", encoding="utf-8")
        public_key.write_text("pub", encoding="utf-8")
        calls = []

        def recording_run_command(cmd, **kwargs):
            calls.append({"cmd": cmd, "kwargs": kwargs})
            return ""

        run_post_boot_script(
            in_memory_session_scope,
            JobRepository,
            lambda: (private_key, public_key),
            recording_run_command,
            lambda _job_id: False,
            SimpleNamespace(base_dir=tmp_path, data_dir=tmp_path),
            job_id,
            "10.0.0.5",
            "ubuntu",
            {"script": {"enabled": True, "path": "scenario.sh", "timeout_seconds": 5}},
            None,
        )
        return calls

    def test_post_script_ends_ready_for_memory_capture_dispatch(self, in_memory_session_scope, tmp_path):
        job_id = self._seed_mounted_job(in_memory_session_scope)

        calls = self._run_script_for_job(in_memory_session_scope, tmp_path, job_id)

        assert calls, "scenario script runner should be invoked"
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job = repo.get_job(job_id)
            assert job is not None
            assert job.status == "waiting_for_capture"
            # The transition must be evented so the release playbook's row-3
            # evidence (running_scenario_script -> waiting_for_capture) exists.
            states = [e.state for e in repo.list_events(job_id)]
            assert "waiting_for_capture" in states
            repo.transition_job_status(job_id, "acquiring_memory")

    def test_post_script_ends_ready_for_disk_capture_dispatch(self, in_memory_session_scope, tmp_path):
        job_id = self._seed_mounted_job(in_memory_session_scope)

        calls = self._run_script_for_job(in_memory_session_scope, tmp_path, job_id)

        assert calls, "scenario script runner should be invoked"
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job = repo.get_job(job_id)
            assert job is not None
            assert job.status == "waiting_for_capture"
            repo.transition_job_status(job_id, "acquiring_disk")
