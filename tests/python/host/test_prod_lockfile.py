"""Guard: a committed, fully-pinned production lockfile exists and install.sh
installs from it."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[3]
LOCK = ROOT / "requirements.lock"


def test_prod_lockfile_exists():
    assert LOCK.exists(), "requirements.lock must be committed for reproducible installs"


def test_prod_lockfile_is_fully_pinned():
    for raw in LOCK.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        # allow hashes / markers after ';' or '--hash'; require a '==' pin token
        assert "==" in line.split(";")[0], f"unpinned dependency line: {raw!r}"


def test_install_script_uses_prod_lockfile():
    install_script = (ROOT / "ops" / "deploy" / "install.sh").read_text(encoding="utf-8")
    # The venv is built inside the release directory, so the lockfile the
    # installer feeds pip is the release's own.
    assert 'pip" install --quiet -r "$root/requirements.lock"' in install_script
    assert '-e "$root" --no-deps' in install_script
