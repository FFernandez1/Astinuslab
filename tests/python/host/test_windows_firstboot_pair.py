"""Injected sequencing of two separately supervised Windows first boots."""

import importlib.util
from pathlib import Path

import pytest


def load():
    p = Path(__file__).parents[3] / "tools/dev/windows_candidate_trial/firstboot_pair.py"
    spec = importlib.util.spec_from_file_location("firstboot_pair", p)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def observation(label):
    return {
        "result": "UNATTENDED_FIRSTBOOT_OBSERVED",
        "failure_phases": [],
        "clean_shutdown": True,
        "protected_state_unchanged": True,
        "all_domains_off": True,
        "qemu_processes": 0,
        "candidate_may_be_running": False,
        "observations": [
            {
                "stage": "post_setup",
                "value": {
                    "computer_name": "WIN-" + label,
                    "machine_sid_prefix": "S-1-5-21-1-2-" + ("3" if label == "a" else "4"),
                },
            }
        ],
    }


class Host:
    def __init__(self, fail=None):
        self.fail, self.calls = fail, []

    def action(self, name):
        self.calls.append(name)
        if self.fail == name:
            raise ValueError("injected private failure")

    def prepare(self):
        self.action("prepare")

    def claim(self):
        self.action("claim")

    def boot(self, label):
        self.action("boot_" + label)
        row = observation(label)
        if self.fail == "guest_" + label:
            row["result"] = "STOP"
            row["failure_phases"] = ["setup"]
        if self.fail == "sid_duplicate":
            row["observations"][0]["value"]["machine_sid_prefix"] = "S-1-5-21-1-2-3"
        return row

    def complete(self, label, row):
        self.action("complete_" + label)

    def close(self):
        self.action("close")

    def record(self, result):
        self.action("record")


def test_two_starts_require_separate_success_and_clean_shutdown():
    h = Host()
    result = load().run(h)
    assert result["result"] == "PAIR_UNATTENDED_FIRSTBOOT_OBSERVED"
    assert result["machine_sid_prefixes_distinct"] is True
    assert result["reusable_image_accepted"] is False
    assert h.calls == ["prepare", "claim", "boot_a", "complete_a", "boot_b", "complete_b", "close", "record"]


@pytest.mark.parametrize(
    "failure",
    [
        "prepare",
        "claim",
        "boot_a",
        "guest_a",
        "complete_a",
        "boot_b",
        "guest_b",
        "complete_b",
        "sid_duplicate",
        "close",
        "record",
    ],
)
def test_stop_preserves_phase_and_never_advances_past_failed_a(failure):
    h = Host(failure)
    result = load().run(h)
    assert result["result"] == "STOP"
    assert result["failure_phases"]
    assert "close" in h.calls
    if failure in ("prepare", "claim", "boot_a", "guest_a", "complete_a"):
        assert "boot_b" not in h.calls
    assert "private" not in str(result)
