import weakref
from types import SimpleNamespace

import pytest

from astinuslab.virtualization import windows_runtime


@pytest.mark.parametrize(
    "outcome", ['{"return":{}}', 86, 1, "[]", '{"return":[]}', '{"return":false}', '{"return":{},"return":{}}']
)
def test_readiness_retries_only_typed_unresponsive_ping_and_closes(monkeypatch, outcome):
    from astinuslab.virtualization import windows_readiness as module

    class LibvirtError(Exception):
        def get_error_code(self):
            return self.args[0]

    events = []
    domain = object()

    def close():
        events.append("closed")
        return 0

    conn = SimpleNamespace(lookupByName=lambda name: domain, close=close)

    def command(actual_domain, payload, timeout, flags):
        assert actual_domain is domain
        assert payload == '{"execute": "guest-ping"}'
        assert timeout == 3 and flags == 0
        events.append("ping")
        if type(outcome) is int:
            raise LibvirtError(outcome)
        return outcome

    modules = {
        "libvirt": SimpleNamespace(open=lambda uri: conn, libvirtError=LibvirtError, VIR_ERR_AGENT_UNRESPONSIVE=86),
        "libvirt_qemu": SimpleNamespace(qemuAgentCommand=command),
    }
    monkeypatch.setattr(module, "import_module", modules.__getitem__)
    if outcome in ('{"return":{}}', 86):
        assert module.probe_windows_agent("qemu:///system", "vm", timeout=3) is (outcome != 86)
    else:
        with pytest.raises(RuntimeError, match="readiness"):
            module.probe_windows_agent("qemu:///system", "vm", timeout=3)
    assert events == ["ping", "closed"]


def test_readiness_does_not_succeed_without_connection_closure(monkeypatch):
    from astinuslab.virtualization import windows_readiness as module

    conn = SimpleNamespace(lookupByName=lambda name: object(), close=lambda: 1)
    modules = {
        "libvirt": SimpleNamespace(open=lambda uri: conn, libvirtError=RuntimeError),
        "libvirt_qemu": SimpleNamespace(qemuAgentCommand=lambda *a: '{"return":{}}'),
    }
    monkeypatch.setattr(module, "import_module", modules.__getitem__)
    with pytest.raises(RuntimeError, match="closure"):
        module.probe_windows_agent("qemu:///system", "vm", timeout=3)


@pytest.mark.parametrize("code", [86, 1])
def test_readiness_releases_domain_retained_by_error_traceback_before_close(monkeypatch, code):
    from astinuslab.virtualization import windows_readiness as module

    class Domain:
        pass

    class LibvirtError(Exception):
        def get_error_code(self):
            return code

    domains = []
    closures = []

    def lookup(_):
        domain = Domain()
        domains.append(weakref.ref(domain))
        return domain

    def close():
        alive = domains[0]() is not None
        closures.append(alive)
        return int(alive)

    def command(domain, *_):
        # The traceback frame owns the argument just like libvirt_qemu.py.
        assert domain is domains[0]()
        raise LibvirtError("unavailable")

    modules = {
        "libvirt": SimpleNamespace(
            open=lambda _: SimpleNamespace(lookupByName=lookup, close=close),
            libvirtError=LibvirtError,
            VIR_ERR_AGENT_UNRESPONSIVE=86,
        ),
        "libvirt_qemu": SimpleNamespace(qemuAgentCommand=command),
    }
    monkeypatch.setattr(module, "import_module", modules.__getitem__)
    if code == 86:
        assert module.probe_windows_agent("qemu:///system", "vm", timeout=3) is False
    else:
        with pytest.raises(RuntimeError, match="readiness failed"):
            module.probe_windows_agent("qemu:///system", "vm", timeout=3)
    assert closures == [False]


@pytest.mark.parametrize("stage", ["setup", "identity"])
def test_readiness_outage_never_dispatches_exec_and_shares_wait_deadline(monkeypatch, stage):
    clock = SimpleNamespace(now=0)
    clock.monotonic = lambda: clock.now
    clock.sleep = lambda seconds: setattr(clock, "now", clock.now + seconds)
    pings = []

    def unavailable(*args, **kwargs):
        pings.append(kwargs["timeout"])
        assert kwargs["timeout"] <= 5 - clock.now
        return False

    monkeypatch.setattr(windows_runtime, "probe_windows_agent", unavailable, raising=False)
    monkeypatch.setattr(windows_runtime, "run_guest_exec", lambda *a, **k: pytest.fail("exec before readiness"))
    with pytest.raises(RuntimeError, match="did not complete|timed out"):
        if stage == "setup":
            windows_runtime.wait_for_windows_setup(
                "qemu:///system", "vm", cancel_requested=lambda: False, max_wait_seconds=5, time_module=clock
            )
        else:
            bootstrap = windows_runtime.WindowsBootstrapResult("NAME", "unused", True, "123")
            windows_runtime.wait_for_windows_identity(
                "qemu:///system", "vm", bootstrap, cancel_requested=lambda: False, max_wait_seconds=5, time_module=clock
            )
    assert pings == [5, 3, 1]
    assert clock.now == 5
