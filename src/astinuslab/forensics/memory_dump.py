from __future__ import annotations

import logging
import struct
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

_ELF_MAGIC = b"\x7fELF"
_PT_LOAD = 1
_PN_XNUM = 0xFFFF


def _elf_required_size(f, ident: bytes, dump_path: Path) -> int:
    """Return the minimum file size implied by the ELF PT_LOAD segments.

    *f* is an open binary handle positioned right after the 16-byte e_ident
    block already read into *ident*. Raises RuntimeError on a structurally
    unreadable header or when no PT_LOAD segment exists.
    """
    ei_class, ei_data = ident[4], ident[5]
    if ei_class == 2:  # ELFCLASS64
        ehdr_fmt, ehdr_size = "HHIQQQIHHHHHH", 64
        phdr_fmt, phdr_size = "IIQQQQQQ", 56
        p_offset_idx, p_filesz_idx = 2, 5
        sh_info_offset, shdr_min_size = 44, 48
    elif ei_class == 1:  # ELFCLASS32
        ehdr_fmt, ehdr_size = "HHIIIIIHHHHHH", 52
        phdr_fmt, phdr_size = "IIIIIIII", 32
        p_offset_idx, p_filesz_idx = 1, 4
        sh_info_offset, shdr_min_size = 28, 32
    else:
        raise RuntimeError(f"Artifact rejected: unsupported ELF class {ei_class} in {dump_path}")

    if ei_data == 1:
        endian = "<"
    elif ei_data == 2:
        endian = ">"
    else:
        raise RuntimeError(f"Artifact rejected: unsupported ELF data encoding {ei_data} in {dump_path}")

    rest = f.read(ehdr_size - 16)
    if len(rest) < ehdr_size - 16:
        raise RuntimeError(f"Artifact rejected: truncated ELF header in {dump_path}")
    fields = struct.unpack(endian + ehdr_fmt, rest)
    e_phoff, e_shoff = fields[4], fields[5]
    e_phentsize, e_phnum = fields[8], fields[9]

    if e_phnum == _PN_XNUM:
        # Real program header count lives in section header 0's sh_info.
        f.seek(e_shoff)
        shdr0 = f.read(shdr_min_size)
        if len(shdr0) < shdr_min_size:
            raise RuntimeError(f"Artifact rejected: truncated section header table in {dump_path}")
        (e_phnum,) = struct.unpack_from(endian + "I", shdr0, sh_info_offset)

    if e_phnum == 0 or e_phoff == 0:
        raise RuntimeError(f"Artifact rejected: ELF dump has no program headers in {dump_path}")
    if e_phentsize < phdr_size:
        raise RuntimeError(f"Artifact rejected: ELF program header entry size {e_phentsize} too small in {dump_path}")

    f.seek(e_phoff)
    table = f.read(e_phnum * e_phentsize)
    if len(table) < e_phnum * e_phentsize:
        raise RuntimeError(f"Artifact rejected: truncated ELF program header table in {dump_path}")

    required = 0
    load_segments = 0
    for i in range(e_phnum):
        entry = struct.unpack_from(endian + phdr_fmt, table, i * e_phentsize)
        if entry[0] != _PT_LOAD:
            continue
        load_segments += 1
        end = entry[p_offset_idx] + entry[p_filesz_idx]
        required = max(required, end)
    if load_segments == 0:
        raise RuntimeError(f"Artifact rejected: ELF dump has no PT_LOAD segments in {dump_path}")
    return required


class MemoryDumper:
    """Host-side memory acquisition via virDomainCoreDump.

    The caller is responsible for opening and closing the libvirt connection.
    """

    def __init__(self, *, libvirt_module):
        self._libvirt = libvirt_module

    def dump(
        self,
        conn,
        vm_name: str,
        output_path: Path,
        *,
        cancel_check: Callable[[], bool] = lambda: False,
    ) -> None:
        """Write an ELF core dump of vm_name's RAM to output_path.

        Uses VIR_DUMP_MEMORY_ONLY — RAM PT_LOAD segments only, no CPU state.
        The domain is suspended by libvirt during the dump and resumed in the
        finally block even if coreDump raises.
        """
        if cancel_check():
            raise RuntimeError("cancel_requested")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        dom = conn.lookupByName(vm_name)
        try:
            dom.coreDump(str(output_path), self._libvirt.VIR_DUMP_MEMORY_ONLY)
        finally:
            # libvirt auto-resumes the domain after a non-crash coreDump, so an
            # unconditional resume() raises "domain is not paused" on every normal
            # capture. Resume only when the domain is still paused; if the state
            # probe itself fails, fall back to attempting the resume.
            try:
                state, _reason = dom.state()
                needs_resume = state == self._libvirt.VIR_DOMAIN_PAUSED
            except Exception:
                needs_resume = True
            if needs_resume:
                try:
                    dom.resume()
                except Exception as exc:
                    logger.warning("Failed to resume domain %s after coreDump: %s", vm_name, exc)

    def validate(self, dump_path: Path) -> None:
        """Raise RuntimeError if dump_path is not a valid, complete ELF dump.

        Beyond the magic bytes, parses the program headers and requires the
        file to contain every PT_LOAD segment in full, so dumps truncated
        mid-write are rejected at capture time instead of surfacing later as
        partial analysis results.
        """
        if not dump_path.exists() or dump_path.stat().st_size == 0:
            raise RuntimeError(f"Artifact rejected: zero-length or missing dump: {dump_path}")
        with dump_path.open("rb") as f:
            ident = f.read(16)
            magic = ident[:4]
            if magic != _ELF_MAGIC:
                raise RuntimeError(f"Artifact rejected: unrecognized dump format (magic={magic!r}) in {dump_path}")
            if len(ident) < 16:
                raise RuntimeError(f"Artifact rejected: truncated ELF header in {dump_path}")
            required = _elf_required_size(f, ident, dump_path)
        file_size = dump_path.stat().st_size
        if file_size < required:
            raise RuntimeError(
                f"Artifact rejected: truncated dump: {dump_path} is {file_size} bytes "
                f"but its PT_LOAD segments extend to {required} bytes"
            )
