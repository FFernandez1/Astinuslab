from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest

from astinuslab.orchestration.deps import JobRunDeps
from astinuslab.orchestration.runners import boot_capture_runner as runner
from astinuslab.virtualization import windows_runtime
from astinuslab.virtualization.windows_runtime import build_windows_bootstrap_script
from tests.python._support.windows_images import write_windows_image


def test_generated_computer_names_are_accepted_by_runtime_bootstrap():
    names = [runner.windows_computer_name(job_id) for job_id in ("job-windows-a", "job-windows-b")]

    for name in names:
        # Exercise the actual bootstrap validator, which the boot-path mock skips.
        build_windows_bootstrap_script("c3NoLWVkMjU1MTkgdGVzdA==", name)
        assert len(name) <= 15
    assert names[0] != names[1]
    assert runner.windows_computer_name("job-windows-a") == names[0]


def _windows_target() -> dict:
    return {
        "platform": "windows",
        "family": "windows",
        "distro": "windows-server",
        "version": "2022",
        "default_username": "Administrator",
        "cloud_init": False,
        "os_variant": "win2k22",
        "reusable": True,
    }


def test_is_windows_target_requires_windows_metadata():
    assert runner.is_windows_target(_windows_target()) is True
    assert runner.is_windows_target({**_windows_target(), "family": "debian"}) is False
    assert runner.is_windows_target({**_windows_target(), "reusable": False}) is False


def test_boot_and_prepare_dispatches_windows_without_cloud_init(monkeypatch):
    calls: list[str] = []

    def fail_cloud_init(*args, **kwargs):
        calls.append("cloud_init")
        raise AssertionError("Windows boot must not stage cloud-init")

    def fake_windows_boot(*args, **kwargs):
        calls.append("windows")
        return "192.168.122.50", {"distro_id": "windows-server", "distro_version": "2022", "source": "qga"}

    monkeypatch.setattr(runner, "boot_and_prepare_windows", fake_windows_boot)
    result = runner.boot_and_prepare(
        "job-windows",
        Path("/images/windows.qcow2"),
        "win2k22",
        "windows-server",
        guest_user="Administrator",
        target_distro="windows-server",
        target_family="windows",
        target_cloud_init_profile=None,
        target_inject_network_config=False,
        target_platform="windows",
        target_reusable=True,
        deps=type(
            "Deps", (), {"boot_and_prepare_windows": fake_windows_boot, "stage_target_cloud_init": fail_cloud_init}
        )(),
    )

    assert result[0] == "192.168.122.50"
    assert result[1]["distro_id"] == "windows-server"
    assert calls == ["windows"]


@pytest.mark.parametrize("use_cache", [False, True])
@pytest.mark.parametrize(
    ("identity_outcome", "keep_failed"),
    [
        ("ok", False),
        ("timeout", False),
        ("timeout", True),
        ("cancel", False),
        ("nvram-collision", False),
        ("owned-runtime", False),
        ("setup-pending", False),
        ("setup-pending", True),
        ("prepared-failure", False),
        ("prepared-failure", True),
    ],
)
def test_boot_and_prepare_windows_runs_qga_first_and_never_stages_seed(
    monkeypatch, tmp_path, use_cache, identity_outcome, keep_failed
):
    from astinuslab.orchestration.deps import JobRunDeps
    from astinuslab.orchestration.runners import boot_capture_runner as module
    from astinuslab.virtualization.uefi import WindowsFirmwareConfig
    from astinuslab.virtualization.vm_lifecycle import VMLifecycle

    events: list[str] = []
    clock = [0.0]
    fake_time = SimpleNamespace(
        monotonic=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    expected_name = runner.windows_computer_name("job-windows")

    def guest_identity(*args, **kwargs):
        # The old boot remains responsive during the scheduled reboot delay.
        boot_id = "638930000000000001" if clock[0] >= 2 and identity_outcome == "ok" else "638930000000000000"
        return windows_runtime.GuestExecResult(
            0, __import__("json").dumps({"computer_name": expected_name, "boot_id": boot_id}), ""
        )

    monkeypatch.setattr(windows_runtime, "run_guest_exec", guest_identity)
    monkeypatch.setattr(windows_runtime, "probe_windows_agent", lambda *a, **k: True)

    def verify_identity(*args, **kwargs):
        events.append("identity")
        return windows_runtime.wait_for_windows_identity(
            *args, **kwargs, time_module=fake_time, max_wait_seconds=5, poll_interval_seconds=2
        )

    def verify_setup(*args, **kwargs):
        events.append("setup")
        if identity_outcome == "setup-pending":
            raise RuntimeError("Windows Setup did not complete")
        return "638930000000000000"

    def prepare_guest(uri, name, bundle, boot):
        assert boot == "638930000000000000"
        assert bundle.profile == "server2022-unattend-v1"
        events.append("prepared")
        if identity_outcome == "prepared-failure":
            raise RuntimeError("Windows prepared guest failed during answer")
        return SimpleNamespace(boot_id=boot, machine_sid_prefix="S-1-5-21-1-2-3", answer_descriptor_hash="b" * 64)

    public_key_path = tmp_path / "id_ed25519.pub"
    public_key_path.write_text("ssh-ed25519 test-runtime-key\n", encoding="utf-8")
    job_storage = tmp_path / "job-storage"
    job_storage.mkdir()
    job_storage.chmod(0o755)
    if identity_outcome == "nvram-collision":
        (job_storage / "astinuslab-job-windows.vars").symlink_to(tmp_path / "must-not-be-created.vars")
    template = tmp_path / "OVMF_VARS_4M.fd"
    template.write_bytes(b"template")
    runtime_tmp = tmp_path / "runtime"
    runtime_tmp.mkdir()
    share_dir = runtime_tmp / "share"
    share_dir.mkdir()

    monkeypatch.setattr(
        module,
        "_stage_boot_runtime_paths_impl",
        lambda **kwargs: (runtime_tmp, runtime_tmp / "cloud-init", share_dir),
    )
    image = write_windows_image(tmp_path / "images" / "windows.qcow2")
    cache = write_windows_image(tmp_path / "storage" / "workload-cache" / "cached.qcow2") if use_cache else None
    for selected in (image, cache):
        if selected is None:
            continue
        selected.parent.chmod(0o755)
        media = selected.parent / "setup.iso"
        media.write_bytes(b"synthetic public media")
        media.chmod(0o644)
        sidecar = selected.with_suffix(".qcow2.meta.json")
        value = __import__("json").loads(sidecar.read_text())
        value["first_boot"]["media"].update(
            size_bytes=media.stat().st_size, sha256=__import__("hashlib").sha256(media.read_bytes()).hexdigest()
        )
        sidecar.write_text(__import__("json").dumps(value))

    def install_backing(**kwargs):
        assert kwargs["image_path"] == image.resolve()
        assert kwargs["cache_image"] == (cache.resolve() if cache else None)
        backing = tmp_path / "backing.qcow2"
        backing.write_bytes((cache or image).read_bytes())
        backing.chmod(0o644)
        return backing, job_storage

    monkeypatch.setattr(module, "_install_backing_image_impl", install_backing)
    monkeypatch.setattr(
        module,
        "WindowsFirmwareConfig",
        lambda: WindowsFirmwareConfig(nvram_template=str(template)),
    )
    monkeypatch.setattr(module, "_persist_guest_access_and_metadata_impl", lambda *a, **k: events.append("persist"))

    class TestLifecycle(VMLifecycle):
        def destroy(self, conn, vm_name):
            events.append("destroy")

    def define_domain(xml):
        assert xml.count("device='cdrom'") == 1
        assert str(job_storage / "windows-setup.iso") in xml
        return SimpleNamespace(create=lambda: events.append("boot"))

    connection = SimpleNamespace(
        close=lambda: None,
        listAllDomains=lambda flags: [],
        defineXML=define_domain,
        networkLookupByName=lambda *a: pytest.fail("DHCP lookup before QGA bootstrap"),
    )
    if identity_outcome == "owned-runtime":
        owner = SimpleNamespace(
            name=lambda: "another-domain",
            isActive=lambda: False,
            isPersistent=lambda: True,
            XMLDesc=lambda flags: (
                f"<domain><devices><disk type='file'><source file='{job_storage}/retained.qcow2'/></disk></devices></domain>"
            ),
        )
        connection.listAllDomains = lambda flags: [owner]
    ssh_deps = JobRunDeps.default()
    ssh_deps.is_cancel_requested = lambda _: False

    def verify_ssh(*args, **kwargs):
        events.append("ssh")

        def run(command, **options):
            assert command[-1] == 'powershell.exe -NoLogo -NoProfile -NonInteractive -Command "exit 0"'
            assert "StrictHostKeyChecking=yes" in command
            assert f"UserKnownHostsFile={runtime_tmp / 'known_hosts'}" in command
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with monkeypatch.context() as patch:
            patch.setattr("subprocess.run", run)
            return ssh_deps.wait_for_guest_ssh(*args, **kwargs)

    def bootstrap(*args, **kwargs):
        assert kwargs["expected_boot_id"] == "638930000000000000"
        events.append("bootstrap")
        return SimpleNamespace(
            computer_name=expected_name,
            ssh_host_key_fingerprint="SHA256:test",
            reboot_required=True,
            boot_id="638930000000000000",
        )

    def parse_ip(*args, **kwargs):
        events.append("ip")
        return "192.168.122.50"

    class FakeDeps:
        settings = SimpleNamespace(
            storage_dir=tmp_path / "storage",
            serial_log_dir=tmp_path / "serial",
            images_dir=tmp_path / "images",
            libvirt_uri="qemu:///system",
            libvirt_network="default",
            base_dir=tmp_path,
        )
        require_noninteractive_sudo = Mock()
        is_cancel_requested = Mock(side_effect=lambda *a: identity_outcome == "cancel" and clock[0] >= 2)
        run_command = Mock()
        get_active_key_paths = Mock(return_value=(tmp_path / "id_ed25519", public_key_path))
        runtime_vm_name = Mock(return_value="astinuslab-job-windows")
        vm_lifecycle = Mock(return_value=TestLifecycle(run_command=Mock(), libvirt_module=SimpleNamespace()))
        open_libvirt_connection = Mock(return_value=connection)
        libvirt_storage_admin_helper = Mock(return_value=tmp_path / "storage-helper")
        target_guest_mac = Mock(return_value="52:54:00:aa:bb:cc")
        job_disk_image_path = Mock(return_value=job_storage / "job-disk.qcow2")
        wait_for_guest_agent = Mock(side_effect=lambda *a, **k: events.append("qga"))
        wait_for_windows_setup = Mock(side_effect=verify_setup)
        prepare_windows_guest = Mock(side_effect=prepare_guest)
        bootstrap_windows_guest = Mock(side_effect=bootstrap)
        wait_for_windows_identity = Mock(side_effect=verify_identity)
        parse_guest_ip = Mock(side_effect=parse_ip)
        record_verified_ssh_host_key = Mock(side_effect=lambda *a, **k: events.append("host-key"))
        wait_for_guest_ssh = Mock(side_effect=verify_ssh)
        session_scope = Mock()
        job_repository_cls = Mock()
        sync_runtime_record = Mock()
        write_guest_metadata_json = Mock(side_effect=lambda *a, **k: events.append("metadata"))

    fake_deps = FakeDeps()
    deps = cast(JobRunDeps, fake_deps)
    if identity_outcome == "owned-runtime":
        with pytest.raises(RuntimeError, match="Windows boot failed during runtime_resources"):
            module.boot_and_prepare_windows(
                "job-windows", image, "win2k22", "windows-server", cache_image=cache, deps=deps
            )
        assert list(job_storage.iterdir()) == []
        assert events == []
        return
    if identity_outcome == "nvram-collision":
        with pytest.raises(RuntimeError, match="Windows boot failed during nvram"):
            module.boot_and_prepare_windows(
                "job-windows", image, "win2k22", "windows-server", cache_image=cache, deps=deps
            )
        assert not (tmp_path / "must-not-be-created.vars").exists()
        assert (job_storage / "astinuslab-job-windows.vars").is_symlink()
        assert events == []
        return
    if identity_outcome == "setup-pending":
        with pytest.raises(RuntimeError, match="Windows boot failed during wait_windows_setup"):
            module.boot_and_prepare_windows(
                "job-windows",
                image,
                "win2k22",
                "windows-server",
                cache_image=cache,
                keep_failed_vm=keep_failed,
                deps=deps,
            )
        assert events == ["boot", "qga", "setup"] + ([] if keep_failed else ["destroy"])
        assert not (runtime_tmp / "windows-runtime.json").exists()
        return
    if identity_outcome == "prepared-failure":
        with pytest.raises(RuntimeError, match="Windows boot failed during prepare_windows_guest"):
            module.boot_and_prepare_windows(
                "job-windows",
                image,
                "win2k22",
                "windows-server",
                cache_image=cache,
                keep_failed_vm=keep_failed,
                deps=deps,
            )
        assert events == ["boot", "qga", "setup", "prepared"] + ([] if keep_failed else ["destroy"])
        fake_deps.bootstrap_windows_guest.assert_not_called()
        return
    if identity_outcome != "ok":
        with pytest.raises(RuntimeError, match="Windows boot failed during wait_windows_identity"):
            module.boot_and_prepare_windows(
                "job-windows",
                image,
                "win2k22",
                "windows-server",
                cache_image=cache,
                keep_failed_vm=keep_failed,
                deps=deps,
            )
        assert events == ["boot", "qga", "setup", "prepared", "bootstrap", "identity"] + (
            [] if keep_failed else ["destroy"]
        )
        assert not (runtime_tmp / "windows-runtime.json").exists()
        return
    guest_ip, metadata = module.boot_and_prepare_windows(
        "job-windows",
        image,
        "win2k22",
        "windows-server",
        cache_image=cache,
        deps=deps,
    )

    assert guest_ip == "192.168.122.50"
    assert metadata["distro_id"] == "windows-server"
    assert clock[0] == 2
    assert events == [
        "boot",
        "qga",
        "setup",
        "prepared",
        "bootstrap",
        "identity",
        "ip",
        "host-key",
        "ssh",
        "persist",
        "metadata",
    ]


@pytest.mark.parametrize("use_cache", [False, True])
@pytest.mark.parametrize("invalid", ["missing", "specialized", "outside", "symlink-outside"])
def test_windows_runner_rejects_actual_source_before_staging(monkeypatch, tmp_path, use_cache, invalid):
    images_dir = tmp_path / "images"
    storage_dir = tmp_path / "storage"
    catalog_image = write_windows_image(images_dir / "catalog.qcow2")
    selected_root = storage_dir / "workload-cache" if use_cache else images_dir
    selected = write_windows_image(selected_root / "selected.qcow2")
    if invalid == "missing":
        selected.with_suffix(".qcow2.meta.json").unlink()
    elif invalid == "specialized":
        write_windows_image(selected, generalization="specialized")
    else:
        outside = write_windows_image(tmp_path / "outside" / "outside.qcow2")
        if invalid == "outside":
            selected = outside
        else:
            selected.unlink()
            selected.symlink_to(outside)

    def unexpected_staging(**kwargs):
        pytest.fail("invalid Windows source reached runtime staging")

    monkeypatch.setattr(runner, "_stage_boot_runtime_paths_impl", unexpected_staging)
    monkeypatch.setattr(runner, "_install_backing_image_impl", unexpected_staging)
    deps = SimpleNamespace(
        settings=SimpleNamespace(images_dir=images_dir, storage_dir=storage_dir, serial_log_dir=tmp_path / "serial"),
        runtime_vm_name=lambda *args: "test-windows",
        require_noninteractive_sudo=Mock(),
        is_cancel_requested=lambda *args: False,
        get_active_key_paths=Mock(side_effect=AssertionError("preflight must precede key access")),
    )

    with pytest.raises(RuntimeError, match="Windows boot failed during preflight"):
        runner.boot_and_prepare_windows(
            "test-job",
            catalog_image if use_cache else selected,
            "win2k22",
            "windows-server",
            cache_image=selected if use_cache else None,
            deps=cast(JobRunDeps, deps),
        )

    deps.get_active_key_paths.assert_not_called()
    deps.require_noninteractive_sudo.assert_not_called()


def test_unvalidated_windows_target_cannot_fall_through_to_linux_staging():
    deps = SimpleNamespace(require_noninteractive_sudo=Mock(side_effect=AssertionError("Linux fallback reached")))
    with pytest.raises(ValueError, match="Windows target"):
        runner.boot_and_prepare(
            "job",
            Path("/unused/image.qcow2"),
            "win2k22",
            "windows-server",
            target_platform="windows",
            target_family="windows",
            target_distro="windows-server",
            target_reusable=False,
            deps=cast(JobRunDeps, deps),
        )
