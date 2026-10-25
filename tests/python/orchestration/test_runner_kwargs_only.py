"""Guard: wide runner dependencies are keyword-only, so transposing two
same-typed positionals can't silently mis-wire a live capture."""

from __future__ import annotations

import inspect

import pytest

from astinuslab.forensics import capture_support
from astinuslab.workloads import workload_support

TARGETS = [
    (capture_support, "acquire_and_publish"),
    (capture_support, "dispatch_boot_capture_acquisition"),
    (workload_support, "provision_stack"),
    (workload_support, "run_workload_install"),
]


@pytest.mark.parametrize("module,name", TARGETS)
def test_no_positional_or_keyword_params(module, name):
    sig = inspect.signature(getattr(module, name))
    offenders = [p.name for p in sig.parameters.values() if p.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD]
    assert not offenders, f"{name} still has positional params: {offenders}"
