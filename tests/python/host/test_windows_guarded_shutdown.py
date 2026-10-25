"""Failure-aware shutdown of an existing Windows trial; no real host calls."""

import importlib.util
from pathlib import Path

import pytest


def module():
    path = Path(__file__).parents[3] / "tools/dev/windows_candidate_trial/guarded_shutdown.py"
    spec = importlib.util.spec_from_file_location("guarded_shutdown", path)
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class Adapter:
    def __init__(self, failure=None):
        self.failure = failure
        self.now = 0
        self.active = failure != "off"
        self.calls = []
        self.probe_failure = None

    def clock(self):
        return self.now

    def sleep(self, value):
        self.now += value

    def snapshot(self):
        self.calls.append("snapshot")
        if self.failure == "binding":
            raise ValueError("binding")
        return {
            "active": self.active,
            "state": [1, 1] if self.active else [5, 2 if self.failure == "reason" else 1],
            "qemu_processes": int(self.active),
            "protected_state_unchanged": True,
        }

    def setup(self):
        self.calls.append("setup")
        if self.failure == "probe":
            self.probe_failure = {"phase": "status", "exited": False, "pid": 42}
            raise ValueError("uncertain")
        return {"boot_id": "changed" if self.failure == "boot" else "boot", "uptime_seconds": self.now}

    def same_boot(self, a, b):
        if self.failure == "regress" or a["boot_id"] != b["boot_id"] or b["uptime_seconds"] < a["uptime_seconds"] + 2:
            raise ValueError("unstable")

    def shutdown(self, value):
        self.calls.append("shutdown")
        if self.failure == "shutdown":
            raise ValueError("uncertain acknowledgement")
        if self.failure != "timeout":
            self.active = False


def test_only_guarded_shutdown_and_no_image_acceptance():
    adapter = Adapter()
    result = module().run(adapter, "boot")
    assert result["result"] == "A_SHUT_DOWN_AFTER_FIRSTBOOT_STOP"
    assert adapter.calls.count("setup") == 2
    assert adapter.calls.count("shutdown") == 1
    assert result["windows_started"] is False
    assert result["reusable_image_accepted"] is False
    assert result["acl_acceptance_passed"] is False


@pytest.mark.parametrize("failure", ["off", "binding", "probe", "boot", "regress", "shutdown", "timeout", "reason"])
def test_failure_never_forces_or_retries_shutdown(failure):
    adapter = Adapter(failure)
    result = module().run(adapter, "boot")
    assert result["result"] == "STOP"
    assert adapter.calls.count("shutdown") <= 1
    if failure in ("off", "binding", "probe", "boot", "regress"):
        assert "shutdown" not in adapter.calls


def test_final_snapshot_preserves_failed_probe_evidence():
    result = module().run(Adapter("probe"), "boot")
    assert result["final_snapshot"]["active"] is True
    assert result["probe_failure"] == {"phase": "status", "exited": False, "pid": 42}
