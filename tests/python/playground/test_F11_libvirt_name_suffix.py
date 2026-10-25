import re

from astinuslab.playground.orchestrator import libvirt_net_name


def test_F11_libvirt_name_includes_short_hash_suffix():
    name_a = libvirt_net_name("pg-1234", "lan")
    name_b = libvirt_net_name("pg-1234", "lan")
    # Must be deterministic for a given (job_id, name).
    assert name_a == name_b
    assert re.search(r"-[0-9a-f]{4,}$", name_a)
