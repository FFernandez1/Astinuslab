from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from ..images.windows_bundle import StagedWindowsMedia, remove_staged_media, verify_staged_media
from .uefi import (
    WindowsFirmwareConfig,
    assert_runtime_directory_unowned,
    domain_file_references,
    paths_alias,
    render_uefi_os_xml,
    validate_nvram_path,
)


class VMIPAddressTimeoutError(RuntimeError):
    pass


class VMIPAddressCancelledError(RuntimeError):
    pass


class VMBootError(Exception):
    """Raised when VM provisioning fails."""


class VMDestroyError(Exception):
    """Raised when VM teardown fails."""


def backing_virtual_size_gb(
    *,
    backing_image: Path,
    run_command,
    cwd: Path | None = None,
    job_id: str | None = None,
) -> int | None:
    """Return a qcow2 backing image's virtual size, rounded up to GiB."""
    import json

    cmd = ["qemu-img", "info", "--output=json", str(backing_image)]
    kwargs: dict[str, Any] = {"cwd": cwd, "capture_output": True, "text": True}
    if job_id is not None:
        kwargs["job_id"] = job_id
    try:
        result = run_command(cmd, **kwargs)
    except Exception:
        return None
    stdout = getattr(result, "stdout", result)
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", "replace")
    if not isinstance(stdout, str):
        return None
    try:
        info = json.loads(stdout)
    except (ValueError, TypeError):
        return None
    virtual_size = info.get("virtual-size")
    if not isinstance(virtual_size, int) or virtual_size <= 0:
        return None
    gib = 1024**3
    return (virtual_size + gib - 1) // gib


@dataclass(frozen=True)
class VMBootResult:
    vm_name: str
    guest_ip: str
    disk_path: Path
    seed_iso_path: Path
    share_dir: Path


class VMLifecycle:
    def __init__(
        self,
        *,
        run_command,
        libvirt_module,
        time_module=None,
        sleep_fn=None,
        prepare_overlay_access: Callable[[Path], None] | None = None,
    ) -> None:
        self._run_command = run_command
        self._libvirt = libvirt_module
        self._time = time_module or time
        self._sleep = sleep_fn or time.sleep
        self._prepare_overlay_access = prepare_overlay_access

    def create_overlay(
        self,
        *,
        backing_image: Path,
        overlay_path: Path,
        disk_size_gb: int | None = None,
        cwd: Path | None = None,
        job_id: str | None = None,
    ) -> Path:
        effective_size_gb = disk_size_gb
        if effective_size_gb is not None:
            backing_size_gb = self._backing_virtual_size_gb(backing_image=backing_image, cwd=cwd, job_id=job_id)
            if backing_size_gb is not None and effective_size_gb < backing_size_gb:
                effective_size_gb = backing_size_gb
        cmd = [
            "qemu-img",
            "create",
            "-f",
            "qcow2",
            "-F",
            "qcow2",
            "-b",
            str(backing_image),
            str(overlay_path),
        ]
        if effective_size_gb:
            cmd.append(f"{effective_size_gb}G")

        # Retry loop to handle transient locking issues (e.g. QEMU process still dying)
        last_exc = None
        for attempt in range(3):
            try:
                kwargs: dict[str, Any] = {"cwd": cwd}
                if job_id is not None:
                    kwargs["job_id"] = job_id
                self._run_command(cmd, **kwargs)
                return overlay_path
            except Exception as exc:
                last_exc = exc
                if 'Failed to get "write" lock' in str(exc) or "Is another process using the image" in str(exc):
                    if attempt < 2:
                        self._sleep(1.0)
                        continue
                raise
        if last_exc:
            raise last_exc
        return overlay_path

    def _backing_virtual_size_gb(
        self,
        *,
        backing_image: Path,
        cwd: Path | None,
        job_id: str | None,
    ) -> int | None:
        return backing_virtual_size_gb(
            backing_image=backing_image,
            run_command=self._run_command,
            cwd=cwd,
            job_id=job_id,
        )

    def create_seed_iso(
        self,
        *,
        seed_iso_path: Path,
        user_data_path: Path,
        meta_data_path: Path,
        network_config_path: Path | None = None,
        cwd: Path | None = None,
    ) -> Path:
        command = ["cloud-localds"]
        if network_config_path is not None:
            command.append(f"--network-config={network_config_path}")
        command.extend(
            [
                str(seed_iso_path),
                str(user_data_path),
                str(meta_data_path),
            ]
        )
        self._run_command(
            command,
            cwd=cwd,
        )
        return seed_iso_path

    def boot(
        self,
        conn,
        *,
        vm_name: str,
        memory_mb: int,
        vcpus: int,
        backing_image: Path,
        disk_path: Path,
        seed_iso_path: Path | None,
        user_data_path: Path | None,
        meta_data_path: Path | None,
        disk_size_gb: int | None = None,
        network_config_path: Path | None = None,
        virtiofs_socket: Path,
        share_dir: Path,
        virtiofs_mount_tag: str = "astinuslab_out",
        network_name: str,
        guest_mac: str,
        machine_type: str | None = None,
        cwd: Path | None = None,
        ip_timeout_seconds: float = 180.0,
        cancel_requested=None,
        ip_resolver: Callable[[], str] | None = None,
        preserve_on_failure: bool = False,
        serial_log_path: Path | None = None,
        filesystem_type: str = "virtiofs",
        job_id: str | None = None,
        firmware: WindowsFirmwareConfig | None = None,
        nvram_path: Path | None = None,
        prepared_media: StagedWindowsMedia | None = None,
    ) -> VMBootResult:
        if prepared_media is not None:
            try:
                if firmware is None or any(
                    p is not None for p in (seed_iso_path, user_data_path, meta_data_path, network_config_path)
                ):
                    raise ValueError("prepared media requires Windows firmware and no Linux seed inputs")
                verify_staged_media(prepared_media, runtime_dir=disk_path.parent)
            except (ValueError, OSError) as exc:
                raise VMBootError(f"Windows prepared media preflight failed: {exc}") from exc
        if firmware is not None:
            try:
                if nvram_path is None:
                    raise ValueError("NVRAM path is required for UEFI firmware")
                owners = domain_file_references(conn)
                if any(name == vm_name for name, _, _ in owners):
                    raise ValueError("Windows domain name already exists")
                validate_nvram_path(
                    nvram_path, [p for _, _, paths in owners for p in paths], runtime_dir=disk_path.parent
                )
                assert_runtime_directory_unowned(conn, disk_path.parent)
            except Exception as exc:
                raise VMBootError(f"Windows NVRAM preflight failed: {exc}") from exc
        else:
            self._destroy_if_present(conn, vm_name)
        domain = None
        try:
            disk_path = self.create_overlay(
                backing_image=backing_image,
                overlay_path=disk_path,
                disk_size_gb=disk_size_gb,
                cwd=cwd,
                job_id=job_id,
            )
            if seed_iso_path is not None:
                if user_data_path is None or meta_data_path is None:
                    raise ValueError("user_data_path and meta_data_path are required when seed_iso_path is set")
                seed_iso_path = self.create_seed_iso(
                    seed_iso_path=seed_iso_path,
                    user_data_path=user_data_path,
                    meta_data_path=meta_data_path,
                    network_config_path=network_config_path,
                    cwd=cwd,
                )
            if self._prepare_overlay_access is not None:
                self._prepare_overlay_access(disk_path)
            domain_xml = self._render_domain_xml(
                vm_name=vm_name,
                memory_mb=memory_mb,
                vcpus=vcpus,
                disk_path=disk_path,
                seed_iso_path=seed_iso_path,
                virtiofs_socket=virtiofs_socket,
                share_dir=share_dir,
                virtiofs_mount_tag=virtiofs_mount_tag,
                network_name=network_name,
                guest_mac=guest_mac,
                machine_type=machine_type,
                serial_log_path=serial_log_path,
                filesystem_type=filesystem_type,
                firmware=firmware,
                nvram_path=nvram_path,
                prepared_media_path=prepared_media.path if prepared_media is not None else None,
            )
            domain = conn.defineXML(domain_xml)
            domain.create()
            if firmware is not None:
                # Windows credentials/identity are established over QGA first.
                # The runner resolves the address after bootstrap and reboot.
                guest_ip = ""
            elif ip_resolver is not None:
                guest_ip = ip_resolver()
            else:
                guest_ip = self._poll_dhcp_lease_ipv4(
                    conn,
                    network_name=network_name,
                    guest_mac=guest_mac,
                    domain_name=vm_name,
                    timeout_seconds=ip_timeout_seconds,
                    cancel_requested=cancel_requested,
                )
        except Exception as exc:
            if firmware is not None and not preserve_on_failure:
                try:
                    if domain is not None:
                        self.destroy(conn, vm_name)
                    assert_runtime_directory_unowned(conn, disk_path.parent)
                    if prepared_media is not None:
                        remove_staged_media(prepared_media, runtime_dir=disk_path.parent)
                except Exception as cleanup_exc:
                    raise VMBootError(
                        f"Failed to boot VM {vm_name}: {exc}; cleanup refused, artifacts preserved: {cleanup_exc}"
                    ) from exc
                if nvram_path is not None:
                    nvram_path.unlink(missing_ok=True)
            elif domain is not None and not preserve_on_failure:
                self._cleanup_defined_domain(domain)
            if not preserve_on_failure:
                self._cleanup_boot_artifacts(disk_path, seed_iso_path)
            raise VMBootError(f"Failed to boot VM {vm_name}: {exc}") from exc
        return VMBootResult(
            vm_name=vm_name,
            guest_ip=guest_ip,
            disk_path=disk_path,
            seed_iso_path=seed_iso_path or Path(),
            share_dir=share_dir,
        )

    def boot_playground(
        self,
        *,
        domain_name: str,
        memory_mb: int,
        vcpus: int,
        base_image_path: str | Path,
        disk_path: Path,
        disk_gb: int | None,
        networks: list[dict],
        libvirt_uri: str,
        machine_type: str | None = None,
        cwd: Path | None = None,
        job_id: str | None = None,
        seed_iso_path: Path | None = None,
        user_data_path: Path | None = None,
        meta_data_path: Path | None = None,
        network_config_path: Path | None = None,
    ) -> VMBootResult:
        if not networks:
            raise ValueError("at least one network is required for playground VM boot")

        conn = self._libvirt.open(libvirt_uri)
        domain = None
        preflight_complete = False
        try:
            self._destroy_if_present(conn, domain_name, strict=True)
            preflight_complete = True
            disk_path = self.create_overlay(
                backing_image=Path(base_image_path),
                overlay_path=disk_path,
                disk_size_gb=disk_gb,
                cwd=cwd,
                job_id=job_id,
            )
            if seed_iso_path is not None:
                if user_data_path is None or meta_data_path is None:
                    raise ValueError("user_data_path and meta_data_path are required when seed_iso_path is set")
                self.create_seed_iso(
                    seed_iso_path=seed_iso_path,
                    user_data_path=user_data_path,
                    meta_data_path=meta_data_path,
                    network_config_path=network_config_path,
                    cwd=cwd,
                )
            if self._prepare_overlay_access is not None:
                self._prepare_overlay_access(disk_path)
            domain_xml = self._render_playground_domain_xml(
                domain_name=domain_name,
                memory_mb=memory_mb,
                vcpus=vcpus,
                disk_path=disk_path,
                seed_iso_path=seed_iso_path,
                networks=networks,
                machine_type=machine_type,
            )
            domain = conn.defineXML(domain_xml)
            domain.create()
        except Exception as exc:
            cleanup_error: Exception | None = None
            if domain is not None:
                try:
                    self._destroy_defined_domain_strict(domain, vm_name=domain_name)
                except Exception as teardown_exc:
                    cleanup_error = teardown_exc
            if preflight_complete and cleanup_error is None:
                try:
                    disk_path.unlink(missing_ok=True)
                    if seed_iso_path is not None:
                        seed_iso_path.unlink(missing_ok=True)
                except OSError:
                    pass
            detail = f"; teardown retry required: {cleanup_error}" if cleanup_error is not None else ""
            raise VMBootError(f"Failed to boot VM {domain_name}: {exc}{detail}") from exc
        finally:
            close = getattr(conn, "close", None)
            if callable(close):
                close()
        return VMBootResult(
            vm_name=domain_name,
            guest_ip="",
            disk_path=disk_path,
            seed_iso_path=seed_iso_path or Path(),
            share_dir=Path(),
        )

    def destroy(self, conn, vm_name: str) -> None:
        close_conn = None
        if isinstance(conn, str):
            conn = self._libvirt.open(conn)
            close_conn = getattr(conn, "close", None)
        try:
            self._destroy_if_present(conn, vm_name, strict=True)
        finally:
            if callable(close_conn):
                close_conn()

    def get_ip(
        self,
        conn,
        network_name: str,
        guest_mac: str,
        *,
        domain_name: str | None = None,
        timeout_seconds: float = 180.0,
        cancel_requested=None,
    ) -> str:
        close_conn = None
        if isinstance(conn, str):
            conn = self._libvirt.open(conn)
            close_conn = getattr(conn, "close", None)
        try:
            return self._poll_dhcp_lease_ipv4(
                conn,
                network_name=network_name,
                guest_mac=guest_mac,
                domain_name=domain_name,
                timeout_seconds=timeout_seconds,
                cancel_requested=cancel_requested,
            )
        finally:
            if callable(close_conn):
                close_conn()

    def _render_domain_xml(
        self,
        *,
        vm_name: str,
        memory_mb: int,
        vcpus: int,
        disk_path: Path,
        seed_iso_path: Path | None,
        virtiofs_socket: Path,
        share_dir: Path,
        virtiofs_mount_tag: str = "astinuslab_out",
        network_name: str,
        guest_mac: str,
        machine_type: str | None = None,
        serial_log_path: Path | None = None,
        filesystem_type: str = "virtiofs",
        firmware: WindowsFirmwareConfig | None = None,
        nvram_path: Path | None = None,
        prepared_media_path: Path | None = None,
    ) -> str:
        if prepared_media_path is not None and (firmware is None or seed_iso_path is not None):
            raise ValueError("prepared media cannot be mixed with Linux seed media")
        cd_path = prepared_media_path if prepared_media_path is not None else seed_iso_path
        escaped_name = escape(vm_name)
        escaped_disk = self._escape_xml_attr(disk_path)
        escaped_seed = self._escape_xml_attr(cd_path) if cd_path is not None else ""
        # virtiofs_socket is kept for call-site compatibility only: the virtiofs device
        # is libvirt-managed, so no external daemon socket is rendered any more.
        _ = virtiofs_socket
        _escaped_share_dir = self._escape_xml_attr(share_dir)
        escaped_mount_tag = self._escape_xml_attr(virtiofs_mount_tag)
        escaped_9p_mount_tag = self._escape_xml_attr(f"{virtiofs_mount_tag}_9p")
        escaped_network = self._escape_xml_attr(network_name)
        escaped_guest_mac = self._escape_xml_attr(guest_mac)
        serial_console_xml = "    <console type='pty'/>"
        if serial_log_path is not None:
            escaped_serial_log_path = self._escape_xml_attr(serial_log_path)
            serial_console_xml = f"""    <console type='file'>
      <source path='{escaped_serial_log_path}'/>
      <target type='serial' port='0'/>
    </console>"""
        machine_attr = ""
        if machine_type:
            machine_attr = f" machine='{self._escape_xml_attr(machine_type)}'"

        if firmware is not None:
            if nvram_path is None:
                raise ValueError("NVRAM path is required for UEFI firmware")
            validate_nvram_path(nvram_path, [])
            os_xml = render_uefi_os_xml(firmware, nvram_path)
            lifecycle_xml = "  <on_reboot>restart</on_reboot>"
            graphics_xml = "    <graphics type='vnc' listen='127.0.0.1' autoport='yes'/>"
        else:
            os_xml = (
                "  <os>\n"
                f"    <type arch='x86_64'{machine_attr}>hvm</type>\n"
                "    <boot dev='hd'/>\n"
                "    <boot dev='cdrom'/>\n"
                "  </os>"
            )
            lifecycle_xml = ""
            graphics_xml = ""

        if cd_path is None:
            seed_xml = ""
        else:
            # Minimal Linux cloud kernels may omit SATA CD-ROM drivers. NoCloud
            # discovers the ISO by label on a read-only virtio block device too.
            device = "cdrom" if prepared_media_path is not None else "disk"
            target = "dev='sda' bus='sata'" if prepared_media_path is not None else "dev='vdb' bus='virtio'"
            seed_xml = f"""    <disk type='file' device='{device}'>
      <driver name='qemu' type='raw'/>
      <source file='{escaped_seed}'/>
      <target {target}/>
      <readonly/>
    </disk>
"""

        if filesystem_type == "9p":
            filesystem_xml = f"""    <filesystem type='mount' accessmode='mapped'>
      <driver type='path' wrpolicy='immediate'/>
      <source dir='{_escaped_share_dir}'/>
      <target dir='{escaped_9p_mount_tag}'/>
    </filesystem>"""
        else:
            # Managed virtiofs: libvirt starts and owns virtiofsd for this domain and
            # tears it down with the domain. An externally spawned daemon cannot work
            # here — under the runner's confinement it fails to enter its own sandbox
            # (MountProc EPERM), and loosening that sandbox is not on the table.
            filesystem_xml = f"""    <filesystem type='mount' accessmode='passthrough'>
      <driver type='virtiofs'/>
      <source dir='{_escaped_share_dir}'/>
      <target dir='{escaped_mount_tag}'/>
    </filesystem>
    <filesystem type='mount' accessmode='mapped'>
      <driver type='path' wrpolicy='immediate'/>
      <source dir='{_escaped_share_dir}'/>
      <target dir='{escaped_9p_mount_tag}'/>
    </filesystem>"""

        domain_type = "kvm" if Path("/dev/kvm").exists() else "qemu"
        return f"""<domain type='{domain_type}'>
  <name>{escaped_name}</name>
  <memory unit='MiB'>{memory_mb}</memory>
  <vcpu>{vcpus}</vcpu>
  <memoryBacking>
    <source type='memfd'/>
    <access mode='shared'/>
  </memoryBacking>
{os_xml}
{lifecycle_xml}
  <features>
    <acpi/>
    <apic/>
  </features>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='{escaped_disk}'/>
      <target dev='vda' bus='virtio'/>
    </disk>
{seed_xml}{filesystem_xml}
    <interface type='network'>
      <mac address='{escaped_guest_mac}'/>
      <source network='{escaped_network}'/>
      <model type='virtio'/>
    </interface>
    <channel type='unix'>
      <target type='virtio' name='org.qemu.guest_agent.0'/>
    </channel>
{serial_console_xml}
{graphics_xml}
  </devices>
</domain>"""

    def _render_playground_domain_xml(
        self,
        *,
        domain_name: str,
        memory_mb: int,
        vcpus: int,
        disk_path: Path,
        networks: list[dict],
        seed_iso_path: Path | None = None,
        machine_type: str | None = None,
    ) -> str:
        escaped_name = escape(domain_name)
        escaped_disk = self._escape_xml_attr(disk_path)
        machine_attr = ""
        if machine_type:
            machine_attr = f" machine='{self._escape_xml_attr(machine_type)}'"
        interface_xml = "\n".join(
            f"""    <interface type='network'>
      <mac address='{self._escape_xml_attr(network["mac"])}'/>
      <source network='{self._escape_xml_attr(network["libvirt_net"])}'/>
      <model type='virtio'/>
    </interface>"""
            for network in networks
        )
        seed_xml = ""
        if seed_iso_path is not None:
            escaped_seed = self._escape_xml_attr(seed_iso_path)
            seed_xml = f"""    <disk type='file' device='disk'>
      <driver name='qemu' type='raw'/>
      <source file='{escaped_seed}'/>
      <target dev='vdb' bus='virtio'/>
      <readonly/>
    </disk>
"""
        domain_type = "kvm" if Path("/dev/kvm").exists() else "qemu"
        return f"""<domain type='{domain_type}'>
  <name>{escaped_name}</name>
  <memory unit='MiB'>{memory_mb}</memory>
  <vcpu>{vcpus}</vcpu>
  <os>
    <type arch='x86_64'{machine_attr}>hvm</type>
    <boot dev='hd'/>
  </os>
  <features>
    <acpi/>
    <apic/>
  </features>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='{escaped_disk}'/>
      <target dev='vda' bus='virtio'/>
    </disk>
{seed_xml}{interface_xml}
    <channel type='unix'>
      <target type='virtio' name='org.qemu.guest_agent.0'/>
    </channel>
    <console type='pty'/>
  </devices>
</domain>"""

    def _destroy_if_present(self, conn, vm_name: str, *, strict: bool = False) -> None:
        try:
            domain = conn.lookupByName(vm_name)
        except self._libvirt.libvirtError as exc:
            if self._is_missing_domain_error(exc):
                return
            raise

        if strict:
            try:
                records = domain_file_references(conn)
                if not any(name == vm_name for name, _, _ in records):
                    raise ValueError("domain missing from ownership inventory")
                owned = [p for name, paths, _ in records if name == vm_name for p in paths]
                others = [p for name, _, paths in records if name != vm_name for p in paths]
                if any(paths_alias(a, b) for a in owned for b in others):
                    raise ValueError("NVRAM path is already assigned to another domain")
            except Exception as exc:
                raise VMDestroyError(f"Cannot verify NVRAM ownership for {vm_name}: {exc}") from exc
            self._destroy_defined_domain_strict(domain, vm_name=vm_name)
            try:
                remaining = domain_file_references(conn)
                if any(name == vm_name for name, _, _ in remaining):
                    raise ValueError("domain remains defined")
                if any(paths_alias(a, b) for a in owned for _, _, paths in remaining for b in paths):
                    raise ValueError("NVRAM remains assigned")
            except Exception as exc:
                raise VMDestroyError(f"Cannot confirm domain {vm_name} released its resources: {exc}") from exc
        else:
            self._cleanup_defined_domain(domain)

    def _destroy_defined_domain_strict(self, domain, *, vm_name: str) -> None:
        try:
            active = bool(domain.isActive())
            if active:
                domain.destroy()
                for _ in range(10):
                    if not domain.isActive():
                        active = False
                        break
                    self._sleep(0.5)
                if active:
                    raise VMDestroyError(f"Domain {vm_name} remained active after destroy")
        except VMDestroyError:
            raise
        except self._libvirt.libvirtError as exc:
            raise VMDestroyError(f"Failed to stop domain {vm_name}: {exc}") from exc

        nvram_error: Exception | None = None
        nvram_flag = getattr(self._libvirt, "VIR_DOMAIN_UNDEFINE_NVRAM", None)
        if nvram_flag is not None and hasattr(domain, "undefineFlags"):
            try:
                domain.undefineFlags(nvram_flag)
                return
            except self._libvirt.libvirtError as exc:
                nvram_error = exc
        try:
            domain.undefine()
        except self._libvirt.libvirtError as exc:
            detail = f"; NVRAM undefine also failed: {nvram_error}" if nvram_error is not None else ""
            raise VMDestroyError(f"Failed to undefine domain {vm_name}: {exc}{detail}") from exc

    def _poll_dhcp_lease_ipv4(
        self,
        conn,
        *,
        network_name: str,
        guest_mac: str,
        domain_name: str | None,
        timeout_seconds: float,
        cancel_requested=None,
    ) -> str:
        network = conn.networkLookupByName(network_name)
        deadline = self._time.monotonic() + timeout_seconds
        guest_mac_lower = guest_mac.lower()

        while True:
            if cancel_requested and cancel_requested():
                raise VMIPAddressCancelledError(
                    f"Cancelled while waiting for IPv4 DHCP lease for {guest_mac} on network {network_name}"
                )

            leases = network.DHCPLeases() or []
            ipv4_address = self._resolve_ipv4_from_leases(guest_mac_lower, leases)
            if ipv4_address is not None:
                return ipv4_address
            if domain_name is not None:
                ipv4_address = self._resolve_ipv4_from_guest_agent(conn, domain_name, guest_mac_lower)
                if ipv4_address is not None:
                    return ipv4_address

            if self._time.monotonic() >= deadline:
                raise VMIPAddressTimeoutError(
                    f"Timed out waiting for IPv4 DHCP lease for {guest_mac} on network {network_name}"
                )
            self._sleep(1.0)

    def _resolve_ipv4_from_leases(self, guest_mac: str, leases: list[dict]) -> str | None:
        for lease in leases:
            if str(lease.get("mac", "")).lower() != guest_mac:
                continue
            ip_address = str(lease.get("ipaddr", ""))
            if "." in ip_address:
                return ip_address
        return None

    def _resolve_ipv4_from_guest_agent(self, conn: Any, domain_name: str, guest_mac: str) -> str | None:
        try:
            domain = conn.lookupByName(domain_name)
            source = self._libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT
            interfaces = domain.interfaceAddresses(source, 0)
        except self._libvirt.libvirtError:
            return None
        if not isinstance(interfaces, dict):
            return None

        ipv4_type = self._libvirt.VIR_IP_ADDR_TYPE_IPV4
        for interface in interfaces.values():
            if str(interface.get("hwaddr", "")).lower() != guest_mac:
                continue
            for address in interface.get("addrs") or []:
                if address.get("type") == ipv4_type and "." in str(address.get("addr", "")):
                    return str(address["addr"])
        return None

    def _cleanup_defined_domain(self, domain) -> None:
        try:
            if domain.isActive():
                domain.destroy()
                # Wait for domain to actually stop
                for _ in range(10):
                    if not domain.isActive():
                        break
                    self._sleep(0.5)
        except self._libvirt.libvirtError:
            pass

        nvram_flag = getattr(self._libvirt, "VIR_DOMAIN_UNDEFINE_NVRAM", None)
        if nvram_flag is not None and hasattr(domain, "undefineFlags"):
            try:
                domain.undefineFlags(nvram_flag)
                return
            except self._libvirt.libvirtError:
                pass
        try:
            domain.undefine()
        except self._libvirt.libvirtError:
            pass

    def _cleanup_boot_artifacts(self, disk_path: Path, seed_iso_path: Path | None) -> None:
        for path in (disk_path, seed_iso_path):
            if path is None:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _is_missing_domain_error(self, error: Exception) -> bool:
        error_code_fn = getattr(error, "get_error_code", None)
        missing_domain_code = getattr(self._libvirt, "VIR_ERR_NO_DOMAIN", None)
        if callable(error_code_fn) and missing_domain_code is not None:
            return error_code_fn() == missing_domain_code
        return False

    def is_missing_interface_error(self, error: Exception) -> bool:
        """Return True when ``error`` indicates the libvirt interface to detach
        is already absent. Matches both VIR_ERR_DEVICE_MISSING and older
        libvirt builds that raise a different code with a
        ``no matching network device`` message.
        """
        if not isinstance(error, BaseException):
            return False
        get_code = getattr(error, "get_error_code", None)
        device_missing_code = getattr(self._libvirt, "VIR_ERR_DEVICE_MISSING", None)
        if callable(get_code) and device_missing_code is not None and get_code() == device_missing_code:
            return True
        get_msg = getattr(error, "get_error_message", None)
        if callable(get_msg):
            msg = get_msg() or ""
            if "no matching network device" in msg or "interface not found" in msg:
                return True
        return False

    def _escape_xml_attr(self, value: str | Path) -> str:
        return escape(str(value), {"'": "&apos;", '"': "&quot;"})

    def attach_nic(self, conn: Any, domain_name: str, network_name: str, mac: str) -> None:
        domain = conn.lookupByName(domain_name)
        safe_mac = escape(mac)
        safe_network_name = escape(network_name)
        xml = f"""
        <interface type='network'>
          <mac address='{safe_mac}'/>
          <source network='{safe_network_name}'/>
          <model type='virtio'/>
        </interface>
        """
        live = getattr(self._libvirt, "VIR_DOMAIN_AFFECT_LIVE", 1)
        config = getattr(self._libvirt, "VIR_DOMAIN_AFFECT_CONFIG", 2)
        domain.attachDeviceFlags(xml, live | config)

    def detach_nic(self, conn: Any, domain_name: str, mac: str) -> None:
        domain = conn.lookupByName(domain_name)
        safe_mac = escape(mac)
        xml = f"""
        <interface type='network'>
          <mac address='{safe_mac}'/>
        </interface>
        """
        live = getattr(self._libvirt, "VIR_DOMAIN_AFFECT_LIVE", 1)
        config = getattr(self._libvirt, "VIR_DOMAIN_AFFECT_CONFIG", 2)
        domain.detachDeviceFlags(xml, live | config)
