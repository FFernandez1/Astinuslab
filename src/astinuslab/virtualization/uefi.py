from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape


@dataclass(frozen=True)
class WindowsFirmwareConfig:
    machine_type: str = "q35"
    loader_code: str = "/usr/share/OVMF/OVMF_CODE_4M.fd"
    nvram_template: str = "/usr/share/OVMF/OVMF_VARS_4M.fd"
    secure_boot: bool = False
    tpm: bool = False


def validate_nvram_path(
    nvram_path: Path,
    active_paths,
    *,
    runtime_dir: Path | None = None,
) -> None:
    """Validate that a runtime NVRAM file is existing, absolute and unshared."""
    if not nvram_path.is_absolute():
        raise ValueError("NVRAM path must be absolute")
    if not nvram_path.exists():
        raise ValueError("NVRAM path does not exist")
    if nvram_path.is_symlink() or not nvram_path.is_file() or nvram_path.stat().st_nlink != 1:
        raise ValueError("NVRAM must be an unaliased regular file")
    if runtime_dir is not None:
        root = runtime_dir.resolve()
        resolved = nvram_path.resolve()
        if resolved.parent != root and root not in resolved.parents:
            raise ValueError("NVRAM path is outside the per-runtime directory")
    resolved = nvram_path.resolve()
    for active_path in active_paths:
        if paths_alias(Path(active_path), resolved):
            raise ValueError("NVRAM path is already assigned")


def paths_alias(first: Path, second: Path) -> bool:
    return first.resolve() == second.resolve() or (first.exists() and second.exists() and first.samefile(second))


def domain_file_references(conn) -> list[tuple[str, set[Path], set[Path]]]:
    """Read live and persistent domain XML; never request secure XML or log it.

    Return domain name, NVRAM paths, and all local source-file paths. Failure
    to enumerate or inspect a domain is not proof that a file is unowned.
    """
    records = []
    try:
        for domain in conn.listAllDomains(0):
            nvram_paths, source_paths = set(), set()
            flags = (0, 2) if domain.isPersistent() else (0,)  # VIR_DOMAIN_XML_INACTIVE = 2
            for flag in flags:
                root = ET.fromstring(domain.XMLDesc(flag))  # noqa: S314 - local libvirt-generated domain XML
                for nvram in root.findall("./os/nvram"):
                    value = (nvram.text or "").strip()
                    source = nvram.find("source")
                    if not value and source is not None:
                        value = source.get("file") or source.get("dev") or ""
                    if not value or not Path(value).is_absolute():
                        raise ValueError("unresolved NVRAM source")
                    nvram_paths.add(Path(value))
                for source in root.findall(".//source"):
                    for attr in ("file", "dev", "dir", "path"):
                        value = source.get(attr) or ""
                        if value and Path(value).is_absolute():
                            source_paths.add(Path(value))
            records.append((domain.name(), nvram_paths, source_paths | nvram_paths))
    except Exception:
        raise RuntimeError("Unable to inspect libvirt domain file ownership") from None
    return records


def assert_runtime_directory_unowned(conn, runtime_dir: Path) -> None:
    root = runtime_dir.resolve()
    for _name, _nvram, paths in domain_file_references(conn):
        for path in paths:
            if path.resolve().is_relative_to(root):
                raise RuntimeError("Runtime directory is still referenced by a libvirt domain")


def render_uefi_os_xml(config: WindowsFirmwareConfig, nvram_path: Path) -> str:
    """Render the Windows-only UEFI portion of a libvirt domain definition."""
    validate_nvram_path(nvram_path, [])
    if config.machine_type != "q35":
        raise ValueError("Windows firmware requires q35")
    if config.secure_boot or config.tpm:
        raise ValueError("Windows reference firmware does not enable Secure Boot or TPM")
    return (
        "<os>\n"
        f"  <type arch='x86_64' machine='{escape(config.machine_type)}'>hvm</type>\n"
        f"  <loader readonly='yes' type='pflash'>{escape(config.loader_code)}</loader>\n"
        f"  <nvram template='{escape(config.nvram_template)}'>{escape(str(nvram_path))}</nvram>\n"
        "  <boot dev='hd'/>\n"
        "  <boot dev='cdrom'/>\n"
        "</os>"
    )
