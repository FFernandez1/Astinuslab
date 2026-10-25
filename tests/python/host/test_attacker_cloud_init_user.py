"""Execute seed rendering with inert host commands, never invoking libvirt/sudo."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
pytestmark = [pytest.mark.unit, pytest.mark.host]


@pytest.mark.parametrize(
    "target_id,explicit_user,expected",
    [
        ("job-test-attacker", "ubuntu", "ubuntu"),
        ("job-test-attacker", "operator", "operator"),
        ("job-test-attacker", None, "kali"),
        ("ubuntu", None, "ubuntu"),
        ("kali", "", "kali"),
    ],
)
def test_attacker_seed_honors_configured_guest_user(tmp_path, target_id, explicit_user, expected):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, body in {
        "sudo": "#!/bin/bash\nexit 0\n",
        "cloud-localds": '#!/bin/bash\nset -euo pipefail\ncp -- "$2" "$TEST_RENDERED_SEED"\n',
    }.items():
        command = fake_bin / name
        command.write_text(body)
        command.chmod(0o755)
    key = tmp_path / "key.pub"
    key.write_text("ssh-ed25519 fixture acceptance\n")
    seed = tmp_path / "rendered-user-data"
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "TEST_RENDERED_SEED": str(seed)}
    env.pop("GUEST_USER", None)
    if explicit_user is not None:
        env["GUEST_USER"] = explicit_user
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "ops/host/bootstrap_cloud_image_libvirt_storage.sh"),
            "job-unit-fixture",
            str(tmp_path / "image.qcow2"),
            str(key),
            target_id,
            "generic",
            "20",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    user = yaml.safe_load(seed.read_text())["users"][1]
    assert user["name"] == expected
    assert user["ssh_authorized_keys"] == ["ssh-ed25519 fixture acceptance"]
