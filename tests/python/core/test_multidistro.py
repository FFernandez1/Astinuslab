"""Tests for multi-distro support: catalog username, GUEST_USER propagation, retention."""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from astinuslab.core import fake_mode
from tests.python._support.helpers import autospec_setattr


def _repo_factory(repo) -> type:
    return cast(type, lambda _session: repo)


class TestCatalogDefaultUsername:
    def test_ubuntu_target_has_ubuntu_username(self):
        from astinuslab.catalog import load_targets

        targets = load_targets()
        ubuntu = next(t for t in targets if t["id"] == "ubuntu-22-04-default")
        assert ubuntu["default_username"] == "ubuntu"

    def test_debian_target_has_debian_username(self):
        from astinuslab.catalog import load_targets

        targets = load_targets()
        debian = next((t for t in targets if t["id"] == "debian-12"), None)
        assert debian is not None, "debian-12 target missing from catalog"
        assert debian["default_username"] == "debian"

    def test_fedora_target_has_fedora_username(self):
        from astinuslab.catalog import load_targets

        targets = load_targets()
        fedora = next((t for t in targets if t["id"] == "fedora-40"), None)
        assert fedora is not None, "fedora-40 target missing from catalog"
        assert fedora["default_username"] == "fedora"

    def test_catalog_exposes_family(self):
        from astinuslab.catalog import load_targets

        targets = load_targets()
        ubuntu = next(t for t in targets if t["id"] == "ubuntu-22-04-default")
        assert ubuntu["family"] == "debian"

    def test_all_targets_have_default_username(self):
        from astinuslab.catalog import load_targets

        for target in load_targets():
            assert "default_username" in target, f"target {target['id']} missing default_username"
            assert target["default_username"], f"target {target['id']} has empty default_username"


def _setup_boot_and_prepare_mocks(tmp_path):
    """Build a JobRunDeps with boot_and_prepare infrastructure stubbed so tests
    can intercept SSH mount extra_env. Returns (captured, deps)."""
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    from astinuslab.orchestration.deps import JobRunDeps

    deps = JobRunDeps.default()
    storage_root = tmp_path / "storage"
    deps.settings = deps.settings.model_copy(
        update={"storage_dir": storage_root, "serial_log_dir": storage_root / "logs" / "serial"}
    )
    captured: dict = {}

    class _FakeLifecycle:
        def boot(self, conn, **kwargs):
            captured["boot_kwargs"] = kwargs
            return type("R", (), {"vm_name": kwargs["vm_name"], "guest_ip": "10.0.0.1"})()

    class _FakeDomain:
        pass

    class _FakeConn:
        def lookupByName(self, name):
            return _FakeDomain()

        def close(self):
            pass

    def _fake_run(cmd, cwd=None, job_id=None, extra_env=None, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        if "mount_output_share_remote" in joined:
            captured["env"] = extra_env or {}
            return ""
        if "fix_libvirt_path_acl.sh" in joined:
            captured["acl_cmd"] = cmd
            captured["acl_path"] = str(cmd[-1])
            return ""
        if "libvirt_storage_admin" in joined:
            return ""
        raise AssertionError(f"unexpected command in _boot_and_prepare: {cmd}")

    @contextmanager
    def _fake_scope():
        session = MagicMock()
        repo = MagicMock()  # noqa: F841
        session.__enter__ = lambda s: s
        session.__exit__ = lambda s, *a: None
        yield session

    (tmp_path / "id.pub").write_text("ssh-ed25519 AAAATESTKEY test\n", encoding="utf-8")
    deps.run_command = _fake_run
    deps.require_noninteractive_sudo = lambda: None
    deps.is_cancel_requested = lambda job_id: False
    deps.vm_lifecycle = lambda: _FakeLifecycle()
    deps.open_libvirt_connection = lambda: _FakeConn()
    deps.wait_for_guest_agent = lambda *a, **k: None
    deps.wait_for_guest_ssh = lambda *a, **k: None
    deps.write_guest_metadata_json = lambda *a, **k: None
    deps.collect_guest_metadata_via_qga = lambda dom, uri: {
        "distro_id": "test",
        "distro_version": "1",
        "kernel_release": "5.0",
        "arch": "x86_64",
    }
    deps.sync_runtime_record = lambda *a, **k: None
    deps.get_active_key_paths = lambda: (tmp_path / "id", tmp_path / "id.pub")
    deps.session_scope = _fake_scope
    deps.job_repository_cls = _repo_factory(MagicMock())
    return captured, deps


class TestGuestUserEnvPropagation:
    @pytest.mark.parametrize("existing_cloud_init", [False, True])
    def test_boot_staging_children_pass_acl_guard_under_service_umask(self, monkeypatch, tmp_path, existing_cloud_init):
        from astinuslab.virtualization import boot_staging
        from ops.host.fix_libvirt_path_acl import _check_owner_and_mode

        storage = tmp_path / "storage"
        storage.mkdir()
        storage.chmod(0o2770)
        job_dir = storage / "tmp" / "job-acl"
        if existing_cloud_init:
            cloud_init = job_dir / "cloud-init"
            cloud_init.mkdir(parents=True)
            cloud_init.chmod(0o770)
        monkeypatch.setattr(boot_staging, "settings", SimpleNamespace(storage_dir=storage))
        monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
        checked = []

        def check_acl(cmd, **kwargs):
            target = Path(cmd[-1])
            # A single-identity runner's primary group need not be the storage
            # group trusted by policy. Validate using the real helper's guard.
            for path in [target, *target.iterdir()]:
                _check_owner_and_mode(path.stat(), os.getuid(), frozenset(), frozenset())
            checked.append(target)

        old_umask = os.umask(0o007)
        try:
            boot_staging.stage_boot_runtime_paths(
                job_id="job-acl",
                base_tmp_dir=storage / "tmp",
                serial_log_dir=storage / "logs",
                run_command=check_acl,
                acl_helper_resolver=lambda: Path("/helper.sh"),
            )
        finally:
            os.umask(old_umask)
        assert checked == [job_dir]
        assert storage.stat().st_mode & 0o7777 == 0o2770

    def test_fake_libvirt_does_not_invoke_acl_helper(self, monkeypatch, tmp_path):
        from astinuslab.core import fake_mode
        from astinuslab.virtualization.boot_staging import stage_boot_runtime_paths

        calls: list[list[str]] = []
        monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", True)
        stage_boot_runtime_paths(
            job_id="fake-job",
            base_tmp_dir=tmp_path / "storage" / "tmp",
            serial_log_dir=tmp_path / "logs",
            run_command=lambda cmd, **kwargs: calls.append(cmd),
            acl_helper_resolver=lambda: (_ for _ in ()).throw(AssertionError("ACL helper required in fake mode")),
        )

        assert not any("fix_libvirt_path_acl" in str(cmd) for cmd in calls)

    def test_boot_and_prepare_uses_installed_acl_policy(self, monkeypatch, tmp_path):
        # This test exercises the real privileged path; the suite runs under
        # ASTINUSLAB_FAKE_LIBVIRT=1, where the ACL helper is deliberately not invoked.
        monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)

        captured, deps = _setup_boot_and_prepare_mocks(tmp_path)
        deps.boot_and_prepare(
            "job-test",
            tmp_path / "img.qcow2",
            "debian12",
            "debian-12",
            guest_user="debian",
            target_distro="debian",
            target_family="debian",
        )

        tmp_root = deps.settings.storage_dir / "tmp" / "job-test"
        assert captured["acl_cmd"] == [
            "sudo",
            "-n",
            "/usr/local/libexec/astinuslab/fix_libvirt_path_acl.sh",
            str(tmp_root),
        ]
        assert captured.get("acl_path") == str(tmp_root)

    def test_boot_and_prepare_surfaces_acl_helper_failure(self, monkeypatch, tmp_path):
        # This test exercises the real privileged path; the suite runs under
        # ASTINUSLAB_FAKE_LIBVIRT=1, where the ACL helper is deliberately not invoked.
        monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
        from subprocess import CalledProcessError

        _, deps = _setup_boot_and_prepare_mocks(tmp_path)

        def _fake_run(cmd, cwd=None, job_id=None, extra_env=None, **kwargs):
            joined = " ".join(str(c) for c in cmd)
            if "fix_libvirt_path_acl.sh" in joined:
                raise CalledProcessError(
                    1,
                    cmd,
                    stderr="DENY: path '/tmp/job-test' resolves to '/tmp/job-test', outside allowed roots",
                )
            if "libvirt_storage_admin" in joined:
                return ""
            if "mount_output_share_remote" in joined:
                return ""
            raise AssertionError(f"unexpected command: {cmd}")

        deps.run_command = _fake_run

        with pytest.raises(CalledProcessError) as excinfo:
            deps.boot_and_prepare(
                "job-test",
                tmp_path / "img.qcow2",
                "debian12",
                "debian-12",
                guest_user="debian",
                target_distro="debian",
                target_family="debian",
            )

        assert "outside allowed roots" in excinfo.value.stderr

    def test_boot_and_prepare_passes_guest_template_env(self, monkeypatch, tmp_path):
        # This test exercises the real privileged path; the suite runs under
        # ASTINUSLAB_FAKE_LIBVIRT=1, where the ACL helper is deliberately not invoked.
        monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
        """_boot_and_prepare must pass guest/distro env vars to SSH mount script."""

        captured, deps = _setup_boot_and_prepare_mocks(tmp_path)
        deps.boot_and_prepare(
            "job-test",
            tmp_path / "img.qcow2",
            "debian12",
            "debian-12",
            guest_user="debian",
            target_distro="debian",
            target_family="debian",
        )

        assert captured["acl_path"] == str(deps.settings.storage_dir / "tmp" / "job-test")
        assert captured["acl_cmd"][-1] == str(deps.settings.storage_dir / "tmp" / "job-test")
        assert captured.get("env", {}).get("GUEST_USER") == "debian"
        assert captured.get("env", {}).get("TARGET_DISTRO") == "debian"
        assert captured.get("env", {}).get("TARGET_FAMILY") == "debian"

    def test_boot_and_prepare_passes_machine_type_env(self, monkeypatch, tmp_path):

        captured, deps = _setup_boot_and_prepare_mocks(tmp_path)
        deps.boot_and_prepare(
            "job-test",
            tmp_path / "img.qcow2",
            "debian12",
            "debian-12",
            guest_user="debian",
            target_distro="debian",
            target_family="debian",
            target_machine_type="pc",
        )

        assert captured.get("env", {}).get("VIRT_MACHINE_TYPE") == "pc"
        assert captured.get("env", {}).get("LIBVIRT_URI") == deps.settings.libvirt_uri
        assert captured.get("env", {}).get("LIBVIRT_NETWORK") == deps.settings.libvirt_network

    def test_boot_and_prepare_passes_target_network_profile_env(self, monkeypatch, tmp_path):

        captured, deps = _setup_boot_and_prepare_mocks(tmp_path)
        deps.boot_and_prepare(
            "job-test",
            tmp_path / "img.qcow2",
            "debian12",
            "debian-12",
            guest_user="debian",
            target_distro="debian",
            target_family="debian",
            target_network_profile="labnet",
        )

        assert captured.get("env", {}).get("LIBVIRT_NETWORK") == "labnet"

    def test_boot_and_prepare_passes_cloud_init_compatibility_env(self, monkeypatch, tmp_path):

        captured, deps = _setup_boot_and_prepare_mocks(tmp_path)
        deps.boot_and_prepare(
            "job-test",
            tmp_path / "img.qcow2",
            "ubuntu20.04",
            "ubuntu-20-04",
            guest_user="ubuntu",
            target_distro="ubuntu",
            target_family="debian",
            target_cloud_init_profile="ubuntu",
            target_inject_network_config=False,
        )

        assert captured.get("env", {}).get("CLOUD_INIT_PROFILE") == "ubuntu"
        assert captured.get("env", {}).get("CLOUD_INIT_NETWORK_CONFIG") == "0"

    def test_boot_and_prepare_wires_robust_ip_resolver_preservation_and_serial_log(self, monkeypatch, tmp_path):

        captured, deps = _setup_boot_and_prepare_mocks(tmp_path)
        parse_calls = []

        def _fake_parse_guest_ip(job_id, libvirt_uri=None, libvirt_network=None):
            parse_calls.append((job_id, libvirt_uri, libvirt_network))
            return "10.0.0.44"

        deps.parse_guest_ip = _fake_parse_guest_ip
        deps.boot_and_prepare(
            "job-test",
            tmp_path / "img.qcow2",
            "debian12",
            "debian-12",
            guest_user="debian",
            target_distro="debian",
            target_family="debian",
            target_network_profile="labnet",
            keep_failed_vm=True,
        )

        boot_kwargs = captured["boot_kwargs"]
        assert boot_kwargs["preserve_on_failure"] is True
        assert boot_kwargs["serial_log_path"] == deps.settings.serial_log_dir / "astinuslab-job-test-serial.log"
        assert boot_kwargs["ip_resolver"]() == "10.0.0.44"
        assert parse_calls == [("job-test", deps.settings.libvirt_uri, "labnet")]

    def test_acquire_and_publish_passes_guest_user_env(self, tmp_path):
        """acquire_and_publish must include GUEST_USER in extra_env for disk capture script."""
        from contextlib import contextmanager
        from unittest.mock import MagicMock

        from astinuslab.orchestration.deps import JobRunDeps

        deps = JobRunDeps.default()
        captured = {}
        deps.settings = deps.settings.model_copy(
            update={"storage_dir": tmp_path / "storage", "serial_log_dir": tmp_path / "serial"}
        )
        disk_path = tmp_path / "disk.qcow2"
        disk_path.write_bytes(b"disk")
        private_key = tmp_path / "id_ed25519"
        public_key = tmp_path / "id_ed25519.pub"
        private_key.write_text("key", encoding="utf-8")
        public_key.write_text("pub", encoding="utf-8")

        def _fake_run(cmd, cwd=None, job_id=None, extra_env=None, **kwargs):
            script = " ".join(str(c) for c in cmd)
            if "acquire_disk_remote" in script:
                captured["env"] = extra_env or {}
                return f"disk_final_path={disk_path}\ndisk_filename=disk.qcow2"
            if cmd and cmd[0] == "sha256sum":
                return "0" * 64 + f"  {cmd[1]}"
            return ""

        deps.run_command = _fake_run
        deps.is_cancel_requested = lambda job_id: False
        conn = MagicMock()
        conn.lookupByName.return_value.isActive.return_value = 1
        deps.open_libvirt_connection = lambda: conn
        deps.check_host_disk_space = lambda *a, **k: None
        deps.get_active_key_paths = lambda: (private_key, public_key)

        # Patch session_scope so DB calls don't fail
        @contextmanager
        def _fake_scope():
            session = MagicMock()
            session.get.return_value = MagicMock(power_state="on")
            yield session

        fake_repo = MagicMock()
        fake_repo.get_runtime.return_value = SimpleNamespace(power_state="on")
        deps.session_scope = _fake_scope
        deps.job_repository_cls = _repo_factory(fake_repo)

        deps.acquire_and_publish(
            "job-test",
            "debian-12",
            "192.168.1.100",
            "astinuslab-job-test",
            "debian",
            "12",
            None,
            guest_user="debian",
            capture_type="disk",
        )

        assert captured.get("env", {}).get("GUEST_USER") == "debian"


class TestGuestIpResolution:
    def test_parse_guest_ip_from_dhcp_lease(self):
        from astinuslab.orchestration.deps import JobRunDeps

        deps = JobRunDeps.default()
        calls = {"leases": 0}

        def _fake_run(cmd, cwd=None, job_id=None, extra_env=None, **kwargs):
            if cmd[:5] == ["virsh", "-c", "qemu:///system", "domiflist", "astinuslab-job-test"]:
                return "Interface  Type       Source     Model       MAC\nvnet0      network    default    virtio      52:54:00:77:c1:2d"
            if cmd[:5] == ["virsh", "-c", "qemu:///system", "net-dhcp-leases", "default"]:
                calls["leases"] += 1
                return "2026-03-15 17:30:53   52:54:00:77:c1:2d   ipv4   192.168.122.109/24   debian"
            if cmd[:5] == ["virsh", "-c", "qemu:///system", "domifaddr", "astinuslab-job-test"]:
                return ""
            raise AssertionError(f"unexpected command: {cmd}")

        deps.run_command = _fake_run
        ip = deps.parse_guest_ip("job-test")
        assert ip == "192.168.122.109"
        assert calls["leases"] >= 1


class TestGuestSshReadiness:
    def test_wait_for_guest_ssh_retries_until_success(self):
        import logging
        from types import SimpleNamespace

        from astinuslab.virtualization import guest_wait

        attempts = {"count": 0}

        class _Result:
            def __init__(self, returncode: int, stderr: str = ""):
                self.returncode = returncode
                self.stderr = stderr
                self.stdout = ""

        def _fake_run(cmd, text=None, capture_output=None, **kwargs):
            attempts["count"] += 1
            if attempts["count"] < 3:
                return _Result(255, "ssh: connect to host 192.168.122.109 port 22: Connection refused")
            return _Result(0)

        fake_subprocess = SimpleNamespace(run=_fake_run, TimeoutExpired=Exception)
        fake_time = SimpleNamespace(sleep=lambda seconds: None, time=time.time, monotonic=time.monotonic)

        guest_wait.wait_for_guest_ssh(
            lambda job_id: False,
            logging.getLogger("test"),
            fake_subprocess,
            fake_time,
            "job-test",
            "192.168.122.109",
            Path("/tmp/key"),
            "debian",
            max_wait_seconds=30,
            poll_interval_seconds=1,
        )

        assert attempts["count"] == 3

    def test_boot_and_prepare_uses_qga_then_mounts_share(self, monkeypatch, tmp_path):
        # This test exercises the real privileged path; the suite runs under
        # ASTINUSLAB_FAKE_LIBVIRT=1, where the ACL helper is deliberately not invoked.
        monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
        """boot_and_prepare must call VMLifecycle.boot → QGA wait → QGA metadata → SSH mount, in order."""

        calls: list[str] = []

        class _FakeLifecycle:
            def boot(self, conn, **kwargs):
                calls.append("lifecycle_boot")
                return type("R", (), {"vm_name": kwargs["vm_name"], "guest_ip": "192.168.122.109"})()

        class _FakeDomain:
            pass

        class _FakeConn:
            def lookupByName(self, name):
                return _FakeDomain()

            def close(self):
                pass

        from contextlib import contextmanager
        from unittest.mock import MagicMock

        @contextmanager
        def _fake_scope():
            yield MagicMock()

        from astinuslab.orchestration.deps import JobRunDeps

        deps = JobRunDeps.default()
        deps.settings = deps.settings.model_copy(
            update={"storage_dir": tmp_path / "storage", "serial_log_dir": tmp_path / "serial"}
        )
        (tmp_path / "id.pub").write_text("ssh-ed25519 KEY test\n", encoding="utf-8")
        deps.require_noninteractive_sudo = lambda: None
        deps.is_cancel_requested = lambda job_id: False
        deps.vm_lifecycle = lambda: _FakeLifecycle()
        deps.open_libvirt_connection = lambda: _FakeConn()
        deps.wait_for_guest_agent = lambda *a, **k: calls.append("wait_qga")

        def fake_qga_metadata(dom, uri):
            calls.append("collect_metadata")
            return {"distro_id": "debian", "distro_version": "12", "kernel_release": "6.1.0", "arch": "x86_64"}

        deps.collect_guest_metadata_via_qga = fake_qga_metadata
        deps.wait_for_guest_ssh = lambda *a, **k: calls.append("wait_ssh")
        deps.sync_runtime_record = lambda *a, **k: None
        deps.get_active_key_paths = lambda: (tmp_path / "id", tmp_path / "id.pub")
        deps.session_scope = _fake_scope
        deps.job_repository_cls = _repo_factory(MagicMock())

        def _fake_run(cmd, cwd=None, job_id=None, extra_env=None, **kwargs):
            joined = " ".join(str(c) for c in cmd)
            if "mount_output_share_remote" in joined:
                calls.append("mount_share")
                return ""
            if "fix_libvirt_path_acl.sh" in joined:
                calls.append("fix_acl")
                return ""
            if "libvirt_storage_admin" in joined:
                return ""
            raise AssertionError(f"unexpected command: {cmd}")

        deps.run_command = _fake_run

        guest_ip, metadata = deps.boot_and_prepare(
            "job-test",
            tmp_path / "img.qcow2",
            "debian12",
            "debian-12",
            guest_user="debian",
            target_distro="debian",
            target_family="debian",
        )

        assert guest_ip == "192.168.122.109"
        assert metadata["distro_id"] == "debian"
        assert calls == ["fix_acl", "lifecycle_boot", "wait_qga", "collect_metadata", "wait_ssh", "mount_share"]

    def test_boot_and_prepare_falls_back_to_ssh_metadata_when_qga_collection_fails(self, monkeypatch, tmp_path):
        # This test exercises the real privileged path; the suite runs under
        # ASTINUSLAB_FAKE_LIBVIRT=1, where the ACL helper is deliberately not invoked.
        monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)

        calls: list[str] = []

        class _FakeLifecycle:
            def boot(self, conn, **kwargs):
                calls.append("lifecycle_boot")
                return type("R", (), {"vm_name": kwargs["vm_name"], "guest_ip": "192.168.122.109"})()

        class _FakeConn:
            def close(self):
                pass

        from contextlib import contextmanager
        from unittest.mock import MagicMock

        @contextmanager
        def _fake_scope():
            yield MagicMock()

        from astinuslab.orchestration.deps import JobRunDeps

        deps = JobRunDeps.default()
        deps.settings = deps.settings.model_copy(
            update={"storage_dir": tmp_path / "storage", "serial_log_dir": tmp_path / "serial"}
        )
        (tmp_path / "id.pub").write_text("ssh-ed25519 KEY test\n", encoding="utf-8")
        (tmp_path / "id").write_text("key\n", encoding="utf-8")
        deps.require_noninteractive_sudo = lambda: None
        deps.is_cancel_requested = lambda job_id: False
        deps.vm_lifecycle = lambda: _FakeLifecycle()
        deps.open_libvirt_connection = lambda: _FakeConn()
        deps.wait_for_guest_agent = lambda *a, **k: calls.append("wait_qga")
        deps.collect_guest_metadata_via_qga = lambda dom, uri: (_ for _ in ()).throw(RuntimeError("qga failed"))
        deps.wait_for_guest_ssh = lambda *a, **k: calls.append("wait_ssh")

        def fake_ssh_metadata(ip, key, user):
            calls.append("collect_ssh_metadata")
            return {"distro_id": "debian", "distro_version": "12", "kernel_release": "6.1.0", "arch": "x86_64"}

        deps.collect_guest_metadata_via_ssh = fake_ssh_metadata
        deps.sync_runtime_record = lambda *a, **k: None
        deps.get_active_key_paths = lambda: (tmp_path / "id", tmp_path / "id.pub")
        deps.session_scope = _fake_scope
        deps.job_repository_cls = _repo_factory(MagicMock())

        def _fake_run(cmd, cwd=None, job_id=None, extra_env=None, **kwargs):
            joined = " ".join(str(c) for c in cmd)
            if "mount_output_share_remote" in joined:
                calls.append("mount_share")
                return ""
            if "fix_libvirt_path_acl.sh" in joined:
                calls.append("fix_acl")
                return ""
            if "libvirt_storage_admin" in joined:
                return ""
            raise AssertionError(f"unexpected command: {cmd}")

        deps.run_command = _fake_run

        guest_ip, metadata = deps.boot_and_prepare(
            "job-test",
            tmp_path / "img.qcow2",
            "debian12",
            "debian-12",
            guest_user="debian",
            target_distro="debian",
            target_family="debian",
        )

        assert guest_ip == "192.168.122.109"
        assert metadata["kernel_release"] == "6.1.0"
        assert metadata["source"] == "ssh"
        assert calls == [
            "fix_acl",
            "lifecycle_boot",
            "wait_qga",
            "wait_ssh",
            "collect_ssh_metadata",
            "wait_ssh",
            "mount_share",
        ]

    def test_write_guest_metadata_json_preserves_metadata_source(self, tmp_path):
        from astinuslab.virtualization.boot_staging import write_guest_metadata_json

        output = tmp_path / "guest-metadata.json"

        write_guest_metadata_json(
            output,
            "job-test",
            {
                "distro_id": "debian",
                "distro_version": "12",
                "kernel_release": "6.1.0",
                "arch": "x86_64",
                "source": "ssh",
            },
        )

        assert '"source": "ssh"' in output.read_text(encoding="utf-8")


class TestGuestIPResolution:
    def test_parse_guest_ip_falls_back_to_domifaddr(self):
        from astinuslab.orchestration.deps import JobRunDeps

        deps = JobRunDeps.default()

        def _fake_run(cmd, cwd=None, job_id=None, extra_env=None, **kwargs):
            if cmd[:5] == ["virsh", "-c", "qemu:///system", "domiflist", "astinuslab-job-test"]:
                return "Interface  Type       Source     Model       MAC\nvnet0      network    default    virtio      52:54:00:77:c1:2d"
            if cmd[:5] == ["virsh", "-c", "qemu:///system", "net-dhcp-leases", "default"]:
                return ""
            if cmd[:5] == ["virsh", "-c", "qemu:///system", "domifaddr", "astinuslab-job-test"]:
                return "Name       MAC address          Protocol     Address\n-------------------------------------------------------------------------------\nvnet0      52:54:00:77:c1:2d    ipv4         192.168.122.109/24"
            raise AssertionError(f"unexpected command: {cmd}")

        deps.run_command = _fake_run
        ip = deps.parse_guest_ip("job-test")
        assert ip == "192.168.122.109"

    def test_parse_guest_ip_uses_configured_network(self):
        from astinuslab.orchestration.deps import JobRunDeps

        deps = JobRunDeps.default()

        def _fake_run(cmd, cwd=None, job_id=None, extra_env=None, **kwargs):
            if cmd[:5] == ["virsh", "-c", "qemu:///custom", "domiflist", "astinuslab-job-test"]:
                return "Interface  Type       Source     Model       MAC\nvnet0      network    labnet     virtio      52:54:00:77:c1:2d"
            if cmd[:5] == ["virsh", "-c", "qemu:///custom", "net-dhcp-leases", "labnet"]:
                return "2026-03-15 17:30:53   52:54:00:77:c1:2d   ipv4   10.10.10.25/24   debian"
            if cmd[:5] == ["virsh", "-c", "qemu:///custom", "domifaddr", "astinuslab-job-test"]:
                return ""
            raise AssertionError(f"unexpected command: {cmd}")

        deps.run_command = _fake_run
        ip = deps.parse_guest_ip("job-test", libvirt_uri="qemu:///custom", libvirt_network="labnet")
        assert ip == "10.10.10.25"

    def test_parse_guest_ip_failure_includes_diagnostics(self, tmp_path):
        import logging
        import subprocess as real_subprocess
        from types import SimpleNamespace

        from astinuslab.core.config import settings
        from astinuslab.jobs import runtime_support

        def _fake_run(cmd, cwd=None, job_id=None, extra_env=None, **kwargs):
            if cmd[:5] == ["virsh", "-c", "qemu:///system", "domiflist", "astinuslab-job-test"]:
                return "Interface  Type       Source     Model       MAC\nvnet0      network    default    virtio      52:54:00:77:c1:2d"
            if cmd[:5] == ["virsh", "-c", "qemu:///system", "net-dhcp-leases", "default"]:
                return "Expiry Time   MAC address   Protocol   IP address   Hostname   Client ID or DUID"
            raise AssertionError(f"unexpected command: {cmd}")

        def _fake_capture(cmd, text=None, capture_output=None, stderr=None):
            class _Result:
                def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
                    self.returncode = returncode
                    self.stdout = stdout
                    self.stderr = stderr

            if cmd[:5] == ["virsh", "-c", "qemu:///system", "domifaddr", "astinuslab-job-test"]:
                return _Result(1, "", "No guest agent available")
            if cmd[:5] == ["virsh", "-c", "qemu:///system", "domiflist", "astinuslab-job-test"]:
                return _Result(
                    0, "Interface  Type  Source  Model  MAC\nvnet0  network  default  virtio  52:54:00:77:c1:2d"
                )
            raise AssertionError(f"unexpected command: {cmd}")

        fake_subprocess = SimpleNamespace(
            run=_fake_capture,
            check_output=lambda *args, **kwargs: "running",
            CalledProcessError=real_subprocess.CalledProcessError,
            DEVNULL=real_subprocess.DEVNULL,
        )
        fake_time = SimpleNamespace(sleep=lambda seconds: None, time=time.time, monotonic=time.monotonic)

        serial_log = tmp_path / "astinuslab-job-test-serial.log"
        serial_log.write_text("cloud-init: no route to host\n", encoding="utf-8")
        expected_serial = str(settings.serial_log_dir / "astinuslab-job-test-serial.log")

        def fake_path(value):
            return serial_log if str(value) == expected_serial else Path(value)

        with pytest.raises(RuntimeError) as excinfo:
            runtime_support.parse_guest_ip(
                "job-test",
                settings,
                _fake_run,
                subprocess_module=fake_subprocess,
                time_module=fake_time,
                path_cls=fake_path,
                logger=logging.getLogger("test"),
            )

        message = str(excinfo.value)
        assert "Could not resolve guest IP from libvirt DHCP/domifaddr" in message
        assert "Detected MAC: 52:54:00:77:c1:2d" in message
        assert "DHCP leases (final poll):" in message
        assert "domifaddr stderr: No guest agent available" in message
        assert "Serial console (last 60 lines):" in message


class TestRetention:
    def test_run_retention_returns_counts(self, tmp_path, monkeypatch):
        import os

        from astinuslab.storage import retention as ret

        dumps_dir = tmp_path / "storage" / "dumps"
        dumps_dir.mkdir(parents=True)
        old_dump = dumps_dir / "old.dump"
        old_dump.write_bytes(b"data")
        old_time = time.time() - (91 * 86400)
        os.utime(old_dump, (old_time, old_time))

        count = ret._purge_old_files(dumps_dir, older_than_days=90, suffix=".dump")
        assert count == 1
        assert not old_dump.exists()

    def test_run_retention_keeps_recent_files(self, tmp_path):
        from astinuslab.storage import retention as ret

        dumps_dir = tmp_path / "storage" / "dumps"
        dumps_dir.mkdir(parents=True)
        recent = dumps_dir / "recent.dump"
        recent.write_bytes(b"data")

        count = ret._purge_old_files(dumps_dir, older_than_days=90, suffix=".dump")
        assert count == 0
        assert recent.exists()

    def test_retention_api_endpoint(self, client, monkeypatch):
        import astinuslab.api as api_mod

        autospec_setattr(
            monkeypatch, api_mod, "trigger_retention_service", lambda: {"dumps_deleted": 3, "logs_deleted": 1}
        )

        r = client.post("/api/v1/run-retention")
        assert r.status_code == 200
        data = r.json()
        assert data["dumps_deleted"] == 3

    def test_storage_admin_page_renders_workbench(self, client):
        r = client.get("/storage", follow_redirects=False)
        assert r.status_code == 200
        assert "Storage Admin" in r.text
        assert "tab-strip" in r.text


def test_scan_images_dir_uses_sidecar(tmp_path):
    from astinuslab import catalog
    from astinuslab.images.image_inspection import SIDECAR_SCHEMA_VERSION, ImageMetadata, save_sidecar

    img = tmp_path / "totally-custom-name.qcow2"
    img.write_bytes(b"")
    meta = ImageMetadata(
        schema_version=SIDECAR_SCHEMA_VERSION,
        distro="alpine",
        version="3.20",
        family="other",
        default_username="alpine",
        os_variant="generic",
        machine_type=None,
        kernel_release=None,
        arch="x86_64",
        cloud_init_profile=None,
        inject_network_config=None,
        source="user",
        detected_at="2026-05-02T00:00:00+00:00",
        user_edited_fields=["distro"],
        inspect_error=None,
    )
    save_sidecar(img, meta)

    targets = catalog._scan_images_dir(tmp_path)
    assert len(targets) == 1
    assert targets[0]["distro"] == "alpine"
    assert targets[0]["version"] == "3.20"
