"""The shipped fake libvirt double must satisfy the metrics sampler.

test_metrics.py exercises the sampler against a purpose-built stand-in. This
file exercises it against the double the app actually installs under
ASTINUSLAB_FAKE_LIBVIRT, because that is the one the CI tiers and a fake-mode
dev run go through. Without it the batch endpoint logged
"'_FakeDomain' object has no attribute 'isActive'" for every VM and the UI drew
no load bars at all.
"""

from __future__ import annotations

import sys
import time

import pytest

from astinuslab.playground.metrics import MetricsSampler

DOMAIN = "astinuslab-pg-pg-test-web-1"


@pytest.fixture
def fake_libvirt():
    """Install the shipped double and hand back the module."""
    from astinuslab import _install_fake_libvirt

    saved = sys.modules.get("libvirt")
    _install_fake_libvirt()
    try:
        yield sys.modules["libvirt"]
    finally:
        if saved is not None:
            sys.modules["libvirt"] = saved
        else:
            sys.modules.pop("libvirt", None)


def _sampler(fake_libvirt):
    return MetricsSampler(connect=lambda: fake_libvirt.open("qemu:///system"))


def test_sampler_runs_against_the_shipped_double(fake_libvirt):
    sample = _sampler(fake_libvirt).sample("vm-1", domain_name=DOMAIN)

    assert sample["state"] == "running"
    assert sample["cpu_pct"] == 0.0  # first sample primes the delta cache
    assert sample["mem_total_mb"] > 0
    assert 0 < sample["mem_pct"] < 100
    assert sample["mem_used_mb"] > 0


def test_second_sample_reports_a_plausible_cpu_load(fake_libvirt):
    sampler = _sampler(fake_libvirt)
    sampler.sample("vm-1", domain_name=DOMAIN)
    sample = sampler.sample("vm-1", domain_name=DOMAIN)

    # The double burns CPU time proportionally to elapsed wall time, so the
    # percentage is stable rather than an artefact of how fast the test ran.
    assert 15.0 < sample["cpu_pct"] < 35.0


def test_a_stopped_domain_reports_stopped(fake_libvirt):
    conn = fake_libvirt.open("qemu:///system")
    conn.lookupByName(DOMAIN).destroy()

    sample = _sampler(fake_libvirt).sample("vm-1", domain_name=DOMAIN)

    assert sample["state"] == "stopped"
    assert sample["cpu_pct"] == 0.0
    assert sample["mem_pct"] == 0.0


def test_looking_up_the_same_domain_twice_returns_the_same_domain(fake_libvirt):
    """Otherwise destroy() is unobservable and every lookup is a fresh VM."""
    conn = fake_libvirt.open("qemu:///system")
    assert conn.lookupByName(DOMAIN) is conn.lookupByName(DOMAIN)


def test_network_counters_are_keyed_by_mac_and_advance(fake_libvirt):
    sampler = _sampler(fake_libvirt)
    first = sampler.sample("vm-1", domain_name=DOMAIN)
    # The counters model a byte rate, so they move with elapsed time rather than
    # per call. The page polls every 5s; a few ms is enough to see movement
    # without asserting anything the real hypervisor would not also do.
    time.sleep(0.05)
    second = sampler.sample("vm-1", domain_name=DOMAIN)

    assert first["rx_bytes"], "the double's domain XML must expose an interface"
    mac = next(iter(first["rx_bytes"]))
    assert second["rx_bytes"][mac] > first["rx_bytes"][mac]
    assert second["tx_bytes"][mac] > first["tx_bytes"][mac]


def test_two_domains_do_not_share_an_identity(fake_libvirt):
    conn = fake_libvirt.open("qemu:///system")
    one = conn.lookupByName("astinuslab-pg-pg-test-web-1")
    two = conn.lookupByName("astinuslab-pg-pg-test-web-2")

    assert one is not two
    assert one.name() != two.name()
    # Distinct MACs, so per-VM network counters cannot be confused for each other.
    assert one.XMLDesc() != two.XMLDesc()


def test_the_double_stays_observability_only(fake_libvirt):
    """Reading metrics is safe to fake; producing evidence is not.

    test_fake_libvirt_guard.py depends on this absence: a memory dump under the
    fake must fail loudly rather than forge an artifact. Adding coreDump here to
    make some other test pass would break that guard.
    """
    domain = fake_libvirt.open("qemu:///system").lookupByName(DOMAIN)

    assert not hasattr(domain, "coreDump")
    assert not hasattr(domain, "coreDumpWithFormat")
