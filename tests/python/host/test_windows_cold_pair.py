"""Safety ordering for the operator's two cold clone preparations; no VM I/O."""

import importlib.util
from pathlib import Path

import pytest

SOURCE = Path(__file__).parents[3] / "tools/dev/windows_candidate_trial/cold_pair.py"


def load():
    spec = importlib.util.spec_from_file_location("cold_pair", SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Host:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    def action(self, name):
        self.calls.append(name)
        if name == self.failure:
            raise ValueError("injected failure with private details")

    def prepare(self):
        self.action("prepare")

    def preflight(self):
        self.action("preflight")

    def claim(self):
        self.action("claim")

    def allocate(self, label):
        self.action("allocate_" + label)

    def stage(self, label):
        self.action("stage_" + label)

    def define(self, label):
        self.action("define_" + label)

    def verify(self):
        self.action("verify")

    def close(self):
        self.action("close")

    def record(self, result):
        self.action("record")

    def snapshot(self):
        self.action("snapshot")
        return {"all_domains_off": True, "qemu_processes": 0, "protected_state_unchanged": True}


def test_pair_preparation_binds_both_copies_before_definition():
    h = Host()
    result = load().run(h)
    assert result["result"] == "COLD_RUNTIME_PAIR_PREPARED"
    assert h.calls == [
        "prepare",
        "preflight",
        "claim",
        "allocate_a",
        "allocate_b",
        "stage_a",
        "stage_b",
        "verify",
        "define_a",
        "define_b",
        "verify",
        "snapshot",
        "close",
        "record",
    ]
    assert result["copies_verified"] == ["a", "b"]
    assert result["domains_defined"] == ["a", "b"]
    assert result["windows_booted"] is False
    assert result["source_modified"] is False
    assert result["reusable_image_accepted"] is False
    assert result["result_recorded"] is True


@pytest.mark.parametrize(
    "failure",
    [
        "prepare",
        "preflight",
        "claim",
        "allocate_a",
        "allocate_b",
        "stage_a",
        "stage_b",
        "verify",
        "define_a",
        "define_b",
        "snapshot",
        "close",
        "record",
    ],
)
def test_failure_stops_forward_work_preserves_phase_and_closes(failure):
    h = Host(failure)
    result = load().run(h)
    assert result["result"] == "STOP"
    assert failure in result["failure_phases"]
    assert "close" in h.calls
    assert "private" not in str(result)
    assert result["artifacts_retained"] is True
    if failure in ("prepare", "preflight", "claim", "allocate_a", "allocate_b", "stage_a", "stage_b", "verify"):
        assert not any(c.startswith("define_") for c in h.calls)
    if failure == "define_a":
        assert "define_b" not in h.calls


@pytest.mark.parametrize(
    "change",
    [
        {"backing-filename": "source.qcow2"},
        {"data-file": "external"},
        {"encrypted": True},
        {"virtual-size": True},
        {"virtual-size": 80 * 1024**3},
        {"snapshots": [{}]},
        {"format-specific": {"type": "qcow2", "data": {"corrupt": True}}},
        {"format-specific": {"type": "qcow2", "data": {"corrupt": False, "encrypt": {}}}},
    ],
)
def test_only_standalone_unencrypted_40gib_qcow2_is_accepted(change):
    m = load()
    image = {
        "format": "qcow2",
        "virtual-size": 40 * 1024**3,
        "format-specific": {"type": "qcow2", "data": {"corrupt": False}},
    }
    m.validate_image(image)
    with pytest.raises(ValueError):
        m.validate_image({**image, **change})
