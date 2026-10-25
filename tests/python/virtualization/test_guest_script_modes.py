"""Regression guards for guest script mode bits."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# These scripts are executed directly after being copied to the guest. scp
# preserves the source mode, so missing +x becomes Permission denied in-guest.
DIRECT_EXEC_GUEST_SCRIPTS = [
    "guest/scripts/detect_environment.sh",
    "guest/scripts/acquire_disk.sh",
    "guest/scripts/build_vol3_symbol.sh",
]


def test_direct_exec_guest_scripts_are_executable():
    missing = [rel for rel in DIRECT_EXEC_GUEST_SCRIPTS if not os.access(REPO_ROOT / rel, os.X_OK)]

    assert missing == [], f"missing +x (scp propagates mode to the guest): {missing}"


def test_detect_environment_env_mode_survives_eval():
    """`pkg_helper.sh` does `eval "$(detect_environment.sh env)"`.

    Fields like PRETTY_NAME ("Ubuntu 24.04.4 LTS") contain spaces, so unquoted
    output makes eval run the second word as a command (exit 127). The values
    must be quoted so eval assigns them intact.
    """
    script = REPO_ROOT / "guest/scripts/detect_environment.sh"
    env_output = subprocess.run(["bash", str(script), "env"], capture_output=True, text=True, check=True).stdout

    result = subprocess.run(
        ["bash", "-c", f'set -eu\neval "{env_output}"\necho "$PACKAGE_MANAGER"'],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"eval of env output failed: {result.stderr}"
