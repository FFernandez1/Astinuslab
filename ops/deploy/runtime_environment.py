"""Run installed checks with EnvironmentFile values, without executing shell input.

The interpreter starting this file must use -I and a clean environment. Root DB
maintenance passes only AstinusLab settings (and HOME for path expansion) to the
release interpreter; runtime-writable PATH/PYTHONPATH/LD_* cannot load root code.
Service-identity probes may use the complete service environment.
"""

from __future__ import annotations

import argparse
import os
import pwd
import re
from pathlib import Path


def read_environment(path: Path) -> dict[str, str]:
    """Parse systemd EnvironmentFile quoting; never expand variables/commands.

    Reject malformed assignments instead of risking maintenance on a fallback DB.
    Missing optional files, blank lines and whole-line comments are supported.
    """
    try:
        # EnvironmentFile preserves literal CR inside values; universal-newline
        # decoding would silently select a different path from systemd.
        with path.open(encoding="utf-8", newline="") as stream:
            text = stream.read()
    except FileNotFoundError:
        return {}
    if any(ord(c) in (0, 0xFEFF) or 0xFDD0 <= ord(c) <= 0xFDEF or ord(c) & 0xFFFF in (0xFFFE, 0xFFFF) for c in text):
        raise ValueError(f"invalid environment encoding in {path}")
    result = {}
    pos = 0
    while pos < len(text):
        end = text.find("\n", pos)
        if end < 0:
            end = len(text)
        line = text[pos:end].lstrip(" \t\r")
        if not line or line.startswith(("#", ";")):
            pos = end + 1
            continue
        if "=" not in line:
            raise ValueError(f"invalid environment assignment in {path}")
        key = line.split("=", 1)[0].strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"invalid environment name in {path}")
        pos = text.index("=", pos, end) + 1
        while pos < len(text) and text[pos] in " \t\r":
            pos += 1
        quote = text[pos] if pos < len(text) and text[pos] in "'\"" else ""
        if quote:
            pos += 1
        value: list[str] = []
        last_significant = 0
        closed = not quote
        while pos < len(text):
            char = text[pos]
            pos += 1
            if quote and char == quote:
                closed = True
                end = text.find("\n", pos)
                if end < 0:
                    end = len(text)
                if text[pos:end].strip(" \t\r"):
                    raise ValueError(f"trailing text after quoted environment value in {path}")
                pos = end + 1
                break
            if not quote and char == "\n":
                break
            if char == "\\" and quote != "'":
                if pos >= len(text):
                    raise ValueError(f"incomplete environment escape in {path}")
                following = text[pos]
                pos += 1
                if following == "\n":
                    # Entering an escape makes preceding whitespace significant
                    # in systemd, even when the escape only continues the line.
                    last_significant = len(value)
                    continue
                if quote == '"' and following not in '\\`$"':
                    value.append("\\")
                value.append(following)
                # Escaped whitespace is data, including at the end of a path.
                last_significant = len(value)
            else:
                value.append(char)
                if char not in " \t\r":
                    last_significant = len(value)
        if not closed:
            raise ValueError(f"unterminated environment quote in {path}")
        result[key] = "".join(value) if quote else "".join(value[:last_significant])
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--settings-only", action="store_true")
    parser.add_argument("--user")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    identity = pwd.getpwnam(args.user) if args.user else pwd.getpwuid(os.getuid())
    environment = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": identity.pw_dir,
        "ASTINUSLAB_DATA_DIR": str(args.data_dir),
        "ASTINUSLAB_API_HOST": "127.0.0.1",
        "ASTINUSLAB_API_PORT": "8000",
        "ASTINUSLAB_API_LOG_LEVEL": "info",
    }
    overrides = read_environment(args.data_dir / "config/astinuslab.env")
    if args.settings_only:
        overrides = {k: v for k, v in overrides.items() if k.startswith("ASTINUSLAB_") or k == "HOME"}
    environment.update(overrides)
    if args.settings_only:
        environment["PYTHONNOUSERSITE"] = "1"
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command or not Path(command[0]).is_absolute():
        parser.error("an absolute executable is required after --")
    os.execve(command[0], command, environment)  # noqa: S606 -- explicit argv, absolute executable, no shell


if __name__ == "__main__":
    main()
