"""Contract for `ops/deploy/ssh_key.py`.

This key is the credential the whole job path runs on: the product reaches
every guest over SSH with it. It lives under `storage/`, which service
identities can reach, so the installer is writing a secret into a directory it
does not have exclusively to itself -- which is why every entry here is
addressed by descriptor and published by linking a validated inode.

The code was a 227-line `python3 - <<'PY'` heredoc in ops/deploy/install.sh
until the extraction, so the only checks that ever reached it were `compile()`
and pyflakes. These cases exercise the module; the shell entry point
`inspect_or_generate_ssh_key` and the `handle_ssh_key` flow around it keep
their own coverage in test_install_script.py.
"""

from __future__ import annotations

import grp
import os
import pwd
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from ops.deploy import ssh_key

ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / "ops/deploy/ssh_key.py"
KEY_NAME = "astinuslab-default"
pytestmark = [pytest.mark.unit, pytest.mark.host]


def _owner() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


def _group() -> str:
    return grp.getgrgid(os.getgid()).gr_name


def _require_ssh_keygen() -> None:
    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen is required to exercise generation")


def _install(tmp_path: Path, *, keys: bool = True) -> Path:
    install = tmp_path / "install"
    (install / "storage").mkdir(parents=True)
    if keys:
        (install / "storage/ssh_keys").mkdir(mode=0o700)
    return install


def _run(capsys, install: Path, *, generate: bool, mode: str = "0700") -> str:
    """Call the module the way the installer does and return its stdout line.

    The inspect outcomes exit 0 after printing; generation returns normally.
    """
    try:
        ssh_key.inspect_or_generate(str(install), _owner(), _group(), str(generate).lower(), mode)
    except SystemExit as stop:
        assert stop.code == 0, stop.code
    return capsys.readouterr().out.strip()


# --------------------------------------------------------------------------
# The three outcomes handle_ssh_key parses.
# --------------------------------------------------------------------------


def test_an_absent_key_directory_reports_missing(tmp_path, capsys):
    install = _install(tmp_path, keys=False)

    assert _run(capsys, install, generate=False) == "missing|"
    assert not (install / "storage/ssh_keys").exists(), "inspection must not create anything"


def test_an_empty_key_directory_reports_missing(tmp_path, capsys):
    install = _install(tmp_path)

    assert _run(capsys, install, generate=False) == "missing|"


def test_generation_publishes_a_key_pair_and_activates_it(tmp_path, capsys):
    _require_ssh_keygen()
    install = _install(tmp_path, keys=False)
    keys = install / "storage/ssh_keys"

    assert _run(capsys, install, generate=True) == f"generated|{KEY_NAME}"

    private, public, marker = keys / KEY_NAME, keys / f"{KEY_NAME}.pub", keys / ".active"
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    assert stat.S_IMODE(public.stat().st_mode) == 0o640
    assert marker.read_text(encoding="utf-8") == f"{KEY_NAME}\n"
    assert public.read_text(encoding="utf-8").startswith("ssh-ed25519 ")
    # Nothing temporary survives a successful publication.
    assert sorted(entry.name for entry in keys.iterdir()) == [".active", KEY_NAME, f"{KEY_NAME}.pub"]


def test_an_activated_key_is_reported_without_being_touched(tmp_path, capsys):
    _require_ssh_keygen()
    install = _install(tmp_path, keys=False)
    _run(capsys, install, generate=True)
    private = install / "storage/ssh_keys" / KEY_NAME
    before = (private.read_bytes(), private.stat().st_ino)

    assert _run(capsys, install, generate=False) == f"existing|{KEY_NAME}"
    assert _run(capsys, install, generate=True) == f"existing|{KEY_NAME}"

    assert (private.read_bytes(), private.stat().st_ino) == before


def test_generation_restores_the_requested_directory_custody(tmp_path, capsys):
    """The module takes the directory while publishing and must hand it back.

    `2700` is the split-identity mode: setgid so the runner's group is
    inherited by anything created later.
    """
    _require_ssh_keygen()
    install = _install(tmp_path, keys=False)

    _run(capsys, install, generate=True, mode="2700")

    keys = install / "storage/ssh_keys"
    assert stat.S_IMODE(keys.stat().st_mode) == 0o2700
    assert keys.stat().st_gid == os.getgid()


# --------------------------------------------------------------------------
# Custody refusals.
# --------------------------------------------------------------------------


def test_a_missing_storage_directory_is_an_error_not_a_missing_key(tmp_path, capsys):
    """`storage/` absent means the install is broken, not that no key exists."""
    install = tmp_path / "install"
    install.mkdir()

    with pytest.raises(RuntimeError, match="storage directory is missing"):
        _run(capsys, install, generate=True)


def test_a_symlinked_active_marker_is_refused_without_reading_its_target(tmp_path, capsys):
    install = _install(tmp_path)
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("operator-owned\n", encoding="utf-8")
    (install / "storage/ssh_keys/.active").symlink_to(sentinel)

    with pytest.raises(OSError):
        _run(capsys, install, generate=False)

    assert sentinel.read_text(encoding="utf-8") == "operator-owned\n"
    assert (install / "storage/ssh_keys/.active").is_symlink()


def test_a_group_writable_active_marker_is_refused(tmp_path, capsys):
    """Anyone who can rewrite the marker can redirect the installer's read."""
    install = _install(tmp_path)
    marker = install / "storage/ssh_keys/.active"
    marker.write_text(f"{KEY_NAME}\n", encoding="utf-8")
    marker.chmod(0o660)

    with pytest.raises(RuntimeError, match="unsafe SSH active-key marker"):
        _run(capsys, install, generate=False)


def test_an_oversized_active_marker_is_refused(tmp_path, capsys):
    install = _install(tmp_path)
    marker = install / "storage/ssh_keys/.active"
    marker.write_text("x" * 129 + "\n", encoding="utf-8")
    marker.chmod(0o640)

    with pytest.raises(RuntimeError, match="unsafe SSH active-key marker"):
        _run(capsys, install, generate=False)


@pytest.mark.parametrize("name", ["..", ".", "../escape", "sub/key", "-leading-dash", "a" * 65])
def test_an_active_marker_naming_something_unusable_is_refused(tmp_path, capsys, name):
    """The marker names a sibling entry, so it must not be able to name a path."""
    install = _install(tmp_path)
    marker = install / "storage/ssh_keys/.active"
    marker.write_text(name + "\n", encoding="utf-8")
    marker.chmod(0o640)

    with pytest.raises(RuntimeError, match="invalid SSH active-key name"):
        _run(capsys, install, generate=False)


def test_an_active_marker_that_is_not_utf8_is_refused(tmp_path, capsys):
    install = _install(tmp_path)
    marker = install / "storage/ssh_keys/.active"
    marker.write_bytes(b"\xff\xfe\n")
    marker.chmod(0o640)

    with pytest.raises(RuntimeError, match="invalid SSH active-key marker"):
        _run(capsys, install, generate=False)


def test_an_active_key_with_unsafe_private_permissions_is_refused(tmp_path, capsys):
    install = _install(tmp_path)
    keys = install / "storage/ssh_keys"
    (keys / ".active").write_text(f"{KEY_NAME}\n", encoding="utf-8")
    (keys / ".active").chmod(0o640)
    (keys / KEY_NAME).write_text("private\n", encoding="utf-8")
    (keys / KEY_NAME).chmod(0o644)
    (keys / f"{KEY_NAME}.pub").write_text("public\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="SSH private key has unsafe permissions"):
        _run(capsys, install, generate=False)


def test_a_hardlinked_active_key_is_refused(tmp_path, capsys):
    """A second link is a second pathname someone else may still control."""
    install = _install(tmp_path)
    keys = install / "storage/ssh_keys"
    (keys / ".active").write_text(f"{KEY_NAME}\n", encoding="utf-8")
    (keys / ".active").chmod(0o640)
    (keys / KEY_NAME).write_text("private\n", encoding="utf-8")
    (keys / KEY_NAME).chmod(0o600)
    os.link(keys / KEY_NAME, tmp_path / "second-link")

    with pytest.raises(RuntimeError, match="unsafe SSH key entry"):
        _run(capsys, install, generate=False)


def test_generation_refuses_to_overwrite_an_inactive_key_entry(tmp_path, capsys):
    """No active marker, but the name is taken: that is an operator's file."""
    _require_ssh_keygen()
    install = _install(tmp_path)
    stale = install / "storage/ssh_keys" / KEY_NAME
    stale.write_text("operator-owned\n", encoding="utf-8")
    stale.chmod(0o600)

    with pytest.raises(RuntimeError, match="refusing to overwrite inactive SSH key entry"):
        _run(capsys, install, generate=True)

    assert stale.read_text(encoding="utf-8") == "operator-owned\n"


def test_a_failed_generation_leaves_no_key_behind(tmp_path, capsys, monkeypatch):
    """A half-published key pair is worse than none: it would activate later."""
    _require_ssh_keygen()
    install = _install(tmp_path, keys=False)
    real_run = ssh_key.subprocess.run

    def fail_after_keygen(arguments, **kwargs):
        # Let ssh-keygen actually write the temporary pair, then fail the way a
        # crash would: the cleanup path is what this case is about.
        real_run(arguments, **kwargs)
        raise RuntimeError("publication interrupted")

    monkeypatch.setattr(ssh_key.subprocess, "run", fail_after_keygen)

    with pytest.raises(RuntimeError, match="publication interrupted"):
        _run(capsys, install, generate=True)

    assert list((install / "storage/ssh_keys").iterdir()) == []


# --------------------------------------------------------------------------
# The command line the shell function invokes.
# --------------------------------------------------------------------------


def _run_module(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MODULE), *arguments],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )


def test_the_command_line_prints_one_pipe_separated_line(tmp_path):
    _require_ssh_keygen()
    install = _install(tmp_path, keys=False)

    generated = _run_module(str(install), _owner(), _group(), "true", "0700")
    assert generated.returncode == 0, generated.stderr
    assert generated.stdout == f"generated|{KEY_NAME}\n"

    existing = _run_module(str(install), _owner(), _group(), "false", "0700")
    assert existing.returncode == 0, existing.stderr
    assert existing.stdout == f"existing|{KEY_NAME}\n"


def test_the_command_line_exits_non_zero_without_printing_an_outcome(tmp_path):
    """`handle_ssh_key` reads stdout only after checking the status."""
    install = tmp_path / "install"
    install.mkdir()

    result = _run_module(str(install), _owner(), _group(), "true", "0700")

    assert result.returncode != 0
    assert result.stdout == ""
    assert "storage directory is missing" in result.stderr


@pytest.mark.parametrize("count", [0, 4, 6])
def test_the_command_line_refuses_an_argument_count_it_cannot_mean(count):
    result = _run_module(*["placeholder"] * count)

    assert result.returncode != 0
    assert "usage: ssh_key.py" in result.stderr
