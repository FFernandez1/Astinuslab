from __future__ import annotations

import sys

import pytest


def test_memory_dumper_refuses_fake_libvirt_domain(tmp_path):
    """The fake libvirt domain has no coreDump, so a forensics memory dump under
    ASTINUSLAB_FAKE_LIBVIRT fails loudly instead of forging evidence. This locks
    in that accidental safety: adding coreDump to _FakeDomain would break here
    before it could break the evidence chain.
    """
    from astinuslab import _install_fake_libvirt
    from astinuslab.forensics.memory_dump import MemoryDumper

    saved = sys.modules.get("libvirt")
    try:
        _install_fake_libvirt()
        fake_libvirt = sys.modules["libvirt"]
        conn = fake_libvirt.open("qemu:///system")
        dumper = MemoryDumper(libvirt_module=fake_libvirt)
        with pytest.raises(AttributeError):
            dumper.dump(conn, "astinuslab-e2e", tmp_path / "mem.elf")
    finally:
        if saved is not None:
            sys.modules["libvirt"] = saved
        else:
            sys.modules.pop("libvirt", None)
