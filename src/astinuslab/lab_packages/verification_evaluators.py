"""Production evaluators for creator-side clean-run verification."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy.orm import Session

from ..core.config import settings
from ..db.models import JobVM
from ..playground import repositories
from ..virtualization.guest_observation import (
    GuestObservationProvider,
    QgaGuestObservationProvider,
)
from . import experience_runner
from .models import GuestAssertion, GuestObservation, LabReadinessProbe


def lowest_ordinal_nic_ip(vm: JobVM) -> str | None:
    """Return the first guest IP using the package launch ordering contract."""

    return next((nic.ip for nic in sorted(vm.nics, key=lambda nic: nic.ordinal) if nic.ip), None)


def _session_vms(session: Session, authored_session_id: str | None) -> dict[str, JobVM]:
    if not authored_session_id:
        return {}
    return {vm.label: vm for vm in repositories.list_session_vms(session, authored_session_id)}


def build_readiness_checker(
    session: Session,
    authored_session_id: str | None,
) -> Callable[[str, tuple[LabReadinessProbe, ...]], bool]:
    """Build a fail-closed readiness checker for one frozen authored session."""

    vms = _session_vms(session, authored_session_id)

    def check(vm_id: str, probes: tuple[LabReadinessProbe, ...]) -> bool:
        vm = vms.get(vm_id)
        if vm is None:
            return False
        host = lowest_ordinal_nic_ip(vm)
        if not host:
            return False
        for probe in probes:
            # There is deliberately no guest shell transport in v1.  A
            # command probe cannot be proven by a host-side hook.
            if probe.kind == "command":
                return False
            try:
                experience_runner.probe_ready(host, probe)
            except experience_runner.ReadinessError:
                return False
            except (OSError, TimeoutError):
                return False
        return True

    return check


def build_checkpoint_evaluator(
    session: Session,
    authored_session_id: str | None,
    *,
    provider: GuestObservationProvider | None = None,
) -> Callable[[GuestObservation | GuestAssertion], bool]:
    """Build a checkpoint evaluator backed only by bounded guest observations."""

    vms = _session_vms(session, authored_session_id)
    observation_provider = provider or QgaGuestObservationProvider(libvirt_uri=settings.libvirt_uri)

    def evaluate(operation: GuestObservation | GuestAssertion) -> bool:
        if operation.scope != "guest":
            return False
        vm = vms.get(operation.vm_id)
        if vm is None or not vm.domain_name:
            return False
        try:
            observed = observation_provider.observe(vm.domain_name, operation)
        except Exception:
            # A provider outage, malformed transport response, or provider
            # implementation error must fail the checkpoint, never attest it.
            return False
        if isinstance(operation, GuestObservation):
            # ``None`` is a valid JSON observation, so successful transport is
            # the signal rather than truthiness of the returned value.
            return True
        if not _finite_json(observed):
            return False
        return _strict_json_equal(operation.expected, observed)

    return evaluate


def _finite_json(value: Any) -> bool:
    if isinstance(value, float):
        return value == value and value not in {float("inf"), float("-inf")}
    if isinstance(value, dict):
        return all(isinstance(key, str) and _finite_json(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return all(_finite_json(item) for item in value)
    return value is None or isinstance(value, (bool, int, str))


def _strict_json_equal(expected: Any, observed: Any) -> bool:
    """Compare JSON values without Python's bool-as-int coercion."""

    if type(expected) is not type(observed):
        return False
    if isinstance(expected, Mapping):
        return set(expected) == set(observed) and all(
            _strict_json_equal(expected[key], observed[key]) for key in expected
        )
    if isinstance(expected, (list, tuple)):
        return len(expected) == len(observed) and all(
            _strict_json_equal(left, right) for left, right in zip(expected, observed, strict=True)
        )
    return expected == observed


__all__ = [
    "build_checkpoint_evaluator",
    "build_readiness_checker",
    "lowest_ordinal_nic_ip",
]
