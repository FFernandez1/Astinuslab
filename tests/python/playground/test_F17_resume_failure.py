import pytest

from astinuslab.playground.captures import DiskCapture


def test_F17_resume_failure_propagates():
    calls: list[list[str]] = []

    def fake_run(cmd):
        calls.append(list(cmd))
        if cmd[0] == "virsh" and "resume" in cmd:
            raise RuntimeError("resume failed")

    cap = DiskCapture(run_command=fake_run, mode="pause_convert")
    with pytest.raises(RuntimeError, match="resume failed"):
        cap.capture(domain_name="d", source_disk_path="/tmp/a", output_path="/tmp/b")
    # Ensure suspend was the first call, convert the second, resume the third.
    assert calls[0] == ["virsh", "-c", "qemu:///system", "suspend", "d"]
    assert calls[1][0] == "qemu-img"
    assert calls[2] == ["virsh", "-c", "qemu:///system", "resume", "d"]
