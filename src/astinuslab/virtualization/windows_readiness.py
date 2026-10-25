"""Windows readiness uses a harmless RPC, never a retried process launch."""

from __future__ import annotations

import json
from importlib import import_module


def probe_windows_agent(libvirt_uri: str, vm_name: str, *, timeout: int) -> bool:
    """Return False only for libvirt's typed agent-unresponsive ping error.

    Each connection is closed before another probe or guest process is allowed.
    No permission or authentication configuration is changed. Guest-exec uses
    its existing transport and must independently succeed without retries.
    """
    if type(timeout) is not int or not 1 <= timeout <= 5:
        raise ValueError("Windows readiness timeout must be 1..5 seconds")
    conn = None
    domain = None
    try:
        libvirt = import_module("libvirt")
        qemu = import_module("libvirt_qemu")
        conn = libvirt.open(libvirt_uri)
        if conn is None:
            raise ValueError("connection")
        domain = conn.lookupByName(vm_name)
        try:
            raw = qemu.qemuAgentCommand(domain, json.dumps({"execute": "guest-ping"}), timeout, 0)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_AGENT_UNRESPONSIVE:
                return False
            raise
        # A ping response has exactly one empty object. Reject duplicate keys,
        # error objects and unexpected output without reproducing guest data.
        if type(raw) is not str or len(raw) > 128:
            raise ValueError("response")

        def unique_object(items):
            value = dict(items)
            if len(value) != len(items):
                raise ValueError("duplicate")
            return value

        value = json.loads(raw, object_pairs_hook=unique_object)
        if value != {"return": {}}:
            raise ValueError("response")
        return True
    except Exception:
        # Leave the exception handler before closing. Its traceback can retain
        # the qemuAgentCommand domain argument and therefore a connection ref.
        # Raising here would keep that reference alive through the finally.
        pass
    finally:
        domain = None
        if conn is not None:
            try:
                if conn.close() != 0:
                    raise ValueError("connection remains referenced")
            except Exception:
                raise RuntimeError(f"Windows agent readiness connection closure failed for {vm_name}") from None
    raise RuntimeError(f"Windows agent readiness failed for {vm_name}") from None
