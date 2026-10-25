import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("active_unit", ["ssh", "sshd", "none"])
def test_sample_workload_checks_distribution_ssh_unit(tmp_path, active_unit):
    commands = tmp_path / "bin"
    commands.mkdir()
    for name, body in {
        "dd": "exit 0",
        "nohup": "exit 0",
        "systemctl": 'echo "$2" >> "$CALLS"; if [ "$2" = "$ACTIVE_UNIT" ]; then echo active; else echo inactive; exit 3; fi',
    }.items():
        path = commands / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
    script = Path(__file__).resolve().parents[3] / "guest/scripts/sample-workload.sh"
    # Isolate the script's scratch files without changing its service-selection logic.
    isolated = tmp_path / "workload.sh"
    isolated.write_text(script.read_text().replace("/tmp/astinuslab-sample", str(tmp_path / "sample")))
    result = subprocess.run(
        ["bash", str(isolated)],
        env={
            **os.environ,
            "PATH": f"{commands}:{os.environ['PATH']}",
            "ACTIVE_UNIT": active_unit,
            "CALLS": str(tmp_path / "calls"),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert (result.returncode == 0) is (active_unit != "none")
    if active_unit != "none":
        assert (tmp_path / "sample/ssh.status").read_text().strip() == "active"
        assert "sample workload completed" in result.stdout
    else:
        assert "sample workload completed" not in result.stdout
    assert (tmp_path / "calls").read_text().splitlines() == (["ssh"] if active_unit == "ssh" else ["ssh", "sshd"])
