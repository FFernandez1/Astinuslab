"""Unit tests for MemoryDumper — Phase 3 native dump."""

from __future__ import annotations

import struct
from unittest.mock import MagicMock

import pytest


class _FakeLibvirt:
    VIR_DUMP_MEMORY_ONLY = 8
    VIR_DOMAIN_RUNNING = 1
    VIR_DOMAIN_PAUSED = 3

    class libvirtError(Exception):
        pass


def _make_dumper():
    from astinuslab.forensics.memory_dump import MemoryDumper

    return MemoryDumper(libvirt_module=_FakeLibvirt())


def _write_elf64(path, *, segments, truncate_to=None):
    """Write a minimal little-endian ELF64 with the given PT_LOAD segments."""
    phnum = len(segments)
    ehdr = b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\x00" * 8
    ehdr += struct.pack(
        "<HHIQQQIHHHHHH",
        4,  # e_type ET_CORE
        62,  # e_machine EM_X86_64
        1,  # e_version
        0,  # e_entry
        64,  # e_phoff (right after the ELF header)
        0,  # e_shoff
        0,  # e_flags
        64,  # e_ehsize
        56,  # e_phentsize
        phnum,  # e_phnum
        0,
        0,
        0,  # e_shentsize, e_shnum, e_shstrndx
    )
    phdrs = b"".join(struct.pack("<IIQQQQQQ", 1, 0, off, 0, 0, size, size, 0) for off, size in segments)
    end = max((off + size for off, size in segments), default=64 + len(phdrs))
    blob = (ehdr + phdrs).ljust(end, b"\x00")
    if truncate_to is not None:
        blob = blob[:truncate_to]
    path.write_bytes(blob)


def test_dump_calls_core_dump_with_correct_args(tmp_path):
    dumper = _make_dumper()
    dom = MagicMock()
    conn = MagicMock()
    conn.lookupByName.return_value = dom
    output = tmp_path / "memory.dump"

    dumper.dump(conn, "astinuslab-job-abc", output)

    conn.lookupByName.assert_called_once_with("astinuslab-job-abc")
    dom.coreDump.assert_called_once_with(str(output), _FakeLibvirt.VIR_DUMP_MEMORY_ONLY)


def test_dump_raises_on_cancel_before_core_dump(tmp_path):
    dumper = _make_dumper()
    dom = MagicMock()
    conn = MagicMock()
    conn.lookupByName.return_value = dom
    output = tmp_path / "memory.dump"

    with pytest.raises(RuntimeError, match="cancel_requested"):
        dumper.dump(conn, "vm-name", output, cancel_check=lambda: True)

    dom.coreDump.assert_not_called()


def test_dump_resumes_domain_even_if_core_dump_fails(tmp_path):
    dumper = _make_dumper()
    dom = MagicMock()
    dom.coreDump.side_effect = RuntimeError("dump failed")
    conn = MagicMock()
    conn.lookupByName.return_value = dom
    output = tmp_path / "memory.dump"

    with pytest.raises(RuntimeError, match="dump failed"):
        dumper.dump(conn, "vm-name", output)

    dom.resume.assert_called_once()


def test_dump_skips_resume_when_domain_running(tmp_path):
    dumper = _make_dumper()
    dom = MagicMock()
    dom.state.return_value = (_FakeLibvirt.VIR_DOMAIN_RUNNING, 1)
    conn = MagicMock()
    conn.lookupByName.return_value = dom

    dumper.dump(conn, "vm-name", tmp_path / "memory.dump")

    dom.resume.assert_not_called()


def test_dump_resumes_when_domain_still_paused(tmp_path):
    dumper = _make_dumper()
    dom = MagicMock()
    dom.state.return_value = (_FakeLibvirt.VIR_DOMAIN_PAUSED, 1)
    conn = MagicMock()
    conn.lookupByName.return_value = dom

    dumper.dump(conn, "vm-name", tmp_path / "memory.dump")

    dom.resume.assert_called_once()


def test_validate_accepts_complete_elf(tmp_path):
    dumper = _make_dumper()
    p = tmp_path / "memory.dump"
    _write_elf64(p, segments=[(0x200, 0x100), (0x400, 0x80)])
    dumper.validate(p)  # must not raise


def test_validate_rejects_truncated_pt_load(tmp_path):
    dumper = _make_dumper()
    p = tmp_path / "memory.dump"
    _write_elf64(p, segments=[(0x200, 0x100)], truncate_to=0x250)
    with pytest.raises(RuntimeError, match="truncated dump"):
        dumper.validate(p)


def test_validate_rejects_elf_without_pt_load(tmp_path):
    dumper = _make_dumper()
    p = tmp_path / "memory.dump"
    _write_elf64(p, segments=[])
    with pytest.raises(RuntimeError, match="no program headers"):
        dumper.validate(p)


def test_validate_rejects_magic_only_file(tmp_path):
    # The old validator accepted exactly this shape (B01 finding).
    dumper = _make_dumper()
    p = tmp_path / "memory.dump"
    p.write_bytes(b"\x7fELF" + b"\x00" * 100)
    with pytest.raises(RuntimeError, match="unsupported ELF class"):
        dumper.validate(p)


def test_validate_rejects_truncated_phdr_table(tmp_path):
    dumper = _make_dumper()
    p = tmp_path / "memory.dump"
    _write_elf64(p, segments=[(0x200, 0x100)], truncate_to=70)  # mid-phdr
    with pytest.raises(RuntimeError, match="truncated ELF program header table"):
        dumper.validate(p)


def test_validate_rejects_truncated_elf_header(tmp_path):
    dumper = _make_dumper()
    p = tmp_path / "memory.dump"
    p.write_bytes(b"\x7fELF")
    with pytest.raises(RuntimeError, match="truncated ELF header"):
        dumper.validate(p)


def test_validate_rejects_zero_length(tmp_path):
    dumper = _make_dumper()
    p = tmp_path / "memory.dump"
    p.write_bytes(b"")
    with pytest.raises(RuntimeError, match="zero-length"):
        dumper.validate(p)


def test_validate_rejects_unknown_magic(tmp_path):
    dumper = _make_dumper()
    p = tmp_path / "memory.dump"
    p.write_bytes(b"\xde\xad\xbe\xef" + b"\x00" * 100)
    with pytest.raises(RuntimeError, match="unrecognized dump format"):
        dumper.validate(p)


def test_dump_creates_output_parent_directory(tmp_path):
    dumper = _make_dumper()
    dom = MagicMock()
    conn = MagicMock()
    conn.lookupByName.return_value = dom
    nested = tmp_path / "deep" / "dir" / "memory.dump"
    assert not nested.parent.exists()

    dumper.dump(conn, "vm-name", nested)

    assert nested.parent.exists()
