from __future__ import annotations

import signal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from astinuslab.virtualization.vm_lifecycle import (
    VMBootError,
    VMBootResult,
    VMDestroyError,
    VMIPAddressCancelledError,
    VMIPAddressTimeoutError,
    VMLifecycle,
)
from astinuslab.virtualization.vm_support import teardown_vm


def _make_libvirt_module() -> Mock:
    libvirt_module = Mock()
    libvirt_module.libvirtError = type("FakeLibvirtError", (Exception,), {})
    libvirt_module.VIR_DOMAIN_UNDEFINE_NVRAM = 4
    libvirt_module.VIR_ERR_NO_DOMAIN = 42
    return libvirt_module


def _teardown_settings(tmp_path: Path, **overrides) -> SimpleNamespace:
    settings = SimpleNamespace(
        base_dir=tmp_path,
        data_dir=tmp_path,
        storage_dir=tmp_path / "storage",
        libvirt_uri="qemu:///system",
        libvirt_image_base_dir=Path("/var/lib/libvirt/images/astinuslab"),
        serial_log_dir=tmp_path / "storage" / "logs" / "serial",
    )
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def _patch_teardown_libvirt(monkeypatch) -> None:
    conn = Mock()
    domain = Mock()
    domain.isActive.return_value = False
    domain.name.return_value = "astinuslab-job-123"
    domain.XMLDesc.return_value = "<domain/>"
    domain.isPersistent.return_value = True
    domains = [domain]
    conn.listAllDomains.side_effect = lambda _flags: list(domains)
    domain.undefineFlags.side_effect = lambda _flags: domains.clear()
    conn.lookupByName.return_value = domain
    libvirt_module = SimpleNamespace(
        open=Mock(return_value=conn),
        libvirtError=Exception,
        VIR_DOMAIN_UNDEFINE_NVRAM=4,
    )
    monkeypatch.setattr("astinuslab.virtualization.vm_support.import_module", Mock(return_value=libvirt_module))


def test_teardown_vm_removes_job_tmp_dir_after_success(tmp_path: Path, monkeypatch) -> None:
    _patch_teardown_libvirt(monkeypatch)
    job_id = "job-123"
    tmp_root = tmp_path / "storage" / "tmp" / job_id
    tmp_root.mkdir(parents=True)
    (tmp_root / "artifact.txt").write_text("temporary data")
    run_command = Mock(side_effect=["", "", ""])

    teardown_vm(
        run_command,
        Mock(),
        _teardown_settings(tmp_path),
        Mock(),
        job_id,
    )

    assert not tmp_root.exists()


def test_teardown_vm_terminates_virtiofsd_before_removing_tmp_dir(tmp_path: Path, monkeypatch) -> None:
    _patch_teardown_libvirt(monkeypatch)
    job_id = "job-123"
    tmp_root = tmp_path / "storage" / "tmp" / job_id
    tmp_root.mkdir(parents=True)
    (tmp_root / "virtiofsd.pid").write_text("4321")
    kill = Mock()
    monkeypatch.setattr("os.kill", kill)
    run_command = Mock(side_effect=["", "", ""])

    teardown_vm(
        run_command,
        Mock(),
        _teardown_settings(tmp_path),
        Mock(),
        job_id,
    )

    kill.assert_called_once_with(4321, signal.SIGTERM)
    assert not tmp_root.exists()


def test_teardown_vm_preserves_job_tmp_dir_when_keep_failed_vm_enabled(tmp_path: Path, monkeypatch) -> None:
    _patch_teardown_libvirt(monkeypatch)
    job_id = "job-123"
    tmp_root = tmp_path / "storage" / "tmp" / job_id
    tmp_root.mkdir(parents=True)
    (tmp_root / "artifact.txt").write_text("temporary data")
    run_command = Mock(side_effect=["", "", ""])

    teardown_vm(
        run_command,
        Mock(),
        _teardown_settings(tmp_path, keep_failed_vm=True),
        Mock(),
        job_id,
    )

    assert tmp_root.exists()


def test_teardown_vm_preserves_job_tmp_dir_when_teardown_fails(tmp_path: Path, monkeypatch) -> None:
    _patch_teardown_libvirt(monkeypatch)
    job_id = "job-123"
    tmp_root = tmp_path / "storage" / "tmp" / job_id
    tmp_root.mkdir(parents=True)
    (tmp_root / "artifact.txt").write_text("temporary data")

    def _run_command(cmd):
        if cmd[:3] == ["sudo", "-n", "/usr/local/libexec/astinuslab/libvirt_storage_admin.sh"]:
            raise RuntimeError("storage cleanup failed")
        return ""

    teardown_vm(
        _run_command,
        Mock(),
        _teardown_settings(tmp_path),
        Mock(),
        job_id,
    )

    assert tmp_root.exists()


def test_teardown_vm_marks_runtime_destroyed_after_success(tmp_path: Path, monkeypatch) -> None:
    _patch_teardown_libvirt(monkeypatch)
    mark_destroyed = Mock()
    mark_failed = Mock()

    teardown_vm(
        Mock(side_effect=["", "", ""]),
        Mock(),
        _teardown_settings(tmp_path),
        mark_destroyed,
        "job-123",
        mark_failed,
    )

    mark_destroyed.assert_called_once_with("job-123", "target")
    mark_failed.assert_not_called()


def test_teardown_vm_marks_runtime_teardown_failed_when_teardown_fails(tmp_path: Path, monkeypatch) -> None:
    _patch_teardown_libvirt(monkeypatch)
    mark_destroyed = Mock()
    mark_failed = Mock()

    def _run_command(cmd):
        if cmd[:3] == ["sudo", "-n", "/usr/local/libexec/astinuslab/libvirt_storage_admin.sh"]:
            raise RuntimeError("storage cleanup failed")
        return ""

    teardown_vm(
        _run_command,
        Mock(),
        _teardown_settings(tmp_path),
        mark_destroyed,
        "job-123-attacker",
        mark_failed,
    )

    mark_destroyed.assert_not_called()
    mark_failed.assert_called_once_with("job-123", "attacker")


def test_render_domain_xml_includes_vm_name_memory_vcpu_network_and_virtiofs() -> None:
    lifecycle = VMLifecycle(run_command=Mock(), libvirt_module=_make_libvirt_module())

    xml = lifecycle._render_domain_xml(
        vm_name="astinuslab-job-123",
        memory_mb=2048,
        vcpus=2,
        disk_path=Path("/var/lib/libvirt/images/astinuslab/job-123/disk.qcow2"),
        seed_iso_path=Path("/var/lib/libvirt/images/astinuslab/job-123/seed.iso"),
        virtiofs_socket=Path("/tmp/astinuslab-job-123.sock"),
        share_dir=Path("/srv/astinuslab/storage/runs/job-123"),
        network_name="astinuslab-net",
        guest_mac="52:54:00:12:34:56",
    )

    assert "<name>astinuslab-job-123</name>" in xml
    assert "<memory unit='MiB'>2048</memory>" in xml
    assert "<vcpu>2</vcpu>" in xml
    assert "<memoryBacking>" in xml
    assert "<source type='memfd'/>" in xml
    assert "<access mode='shared'/>" in xml
    assert "filesystem type='mount' accessmode='passthrough'" in xml
    assert "source socket=" not in xml
    assert "<target dir='astinuslab_out'/>" in xml
    assert "filesystem type='mount' accessmode='mapped'" in xml
    assert "source dir='/srv/astinuslab/storage/runs/job-123'" in xml
    assert "<target dir='astinuslab_out_9p'/>" in xml
    assert "<source network='astinuslab-net'/>" in xml
    assert "<mac address='52:54:00:12:34:56'/>" in xml
    assert "<target dev='vdb' bus='virtio'/>" in xml
    assert "device='cdrom'" not in xml
    assert "<readonly/>" in xml


def test_render_domain_xml_adds_uefi_only_for_windows_firmware(tmp_path: Path) -> None:
    from astinuslab.virtualization.uefi import WindowsFirmwareConfig

    nvram = tmp_path / "runtime.vars"
    nvram.write_bytes(b"vars")
    lifecycle = VMLifecycle(run_command=Mock(), libvirt_module=_make_libvirt_module())

    xml = lifecycle._render_domain_xml(
        vm_name="astinuslab-win-job-123",
        memory_mb=8192,
        vcpus=4,
        disk_path=Path("/images/overlay.qcow2"),
        seed_iso_path=Path("/images/seed.iso"),
        virtiofs_socket=Path("/tmp/job.sock"),
        share_dir=Path("/srv/astinuslab/job-123"),
        network_name="default",
        guest_mac="52:54:00:12:34:56",
        firmware=WindowsFirmwareConfig(),
        nvram_path=nvram,
    )

    assert "<loader readonly='yes' type='pflash'>/usr/share/OVMF/OVMF_CODE_4M.fd</loader>" in xml
    assert f"<nvram template='/usr/share/OVMF/OVMF_VARS_4M.fd'>{nvram}</nvram>" in xml
    assert "<tpm" not in xml
    assert "<on_reboot>restart</on_reboot>" in xml
    assert "<graphics type='vnc' listen='127.0.0.1' autoport='yes'/>" in xml


@pytest.mark.parametrize("mixed", [False, True])
def test_windows_prepared_media_attaches_verified_copy_without_seed_generation(tmp_path, mixed):
    import xml.etree.ElementTree as ET

    from astinuslab.images.windows_bundle import stage_windows_media
    from astinuslab.virtualization.uefi import WindowsFirmwareConfig
    from tests.python._support.windows_images import create_windows_bundle_fixture

    source, job, shared, bundle = create_windows_bundle_fixture(tmp_path)
    media = stage_windows_media(bundle, source_dir=source, runtime_dir=job)
    nvram = job / "runtime.vars"
    nvram.write_bytes(b"vars")
    conn = Mock()
    conn.listAllDomains.return_value = []
    conn.defineXML.return_value = Mock()
    commands = []
    lifecycle = VMLifecycle(
        run_command=lambda args, **kwargs: commands.append(args), libvirt_module=_make_libvirt_module()
    )

    def boot():
        return lifecycle.boot(
            conn,
            vm_name="windows-job",
            memory_mb=8192,
            vcpus=4,
            backing_image=job / "backing.qcow2",
            disk_path=job / "overlay.qcow2",
            seed_iso_path=job / "seed.iso" if mixed else None,
            user_data_path=None,
            meta_data_path=None,
            virtiofs_socket=job / "sock",
            share_dir=job / "share",
            network_name="default",
            guest_mac="52:54:00:12:34:56",
            firmware=WindowsFirmwareConfig(),
            nvram_path=nvram,
            prepared_media=media,
            preserve_on_failure=True,
        )

    if mixed:
        with pytest.raises(VMBootError, match="media"):
            boot()
        conn.defineXML.assert_not_called()
        assert not commands
    else:
        boot()
        root = ET.fromstring(conn.defineXML.call_args.args[0])
        cds = root.findall("./devices/disk[@device='cdrom']")
        assert len(cds) == 1
        cd_source = cds[0].find("source")
        assert cd_source is not None
        assert cd_source.get("file") == str(media.path)
        assert cds[0].find("readonly") is not None
        assert all(args[0] != "cloud-localds" for args in commands)
    assert shared.read_bytes() == b"synthetic public media"


@pytest.mark.parametrize("injected_resolver", [False, True])
def test_windows_boot_returns_before_ip_discovery_without_linux_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, injected_resolver: bool
) -> None:
    run_command = Mock()
    libvirt_module = _make_libvirt_module()
    conn = Mock()
    conn.listAllDomains.return_value = []
    conn.networkLookupByName.side_effect = AssertionError("DHCP must follow QGA bootstrap")
    domain = Mock()
    conn.defineXML.return_value = domain
    lifecycle = VMLifecycle(run_command=run_command, libvirt_module=libvirt_module)
    monkeypatch.setattr(lifecycle, "_destroy_if_present", Mock())
    nvram = tmp_path / "runtime.vars"
    nvram.write_bytes(b"vars")

    result = lifecycle.boot(
        conn,
        vm_name="astinuslab-win-job-123",
        memory_mb=8192,
        vcpus=4,
        backing_image=Path("/images/base.qcow2"),
        disk_path=tmp_path / "overlay.qcow2",
        seed_iso_path=None,
        user_data_path=None,
        meta_data_path=None,
        virtiofs_socket=Path("/tmp/job.sock"),
        share_dir=Path("/srv/astinuslab/job-123"),
        network_name="default",
        guest_mac="52:54:00:12:34:56",
        firmware=__import__(
            "astinuslab.virtualization.uefi", fromlist=["WindowsFirmwareConfig"]
        ).WindowsFirmwareConfig(),
        nvram_path=nvram,
        ip_resolver=(lambda: pytest.fail("resolver must follow QGA bootstrap")) if injected_resolver else None,
        preserve_on_failure=True,
    )

    assert result.guest_ip == ""
    assert result.seed_iso_path == Path()
    assert all(call.args[0][0] != "cloud-localds" for call in run_command.call_args_list)


def test_render_domain_xml_includes_file_backed_serial_console_path() -> None:
    lifecycle = VMLifecycle(run_command=Mock(), libvirt_module=_make_libvirt_module())

    xml = lifecycle._render_domain_xml(
        vm_name="astinuslab-job-123",
        memory_mb=2048,
        vcpus=2,
        disk_path=Path("/var/lib/libvirt/images/astinuslab/job-123/disk.qcow2"),
        seed_iso_path=Path("/var/lib/libvirt/images/astinuslab/job-123/seed.iso"),
        virtiofs_socket=Path("/tmp/astinuslab-job-123.sock"),
        share_dir=Path("/srv/astinuslab/storage/runs/job-123"),
        network_name="astinuslab-net",
        guest_mac="52:54:00:12:34:56",
        serial_log_path=Path("/tmp/astinuslab-job-123-serial.log"),
    )

    assert "<console type='file'>" in xml
    assert "<source path='/tmp/astinuslab-job-123-serial.log'/>" in xml
    assert "<target type='serial' port='0'/>" in xml


def test_render_domain_xml_escapes_apostrophes_in_attribute_values() -> None:
    lifecycle = VMLifecycle(run_command=Mock(), libvirt_module=_make_libvirt_module())

    xml = lifecycle._render_domain_xml(
        vm_name="astinuslab-job-123",
        memory_mb=2048,
        vcpus=2,
        disk_path=Path("/var/lib/libvirt/images/astinuslab/job's/disk.qcow2"),
        seed_iso_path=Path("/var/lib/libvirt/images/astinuslab/job's/seed.iso"),
        virtiofs_socket=Path("/tmp/astinuslab-job's.sock"),
        share_dir=Path("/srv/astinuslab/job's-share"),
        network_name="astinuslab-net",
        guest_mac="52:54:00:12:34:56",
    )

    assert "job&apos;s" in xml
    assert "<source file='/var/lib/libvirt/images/astinuslab/job&apos;s/disk.qcow2'/>" in xml
    # The virtiofs device is libvirt-managed, so the escaped value that matters is
    # the shared directory it hands to the daemon it owns.
    assert "<source dir='/srv/astinuslab/job&apos;s-share'/>" in xml
    assert "source socket=" not in xml


def test_render_domain_xml_lets_libvirt_manage_the_virtiofs_daemon() -> None:
    """The virtiofs device must use a managed <source dir>, not an external socket.

    A runner-spawned virtiofsd dies inside the service sandbox (Error entering
    sandbox: MountProc EPERM), and the only fix that does not weaken the sandbox is
    to let libvirt start and own the daemon from the domain definition.
    """
    lifecycle = VMLifecycle(run_command=Mock(), libvirt_module=_make_libvirt_module())

    xml = lifecycle._render_domain_xml(
        vm_name="astinuslab-job-123",
        memory_mb=2048,
        vcpus=2,
        disk_path=Path("/var/lib/libvirt/images/astinuslab/job-123/disk.qcow2"),
        seed_iso_path=Path("/var/lib/libvirt/images/astinuslab/job-123/seed.iso"),
        virtiofs_socket=Path("/tmp/astinuslab-job-123.sock"),
        share_dir=Path("/srv/astinuslab/storage/tmp/job-123/virtiofs-staging"),
        network_name="astinuslab-net",
        guest_mac="52:54:00:12:34:56",
    )

    assert (
        """    <filesystem type='mount' accessmode='passthrough'>
      <driver type='virtiofs'/>
      <source dir='/srv/astinuslab/storage/tmp/job-123/virtiofs-staging'/>
      <target dir='astinuslab_out'/>
    </filesystem>"""
        in xml
    )
    assert "source socket=" not in xml
    assert "/tmp/astinuslab-job-123.sock" not in xml
    # The 9p companion share stays alongside it as the in-guest mount fallback.
    assert "<target dir='astinuslab_out_9p'/>" in xml


def test_render_domain_xml_9p_mode_still_emits_only_the_9p_share() -> None:
    lifecycle = VMLifecycle(run_command=Mock(), libvirt_module=_make_libvirt_module())

    xml = lifecycle._render_domain_xml(
        vm_name="astinuslab-job-123",
        memory_mb=2048,
        vcpus=2,
        disk_path=Path("/var/lib/libvirt/images/astinuslab/job-123/disk.qcow2"),
        seed_iso_path=Path("/var/lib/libvirt/images/astinuslab/job-123/seed.iso"),
        virtiofs_socket=Path("/tmp/astinuslab-job-123.sock"),
        share_dir=Path("/srv/astinuslab/storage/tmp/job-123/virtiofs-staging"),
        network_name="astinuslab-net",
        guest_mac="52:54:00:12:34:56",
        filesystem_type="9p",
    )

    assert (
        """    <filesystem type='mount' accessmode='mapped'>
      <driver type='path' wrpolicy='immediate'/>
      <source dir='/srv/astinuslab/storage/tmp/job-123/virtiofs-staging'/>
      <target dir='astinuslab_out_9p'/>
    </filesystem>"""
        in xml
    )
    assert "<driver type='virtiofs'/>" not in xml
    assert "source socket=" not in xml


def test_render_playground_domain_xml_includes_optional_seed_iso() -> None:
    lifecycle = VMLifecycle(run_command=Mock(), libvirt_module=_make_libvirt_module())

    xml = lifecycle._render_playground_domain_xml(
        domain_name="astinuslab-pg-pg-1-web-1",
        memory_mb=2048,
        vcpus=2,
        disk_path=Path("/var/lib/astinuslab/playground/web-1.qcow2"),
        seed_iso_path=Path("/var/lib/astinuslab/playground/web-1-seed.iso"),
        networks=[{"libvirt_net": "astinuslab-pg-pg-1-ab-cdf2", "mac": "52:54:00:aa:bb:cc"}],
    )

    assert "device='cdrom'" not in xml
    assert "<source file='/var/lib/astinuslab/playground/web-1-seed.iso'/>" in xml
    assert "<target dev='vdb' bus='virtio'/>" in xml
    assert "<readonly/>" in xml


def test_render_domain_xml_enables_acpi_and_apic() -> None:
    lifecycle = VMLifecycle(run_command=Mock(), libvirt_module=_make_libvirt_module())

    xml = lifecycle._render_domain_xml(
        vm_name="astinuslab-job-123",
        memory_mb=2048,
        vcpus=2,
        disk_path=Path("/var/lib/libvirt/images/astinuslab/job-123/disk.qcow2"),
        seed_iso_path=Path("/var/lib/libvirt/images/astinuslab/job-123/seed.iso"),
        virtiofs_socket=Path("/tmp/astinuslab-job-123.sock"),
        share_dir=Path("/srv/astinuslab/storage/runs/job-123"),
        network_name="astinuslab-net",
        guest_mac="52:54:00:12:34:56",
    )

    assert "<features>" in xml
    assert "<acpi/>" in xml
    assert "<apic/>" in xml


def test_render_playground_domain_xml_enables_acpi_and_apic() -> None:
    lifecycle = VMLifecycle(run_command=Mock(), libvirt_module=_make_libvirt_module())

    xml = lifecycle._render_playground_domain_xml(
        domain_name="astinuslab-pg-pg-1-web-1",
        memory_mb=2048,
        vcpus=2,
        disk_path=Path("/var/lib/astinuslab/playground/web-1.qcow2"),
        networks=[{"libvirt_net": "astinuslab-pg-pg-1-ab-cdf2", "mac": "52:54:00:aa:bb:cc"}],
    )

    assert "<features>" in xml
    assert "<acpi/>" in xml
    assert "<apic/>" in xml


def test_create_overlay_invokes_qemu_img() -> None:
    run_command = Mock()
    lifecycle = VMLifecycle(run_command=run_command, libvirt_module=_make_libvirt_module())

    overlay_path = lifecycle.create_overlay(
        backing_image=Path("/images/base.qcow2"),
        overlay_path=Path("/images/overlay.qcow2"),
        cwd=Path("/work"),
    )

    assert overlay_path == Path("/images/overlay.qcow2")
    run_command.assert_called_once_with(
        [
            "qemu-img",
            "create",
            "-f",
            "qcow2",
            "-F",
            "qcow2",
            "-b",
            "/images/base.qcow2",
            "/images/overlay.qcow2",
        ],
        cwd=Path("/work"),
    )


def test_create_overlay_defers_access_until_all_boot_artifacts_exist() -> None:
    events: list[str] = []
    run_command = Mock(side_effect=lambda *_args, **_kwargs: events.append("qemu-img"))
    prepare_overlay_access = Mock(side_effect=lambda _path: events.append("acl"))
    lifecycle = VMLifecycle(
        run_command=run_command,
        libvirt_module=_make_libvirt_module(),
        prepare_overlay_access=prepare_overlay_access,
    )
    overlay_path = Path("/storage/playground/disks/job-1/target.qcow2")

    lifecycle.create_overlay(
        backing_image=Path("/images/base.qcow2"),
        overlay_path=overlay_path,
        cwd=Path("/work"),
    )

    prepare_overlay_access.assert_not_called()
    assert events == ["qemu-img"]


def test_boot_playground_applies_acl_after_overlay_and_seed_before_libvirt() -> None:
    events: list[str] = []

    def run_command(command, **_kwargs):
        events.append(command[0])

    prepare_overlay_access = Mock(side_effect=lambda _path: events.append("acl"))
    domain = Mock()
    domain.create.side_effect = lambda: events.append("create")
    conn = Mock()

    def define_xml(_xml):
        events.append("define")
        return domain

    conn.defineXML.side_effect = define_xml
    libvirt_module = _make_libvirt_module()
    libvirt_module.open.return_value = conn
    lifecycle = VMLifecycle(
        run_command=run_command,
        libvirt_module=libvirt_module,
        prepare_overlay_access=prepare_overlay_access,
    )
    lifecycle._destroy_if_present = Mock()  # type: ignore[method-assign]
    overlay_path = Path("/storage/playground/disks/job-1/target.qcow2")

    lifecycle.boot_playground(
        domain_name="astinuslab-pg-job-1-vm-1",
        memory_mb=1024,
        vcpus=1,
        base_image_path=Path("/images/base.qcow2"),
        disk_path=overlay_path,
        disk_gb=None,
        networks=[{"libvirt_net": "astinuslab-pg-job-1-net", "mac": "52:54:00:aa:bb:cc"}],
        libvirt_uri="qemu:///system",
        cwd=Path("/work"),
        seed_iso_path=Path("/storage/playground/disks/job-1/seed.iso"),
        user_data_path=Path("/work/user-data"),
        meta_data_path=Path("/work/meta-data"),
    )

    prepare_overlay_access.assert_called_once_with(overlay_path)
    assert events == ["qemu-img", "cloud-localds", "acl", "define", "create"]


def test_create_overlay_bumps_size_to_backing_when_requested_smaller() -> None:
    backing_bytes = 80 * (1024**3) + 100  # 80 GiB + epsilon -> rounds up to 81 GiB
    qemu_info = SimpleNamespace(stdout=f'{{"virtual-size": {backing_bytes}}}')
    run_command = Mock(side_effect=[qemu_info, None])
    lifecycle = VMLifecycle(run_command=run_command, libvirt_module=_make_libvirt_module())

    lifecycle.create_overlay(
        backing_image=Path("/images/base.qcow2"),
        overlay_path=Path("/images/overlay.qcow2"),
        disk_size_gb=20,
        cwd=Path("/work"),
    )

    create_call = run_command.call_args_list[-1]
    assert create_call.args[0][-1] == "81G"


def test_create_overlay_keeps_requested_size_when_larger_than_backing() -> None:
    backing_bytes = 8 * (1024**3)
    qemu_info = SimpleNamespace(stdout=f'{{"virtual-size": {backing_bytes}}}')
    run_command = Mock(side_effect=[qemu_info, None])
    lifecycle = VMLifecycle(run_command=run_command, libvirt_module=_make_libvirt_module())

    lifecycle.create_overlay(
        backing_image=Path("/images/base.qcow2"),
        overlay_path=Path("/images/overlay.qcow2"),
        disk_size_gb=40,
        cwd=Path("/work"),
    )

    create_call = run_command.call_args_list[-1]
    assert create_call.args[0][-1] == "40G"


def test_create_seed_iso_invokes_cloud_localds() -> None:
    run_command = Mock()
    lifecycle = VMLifecycle(run_command=run_command, libvirt_module=_make_libvirt_module())

    seed_iso_path = lifecycle.create_seed_iso(
        seed_iso_path=Path("/images/seed.iso"),
        user_data_path=Path("/tmp/user-data"),
        meta_data_path=Path("/tmp/meta-data"),
        cwd=Path("/work"),
    )

    assert seed_iso_path == Path("/images/seed.iso")
    run_command.assert_called_once_with(
        [
            "cloud-localds",
            "/images/seed.iso",
            "/tmp/user-data",
            "/tmp/meta-data",
        ],
        cwd=Path("/work"),
    )


def test_boot_creates_artifacts_starts_domain_and_returns_boot_result(monkeypatch: pytest.MonkeyPatch) -> None:
    run_command = Mock()
    libvirt_module = _make_libvirt_module()
    fake_time = Mock()
    fake_time.monotonic.side_effect = [0.0, 1.0]
    conn = Mock()
    domain = Mock()
    conn.defineXML.return_value = domain
    network = Mock()
    network.DHCPLeases.return_value = [
        {"mac": "52:54:00:aa:bb:cc", "ipaddr": "192.168.122.77"},
    ]
    conn.networkLookupByName.return_value = network
    lifecycle = VMLifecycle(run_command=run_command, libvirt_module=libvirt_module, time_module=fake_time)
    destroy_mock = Mock()
    monkeypatch.setattr(lifecycle, "_destroy_if_present", destroy_mock)

    result = lifecycle.boot(
        conn,
        vm_name="astinuslab-job-123",
        memory_mb=2048,
        vcpus=2,
        backing_image=Path("/images/base.qcow2"),
        disk_path=Path("/images/overlay.qcow2"),
        seed_iso_path=Path("/images/seed.iso"),
        user_data_path=Path("/tmp/user-data"),
        meta_data_path=Path("/tmp/meta-data"),
        virtiofs_socket=Path("/tmp/astinuslab-job-123.sock"),
        share_dir=Path("/srv/astinuslab/job-123"),
        network_name="astinuslab-net",
        guest_mac="52:54:00:aa:bb:cc",
        cwd=Path("/work"),
    )

    assert result == VMBootResult(
        vm_name="astinuslab-job-123",
        guest_ip="192.168.122.77",
        disk_path=Path("/images/overlay.qcow2"),
        seed_iso_path=Path("/images/seed.iso"),
        share_dir=Path("/srv/astinuslab/job-123"),
    )
    destroy_mock.assert_called_once_with(conn, "astinuslab-job-123")
    assert run_command.call_args_list == [
        call(
            [
                "qemu-img",
                "create",
                "-f",
                "qcow2",
                "-F",
                "qcow2",
                "-b",
                "/images/base.qcow2",
                "/images/overlay.qcow2",
            ],
            cwd=Path("/work"),
        ),
        call(
            [
                "cloud-localds",
                "/images/seed.iso",
                "/tmp/user-data",
                "/tmp/meta-data",
            ],
            cwd=Path("/work"),
        ),
    ]
    conn.defineXML.assert_called_once()
    assert "astinuslab-net" in conn.defineXML.call_args.args[0]
    assert "52:54:00:aa:bb:cc" in conn.defineXML.call_args.args[0]
    domain.create.assert_called_once_with()
    conn.networkLookupByName.assert_called_once_with("astinuslab-net")
    network.DHCPLeases.assert_called_once_with()


def test_boot_uses_injected_ip_resolver_instead_of_direct_dhcp_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    run_command = Mock()
    libvirt_module = _make_libvirt_module()
    conn = Mock()
    domain = Mock()
    conn.defineXML.return_value = domain
    ip_resolver = Mock(return_value="192.168.122.88")
    lifecycle = VMLifecycle(run_command=run_command, libvirt_module=libvirt_module)
    monkeypatch.setattr(lifecycle, "_destroy_if_present", Mock())

    result = lifecycle.boot(
        conn,
        vm_name="astinuslab-job-123",
        memory_mb=2048,
        vcpus=2,
        backing_image=Path("/images/base.qcow2"),
        disk_path=Path("/images/overlay.qcow2"),
        seed_iso_path=Path("/images/seed.iso"),
        user_data_path=Path("/tmp/user-data"),
        meta_data_path=Path("/tmp/meta-data"),
        virtiofs_socket=Path("/tmp/astinuslab-job-123.sock"),
        share_dir=Path("/srv/astinuslab/job-123"),
        network_name="astinuslab-net",
        guest_mac="52:54:00:aa:bb:cc",
        ip_resolver=ip_resolver,
    )

    assert result.guest_ip == "192.168.122.88"
    domain.create.assert_called_once_with()
    ip_resolver.assert_called_once_with()
    conn.networkLookupByName.assert_not_called()


def test_boot_cleans_up_defined_domain_when_ip_resolution_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    run_command = Mock()
    libvirt_module = _make_libvirt_module()
    fake_time = Mock()
    fake_time.monotonic.side_effect = [0.0, 5.0]
    conn = Mock()
    domain = Mock()
    conn.defineXML.return_value = domain
    network = Mock()
    network.DHCPLeases.return_value = []
    conn.networkLookupByName.return_value = network
    lifecycle = VMLifecycle(
        run_command=run_command, libvirt_module=libvirt_module, time_module=fake_time, sleep_fn=Mock()
    )
    monkeypatch.setattr(lifecycle, "_destroy_if_present", Mock())
    disk_path = Mock(spec=Path)
    seed_iso_path = Mock(spec=Path)

    with pytest.raises(VMBootError, match="Timed out waiting for IPv4 DHCP lease"):
        lifecycle.boot(
            conn,
            vm_name="astinuslab-job-123",
            memory_mb=2048,
            vcpus=2,
            backing_image=Path("/images/base.qcow2"),
            disk_path=disk_path,
            seed_iso_path=seed_iso_path,
            user_data_path=Path("/tmp/user-data"),
            meta_data_path=Path("/tmp/meta-data"),
            virtiofs_socket=Path("/tmp/astinuslab-job-123.sock"),
            share_dir=Path("/srv/astinuslab/job-123"),
            network_name="astinuslab-net",
            guest_mac="52:54:00:aa:bb:cc",
            cwd=Path("/work"),
            ip_timeout_seconds=3.0,
        )

    domain.create.assert_called_once_with()
    domain.destroy.assert_called_once_with()
    domain.undefineFlags.assert_called_once_with(libvirt_module.VIR_DOMAIN_UNDEFINE_NVRAM)
    disk_path.unlink.assert_called_once_with(missing_ok=True)
    seed_iso_path.unlink.assert_called_once_with(missing_ok=True)


def test_boot_preserves_domain_and_artifacts_when_ip_resolution_fails_and_preserve_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_command = Mock()
    libvirt_module = _make_libvirt_module()
    conn = Mock()
    domain = Mock()
    conn.defineXML.return_value = domain
    lifecycle = VMLifecycle(run_command=run_command, libvirt_module=libvirt_module)
    monkeypatch.setattr(lifecycle, "_destroy_if_present", Mock())
    disk_path = Mock(spec=Path)
    seed_iso_path = Mock(spec=Path)

    with pytest.raises(VMBootError, match="resolver failed"):
        lifecycle.boot(
            conn,
            vm_name="astinuslab-job-123",
            memory_mb=2048,
            vcpus=2,
            backing_image=Path("/images/base.qcow2"),
            disk_path=disk_path,
            seed_iso_path=seed_iso_path,
            user_data_path=Path("/tmp/user-data"),
            meta_data_path=Path("/tmp/meta-data"),
            virtiofs_socket=Path("/tmp/astinuslab-job-123.sock"),
            share_dir=Path("/srv/astinuslab/job-123"),
            network_name="astinuslab-net",
            guest_mac="52:54:00:aa:bb:cc",
            ip_resolver=Mock(side_effect=RuntimeError("resolver failed")),
            preserve_on_failure=True,
        )

    domain.create.assert_called_once_with()
    domain.destroy.assert_not_called()
    domain.undefineFlags.assert_not_called()
    disk_path.unlink.assert_not_called()
    seed_iso_path.unlink.assert_not_called()


def test_boot_cleans_up_defined_domain_when_create_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    run_command = Mock()
    libvirt_module = _make_libvirt_module()
    conn = Mock()
    domain = Mock()
    domain.create.side_effect = libvirt_module.libvirtError("create failed")
    conn.defineXML.return_value = domain
    lifecycle = VMLifecycle(run_command=run_command, libvirt_module=libvirt_module, sleep_fn=Mock())
    monkeypatch.setattr(lifecycle, "_destroy_if_present", Mock())
    disk_path = Mock(spec=Path)
    seed_iso_path = Mock(spec=Path)

    with pytest.raises(VMBootError, match="create failed"):
        lifecycle.boot(
            conn,
            vm_name="astinuslab-job-123",
            memory_mb=2048,
            vcpus=2,
            backing_image=Path("/images/base.qcow2"),
            disk_path=disk_path,
            seed_iso_path=seed_iso_path,
            user_data_path=Path("/tmp/user-data"),
            meta_data_path=Path("/tmp/meta-data"),
            virtiofs_socket=Path("/tmp/astinuslab-job-123.sock"),
            share_dir=Path("/srv/astinuslab/job-123"),
            network_name="astinuslab-net",
            guest_mac="52:54:00:aa:bb:cc",
            cwd=Path("/work"),
        )

    domain.destroy.assert_called_once_with()
    domain.undefineFlags.assert_called_once_with(libvirt_module.VIR_DOMAIN_UNDEFINE_NVRAM)
    disk_path.unlink.assert_called_once_with(missing_ok=True)
    seed_iso_path.unlink.assert_called_once_with(missing_ok=True)


@pytest.mark.parametrize("preserve_on_failure", [False, True])
def test_boot_wraps_acl_failure_and_honors_artifact_preservation(
    monkeypatch: pytest.MonkeyPatch,
    preserve_on_failure: bool,
) -> None:
    libvirt_module = _make_libvirt_module()
    lifecycle = VMLifecycle(
        run_command=Mock(),
        libvirt_module=libvirt_module,
        prepare_overlay_access=Mock(side_effect=RuntimeError("ACL preparation failed")),
    )
    monkeypatch.setattr(lifecycle, "_destroy_if_present", Mock())
    disk_path = Mock(spec=Path)
    seed_iso_path = Mock(spec=Path)

    with pytest.raises(VMBootError, match="ACL preparation failed"):
        lifecycle.boot(
            Mock(),
            vm_name="astinuslab-job-123",
            memory_mb=2048,
            vcpus=2,
            backing_image=Path("/images/base.qcow2"),
            disk_path=disk_path,
            seed_iso_path=seed_iso_path,
            user_data_path=Path("/tmp/user-data"),
            meta_data_path=Path("/tmp/meta-data"),
            virtiofs_socket=Path("/tmp/astinuslab-job-123.sock"),
            share_dir=Path("/srv/astinuslab/job-123"),
            network_name="astinuslab-net",
            guest_mac="52:54:00:aa:bb:cc",
            preserve_on_failure=preserve_on_failure,
        )

    if preserve_on_failure:
        disk_path.unlink.assert_not_called()
        seed_iso_path.unlink.assert_not_called()
    else:
        disk_path.unlink.assert_called_once_with(missing_ok=True)
        seed_iso_path.unlink.assert_called_once_with(missing_ok=True)


def test_get_ip_returns_ipv4_from_dhcp_leases_by_mac() -> None:
    fake_time = Mock()
    fake_time.monotonic.side_effect = [0.0, 1.0]
    lifecycle = VMLifecycle(
        run_command=Mock(),
        libvirt_module=_make_libvirt_module(),
        time_module=fake_time,
    )
    network = Mock()
    network.DHCPLeases.return_value = [
        {"mac": "52:54:00:00:00:01", "ipaddr": "fe80::1234"},
        {"mac": "52:54:00:aa:bb:cc", "ipaddr": "192.168.122.55"},
        {"mac": "52:54:00:00:00:02", "ipaddr": "192.168.122.99"},
    ]
    conn = Mock()
    conn.networkLookupByName.return_value = network

    ip_address = lifecycle.get_ip(conn, "astinuslab-net", "52:54:00:aa:bb:cc")

    assert ip_address == "192.168.122.55"
    conn.networkLookupByName.assert_called_once_with("astinuslab-net")
    network.DHCPLeases.assert_called_once_with()


def test_get_ip_polls_until_ipv4_appears() -> None:
    fake_time = Mock()
    fake_time.monotonic.side_effect = [0.0, 1.0, 2.0]
    fake_sleep = Mock()
    lifecycle = VMLifecycle(
        run_command=Mock(),
        libvirt_module=_make_libvirt_module(),
        time_module=fake_time,
        sleep_fn=fake_sleep,
    )
    network = Mock()
    network.DHCPLeases.side_effect = [
        [{"mac": "52:54:00:aa:bb:cc", "ipaddr": "fe80::1234"}],
        [{"mac": "52:54:00:aa:bb:cc", "ipaddr": "192.168.122.55"}],
    ]
    conn = Mock()
    conn.networkLookupByName.return_value = network

    ip_address = lifecycle.get_ip(conn, "astinuslab-net", "52:54:00:aa:bb:cc", timeout_seconds=5.0)

    assert ip_address == "192.168.122.55"
    assert network.DHCPLeases.call_count == 2
    fake_sleep.assert_called_once_with(1.0)


@pytest.mark.parametrize("leases", [[], None])
def test_get_ip_returns_static_ipv4_from_guest_agent_when_dhcp_has_no_lease(leases) -> None:
    fake_time = Mock()
    fake_time.monotonic.return_value = 0.0
    libvirt_module = _make_libvirt_module()
    libvirt_module.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT = 1
    libvirt_module.VIR_IP_ADDR_TYPE_IPV4 = 0
    lifecycle = VMLifecycle(
        run_command=Mock(),
        libvirt_module=libvirt_module,
        time_module=fake_time,
    )
    network = Mock()
    network.DHCPLeases.return_value = leases
    domain = Mock()
    domain.interfaceAddresses.return_value = {
        "lo": {
            "hwaddr": "00:00:00:00:00:00",
            "addrs": [{"type": 0, "addr": "127.0.0.1", "prefix": 8}],
        },
        "enp1s0": {
            "hwaddr": "52:54:00:aa:bb:cc",
            "addrs": [{"type": 0, "addr": "10.77.10.10", "prefix": 24}],
        },
    }
    conn = Mock()
    conn.networkLookupByName.return_value = network
    conn.lookupByName.return_value = domain

    ip_address = lifecycle.get_ip(
        conn,
        "astinuslab-net",
        "52:54:00:aa:bb:cc",
        domain_name="astinuslab-pg-lab1-target",
    )

    assert ip_address == "10.77.10.10"
    conn.lookupByName.assert_called_once_with("astinuslab-pg-lab1-target")
    domain.interfaceAddresses.assert_called_once_with(1, 0)


def test_get_ip_retries_guest_interface_without_addresses() -> None:
    libvirt_module = _make_libvirt_module()
    libvirt_module.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT = 1
    libvirt_module.VIR_IP_ADDR_TYPE_IPV4 = 0
    fake_time = Mock()
    fake_time.monotonic.return_value = 0.0
    sleep = Mock()
    lifecycle = VMLifecycle(run_command=Mock(), libvirt_module=libvirt_module, time_module=fake_time, sleep_fn=sleep)
    conn = Mock()
    conn.networkLookupByName.return_value.DHCPLeases.side_effect = [
        [],
        [{"mac": "52:54:00:aa:bb:cc", "ipaddr": "192.168.122.55"}],
    ]
    conn.lookupByName.return_value.interfaceAddresses.return_value = {
        "eth0": {"hwaddr": "52:54:00:aa:bb:cc", "addrs": None},
    }

    assert lifecycle.get_ip(conn, "default", "52:54:00:aa:bb:cc", domain_name="astinuslab-test") == "192.168.122.55"
    sleep.assert_called_once_with(1.0)


def test_guest_agent_lookup_is_ignored_when_it_does_not_return_a_mapping() -> None:
    """A guest with no agent installed answers, but not with interfaces.

    ``interfaceAddresses`` is documented to return a dict. A guest whose agent
    is absent or still starting can answer with something else, and treating
    that as interfaces would raise inside the poll loop rather than wait for the
    lease the guest may still take.
    """
    fake_time = Mock()
    fake_time.monotonic.side_effect = [0.0, 181.0]
    libvirt_module = _make_libvirt_module()
    libvirt_module.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT = 1
    libvirt_module.VIR_IP_ADDR_TYPE_IPV4 = 0
    lifecycle = VMLifecycle(
        run_command=Mock(),
        libvirt_module=libvirt_module,
        time_module=fake_time,
        sleep_fn=Mock(),
    )
    network = Mock()
    network.DHCPLeases.return_value = []
    domain = Mock()
    domain.interfaceAddresses.return_value = []
    conn = Mock()
    conn.networkLookupByName.return_value = network
    conn.lookupByName.return_value = domain

    with pytest.raises(VMIPAddressTimeoutError):
        lifecycle.get_ip(
            conn,
            "astinuslab-net",
            "52:54:00:aa:bb:cc",
            domain_name="astinuslab-pg-lab1-target",
        )

    domain.interfaceAddresses.assert_called_once_with(1, 0)


def test_guest_agent_failure_leaves_the_dhcp_wait_running() -> None:
    """A domain libvirt cannot answer for must not end the wait.

    The agent query is an addition to the lease poll, not a replacement: a
    domain that is still defining, or a libvirt that refuses the call, has to
    fall back to waiting rather than surface its error to the spawn.
    """
    fake_time = Mock()
    fake_time.monotonic.side_effect = [0.0, 181.0]
    libvirt_module = _make_libvirt_module()
    libvirt_module.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT = 1
    libvirt_module.VIR_IP_ADDR_TYPE_IPV4 = 0
    lifecycle = VMLifecycle(
        run_command=Mock(),
        libvirt_module=libvirt_module,
        time_module=fake_time,
        sleep_fn=Mock(),
    )
    network = Mock()
    network.DHCPLeases.return_value = []
    conn = Mock()
    conn.networkLookupByName.return_value = network
    conn.lookupByName.side_effect = libvirt_module.libvirtError("no such domain")

    with pytest.raises(VMIPAddressTimeoutError):
        lifecycle.get_ip(
            conn,
            "astinuslab-net",
            "52:54:00:aa:bb:cc",
            domain_name="astinuslab-pg-lab1-target",
        )


def test_guest_agent_addresses_for_another_interface_are_not_returned() -> None:
    """The agent reports every interface; only the NIC being waited on counts.

    Returning an address from the wrong interface would hand the caller a
    reachable-looking address on a network the Job never attached, which is
    worse than waiting.
    """
    fake_time = Mock()
    fake_time.monotonic.side_effect = [0.0, 181.0]
    libvirt_module = _make_libvirt_module()
    libvirt_module.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT = 1
    libvirt_module.VIR_IP_ADDR_TYPE_IPV4 = 0
    lifecycle = VMLifecycle(
        run_command=Mock(),
        libvirt_module=libvirt_module,
        time_module=fake_time,
        sleep_fn=Mock(),
    )
    network = Mock()
    network.DHCPLeases.return_value = []
    domain = Mock()
    domain.interfaceAddresses.return_value = {
        "lo": {
            "hwaddr": "00:00:00:00:00:00",
            "addrs": [{"type": 0, "addr": "127.0.0.1", "prefix": 8}],
        },
        "enp2s0": {
            "hwaddr": "52:54:00:dd:ee:ff",
            "addrs": [{"type": 0, "addr": "10.77.20.10", "prefix": 24}],
        },
    }
    conn = Mock()
    conn.networkLookupByName.return_value = network
    conn.lookupByName.return_value = domain

    with pytest.raises(VMIPAddressTimeoutError):
        lifecycle.get_ip(
            conn,
            "astinuslab-net",
            "52:54:00:aa:bb:cc",
            domain_name="astinuslab-pg-lab1-target",
        )


def test_get_ip_stops_when_cancel_requested() -> None:
    fake_time = Mock()
    fake_time.monotonic.side_effect = [0.0, 1.0]
    lifecycle = VMLifecycle(
        run_command=Mock(),
        libvirt_module=_make_libvirt_module(),
        time_module=fake_time,
        sleep_fn=Mock(),
    )
    network = Mock()
    network.DHCPLeases.return_value = []
    conn = Mock()
    conn.networkLookupByName.return_value = network

    with pytest.raises(VMIPAddressCancelledError):
        lifecycle.get_ip(
            conn,
            "astinuslab-net",
            "52:54:00:aa:bb:cc",
            timeout_seconds=30.0,
            cancel_requested=lambda: True,
        )


def test_destroy_existing_domain_calls_destroy_and_undefine_flags() -> None:
    libvirt_module = _make_libvirt_module()
    domain = Mock()
    conn = Mock()
    conn.lookupByName.return_value = domain
    lifecycle = VMLifecycle(run_command=Mock(), libvirt_module=libvirt_module, sleep_fn=Mock())

    lifecycle._destroy_if_present(conn, "astinuslab-job-123")

    conn.lookupByName.assert_called_once_with("astinuslab-job-123")
    domain.destroy.assert_called_once_with()
    domain.undefineFlags.assert_called_once_with(libvirt_module.VIR_DOMAIN_UNDEFINE_NVRAM)


def test_public_destroy_surfaces_libvirt_destroy_failure() -> None:
    libvirt_module = _make_libvirt_module()
    domain = Mock()
    domain.isActive.return_value = True
    domain.destroy.side_effect = libvirt_module.libvirtError("destroy failed")
    conn = Mock()
    domain.name.return_value = "astinuslab-job-123"
    domain.XMLDesc.return_value = "<domain/>"
    conn.listAllDomains.return_value = [domain]
    conn.lookupByName.return_value = domain
    lifecycle = VMLifecycle(run_command=Mock(), libvirt_module=libvirt_module)

    with pytest.raises(VMDestroyError, match="destroy failed"):
        lifecycle.destroy(conn, "astinuslab-job-123")

    domain.undefine.assert_not_called()
    domain.undefineFlags.assert_not_called()


def test_public_destroy_requires_the_domain_to_stop_before_undefine() -> None:
    libvirt_module = _make_libvirt_module()
    domain = Mock()
    domain.isActive.return_value = True
    conn = Mock()
    domain.name.return_value = "astinuslab-job-123"
    domain.XMLDesc.return_value = "<domain/>"
    conn.listAllDomains.return_value = [domain]
    conn.lookupByName.return_value = domain
    lifecycle = VMLifecycle(run_command=Mock(), libvirt_module=libvirt_module, sleep_fn=Mock())

    with pytest.raises(VMDestroyError, match="remained active"):
        lifecycle.destroy(conn, "astinuslab-job-123")

    domain.undefine.assert_not_called()
    domain.undefineFlags.assert_not_called()


def test_destroy_if_present_falls_back_to_plain_undefine_when_flags_missing() -> None:
    libvirt_module = _make_libvirt_module()
    del libvirt_module.VIR_DOMAIN_UNDEFINE_NVRAM
    domain = Mock()
    conn = Mock()
    conn.lookupByName.return_value = domain
    lifecycle = VMLifecycle(run_command=Mock(), libvirt_module=libvirt_module, sleep_fn=Mock())

    lifecycle._destroy_if_present(conn, "astinuslab-job-123")

    domain.undefine.assert_called_once_with()


def test_destroy_if_present_ignores_missing_domain() -> None:
    libvirt_module = _make_libvirt_module()
    error = libvirt_module.libvirtError("missing")
    error.get_error_code = Mock(return_value=libvirt_module.VIR_ERR_NO_DOMAIN)
    conn = Mock()
    conn.lookupByName.side_effect = error
    lifecycle = VMLifecycle(run_command=Mock(), libvirt_module=libvirt_module)

    lifecycle._destroy_if_present(conn, "astinuslab-job-123")

    conn.lookupByName.assert_called_once_with("astinuslab-job-123")


def test_destroy_if_present_propagates_non_missing_lookup_error() -> None:
    libvirt_module = _make_libvirt_module()
    error = libvirt_module.libvirtError("connection failure")
    error.get_error_code = Mock(return_value=99)
    conn = Mock()
    conn.lookupByName.side_effect = error
    lifecycle = VMLifecycle(run_command=Mock(), libvirt_module=libvirt_module)

    with pytest.raises(libvirt_module.libvirtError, match="connection failure"):
        lifecycle._destroy_if_present(conn, "astinuslab-job-123")


def test_get_ip_raises_when_no_ipv4_lease_found_before_timeout() -> None:
    fake_time = Mock()
    fake_time.monotonic.side_effect = [0.0, 5.0]
    lifecycle = VMLifecycle(run_command=Mock(), libvirt_module=_make_libvirt_module(), time_module=fake_time)
    network = Mock()
    network.DHCPLeases.return_value = [
        {"mac": "52:54:00:aa:bb:cc", "ipaddr": "fe80::1234"},
    ]
    conn = Mock()
    conn.networkLookupByName.return_value = network

    with pytest.raises(VMIPAddressTimeoutError):
        lifecycle.get_ip(conn, "astinuslab-net", "52:54:00:aa:bb:cc", timeout_seconds=3.0)


def test_attach_nic(monkeypatch):
    from astinuslab.virtualization.vm_lifecycle import VMLifecycle

    class MockDomain:
        def __init__(self):
            self.calls = []

        def attachDeviceFlags(self, xml, flags):
            self.calls.append(xml)

    class MockConn:
        def __init__(self):
            self.domain = MockDomain()

        def lookupByName(self, name):
            return self.domain

    conn = MockConn()
    lifecycle = VMLifecycle(run_command=lambda *a, **k: None, libvirt_module=None)
    lifecycle.attach_nic(conn, "test-domain", "net1", "11:22:33:44:55:66")

    assert len(conn.domain.calls) == 1
    assert "<mac address='11:22:33:44:55:66'/>" in conn.domain.calls[0]
    assert "<source network='net1'/>" in conn.domain.calls[0]


def test_detach_nic():
    from astinuslab.virtualization.vm_lifecycle import VMLifecycle

    class MockDomain:
        def __init__(self):
            self.calls = []

        def detachDeviceFlags(self, xml, flags):
            self.calls.append(xml)

    class MockConn:
        def __init__(self):
            self.domain = MockDomain()

        def lookupByName(self, name):
            return self.domain

    conn = MockConn()
    lifecycle = VMLifecycle(run_command=lambda *a, **k: None, libvirt_module=None)
    lifecycle.detach_nic(conn, "test-domain", "11:22:33:44:55:66")

    assert len(conn.domain.calls) == 1
    assert "<mac address='11:22:33:44:55:66'/>" in conn.domain.calls[0]


def test_is_missing_interface_error_matches_device_missing_code():
    libvirt_module = Mock()
    libvirt_module.VIR_ERR_DEVICE_MISSING = 99
    lifecycle = VMLifecycle(run_command=Mock(), libvirt_module=libvirt_module)

    class FakeErr(Exception):
        def __init__(self, code: int, msg: str = ""):
            self._code = code
            self._msg = msg

        def get_error_code(self):
            return self._code

        def get_error_message(self):
            return self._msg

    assert lifecycle.is_missing_interface_error(FakeErr(99))
    assert lifecycle.is_missing_interface_error(FakeErr(38, "no matching network device with mac 52:54:00:aa:bb:cc"))
    assert not lifecycle.is_missing_interface_error(FakeErr(38, "general failure"))
    assert not lifecycle.is_missing_interface_error(RuntimeError("not a libvirt error"))
