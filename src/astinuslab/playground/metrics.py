"""Libvirt domain metrics sampler with process-local CPU-delta cache."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Protocol
from xml.etree import ElementTree


class _Conn(Protocol):
    def lookupByName(self, name: str): ...
    def close(self) -> None: ...


def _default_connect():
    import libvirt

    from ..core.config import settings

    conn = libvirt.open(settings.libvirt_uri)
    if conn is None:
        raise RuntimeError(f"could not open libvirt: {settings.libvirt_uri}")
    return conn


class MetricsSampler:
    """Sample CPU% and MEM% for a libvirt domain.

    CPU% is computed from the delta between successive cpu_time (ns) samples
    divided by elapsed wall time (ns) and the number of vCPUs. First call
    returns 0% and primes the cache.
    """

    def __init__(
        self, connect: Callable[[], _Conn] = _default_connect, clock_ns: Callable[[], int] = time.monotonic_ns
    ) -> None:
        self._connect = connect
        self._clock_ns = clock_ns
        # vm_id → (last_cpu_time_ns, last_wall_ns)
        self._last: dict[str, tuple[int, int]] = {}
        self._conn: _Conn | None = None
        self._lock = threading.Lock()

    def _ensure_conn(self) -> _Conn:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def _reset_conn(self) -> None:
        conn = self._conn
        self._conn = None
        if conn is not None:
            close = getattr(conn, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()

    def sample(self, vm_id: str, *, domain_name: str) -> dict:
        with self._lock:
            return self._sample_locked(vm_id, domain_name=domain_name)

    def _sample_locked(self, vm_id: str, *, domain_name: str) -> dict:
        try:
            conn = self._ensure_conn()
            dom = conn.lookupByName(domain_name)
            running = bool(dom.isActive())
            info = dom.info()
            mem_total_kb = int(info[1])
            mem_total_mb = mem_total_kb // 1024

            if not running:
                self._last.pop(vm_id, None)
                return {
                    "vm_id": vm_id,
                    "state": "stopped",
                    "cpu_pct": 0.0,
                    "mem_pct": 0.0,
                    "mem_used_mb": 0,
                    "mem_total_mb": mem_total_mb,
                    "rx_bytes": {},
                    "tx_bytes": {},
                    "sampled_at": datetime.now(UTC),
                }

            vcpus = max(1, int(info[3]))
            cpu_time_ns = int(dom.getCPUStats(True)[0]["cpu_time"])
            wall_ns = self._clock_ns()

            cpu_pct = 0.0
            prev = self._last.get(vm_id)
            if prev is not None:
                d_cpu = cpu_time_ns - prev[0]
                d_wall = wall_ns - prev[1]
                if d_wall > 0:
                    cpu_pct = max(0.0, min(100.0, (d_cpu / d_wall) / vcpus * 100.0))
            self._last[vm_id] = (cpu_time_ns, wall_ns)

            mem_used_kb = self._mem_used_kb(dom, mem_total_kb)
            mem_used_mb = mem_used_kb // 1024
            mem_pct = (mem_used_kb / mem_total_kb * 100.0) if mem_total_kb else 0.0
            rx_bytes, tx_bytes = self._network_bytes(dom)

            return {
                "vm_id": vm_id,
                "state": "running",
                "cpu_pct": round(cpu_pct, 1),
                "mem_pct": round(mem_pct, 1),
                "mem_used_mb": mem_used_mb,
                "mem_total_mb": mem_total_mb,
                "rx_bytes": rx_bytes,
                "tx_bytes": tx_bytes,
                "sampled_at": datetime.now(UTC),
            }
        except Exception as exc:
            self._reset_conn()
            raise exc

    @staticmethod
    def _mem_used_kb(dom, mem_total_kb: int) -> int:
        try:
            stats = dom.memoryStats()
        except Exception as exc:
            _ = exc
            return 0
        if "available" in stats and "unused" in stats:
            return max(0, int(stats["available"]) - int(stats["unused"]))
        if "rss" in stats:
            return int(stats["rss"])
        if "actual" in stats:
            return int(stats["actual"])
        return 0

    @staticmethod
    def _network_bytes(dom) -> tuple[dict[str, int], dict[str, int]]:
        try:
            root = ElementTree.fromstring(dom.XMLDesc())  # noqa: S314  # parses local libvirt domain XML
            interfaces = root.findall(".//interface")
        except Exception as exc:
            _ = exc
            return {}, {}

        rx_bytes: dict[str, int] = {}
        tx_bytes: dict[str, int] = {}
        try:
            for iface in interfaces:
                mac = iface.find("mac")
                target = iface.find("target")
                mac_address = mac.get("address") if mac is not None else None
                target_dev = target.get("dev") if target is not None else None
                if not mac_address or not target_dev:
                    continue
                stats = dom.interfaceStats(target_dev)
                rx_bytes[mac_address] = int(stats[0])
                tx_bytes[mac_address] = int(stats[4])
        except Exception as exc:
            _ = exc
            return {}, {}
        return rx_bytes, tx_bytes


# Process-global sampler used by the API endpoint.
_sampler: MetricsSampler | None = None


def get_sampler() -> MetricsSampler:
    global _sampler
    if _sampler is None:
        _sampler = MetricsSampler()
    return _sampler
