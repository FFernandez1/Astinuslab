"""Bounded readiness evaluation for package instances.

Host shell hooks are intentionally absent.  Command probes are rejected by the
portable model/preflight; TCP/HTTP probes are exercised by the VM coordinator.
"""

from __future__ import annotations

import socket
from urllib.request import urlopen

from .models import LabReadinessProbe


class ReadinessError(RuntimeError):
    pass


def probe_ready(host: str, probe: LabReadinessProbe) -> None:
    if probe.kind == "tcp":
        if probe.port is None:
            raise ReadinessError("TCP readiness probe lacks a port")
        with socket.create_connection((host, probe.port), timeout=probe.timeout_seconds):
            return
    if probe.kind == "http":
        if probe.port is None or probe.path is None:
            raise ReadinessError("HTTP readiness probe lacks a port or path")
        with urlopen(f"http://{host}:{probe.port}{probe.path}", timeout=probe.timeout_seconds) as response:  # noqa: S310 - guest address comes from the local VM network
            if not 200 <= response.status < 400:
                raise ReadinessError("HTTP readiness probe failed")
            return
    raise ReadinessError("unsupported readiness probe")
