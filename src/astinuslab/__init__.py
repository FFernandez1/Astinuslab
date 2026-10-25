from __future__ import annotations

import sys
import time
import types
import zlib
from xml.etree import ElementTree

# Synthetic load the double reports. Fixed fractions rather than random
# values so a test can assert a range instead of merely "not None".
_FAKE_CPU_UTILISATION = 0.25
_FAKE_MEMORY_USED_FRACTION = 0.40
_FAKE_MEMORY_TOTAL_KB = 2048 * 1024
_FAKE_RX_BYTES_PER_SECOND = 4096
_FAKE_TX_BYTES_PER_SECOND = 2048
_FAKE_EPOCH_NS = time.monotonic_ns()


def _elapsed_ns() -> int:
    # Always positive: the sampler divides by the wall-clock delta.
    return max(1, time.monotonic_ns() - _FAKE_EPOCH_NS)


class _FakeDomain:
    """Read-only observability double for libvirt.virDomain.

    It answers the metrics sampler (isActive/info/getCPUStats/memoryStats/
    interfaceStats/XMLDesc) with fabricated but self-consistent numbers, so
    fake-mode runs and the CI tiers exercise the same code path the real
    hypervisor drives.

    It deliberately has NO coreDump: reading a metric is safe to fake,
    producing forensic evidence is not. test_fake_libvirt_guard.py asserts
    that absence — a memory dump under the fake must fail loudly rather than
    write a forged artifact. Keep new methods on the observability side of
    that line.
    """

    def __init__(self, name: str = "astinuslab-e2e", *, memory_kb: int = _FAKE_MEMORY_TOTAL_KB, vcpus: int = 1):
        self._name = name
        self._memory_kb = memory_kb
        self._vcpus = vcpus
        self._active = True
        # Stable per-name MAC so two domains never share network counters.
        # crc32, not hash(): str hashing is salted per process, and a double
        # that changes identity between runs is a flake waiting to happen.
        digest = zlib.crc32(name.encode("utf-8")) % (256**3)
        self._mac = f"52:54:00:{digest >> 16 & 0xFF:02x}:{digest >> 8 & 0xFF:02x}:{digest & 0xFF:02x}"
        self._target_dev = f"vnet{digest % 100}"

    def name(self):
        return self._name

    def create(self):
        self._active = True
        return 0

    def destroy(self):
        self._active = False
        return 0

    def undefine(self):
        return 0

    def isActive(self):
        return 1 if self._active else 0

    def _cpu_time_ns(self) -> int:
        # Burn CPU proportionally to elapsed wall time, so the sampler's
        # delta lands on `_FAKE_CPU_UTILISATION` regardless of sampling interval.
        return int(_elapsed_ns() * _FAKE_CPU_UTILISATION * self._vcpus)

    def info(self):
        # libvirt virDomainInfo: [state, maxMem_kb, memory_kb, nrVirtCpu, cpuTime_ns]
        state = 1 if self._active else 5
        return [state, self._memory_kb, self._memory_kb, self._vcpus, self._cpu_time_ns()]

    def getCPUStats(self, _total):
        return [{"cpu_time": self._cpu_time_ns()}]

    def memoryStats(self):
        unused = int(self._memory_kb * (1.0 - _FAKE_MEMORY_USED_FRACTION))
        return {"available": self._memory_kb, "unused": unused}

    def interfaceStats(self, _dev):
        seconds = _elapsed_ns() / 1_000_000_000
        rx = int(seconds * _FAKE_RX_BYTES_PER_SECOND)
        tx = int(seconds * _FAKE_TX_BYTES_PER_SECOND)
        # (rx_bytes, rx_pkts, rx_errs, rx_drop, tx_bytes, tx_pkts, tx_errs, tx_drop)
        return (rx, rx // 512, 0, 0, tx, tx // 512, 0, 0)

    def XMLDesc(self, _flags=0):
        return (
            f"<domain><name>{self._name}</name><devices>"
            f'<interface type="network">'
            f'<mac address="{self._mac}"/>'
            f'<target dev="{self._target_dev}"/>'
            f"</interface></devices></domain>"
        )


def _install_fake_libvirt() -> None:
    libvirt = types.ModuleType("libvirt")
    missing_network_error = 43

    class libvirtError(Exception):
        def __init__(self, message: str = "", code: int | None = None):
            super().__init__(message)
            self._error_code = code

        def get_error_code(self):
            return self._error_code

    domain_registry: dict[str, _FakeDomain] = {}

    def _domain(name: str) -> _FakeDomain:
        """One object per name, so destroy() is observable across lookups."""
        domain = domain_registry.get(name)
        if domain is None:
            domain = _FakeDomain(name)
            domain_registry[name] = domain
        return domain

    class _FakeNetwork:
        def __init__(self, name: str, xml: str):
            self._name = name
            self._xml = xml
            self._active = False
            self._autostart = False
            self._defined = True

        def name(self):
            return self._name

        def create(self):
            self._active = True
            return 0

        def setAutostart(self, enabled):
            self._autostart = bool(enabled)
            return 0

        def isActive(self):
            return self._active

        def destroy(self):
            self._active = False
            return 0

        def undefine(self):
            if network_registry.get(self._name) is self:
                del network_registry[self._name]
            self._defined = False
            return 0

        def XMLDesc(self, _flags=0):
            return self._xml

        def DHCPLeases(self):
            return [{"mac": "52:54:00:00:00:01", "ipaddr": "10.42.1.10"}]

    network_registry: dict[str, _FakeNetwork] = {}

    class _FakeConnection:
        def defineXML(self, xml):
            try:
                root = ElementTree.fromstring(xml)  # noqa: S314 - local generated libvirt XML
                name = (root.findtext("name") or "").strip()
            except ElementTree.ParseError:
                name = ""
            return _domain(name) if name else _FakeDomain()

        def networkDefineXML(self, xml):
            try:
                root = ElementTree.fromstring(xml)  # noqa: S314 - local generated libvirt XML
            except ElementTree.ParseError as exc:
                raise libvirtError("invalid network XML") from exc
            name = root.findtext("name")
            if not name or not name.strip():
                raise libvirtError("network XML is missing a name")
            network = _FakeNetwork(name.strip(), xml)
            network_registry[network.name()] = network
            return network

        def lookupByName(self, name):
            return _domain(name)

        def networkLookupByName(self, name):
            try:
                return network_registry[name]
            except KeyError as exc:
                raise libvirtError(f"network {name} not found", code=missing_network_error) from exc

        def listAllNetworks(self):
            return list(network_registry.values())

        def close(self):
            return 0

    libvirt.open = lambda _uri: _FakeConnection()
    libvirt.openReadOnly = lambda _uri: _FakeConnection()
    libvirt.registerErrorHandler = lambda *args, **kwargs: None
    libvirt.VIR_DUMP_MEMORY_ONLY = 1
    libvirt.VIR_DOMAIN_PAUSED = 3
    libvirt.VIR_ERR_NO_NETWORK = missing_network_error
    libvirt.libvirtError = libvirtError
    libvirt.virConnect = _FakeConnection
    libvirt.virDomain = _FakeDomain
    libvirt.virNetwork = _FakeNetwork
    # Force-replace (not setdefault): on CI a real libvirt may already be
    # imported before this runs, and setdefault would leave the daemon-bound
    # module in place. When the env flag is set the fake must always win.
    sys.modules["libvirt"] = libvirt


from .core.fake_mode import FAKE_LIBVIRT, fake_mode_banner  # noqa: E402

if FAKE_LIBVIRT:
    _install_fake_libvirt()

_banner = fake_mode_banner()
if _banner is not None:
    # logging is not configured yet at package-import time, so write to stderr
    # directly.  configure_logging() runs later in main.py / celery_main.py.
    print(f"astinuslab: {_banner}", file=sys.stderr)
