from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astinuslab.virtualization.uefi import (
    WindowsFirmwareConfig,
    render_uefi_os_xml,
    validate_nvram_path,
)
from astinuslab.virtualization.vm_lifecycle import VMBootError, VMDestroyError, VMLifecycle


class FakeLibvirtError(Exception):
    def get_error_code(self):
        return 42


LIBVIRT = SimpleNamespace(
    libvirtError=FakeLibvirtError, VIR_ERR_NO_DOMAIN=42, VIR_DOMAIN_UNDEFINE_NVRAM=4, VIR_DOMAIN_XML_INACTIVE=2
)


class Domain:
    def __init__(self, conn, name, xml, inactive_xml=None):
        self.conn, self.domain_name, self.xml = conn, name, xml
        self.inactive_xml = inactive_xml or xml
        self.active = False
        self.cleanup_mode = "ok"
        self.destroy_calls = 0

    def name(self):
        return self.domain_name

    def XMLDesc(self, flags=0):
        return self.inactive_xml if flags == 2 else self.xml

    def isPersistent(self):
        return True

    def isActive(self):
        return self.active

    def create(self):
        self.active = True
        if self.conn.fail_start:
            raise RuntimeError("domain start failed")

    def destroy(self):
        self.destroy_calls += 1
        self.active = False

    def undefineFlags(self, flags):
        if self.cleanup_mode == "error":
            raise FakeLibvirtError("undefine failed")
        if self.cleanup_mode == "ok":
            self.conn.domains.remove(self)

    def undefine(self):
        self.undefineFlags(0)


class Connection:
    close = Mock()

    def __init__(self):
        self.domains = []
        self.cleanup_mode = "ok"

        self.fail_start = False

    def listAllDomains(self, flags=0):
        return list(self.domains)

    def lookupByName(self, name):
        for domain in self.domains:
            if domain.name() == name:
                return domain
        raise FakeLibvirtError("missing")

    def defineXML(self, xml):
        domain = Domain(self, ET.fromstring(xml).findtext("name"), xml)
        domain.cleanup_mode = self.cleanup_mode
        self.domains.append(domain)
        return domain


def boot_windows(conn, tmp_path, *, nvram=None, fail=False):
    nvram = nvram or tmp_path / "runtime.vars"
    if not nvram.exists():
        nvram.write_bytes(b"vars")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        (tmp_path / "overlay.qcow2").write_bytes(b"overlay")

    lifecycle = VMLifecycle(run_command=run, libvirt_module=LIBVIRT, sleep_fn=lambda _: None)

    conn.fail_start = fail

    result = lifecycle.boot(
        conn,
        vm_name="test-windows",
        memory_mb=8192,
        vcpus=4,
        backing_image=tmp_path / "base.qcow2",
        disk_path=tmp_path / "overlay.qcow2",
        seed_iso_path=None,
        user_data_path=None,
        meta_data_path=None,
        virtiofs_socket=tmp_path / "fs.sock",
        share_dir=tmp_path,
        network_name="private",
        guest_mac="52:54:00:12:34:56",
        firmware=WindowsFirmwareConfig(),
        nvram_path=nvram,
    )
    return result, calls


@pytest.mark.parametrize("owner_state", ["active", "inactive", "saved-config"])
def test_boot_rejects_nvram_owned_by_another_domain_before_overlay(tmp_path, owner_state):
    nvram = tmp_path / "runtime.vars"
    nvram.write_bytes(b"original")
    xml = f"<domain><os><nvram>{nvram}</nvram></os></domain>"
    conn = Connection()
    owner = Domain(conn, "other", "<domain/>" if owner_state == "saved-config" else xml, xml)
    owner.active = owner_state != "inactive"
    conn.domains.append(owner)
    with pytest.raises(VMBootError, match="already assigned"):
        boot_windows(conn, tmp_path)
    assert not (tmp_path / "overlay.qcow2").exists()
    assert nvram.read_bytes() == b"original"
    assert owner.destroy_calls == 0


def test_boot_rejects_nvram_outside_job_directory_before_overlay(tmp_path):
    runtime = tmp_path / "job"
    runtime.mkdir()
    outside = tmp_path / "outside.vars"
    outside.write_bytes(b"outside")
    with pytest.raises(VMBootError, match="per-runtime directory"):
        boot_windows(Connection(), runtime, nvram=outside)
    assert not (runtime / "overlay.qcow2").exists()
    assert outside.read_bytes() == b"outside"


def test_windows_boot_never_replaces_an_existing_domain_name(tmp_path):
    conn = Connection()
    existing = Domain(conn, "test-windows", "<domain/>")
    conn.domains.append(existing)
    with pytest.raises(VMBootError, match="already exists"):
        boot_windows(conn, tmp_path)
    assert conn.domains == [existing]
    assert existing.destroy_calls == 0


@pytest.mark.parametrize("failure", ["error", "still-defined"])
def test_windows_boot_failure_preserves_artifacts_until_undefinition_proven(tmp_path, failure):
    conn = Connection()
    conn.cleanup_mode = failure
    with pytest.raises(VMBootError, match="artifacts preserved"):
        boot_windows(conn, tmp_path, fail=True)
    assert (tmp_path / "overlay.qcow2").read_bytes() == b"overlay"
    assert (tmp_path / "runtime.vars").read_bytes() == b"vars"
    assert conn.domains


def test_windows_boot_failure_removes_only_released_job_files(tmp_path):
    conn = Connection()
    untouched = tmp_path / "base.qcow2"
    untouched.write_bytes(b"base")
    with pytest.raises(VMBootError, match="domain start failed"):
        boot_windows(conn, tmp_path, fail=True)
    assert not conn.domains
    assert not (tmp_path / "overlay.qcow2").exists()
    assert not (tmp_path / "runtime.vars").exists()
    assert untouched.read_bytes() == b"base"


def test_strict_destroy_refuses_nvram_shared_with_another_domain(tmp_path):
    conn = Connection()
    nvram = tmp_path / "shared.vars"
    nvram.write_bytes(b"shared")
    xml = f"<domain><os><nvram>{nvram}</nvram></os></domain>"
    a, b = Domain(conn, "a", xml), Domain(conn, "b", xml)
    conn.domains.extend([a, b])
    with pytest.raises(VMDestroyError, match="already assigned"):
        VMLifecycle(run_command=Mock(), libvirt_module=LIBVIRT).destroy(conn, "a")
    assert conn.domains == [a, b]
    assert nvram.read_bytes() == b"shared"


@pytest.mark.parametrize("kind", ["directory", "symlink", "hardlink"])
def test_nvram_must_be_an_unaliased_regular_file(tmp_path, kind):
    nvram = tmp_path / "runtime.vars"
    source = tmp_path / "source.vars"
    source.write_bytes(b"original")
    if kind == "directory":
        nvram.mkdir()
    elif kind == "symlink":
        nvram.symlink_to(source)
    else:
        nvram.hardlink_to(source)
    with pytest.raises(ValueError, match="regular file|alias"):
        validate_nvram_path(nvram, [], runtime_dir=tmp_path)


def test_teardown_preserves_job_directory_referenced_by_another_domain(tmp_path, monkeypatch):
    from astinuslab.virtualization import vm_support

    conn = Connection()
    job_dir = tmp_path / "images" / "job"
    job_dir.mkdir(parents=True)
    nvram = job_dir / "runtime.vars"
    nvram.write_bytes(b"still owned")
    conn.domains.append(Domain(conn, "other", f"<domain><os><nvram>{nvram}</nvram></os></domain>"))
    fake_libvirt = SimpleNamespace(**vars(LIBVIRT), open=lambda _: conn)
    monkeypatch.setattr(vm_support, "import_module", lambda _: fake_libvirt)
    commands = []
    settings = SimpleNamespace(
        libvirt_uri="qemu:///system",
        libvirt_image_base_dir=tmp_path / "images",
        storage_dir=tmp_path / "storage",
        serial_log_dir=tmp_path / "serial",
    )
    destroyed = []
    private_tmp = settings.storage_dir / "tmp" / "job"
    private_tmp.mkdir(parents=True)
    (private_tmp / "virtiofsd.pid").write_text("12345")
    settings.serial_log_dir.mkdir()
    serial = settings.serial_log_dir / "astinuslab-job-serial.log"
    serial.write_text("preserve diagnostics")
    monkeypatch.setattr("os.kill", lambda *args: pytest.fail("teardown failure must preserve the share process"))
    result = vm_support.teardown_vm(
        lambda cmd: commands.append(cmd), Mock(), settings, lambda *a: destroyed.append(a), "job"
    )
    assert result is False
    assert nvram.read_bytes() == b"still owned"
    assert not commands
    assert not destroyed
    assert serial.read_text() == "preserve diagnostics"


def test_boot_fails_closed_when_domain_ownership_cannot_be_read(tmp_path, monkeypatch):
    conn = Connection()
    monkeypatch.setattr(conn, "listAllDomains", Mock(side_effect=FakeLibvirtError("enumeration failed")))
    with pytest.raises(VMBootError, match="ownership"):
        boot_windows(conn, tmp_path)
    assert not (tmp_path / "overlay.qcow2").exists()


def test_boot_preserves_overlay_owned_by_another_domain(tmp_path):
    disk = tmp_path / "overlay.qcow2"
    disk.write_bytes(b"other VM disk")
    conn = Connection()
    conn.domains.append(
        Domain(conn, "other", f"<domain><devices><disk><source file='{disk}'/></disk></devices></domain>")
    )
    with pytest.raises(VMBootError, match="still referenced"):
        boot_windows(conn, tmp_path)
    assert disk.read_bytes() == b"other VM disk"


def test_render_windows_uefi_xml_uses_q35_ovmf_and_unique_nvram(tmp_path: Path):
    nvram = tmp_path / "runtime.vars"
    nvram.write_bytes(b"vars")

    xml = render_uefi_os_xml(WindowsFirmwareConfig(), nvram)

    assert "arch='x86_64'" in xml
    assert "machine='q35'" in xml
    assert "readonly='yes'" in xml
    assert "/usr/share/OVMF/OVMF_CODE_4M.fd" in xml
    assert "/usr/share/OVMF/OVMF_VARS_4M.fd" in xml
    assert str(nvram) in xml
    assert "<nvram" in xml
    assert "<tpm" not in xml
    assert "secure='yes'" not in xml


def test_validate_nvram_path_rejects_duplicate_assignment(tmp_path: Path):
    nvram = tmp_path / "runtime.vars"
    nvram.write_bytes(b"vars")

    with pytest.raises(ValueError, match="already assigned"):
        validate_nvram_path(nvram, [nvram])


def test_validate_nvram_path_rejects_relative_or_missing_paths(tmp_path: Path):
    with pytest.raises(ValueError, match="absolute"):
        validate_nvram_path(Path("runtime.vars"), [])
    with pytest.raises(ValueError, match="does not exist"):
        validate_nvram_path(tmp_path / "missing.vars", [])
