"""Contract for `ops/deploy/managed_config.py`.

The installer captures this snapshot before an upgrade rewrites the
configuration the installation owns, and replays it when a later phase fails.
Getting it wrong is a rollback that leaves an operator with the candidate's
configuration, or with credentials the candidate generated and nothing
recorded that they are new.

The same module also installs the shipped defaults, validates the managed
destinations, and writes the auth section, so a second class of failure is in
scope here: a config phase that reports success while leaving the installation
authenticating against a value the launcher environment then overrides.

The code was six `python3 - <<'PY'` heredocs in ops/deploy/install.sh until the
extraction, so the only checks that ever reached it were `compile()` and
pyflakes. These cases exercise the module itself; the shell entry points
`snapshot_managed_config_state`, `restore_managed_config_state`,
`install_config_if_missing`, `validate_managed_config_destinations`,
`neutralise_env_auth_overrides` and `configure_admin_auth` keep their own
coverage in test_install_script.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from ops.deploy import managed_config
from ops.deploy.runtime_environment import read_environment

ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / "ops/deploy/managed_config.py"
pytestmark = [pytest.mark.unit, pytest.mark.host]


def test_database_endpoint_update_preserves_unrelated_bytes_and_metadata(tmp_path):
    path = tmp_path / "astinuslab.env"
    original = b'# operator notes\nSECRET="retained\\$value"\nASTINUSLAB_DB_PATH="old.db"\n'
    path.write_bytes(original)
    path.chmod(0o640)
    before = path.stat()
    updates = {"ASTINUSLAB_DB_PATH": '/srv/install/storage/databases/a $`".db'}
    digest = managed_config.set_database_endpoints(path, updates, hashlib.sha256(original).hexdigest())
    assert path.read_bytes().startswith(original)
    assert read_environment(path)["ASTINUSLAB_DB_PATH"] == updates["ASTINUSLAB_DB_PATH"]
    assert read_environment(path)["SECRET"] == "retained$value"
    after = path.stat()
    assert (after.st_ino, after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) == (
        before.st_ino,
        before.st_uid,
        before.st_gid,
        0o640,
    )
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    managed_config.set_database_endpoints(path, updates, digest)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_database_endpoint_update_rejects_concurrent_edit(tmp_path):
    path = tmp_path / "astinuslab.env"
    path.write_bytes(b"KEEP=operator-change\n")
    with pytest.raises(SystemExit, match="changed"):
        managed_config.set_database_endpoints(path, {"ASTINUSLAB_DB_PATH": "/new.db"}, "0" * 64)
    assert path.read_bytes() == b"KEEP=operator-change\n"


def test_database_endpoint_update_handles_quoted_multiline_environment(tmp_path):
    path = tmp_path / "astinuslab.env"
    original = b'SECRET="first\nASTINUSLAB_DB_PATH=inside-secret\nlast"\nASTINUSLAB_DB_PATH="old\nname.db"'
    path.write_bytes(original)
    managed_config.set_database_endpoints(path, {"ASTINUSLAB_DB_PATH": "/new.db"}, hashlib.sha256(original).hexdigest())
    assert path.read_bytes().startswith(original)
    assert read_environment(path) == {
        "SECRET": "first\nASTINUSLAB_DB_PATH=inside-secret\nlast",
        "ASTINUSLAB_DB_PATH": "/new.db",
    }


def test_database_endpoint_update_rejects_non_database_keys(tmp_path):
    path = tmp_path / "astinuslab.env"
    path.write_bytes(b"KEEP=original\n")
    with pytest.raises(SystemExit, match="database"):
        managed_config.set_database_endpoints(path, {"PATH": "/unsafe"}, hashlib.sha256(path.read_bytes()).hexdigest())
    assert path.read_bytes() == b"KEEP=original\n"


def _install_tree(root: Path, present: dict[str, bytes] | None = None) -> None:
    """Build an installation root holding the named managed files."""
    for relative, payload in (present or {}).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)


def _records(snapshot: Path) -> dict[str, dict]:
    return {record["path"]: record for record in json.loads(snapshot.read_text(encoding="utf-8"))}


def test_capture_records_every_managed_path_present_or_absent(tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    _install_tree(root, {"astinuslab.toml": b"[core]\n"})
    snapshot = tmp_path / "managed-config.json"

    managed_config.capture(root, snapshot)

    records = _records(snapshot)
    assert set(records) == set(managed_config.MANAGED_PATHS)
    assert records["astinuslab.toml"]["exists"] is True
    # A file whose parent directory does not exist is recorded as absent rather
    # than skipped: rollback needs the difference to know what to remove.
    assert records["storage/ssh_keys/.active"]["exists"] is False


def test_capture_writes_a_snapshot_only_its_owner_can_read(tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    snapshot = tmp_path / "managed-config.json"

    managed_config.capture(root, snapshot)

    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600


def test_capture_refuses_an_existing_destination(tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    snapshot = tmp_path / "managed-config.json"
    snapshot.write_text("[]", encoding="utf-8")

    with pytest.raises(FileExistsError):
        managed_config.capture(root, snapshot)


def test_capture_refuses_a_managed_file_with_a_second_link(tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    _install_tree(root, {"astinuslab.toml": b"[core]\n"})
    os.link(root / "astinuslab.toml", tmp_path / "elsewhere.toml")
    snapshot = tmp_path / "managed-config.json"

    with pytest.raises(SystemExit) as caught:
        managed_config.capture(root, snapshot)

    assert "not a safe regular file" in str(caught.value)


def test_capture_refuses_a_managed_path_that_is_a_symlink(tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    outside = tmp_path / "outside.toml"
    outside.write_bytes(b"foreign\n")
    (root / "astinuslab.toml").symlink_to(outside)
    snapshot = tmp_path / "managed-config.json"

    # O_NOFOLLOW turns the open into ELOOP rather than reading through the link.
    with pytest.raises(OSError):
        managed_config.capture(root, snapshot)


def test_restore_puts_back_the_recorded_bytes_and_mode(tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    _install_tree(root, {"config/astinuslab.env": b"ASTINUSLAB_X=1\n"})
    (root / "config/astinuslab.env").chmod(0o600)
    snapshot = tmp_path / "managed-config.json"
    managed_config.capture(root, snapshot)

    (root / "config/astinuslab.env").write_bytes(b"ASTINUSLAB_X=2\n")
    (root / "config/astinuslab.env").chmod(0o644)
    managed_config.restore(root, snapshot)

    assert (root / "config/astinuslab.env").read_bytes() == b"ASTINUSLAB_X=1\n"
    assert stat.S_IMODE((root / "config/astinuslab.env").stat().st_mode) == 0o600


def test_restore_removes_a_file_the_candidate_created(tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    (root / "storage/ssh_keys").mkdir(parents=True)
    snapshot = tmp_path / "managed-config.json"
    managed_config.capture(root, snapshot)

    # Exactly the post-key rollback case: keys generated after the snapshot.
    (root / "storage/ssh_keys/astinuslab-default").write_bytes(b"PRIVATE\n")
    (root / "storage/ssh_keys/astinuslab-default.pub").write_bytes(b"PUBLIC\n")
    managed_config.restore(root, snapshot)

    assert not (root / "storage/ssh_keys/astinuslab-default").exists()
    assert not (root / "storage/ssh_keys/astinuslab-default.pub").exists()


def test_restore_leaves_an_absent_file_absent_when_its_parent_never_appeared(tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    snapshot = tmp_path / "managed-config.json"
    managed_config.capture(root, snapshot)

    managed_config.restore(root, snapshot)

    assert not (root / "storage/ssh_keys").exists()


def test_restore_refuses_to_remove_a_candidate_file_that_gained_a_link(tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    (root / "storage/ssh_keys").mkdir(parents=True)
    snapshot = tmp_path / "managed-config.json"
    managed_config.capture(root, snapshot)

    created = root / "storage/ssh_keys/astinuslab-default"
    created.write_bytes(b"PRIVATE\n")
    os.link(created, tmp_path / "harvested")

    with pytest.raises(SystemExit) as caught:
        managed_config.restore(root, snapshot)

    assert "refusing to remove unsafe candidate config" in str(caught.value)
    assert created.exists()


def test_restore_refuses_a_managed_file_replaced_since_the_snapshot(tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    _install_tree(root, {"astinuslab.toml": b"[core]\n"})
    snapshot = tmp_path / "managed-config.json"
    managed_config.capture(root, snapshot)

    # Same path, different inode: writing the recorded bytes here would land in
    # a file this installation never measured.
    replacement = root / "astinuslab.toml.new"
    replacement.write_bytes(b"[core]\nreplaced = true\n")
    replacement.replace(root / "astinuslab.toml")

    with pytest.raises(SystemExit) as caught:
        managed_config.restore(root, snapshot)

    assert "refusing replaced candidate config" in str(caught.value)


def test_restore_refuses_a_managed_file_that_disappeared(tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    _install_tree(root, {"astinuslab.toml": b"[core]\n"})
    snapshot = tmp_path / "managed-config.json"
    managed_config.capture(root, snapshot)

    (root / "astinuslab.toml").unlink()

    with pytest.raises(SystemExit) as caught:
        managed_config.restore(root, snapshot)

    assert "disappeared during rollback" in str(caught.value)


def test_restore_refuses_a_world_readable_snapshot(tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    snapshot = tmp_path / "managed-config.json"
    managed_config.capture(root, snapshot)
    snapshot.chmod(0o644)

    with pytest.raises(SystemExit) as caught:
        managed_config.restore(root, snapshot)

    assert "snapshot is not protected" in str(caught.value)


@pytest.mark.parametrize("relative", ["../escape.toml", "config/../../escape.toml", "config/./targets.yaml"])
def test_restore_refuses_a_traversing_snapshot_path(tmp_path, relative):
    root = tmp_path / "install"
    root.mkdir()
    snapshot = tmp_path / "managed-config.json"
    snapshot.write_text(json.dumps([{"path": relative, "exists": False}]), encoding="utf-8")
    snapshot.chmod(0o600)

    with pytest.raises(SystemExit) as caught:
        managed_config.restore(root, snapshot)

    assert "unsafe managed config snapshot path" in str(caught.value)


def test_the_module_runs_as_the_subprocess_the_installer_invokes(tmp_path):
    """The installer calls this by path, not by import, so exercise that path."""
    root = tmp_path / "install"
    root.mkdir()
    _install_tree(root, {"astinuslab.toml": b"[core]\n"})
    snapshot = tmp_path / "managed-config.json"

    captured = subprocess.run(
        [sys.executable, str(MODULE), "capture", str(root), str(snapshot)],
        capture_output=True,
        text=True,
    )
    assert captured.returncode == 0, captured.stderr
    (root / "astinuslab.toml").write_bytes(b"[core]\ncandidate = true\n")

    restored = subprocess.run(
        [sys.executable, str(MODULE), "restore", str(root), str(snapshot)],
        capture_output=True,
        text=True,
    )
    assert restored.returncode == 0, restored.stderr
    assert (root / "astinuslab.toml").read_bytes() == b"[core]\n"


def test_an_unknown_action_is_refused(tmp_path):
    result = subprocess.run(
        [sys.executable, str(MODULE), "demolish", str(tmp_path), str(tmp_path / "x.json")],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "unknown managed_config.py action" in result.stderr


# --------------------------------------------------------------------------
# install-config
# --------------------------------------------------------------------------


def test_install_config_installs_a_default_that_is_not_there_yet(tmp_path):
    source = tmp_path / "default.toml"
    source.write_bytes(b"[core]\nfrom = 'default'\n")
    destination = tmp_path / "install/astinuslab.toml"
    destination.parent.mkdir()

    outcome = managed_config.install_config_if_missing(source, destination)

    assert outcome == "installed"
    assert destination.read_bytes() == b"[core]\nfrom = 'default'\n"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o640


def test_install_config_never_overwrites_what_the_operator_already_has(tmp_path):
    source = tmp_path / "default.toml"
    source.write_bytes(b"[core]\nfrom = 'default'\n")
    destination = tmp_path / "install/astinuslab.toml"
    destination.parent.mkdir()
    destination.write_bytes(b"[core]\nfrom = 'operator'\n")

    outcome = managed_config.install_config_if_missing(source, destination)

    assert outcome == "preserved"
    assert destination.read_bytes() == b"[core]\nfrom = 'operator'\n"


def test_install_config_reports_a_default_the_release_does_not_ship(tmp_path):
    destination = tmp_path / "install/astinuslab.toml"
    destination.parent.mkdir()

    outcome = managed_config.install_config_if_missing(tmp_path / "absent.toml", destination)

    assert outcome == "missing-source"
    assert not destination.exists()


def test_install_config_leaves_no_temporary_behind(tmp_path):
    source = tmp_path / "default.toml"
    source.write_bytes(b"[core]\n")
    destination = tmp_path / "install/astinuslab.toml"
    destination.parent.mkdir()

    managed_config.install_config_if_missing(source, destination)

    assert sorted(path.name for path in destination.parent.iterdir()) == ["astinuslab.toml"]


def test_install_config_refuses_a_destination_with_a_second_link(tmp_path):
    source = tmp_path / "default.toml"
    source.write_bytes(b"[core]\n")
    destination = tmp_path / "install/astinuslab.toml"
    destination.parent.mkdir()
    destination.write_bytes(b"operator\n")
    os.link(destination, tmp_path / "harvested")

    with pytest.raises(SystemExit) as caught:
        managed_config.install_config_if_missing(source, destination)

    assert "destination is non-regular or multiply linked" in str(caught.value)


def test_install_config_refuses_a_multiply_linked_source(tmp_path):
    source = tmp_path / "default.toml"
    source.write_bytes(b"[core]\n")
    os.link(source, tmp_path / "second-link.toml")
    destination = tmp_path / "install/astinuslab.toml"
    destination.parent.mkdir()

    with pytest.raises(SystemExit) as caught:
        managed_config.install_config_if_missing(source, destination)

    assert "source is non-regular or multiply linked" in str(caught.value)


# --------------------------------------------------------------------------
# validate-destinations
# --------------------------------------------------------------------------


def test_validate_destinations_accepts_paths_that_do_not_exist_yet(tmp_path):
    root = tmp_path / "install"
    root.mkdir()

    # This runs before the defaults are installed, so absent is the normal case.
    managed_config.validate_destinations(root, ["astinuslab.toml", "config/astinuslab.env"])


def test_validate_destinations_refuses_a_multiply_linked_destination(tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    (root / "astinuslab.toml").write_bytes(b"x\n")
    os.link(root / "astinuslab.toml", tmp_path / "harvested")

    with pytest.raises(SystemExit) as caught:
        managed_config.validate_destinations(root, ["astinuslab.toml"])

    assert "managed config is non-regular or multiply linked: astinuslab.toml" in str(caught.value)


def test_validate_destinations_refuses_a_destination_that_is_a_directory(tmp_path):
    root = tmp_path / "install"
    (root / "astinuslab.toml").mkdir(parents=True)

    with pytest.raises(SystemExit) as caught:
        managed_config.validate_destinations(root, ["astinuslab.toml"])

    assert "managed config is non-regular or multiply linked: astinuslab.toml" in str(caught.value)


def test_validate_destinations_refuses_a_symlinked_destination(tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    outside = tmp_path / "outside.toml"
    outside.write_bytes(b"foreign\n")
    (root / "astinuslab.toml").symlink_to(outside)

    with pytest.raises(OSError):
        managed_config.validate_destinations(root, ["astinuslab.toml"])


# --------------------------------------------------------------------------
# neutralise-env-auth
# --------------------------------------------------------------------------


def test_neutralise_comments_out_the_keys_that_would_beat_the_toml(tmp_path):
    env_file = tmp_path / "astinuslab.env"
    env_file.write_text(
        "ASTINUSLAB_API_PORT=8000\nASTINUSLAB_AUTH_ENABLED=false\nASTINUSLAB_OTHER=keep\n",
        encoding="utf-8",
    )

    managed_config.neutralise_env_auth_overrides(env_file)

    text = env_file.read_text(encoding="utf-8")
    assert "# ASTINUSLAB_AUTH_ENABLED=false   # managed in astinuslab.toml [auth]" in text
    # Everything the operator set for other reasons survives untouched.
    assert "ASTINUSLAB_API_PORT=8000" in text
    assert "ASTINUSLAB_OTHER=keep" in text


def test_neutralise_also_catches_an_indented_key(tmp_path):
    env_file = tmp_path / "astinuslab.env"
    env_file.write_text("  ASTINUSLAB_ADMIN_API_TOKEN_HASH=sha256:deadbeef\n", encoding="utf-8")

    managed_config.neutralise_env_auth_overrides(env_file)

    assert env_file.read_text(encoding="utf-8").startswith("# ASTINUSLAB_ADMIN_API_TOKEN_HASH=sha256:deadbeef")


def test_neutralise_is_a_no_op_the_second_time(tmp_path):
    env_file = tmp_path / "astinuslab.env"
    env_file.write_text("ASTINUSLAB_AUTH_ENABLED=false\n", encoding="utf-8")
    managed_config.neutralise_env_auth_overrides(env_file)
    once = env_file.read_text(encoding="utf-8")

    managed_config.neutralise_env_auth_overrides(env_file)

    assert env_file.read_text(encoding="utf-8") == once


def test_neutralise_refuses_a_multiply_linked_env_file(tmp_path):
    env_file = tmp_path / "astinuslab.env"
    env_file.write_text("ASTINUSLAB_AUTH_ENABLED=false\n", encoding="utf-8")
    os.link(env_file, tmp_path / "harvested")

    with pytest.raises(SystemExit) as caught:
        managed_config.neutralise_env_auth_overrides(env_file)

    assert "launcher environment is non-regular or multiply linked" in str(caught.value)


# --------------------------------------------------------------------------
# configure-admin-auth
# --------------------------------------------------------------------------


def test_configure_admin_auth_creates_the_file_when_there_is_none(tmp_path):
    path = tmp_path / "astinuslab.toml"

    managed_config.configure_admin_auth(path, enabled="true", token_hash="sha256:aaa")

    text = path.read_text(encoding="utf-8")
    assert "[auth]" in text
    assert "enabled = true" in text
    assert 'admin_api_token_hash = "sha256:aaa"' in text
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_configure_admin_auth_appends_a_section_to_a_config_without_one(tmp_path):
    path = tmp_path / "astinuslab.toml"
    path.write_text("[core]\nname = 'x'\n", encoding="utf-8")

    managed_config.configure_admin_auth(path, enabled="true", token_hash="sha256:bbb")

    text = path.read_text(encoding="utf-8")
    assert "[core]" in text and "name = 'x'" in text
    assert "[auth]" in text


def test_configure_admin_auth_replaces_the_values_in_an_existing_section(tmp_path):
    path = tmp_path / "astinuslab.toml"
    path.write_text(
        "[core]\nname = 'x'\n\n[auth]\n# comment\nenabled = false\nadmin_api_token_hash = \"old\"\n",
        encoding="utf-8",
    )

    managed_config.configure_admin_auth(path, enabled="true", token_hash="sha256:ccc")

    text = path.read_text(encoding="utf-8")
    assert "enabled = true" in text
    assert '"old"' not in text
    assert 'admin_api_token_hash = "sha256:ccc"' in text
    # The operator's own comment is not collateral.
    assert "# comment" in text


def test_configure_admin_auth_keeps_the_newline_after_a_bare_section_header(tmp_path):
    """The regression the body's own comment records.

    With `\\s` instead of `[ \\t]`, a header followed directly by the key ate the
    separating newline and produced "[auth]enabled = true" -- invalid TOML. The
    shipped example has comments in between, which hid it.
    """
    path = tmp_path / "astinuslab.toml"
    path.write_text("[auth]\nenabled = false\n", encoding="utf-8")

    managed_config.configure_admin_auth(path, enabled="true", token_hash="sha256:ddd")

    text = path.read_text(encoding="utf-8")
    assert "[auth]enabled" not in text
    assert "[auth]\n" in text


def test_configure_admin_auth_adds_keys_a_section_is_missing(tmp_path):
    path = tmp_path / "astinuslab.toml"
    path.write_text("[auth]\n# only a comment\n", encoding="utf-8")

    managed_config.configure_admin_auth(path, enabled="false", token_hash="sha256:eee")

    text = path.read_text(encoding="utf-8")
    assert "enabled = false" in text
    assert 'admin_api_token_hash = "sha256:eee"' in text


def test_configure_admin_auth_does_not_reach_into_the_next_section(tmp_path):
    path = tmp_path / "astinuslab.toml"
    path.write_text("[auth]\nenabled = false\n\n[other]\nenabled = false\n", encoding="utf-8")

    managed_config.configure_admin_auth(path, enabled="true", token_hash="sha256:fff")

    text = path.read_text(encoding="utf-8")
    other = text[text.index("[other]") :]
    assert "enabled = false" in other


def test_configure_admin_auth_refuses_a_multiply_linked_config(tmp_path):
    path = tmp_path / "astinuslab.toml"
    path.write_text("[auth]\n", encoding="utf-8")
    os.link(path, tmp_path / "harvested")

    with pytest.raises(SystemExit) as caught:
        managed_config.configure_admin_auth(path, enabled="true", token_hash="sha256:aaa")

    assert "admin config is non-regular, multiply linked" in str(caught.value)


def test_configure_admin_auth_reads_its_secret_from_the_environment_not_argv(tmp_path):
    """/proc/<pid>/cmdline is world-readable; /proc/<pid>/environ is not.

    Passing the hash as an argument would publish it to every local account for
    the lifetime of the call, so the subprocess interface takes it from the
    environment and the shell sets it inline.
    """
    path = tmp_path / "astinuslab.toml"

    result = subprocess.run(
        [sys.executable, str(MODULE), "configure-admin-auth"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "AUTH_CONFIG_PATH": str(path),
            "AUTH_TOKEN_HASH": "sha256:secret",
            "AUTH_ENABLED": "true",
        },
    )

    assert result.returncode == 0, result.stderr
    assert 'admin_api_token_hash = "sha256:secret"' in path.read_text(encoding="utf-8")
