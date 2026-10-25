"""Test-double kill switches, and the single place that reports them.

Three env vars put the backend into a state where it does not touch a real
hypervisor.  All exist so the test tiers can run without libvirt.  None is set
by install.sh, any systemd unit, or astinuslab.toml.

They are read here, once, at import.  Every other module asks this module
rather than calling os.getenv itself, so that "am I faking?" has exactly one
answer per process and that answer can be surfaced to operators.

Faked captures produce artifacts that are byte-for-byte reproducible and
therefore hash cleanly.  A checksum proves nothing about them.  Anything that
writes an artifact manifest MUST stamp fake_mode so the forgery is legible on
disk.
"""

from __future__ import annotations

import os

_TRUTHY = {"1", "true", "yes", "on"}


def _flag(name: str) -> bool:
    return os.getenv(name, "").lower() in _TRUTHY


FAKE_LIBVIRT = _flag("ASTINUSLAB_FAKE_LIBVIRT")
FAKE_PLAYGROUND = _flag("ASTINUSLAB_FAKE_PLAYGROUND")
# Job orchestration is the one flow that reaches for `sudo` -- it installs a
# base image and defines a domain through a privileged host helper. Neither of
# the flags above covers it, so on a host where that sudo rule is configured a
# job run provisions a real VM. This flag replaces the bindings that reach the
# host with ones that refuse, so the state machine can be driven end to end.
# It is deliberately separate: the Jobs test tier wants admission tested
# against a backend that enqueues and never executes, and must not get this.
FAKE_ORCHESTRATION = _flag("ASTINUSLAB_FAKE_ORCHESTRATION")


def fake_mode_active() -> bool:
    """True when any test double is standing in for real virtualization."""
    return FAKE_LIBVIRT or FAKE_PLAYGROUND or FAKE_ORCHESTRATION


def fake_mode_flags() -> dict[str, bool]:
    """Per-flag detail for /health and the Settings page."""
    return {
        "fake_libvirt": FAKE_LIBVIRT,
        "fake_playground": FAKE_PLAYGROUND,
        "fake_orchestration": FAKE_ORCHESTRATION,
    }


def fake_mode_banner() -> str | None:
    """One-line operator warning, or None when the backend is real."""
    if not fake_mode_active():
        return None
    active = [name for name, on in fake_mode_flags().items() if on]
    return (
        "FAKE MODE ACTIVE (" + ", ".join(sorted(active)) + "). "
        "Virtualization is simulated. Any artifact produced in this process is "
        "SYNTHETIC and is not forensic evidence, even though it carries a valid "
        "SHA-256 and manifest."
    )
