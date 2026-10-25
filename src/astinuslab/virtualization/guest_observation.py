"""Read-only, bounded observations from a running guest.

The portable package format deliberately has no host command or arbitrary guest
shell hook.  Checkpoints may only read JSON files from the documented
``/var/lib/astinuslab/observations`` directory through the QEMU Guest Agent.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import time
import unicodedata
from collections.abc import Callable, Mapping
from pathlib import PurePosixPath
from typing import Any, Protocol

from pydantic import JsonValue

from ..lab_packages.models import GuestAssertion, GuestObservation
from .guest_wait import _run_qga_command

OBSERVATION_ROOT = "/var/lib/astinuslab/observations"
MAX_OBSERVATION_BYTES = 64 * 1024
_QGA_CHUNK_BYTES = 8192


class GuestObservationError(RuntimeError):
    """The guest observation could not be read or trusted."""


class GuestObservationProvider(Protocol):
    """Transport boundary used by checkpoint evaluators.

    Implementations must be read-only and return one finite JSON value.  They
    must not expose host paths, host commands, or arbitrary guest execution.
    """

    def observe(self, vm_name: str, operation: GuestObservation | GuestAssertion) -> JsonValue: ...


def normalize_observation_path(path: str) -> str:
    """Return an absolute path below :data:`OBSERVATION_ROOT`.

    Relative paths are convenient in authored definitions and are rooted at
    ``OBSERVATION_ROOT``.  Absolute paths are accepted only when they already
    use that root.  Traversal, alternate separators, control/bidi characters,
    and empty components are rejected rather than silently normalized.
    """

    if not isinstance(path, str) or not path or "\\" in path:
        raise GuestObservationError("invalid guest observation path")
    path = unicodedata.normalize("NFC", path)
    if any(
        ord(character) < 0x20
        or ord(character) == 0x7F
        or 0x80 <= ord(character) <= 0x9F
        or unicodedata.category(character) in {"Cc", "Cf"}
        for character in path
    ):
        raise GuestObservationError("invalid guest observation path")
    if path.startswith(OBSERVATION_ROOT):
        relative = path[len(OBSERVATION_ROOT) :]
        if relative.startswith("/"):
            relative = relative[1:]
        elif relative:
            raise GuestObservationError("guest observation path is outside the observation root")
    elif path.startswith("/"):
        raise GuestObservationError("guest observation path is outside the observation root")
    else:
        relative = path
    if not relative or any(part in {"", ".", ".."} for part in relative.split("/")):
        raise GuestObservationError("invalid guest observation path")
    normalized = PurePosixPath(relative)
    if normalized.is_absolute() or str(normalized) != relative:
        raise GuestObservationError("invalid guest observation path")
    return f"{OBSERVATION_ROOT}/{normalized}"


def _finite_json(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Mapping):
        return all(isinstance(key, str) and _finite_json(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return all(_finite_json(item) for item in value)
    return value is None or isinstance(value, (bool, int, str))


def _reject_nonfinite(value: str) -> Any:
    raise GuestObservationError(f"non-finite JSON constant {value!r}")


def _decode_response(raw: str, *, operation: str) -> Any:
    try:
        response = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise GuestObservationError(f"malformed QGA response for {operation}") from exc
    if not isinstance(response, dict) or "return" not in response or "error" in response:
        raise GuestObservationError(f"QGA operation {operation} failed")
    return response["return"]


def _remaining_seconds(deadline: float) -> int:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise GuestObservationError("guest observation timed out")
    return max(1, min(600, math.ceil(remaining)))


class QgaGuestObservationProvider:
    """Read a finite JSON document through QGA's file API only."""

    def __init__(
        self,
        *,
        libvirt_uri: str,
        max_bytes: int = MAX_OBSERVATION_BYTES,
        qga_command: Callable[[str, str, dict[str, Any], int], str] | None = None,
    ) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= MAX_OBSERVATION_BYTES:
            raise ValueError("max_bytes must be between 1 and 65536")
        self._libvirt_uri = libvirt_uri
        self._max_bytes = max_bytes
        self._qga_command = qga_command or _run_qga_command

    def observe(self, vm_name: str, operation: GuestObservation | GuestAssertion) -> JsonValue:
        if not isinstance(operation, (GuestObservation, GuestAssertion)) or operation.scope != "guest":
            raise GuestObservationError("unsupported guest observation operation")
        path = normalize_observation_path(operation.path)
        deadline = time.monotonic() + operation.timeout_seconds
        handle: int | None = None
        try:
            opened = _decode_response(
                self._call_qga(
                    self._libvirt_uri,
                    vm_name,
                    {"execute": "guest-file-open", "arguments": {"path": path, "mode": "r"}},
                    _remaining_seconds(deadline),
                ),
                operation="guest-file-open",
            )
            if isinstance(opened, bool) or not isinstance(opened, int) or opened < 0:
                raise GuestObservationError("QGA returned an invalid file handle")
            handle = opened
            chunks: list[bytes] = []
            total = 0
            while True:
                requested = min(_QGA_CHUNK_BYTES, self._max_bytes - total)
                if requested <= 0:
                    raise GuestObservationError("guest observation exceeds 64 KiB")
                result = _decode_response(
                    self._call_qga(
                        self._libvirt_uri,
                        vm_name,
                        {
                            "execute": "guest-file-read",
                            "arguments": {"handle": handle, "count": requested},
                        },
                        _remaining_seconds(deadline),
                    ),
                    operation="guest-file-read",
                )
                if not isinstance(result, dict):
                    raise GuestObservationError("QGA returned an invalid file-read response")
                encoded = result.get("buf-b64", "")
                count = result.get("count")
                eof = result.get("eof")
                if (
                    isinstance(count, bool)
                    or not isinstance(count, int)
                    or count < 0
                    or count > requested
                    or not isinstance(eof, bool)
                    or not isinstance(encoded, str)
                ):
                    raise GuestObservationError("QGA returned an invalid file-read response")
                try:
                    chunk = base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error) as exc:
                    raise GuestObservationError("QGA returned invalid file bytes") from exc
                if len(chunk) != count or total + len(chunk) > self._max_bytes:
                    raise GuestObservationError("guest observation exceeds 64 KiB")
                if count == 0 and not eof:
                    raise GuestObservationError("QGA returned an empty non-EOF read")
                chunks.append(chunk)
                total += len(chunk)
                if eof:
                    break
            try:
                text = b"".join(chunks).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise GuestObservationError("guest observation is not UTF-8") from exc
            try:
                value = json.loads(text, parse_constant=_reject_nonfinite)
            except GuestObservationError:
                raise
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise GuestObservationError("guest observation is not valid JSON") from exc
            if not _finite_json(value):
                raise GuestObservationError("guest observation is not finite JSON")
            return value
        finally:
            if handle is not None:
                try:
                    self._call_qga(
                        self._libvirt_uri,
                        vm_name,
                        {"execute": "guest-file-close", "arguments": {"handle": handle}},
                        _remaining_seconds(deadline),
                    )
                except Exception:
                    # A read error is already being raised, and a close error
                    # cannot make the observation trustworthy.  Do not hide
                    # the original failure; the next verification will fail
                    # closed if the guest agent remains unavailable.
                    pass

    def _call_qga(self, libvirt_uri: str, vm_name: str, payload: dict[str, Any], timeout: int) -> str:
        try:
            return self._qga_command(libvirt_uri, vm_name, payload, timeout)
        except GuestObservationError:
            raise
        except Exception as exc:
            raise GuestObservationError("QEMU Guest Agent is unavailable") from exc


__all__ = [
    "GuestObservationError",
    "GuestObservationProvider",
    "MAX_OBSERVATION_BYTES",
    "OBSERVATION_ROOT",
    "QgaGuestObservationProvider",
    "normalize_observation_path",
]
