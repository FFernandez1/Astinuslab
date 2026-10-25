from __future__ import annotations

import re
import stat
import subprocess
from datetime import datetime
from pathlib import Path

from ..core.config import settings as _settings

_KEY_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _split_group_readable(keys_dir: Path) -> bool:
    return stat.S_IMODE(keys_dir.stat().st_mode) == 0o2750


def _keys_dir(*, create: bool = True) -> Path:
    keys_dir = Path(_settings.storage_dir) / "ssh_keys"
    if create:
        keys_dir.mkdir(parents=True, exist_ok=True)
        if not _split_group_readable(keys_dir):
            keys_dir.chmod(0o700)
    return keys_dir


def _validate_key_name(name: str) -> str:
    if not _KEY_NAME_RE.fullmatch(name or ""):
        raise ValueError("Invalid key name")
    return name


def get_active_key_paths() -> tuple[Path, Path]:
    """Return (private_key, public_key) paths for the currently active key.

    Raises RuntimeError if no key is configured. Use the Security page in
    the web UI to generate and activate a key before launching jobs.
    """
    keys_dir = _keys_dir(create=False)
    active_file = keys_dir / ".active"
    if active_file.exists():
        name = active_file.read_text().strip()
        try:
            safe_name = _validate_key_name(name)
        except ValueError:
            safe_name = ""
        if safe_name:
            priv = keys_dir / safe_name
            pub = keys_dir / f"{safe_name}.pub"
            if priv.exists() and pub.exists():
                return priv, pub
    raise RuntimeError("No active SSH key is configured. Go to Security in the web UI to generate and select one.")


def list_keys() -> list[dict]:
    d = _keys_dir()
    active_file = d / ".active"
    active_name = active_file.read_text().strip() if active_file.exists() else None
    keys = []
    for pub in sorted(d.glob("*.pub")):
        name = pub.stem
        try:
            _validate_key_name(name)
        except ValueError:
            continue
        priv = d / name
        if not priv.exists():
            continue
        try:
            fp = subprocess.run(
                ["ssh-keygen", "-l", "-f", str(pub)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if fp.returncode == 0:
                # ssh-keygen -l outputs: "256 SHA256:xxx comment (ED25519)"
                # Extract just the SHA256:xxx hash — the key type is always
                # ED25519 here so showing it adds no information.
                parts = fp.stdout.strip().split()
                fingerprint = next((p for p in parts if p.startswith("SHA256:")), fp.stdout.strip())
            else:
                fingerprint = "—"
        except Exception:
            fingerprint = "—"
        stat = priv.stat()
        keys.append(
            {
                "name": name,
                "private_path": str(priv),
                "public_path": str(pub),
                "fingerprint": fingerprint,
                "created_at": datetime.fromtimestamp(stat.st_mtime),
                "active": name == active_name,
            }
        )
    return keys


def generate_key(name: str) -> dict:
    """Generate a new ED25519 key pair. Raises ValueError if name is invalid or exists."""
    name = _validate_key_name(name)
    d = _keys_dir()
    priv = d / name
    if priv.exists():
        raise ValueError(f"A key named '{name}' already exists")
    result = subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(priv), "-N", "", "-C", f"astinuslab/{name}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ssh-keygen failed")
    priv.chmod(0o640 if _split_group_readable(d) else 0o600)
    return next(key for key in list_keys() if key["name"] == name)


def set_active(name: str) -> None:
    """Mark a key as the active one."""
    name = _validate_key_name(name)
    d = _keys_dir()
    priv = d / name
    if not priv.exists():
        raise ValueError(f"Key '{name}' not found")
    active_file = d / ".active"
    active_file.write_text(name)
    active_file.chmod(0o640 if _split_group_readable(d) else 0o600)


def delete_key(name: str) -> None:
    """Delete a key pair. If it was the active key, the active selection is cleared."""
    name = _validate_key_name(name)
    d = _keys_dir()
    priv = d / name
    pub = d / f"{name}.pub"
    if not priv.exists():
        raise ValueError(f"Key '{name}' not found")
    active_file = d / ".active"
    if active_file.exists() and active_file.read_text().strip() == name:
        active_file.unlink(missing_ok=True)
    priv.unlink(missing_ok=True)
    pub.unlink(missing_ok=True)
