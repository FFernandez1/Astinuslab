from __future__ import annotations

import base64
import json
import math
import time
from dataclasses import dataclass

from .guest_wait import MAX_QGA_RESPONSE_BYTES, _run_qga_command


class GuestExecError(RuntimeError):
    """A bounded QGA guest-process operation failed."""

    def __init__(self, vm_name: str, phase: str):
        super().__init__(f"QGA guest execution failed for {vm_name} during {phase}")
        self.vm_name = vm_name
        self.phase = phase


class GuestExecOutputLimitError(GuestExecError):
    """QGA returned more guest-process output than the bounded policy allows."""


@dataclass(frozen=True)
class GuestExecResult:
    exitcode: int
    stdout: str
    stderr: str


def _decode_output(value: str | None, *, vm_name: str, phase: str) -> str:
    if not value:
        return ""
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise GuestExecError(vm_name, phase) from None
    if len(decoded) > MAX_QGA_RESPONSE_BYTES:
        raise GuestExecOutputLimitError(vm_name, phase)
    return decoded.decode("utf-8", errors="replace")


def run_guest_exec(
    libvirt_uri: str,
    vm_name: str,
    path: str,
    args: list[str],
    timeout: int = 30,
    *,
    discard_stderr: bool = False,
) -> GuestExecResult:
    """Run a bounded guest process through QGA without exposing its arguments.

    With discard_stderr, guest stderr is never base64-decoded or returned.
    """
    deadline = time.monotonic() + max(1, timeout)

    def remaining_timeout() -> int:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GuestExecError(vm_name, "timeout")
        # virsh takes whole seconds; a single request can overrun by <1 second.
        return min(30, math.ceil(remaining))

    phase = "launch"
    try:
        launch = json.loads(
            _run_qga_command(
                libvirt_uri,
                vm_name,
                {
                    "execute": "guest-exec",
                    "arguments": {"path": path, "arg": list(args), "capture-output": True},
                },
                timeout=remaining_timeout(),
            )
        )
        pid = launch.get("return", {}).get("pid")
        if type(pid) is not int or pid <= 0:
            raise GuestExecError(vm_name, phase)

        phase = "poll"
        while time.monotonic() < deadline:
            status = json.loads(
                _run_qga_command(
                    libvirt_uri,
                    vm_name,
                    {"execute": "guest-exec-status", "arguments": {"pid": pid}},
                    timeout=remaining_timeout(),
                )
            ).get("return", {})
            if time.monotonic() >= deadline:
                raise GuestExecError(vm_name, "timeout")
            if type(status.get("exited")) is not bool:
                raise GuestExecError(vm_name, "poll")
            if status["exited"] is False:
                time.sleep(min(1, max(0, deadline - time.monotonic())))
                continue
            if status.get("out-truncated", False) is not False:
                raise GuestExecOutputLimitError(vm_name, "decode")
            stdout = _decode_output(status.get("out-data"), vm_name=vm_name, phase="decode")
            stderr = "" if discard_stderr else _decode_output(status.get("err-data"), vm_name=vm_name, phase="decode")
            exitcode = status.get("exitcode")
            if type(exitcode) is not int:
                raise GuestExecError(vm_name, "decode")
            return GuestExecResult(exitcode=exitcode, stdout=stdout, stderr=stderr)
        raise GuestExecError(vm_name, "timeout")
    except GuestExecError:
        raise
    except Exception:
        raise GuestExecError(vm_name, phase) from None
