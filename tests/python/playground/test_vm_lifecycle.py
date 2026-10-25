from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from astinuslab.virtualization.vm_lifecycle import VMBootError, VMLifecycle


def test_render_playground_domain_xml_includes_all_network_interfaces() -> None:
    lifecycle = VMLifecycle(run_command=Mock(), libvirt_module=Mock())

    xml = lifecycle._render_playground_domain_xml(
        domain_name="astinuslab-pg-pg-1-web",
        memory_mb=2048,
        vcpus=2,
        disk_path=Path("/var/lib/astinuslab/playground/pg-1/web.qcow2"),
        networks=[
            {"libvirt_net": "astinuslab-pg-pg-1-ab", "mac": "52:54:00:aa:bb:01"},
            {"libvirt_net": "astinuslab-pg-pg-1-db", "mac": "52:54:00:aa:bb:02"},
        ],
    )

    assert "<name>astinuslab-pg-pg-1-web</name>" in xml
    assert xml.count("<interface type='network'>") == 2
    assert "<source network='astinuslab-pg-pg-1-ab'/>" in xml
    assert "<source network='astinuslab-pg-pg-1-db'/>" in xml
    assert "<mac address='52:54:00:aa:bb:01'/>" in xml
    assert "<mac address='52:54:00:aa:bb:02'/>" in xml


def test_boot_playground_destroys_stale_domain_before_creating_overlay(tmp_path: Path) -> None:
    events: list[str] = []

    class MissingDomainError(Exception):
        def get_error_code(self):
            return 42

    def _run_command(cmd, cwd=None):
        events.append("overlay")

    def _lookup_by_name(name):
        events.append("lookup")
        raise MissingDomainError("missing")

    conn = Mock()
    conn.lookupByName.side_effect = _lookup_by_name
    conn.defineXML.return_value = Mock()
    libvirt_module = Mock()
    libvirt_module.open.return_value = conn
    libvirt_module.libvirtError = MissingDomainError
    libvirt_module.VIR_ERR_NO_DOMAIN = 42
    lifecycle = VMLifecycle(run_command=_run_command, libvirt_module=libvirt_module)

    lifecycle.boot_playground(
        domain_name="astinuslab-pg-pg-1-web",
        memory_mb=2048,
        vcpus=2,
        base_image_path=tmp_path / "base.qcow2",
        disk_path=tmp_path / "web.qcow2",
        disk_gb=None,
        networks=[{"libvirt_net": "astinuslab-pg-pg-1-ab", "mac": "52:54:00:aa:bb:01"}],
        libvirt_uri="qemu:///system",
        cwd=tmp_path,
    )

    assert events == ["lookup", "overlay"]


def test_boot_playground_preserves_artifacts_when_partial_create_cannot_be_stopped(tmp_path: Path) -> None:
    class FakeLibvirtError(Exception):
        pass

    disk = tmp_path / "web.qcow2"
    seed = tmp_path / "seed.iso"

    def run_command(command, **_kwargs):
        output = Path(command[1] if command[0] == "cloud-localds" else command[-1])
        output.write_bytes(b"boot artifact")

    domain = Mock()
    domain.isActive.return_value = True
    domain.destroy.side_effect = FakeLibvirtError("destroy failed")

    def partial_create():
        raise FakeLibvirtError("create failed after activation")

    domain.create.side_effect = partial_create
    conn = Mock()
    conn.defineXML.return_value = domain
    libvirt_module = Mock()
    libvirt_module.open.return_value = conn
    libvirt_module.libvirtError = FakeLibvirtError
    libvirt_module.VIR_DOMAIN_UNDEFINE_NVRAM = 4
    lifecycle = VMLifecycle(run_command=run_command, libvirt_module=libvirt_module)
    lifecycle._destroy_if_present = Mock()  # type: ignore[method-assign]

    with pytest.raises(VMBootError, match="create failed after activation"):
        lifecycle.boot_playground(
            domain_name="astinuslab-pg-pg-1-web",
            memory_mb=2048,
            vcpus=2,
            base_image_path=tmp_path / "base.qcow2",
            disk_path=disk,
            disk_gb=None,
            networks=[{"libvirt_net": "astinuslab-pg-pg-1-ab", "mac": "52:54:00:aa:bb:01"}],
            libvirt_uri="qemu:///system",
            cwd=tmp_path,
            seed_iso_path=seed,
            user_data_path=tmp_path / "user-data",
            meta_data_path=tmp_path / "meta-data",
        )

    assert disk.read_bytes() == b"boot artifact"
    assert seed.read_bytes() == b"boot artifact"
    domain.undefine.assert_not_called()
    domain.undefineFlags.assert_not_called()


def test_boot_playground_preserves_overlay_when_stale_domain_cannot_be_stopped(tmp_path: Path) -> None:
    class FakeLibvirtError(Exception):
        pass

    disk = tmp_path / "web.qcow2"
    disk.write_bytes(b"existing overlay")
    domain = Mock()
    domain.isActive.return_value = True
    domain.destroy.side_effect = FakeLibvirtError("destroy failed")
    domain.name.return_value = "astinuslab-pg-pg-1-web"
    domain.isPersistent.return_value = True
    domain.XMLDesc.return_value = "<domain><os><type>hvm</type></os></domain>"
    conn = Mock()
    conn.lookupByName.return_value = domain
    conn.listAllDomains.return_value = [domain]
    libvirt_module = Mock()
    libvirt_module.open.return_value = conn
    libvirt_module.libvirtError = FakeLibvirtError
    lifecycle = VMLifecycle(run_command=Mock(), libvirt_module=libvirt_module)

    with pytest.raises(VMBootError, match="destroy failed"):
        lifecycle.boot_playground(
            domain_name="astinuslab-pg-pg-1-web",
            memory_mb=2048,
            vcpus=2,
            base_image_path=tmp_path / "base.qcow2",
            disk_path=disk,
            disk_gb=None,
            networks=[{"libvirt_net": "astinuslab-pg-pg-1-ab", "mac": "52:54:00:aa:bb:01"}],
            libvirt_uri="qemu:///system",
            cwd=tmp_path,
        )

    assert disk.read_bytes() == b"existing overlay"
    domain.undefine.assert_not_called()
