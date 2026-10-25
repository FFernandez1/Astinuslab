from __future__ import annotations

from astinuslab.playground.metrics import MetricsSampler
from tests.python._support.helpers import autospec_setattr


class FakeDomain:
    """Minimal stand-in for libvirt.virDomain."""

    def __init__(
        self,
        *,
        running=True,
        cpu_time_ns=0,
        vcpus=1,
        mem_total_kb=2048 * 1024,
        mem_unused_kb=1024 * 1024,
        xml_desc=None,
        interface_stats=None,
    ):
        self._running = running
        self._cpu_time_ns = cpu_time_ns
        self._vcpus = vcpus
        self._mem_total_kb = mem_total_kb
        self._mem_unused_kb = mem_unused_kb
        self._xml_desc = xml_desc
        self._interface_stats = interface_stats or {}
        self.interface_stats_calls = []

    def isActive(self):
        return 1 if self._running else 0

    def info(self):
        # [state, max_mem_kb, mem_kb, vcpus, cpu_time_ns]
        return [1 if self._running else 5, self._mem_total_kb, self._mem_total_kb, self._vcpus, self._cpu_time_ns]

    def getCPUStats(self, total):
        return [{"cpu_time": self._cpu_time_ns}]

    def memoryStats(self):
        return {"available": self._mem_total_kb, "unused": self._mem_unused_kb}

    def XMLDesc(self):
        if isinstance(self._xml_desc, Exception):
            raise self._xml_desc
        if self._xml_desc is None:
            raise RuntimeError("XMLDesc unavailable")
        return self._xml_desc

    def interfaceStats(self, dev):
        self.interface_stats_calls.append(dev)
        if dev not in self._interface_stats:
            raise RuntimeError("interfaceStats unavailable")
        return self._interface_stats[dev]


class FakeConn:
    def __init__(self, dom):
        self._dom = dom
        self.closed = False

    def lookupByName(self, name):
        return self._dom

    def close(self):
        self.closed = True


def test_sampler_reuses_libvirt_connection_between_samples():
    dom = FakeDomain(cpu_time_ns=1_000_000_000)
    connections = []

    def connect():
        conn = FakeConn(dom)
        connections.append(conn)
        return conn

    sampler = MetricsSampler(connect=connect, clock_ns=lambda: 0)

    sampler.sample("vm1", domain_name="d")
    sampler.sample("vm2", domain_name="d")

    assert len(connections) == 1
    assert connections[0].closed is False


def test_first_sample_returns_zero_cpu():
    dom = FakeDomain(cpu_time_ns=1_000_000_000, vcpus=2)
    sampler = MetricsSampler(connect=lambda: FakeConn(dom), clock_ns=lambda: 0)
    m = sampler.sample("vm1", domain_name="d")
    assert m["cpu_pct"] == 0.0
    assert m["mem_total_mb"] == 2048
    # 1024 MiB unused on 2048 MiB total → 50% used
    assert 49 <= m["mem_pct"] <= 51


def test_second_sample_returns_cpu_delta():
    # 1s wall, 0.5s CPU on 2 vcpus → 25%
    dom = FakeDomain(cpu_time_ns=0, vcpus=2)
    clock = {"v": 0}
    sampler = MetricsSampler(connect=lambda: FakeConn(dom), clock_ns=lambda: clock["v"])
    sampler.sample("vm1", domain_name="d")  # prime
    dom._cpu_time_ns = 500_000_000
    clock["v"] = 1_000_000_000
    m = sampler.sample("vm1", domain_name="d")
    assert 24.0 <= m["cpu_pct"] <= 26.0


def test_non_running_domain_returns_zeros():
    dom = FakeDomain(running=False)
    sampler = MetricsSampler(connect=lambda: FakeConn(dom), clock_ns=lambda: 0)
    m = sampler.sample("vm1", domain_name="d")
    assert m["cpu_pct"] == 0.0
    assert m["mem_pct"] == 0.0
    assert m["state"] == "stopped"


def test_cpu_pct_clamped_to_100():
    dom = FakeDomain(cpu_time_ns=0, vcpus=1)
    clock = {"v": 0}
    sampler = MetricsSampler(connect=lambda: FakeConn(dom), clock_ns=lambda: clock["v"])
    sampler.sample("vm1", domain_name="d")
    dom._cpu_time_ns = 5_000_000_000  # 5s of cpu in 1s wall
    clock["v"] = 1_000_000_000
    m = sampler.sample("vm1", domain_name="d")
    assert m["cpu_pct"] == 100.0


def test_memory_stats_fallback_when_unused_missing():
    class DomNoUnused(FakeDomain):
        def memoryStats(self):
            return {"rss": 768 * 1024}  # 768 MiB rss

    dom = DomNoUnused()
    sampler = MetricsSampler(connect=lambda: FakeConn(dom), clock_ns=lambda: 0)
    m = sampler.sample("vm1", domain_name="d")
    # 768 / 2048 ≈ 37.5%
    assert 37.0 <= m["mem_pct"] <= 38.0


def test_network_stats_are_keyed_by_mac_and_sampled_by_target_dev():
    xml = """
    <domain>
      <devices>
        <interface type="network">
          <mac address="52:54:00:aa:bb:cc"/>
          <target dev="vnet0"/>
        </interface>
      </devices>
    </domain>
    """
    dom = FakeDomain(
        xml_desc=xml,
        interface_stats={"vnet0": (1234, 5, 0, 0, 5678, 9, 0, 0)},
    )
    sampler = MetricsSampler(connect=lambda: FakeConn(dom), clock_ns=lambda: 0)

    m = sampler.sample("vm1", domain_name="d")

    assert m["rx_bytes"] == {"52:54:00:aa:bb:cc": 1234}
    assert m["tx_bytes"] == {"52:54:00:aa:bb:cc": 5678}
    assert dom.interface_stats_calls == ["vnet0"]


def test_network_stats_are_empty_when_xml_desc_is_unavailable():
    dom = FakeDomain(xml_desc=RuntimeError("no xml"))
    sampler = MetricsSampler(connect=lambda: FakeConn(dom), clock_ns=lambda: 0)

    m = sampler.sample("vm1", domain_name="d")

    assert m["rx_bytes"] == {}
    assert m["tx_bytes"] == {}
    assert dom.interface_stats_calls == []


from datetime import UTC, datetime

from fastapi.testclient import TestClient


def test_metrics_endpoint_returns_payload(in_memory_session_scope, monkeypatch):
    from astinuslab import db as db_mod
    from astinuslab.db.models import PlaygroundSession
    from astinuslab.main import app
    from astinuslab.playground import metrics as playground_metrics
    from astinuslab.playground import repositories as pr

    class FakeSampler:
        def sample(self, vm_id, *, domain_name):
            return {
                "vm_id": vm_id,
                "state": "running",
                "cpu_pct": 42.5,
                "mem_pct": 33.3,
                "mem_used_mb": 682,
                "mem_total_mb": 2048,
                "sampled_at": datetime.now(UTC),
            }

    autospec_setattr(monkeypatch, playground_metrics, "get_sampler", lambda: FakeSampler())
    monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)

    with in_memory_session_scope() as s:
        j = PlaygroundSession(
            id="pg-z",
            status="running",
            created_at=datetime.now(UTC),
            started_at=datetime.now(UTC),
        )
        s.add(j)
        s.commit()
        pr.register_vm(
            s,
            vm_id="v9",
            job_id="pg-z",
            label="t",
            image_id="ubuntu-22-04-base",
            domain_name="astinuslab-pg-pg-z-t",
            disk_path="/tmp/d.qcow2",
            role="target",
        )

    client = TestClient(app)
    res = client.get("/api/v1/playground/vms/v9/metrics")
    assert res.status_code == 200
    body = res.json()
    assert body["cpu_pct"] == 42.5
    assert body["mem_total_mb"] == 2048


def test_metrics_endpoint_404_for_unknown_vm(in_memory_session_scope, monkeypatch):
    from astinuslab import db as db_mod
    from astinuslab.main import app

    monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)

    client = TestClient(app)
    res = client.get("/api/v1/playground/vms/does-not-exist/metrics")
    assert res.status_code == 404


def test_sample_serializes_concurrent_callers():
    import threading
    import time

    inflight = 0
    max_inflight = 0
    guard = threading.Lock()

    class FakeDom:
        def isActive(self):
            nonlocal inflight, max_inflight
            with guard:
                inflight += 1
                max_inflight = max(max_inflight, inflight)
            time.sleep(0.02)
            with guard:
                inflight -= 1
            return False

        def info(self):
            return [0, 1024, 0, 1, 0]

    class FakeConn:
        def lookupByName(self, name):
            return FakeDom()

        def close(self):
            pass

    sampler = MetricsSampler(connect=lambda: FakeConn())
    threads = [threading.Thread(target=lambda: sampler.sample(f"vm-{i}", domain_name="d")) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert max_inflight == 1
