"""Unit tests for the keystore SSH key management module."""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import astinuslab.core.config as config_mod
import astinuslab.storage.keystore as ks_mod

# Captured at import time, before the conftest autouse stub
# (_stub_active_ssh_key) replaces keystore.get_active_key_paths per-test.
_REAL_GET_ACTIVE_KEY_PATHS = ks_mod.get_active_key_paths


@pytest.fixture(autouse=True)
def _unstub_keystore(monkeypatch):
    """These tests exercise the real keystore; undo the global conftest stub."""
    monkeypatch.setattr(ks_mod, "get_active_key_paths", _REAL_GET_ACTIVE_KEY_PATHS)


@pytest.fixture()
def key_dir(tmp_path, monkeypatch):
    """Redirect keystore to a temporary directory."""
    storage_dir = tmp_path / "storage"
    keys_dir = storage_dir / "ssh_keys"
    keys_dir.mkdir(parents=True)
    monkeypatch.setattr(
        ks_mod,
        "_settings",
        SimpleNamespace(base_dir=tmp_path / "base", data_dir=tmp_path / "base", storage_dir=storage_dir),
    )
    return keys_dir


def _plant_key(keys_dir: Path, name: str) -> None:
    """Create a dummy key pair in the temp dir (no real keygen needed)."""
    (keys_dir / name).write_text("PRIVATE_KEY")
    (keys_dir / f"{name}.pub").write_text("ssh-ed25519 AAAA test")


def test_uses_configured_storage_dir_at_call_time(tmp_path, monkeypatch):
    base_dir = tmp_path / "base"
    storage_dir = tmp_path / "isolated-storage"
    legacy_keys_dir = base_dir / "storage" / "ssh_keys"
    expected_keys_dir = storage_dir / "ssh_keys"

    # Cage the legacy import-time path beneath tmp_path before changing the
    # configured storage directory. This keeps the RED test hermetic while
    # proving lookups must happen at call time.
    test_settings = SimpleNamespace(base_dir=base_dir, data_dir=base_dir, storage_dir=tmp_path / "initial-storage")
    settings_patch = pytest.MonkeyPatch()
    settings_patch.setattr(config_mod, "settings", test_settings)
    importlib.reload(ks_mod)

    try:
        test_settings.storage_dir = storage_dir

        def fake_run(command, **_kwargs):
            if command[1:3] == ["-t", "ed25519"]:
                private_path = Path(command[command.index("-f") + 1])
                assert private_path.is_relative_to(tmp_path)
                _plant_key(private_path.parent, private_path.name)
                return SimpleNamespace(returncode=0, stderr="", stdout="")
            return SimpleNamespace(returncode=0, stderr="", stdout="256 SHA256:test isolated (ED25519)")

        monkeypatch.setattr(ks_mod.subprocess, "run", fake_run)

        generated = ks_mod.generate_key("isolated-key")
        ks_mod.set_active("isolated-key")
        private_path, public_path = ks_mod.get_active_key_paths()
        listed = ks_mod.list_keys()

        assert expected_keys_dir.is_dir()
        assert expected_keys_dir.stat().st_mode & 0o777 == 0o700
        assert expected_keys_dir / ".active" == private_path.parent / ".active"
        assert (expected_keys_dir / ".active").read_text() == "isolated-key"
        assert private_path == expected_keys_dir / "isolated-key"
        assert public_path == expected_keys_dir / "isolated-key.pub"
        assert generated["private_path"] == str(private_path)
        assert generated["public_path"] == str(public_path)
        assert listed[0]["private_path"] == str(private_path)
        assert listed[0]["public_path"] == str(public_path)
        assert private_path.read_text() == "PRIVATE_KEY"
        assert private_path.stat().st_mode & 0o777 == 0o600
        assert public_path.read_text() == "ssh-ed25519 AAAA test"
        assert not legacy_keys_dir.exists()
    finally:
        settings_patch.undo()
        importlib.reload(ks_mod)


class TestGetActiveKeyPaths:
    def test_raises_when_no_active_file(self, key_dir):
        with pytest.raises(RuntimeError, match="No active SSH key"):
            ks_mod.get_active_key_paths()

    def test_raises_when_active_file_points_to_missing_key(self, key_dir):
        (key_dir / ".active").write_text("ghost-key")
        with pytest.raises(RuntimeError, match="No active SSH key"):
            ks_mod.get_active_key_paths()

    def test_returns_paths_when_key_present(self, key_dir):
        _plant_key(key_dir, "lab-key")
        (key_dir / ".active").write_text("lab-key")
        priv, pub = ks_mod.get_active_key_paths()
        assert priv.name == "lab-key"
        assert pub.name == "lab-key.pub"

    def test_rejects_active_file_with_traversal_name(self, key_dir):
        (key_dir / ".active").write_text("../astinuslab.db")
        with pytest.raises(RuntimeError, match="No active SSH key"):
            ks_mod.get_active_key_paths()


class TestListKeys:
    def test_empty_dir_returns_empty_list(self, key_dir):
        assert ks_mod.list_keys() == []

    def test_lists_existing_key_pairs(self, key_dir):
        _plant_key(key_dir, "mykey")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1  # fingerprint unavailable
            keys = ks_mod.list_keys()
        assert len(keys) == 1
        assert keys[0]["name"] == "mykey"

    def test_active_flag_set_correctly(self, key_dir):
        _plant_key(key_dir, "mykey")
        (key_dir / ".active").write_text("mykey")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            keys = ks_mod.list_keys()
        assert keys[0]["active"] is True


class TestGenerateKey:
    @pytest.mark.parametrize("name", ["", ".hidden", "../outside", "nested/key", "bad key", "bad$key"])
    def test_rejects_invalid_key_names(self, key_dir, name):
        with pytest.raises(ValueError, match="Invalid key name"):
            ks_mod.generate_key(name)

    def test_passes_timeout_to_ssh_keygen(self, key_dir):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            _plant_key(key_dir, "mykey")
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        with patch("subprocess.run", side_effect=fake_run):
            ks_mod.generate_key("mykey")

        assert calls[0][0][:3] == ["ssh-keygen", "-t", "ed25519"]
        assert calls[0][1]["timeout"] == 30

    def test_preserves_installer_managed_split_group_readability(self, key_dir):
        runner = "libvirt-qemu"
        key_dir.chmod(0o2700)
        subprocess.run(
            [
                "setfacl",
                "-m",
                f"u:{runner}:r-x,m::r-x,d:u:{runner}:r-x,d:g::---,d:m::r-x,d:o::---",
                key_dir,
            ],
            check=True,
        )

        def fake_run(cmd, **_kwargs):
            _plant_key(key_dir, "split-key")
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        with patch("subprocess.run", side_effect=fake_run):
            ks_mod.generate_key("split-key")
        ks_mod.set_active("split-key")

        assert key_dir.stat().st_mode & 0o7777 == 0o2750
        assert (key_dir / "split-key").stat().st_mode & 0o777 == 0o640
        assert (key_dir / ".active").stat().st_mode & 0o777 == 0o640
        private_acl = subprocess.run(
            ["getfacl", "-cp", key_dir / "split-key"], check=True, capture_output=True, text=True
        ).stdout
        assert "group::---" in private_acl
        assert f"user:{runner}:r-x\t#effective:r--" in private_acl


class TestSetActive:
    def test_sets_active_key(self, key_dir):
        _plant_key(key_dir, "mykey")
        ks_mod.set_active("mykey")
        assert (key_dir / ".active").read_text().strip() == "mykey"

    def test_raises_for_nonexistent_key(self, key_dir):
        with pytest.raises(ValueError, match="not found"):
            ks_mod.set_active("ghost")

    def test_rejects_traversal_name_even_if_target_exists(self, key_dir):
        outside = key_dir.parent / "astinuslab.db"
        outside.write_text("db")

        with pytest.raises(ValueError, match="Invalid key name"):
            ks_mod.set_active("../astinuslab.db")

        assert not (key_dir / ".active").exists()


class TestDeleteKey:
    def test_deletes_key_pair(self, key_dir):
        _plant_key(key_dir, "mykey")
        ks_mod.delete_key("mykey")
        assert not (key_dir / "mykey").exists()
        assert not (key_dir / "mykey.pub").exists()

    def test_clears_active_when_deleting_active_key(self, key_dir):
        _plant_key(key_dir, "mykey")
        (key_dir / ".active").write_text("mykey")
        ks_mod.delete_key("mykey")
        assert not (key_dir / ".active").exists()

    def test_raises_for_nonexistent_key(self, key_dir):
        with pytest.raises(ValueError, match="not found"):
            ks_mod.delete_key("ghost")

    def test_rejects_traversal_name_without_unlinking_outside_file(self, key_dir):
        outside = key_dir.parent / "astinuslab.db"
        outside.write_text("db")

        with pytest.raises(ValueError, match="Invalid key name"):
            ks_mod.delete_key("../astinuslab.db")

        assert outside.exists()

    def test_does_not_clear_active_when_deleting_inactive_key(self, key_dir):
        _plant_key(key_dir, "key1")
        _plant_key(key_dir, "key2")
        (key_dir / ".active").write_text("key2")
        ks_mod.delete_key("key1")
        assert (key_dir / ".active").read_text().strip() == "key2"
