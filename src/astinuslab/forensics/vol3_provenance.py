"""Execution evidence shared by batch and interactive Volatility runners.

Hashes identify bytes at the recorded boundary, not acquisition validity or the
entire external Python package behind a launcher. No external tools are installed
or imported to construct this record.
"""

from __future__ import annotations

import hashlib
import shlex
import shutil
from pathlib import Path
from typing import Any

from .vol3_tools import VerifiedVolatility

_TRUSTED_ISF_ORIGINS = frozenset({"prebuilt_isf", "debuginfod", "host_fetch"})


def isf_trust(source: str | None) -> str:
    return "verified_origin" if source in _TRUSTED_ISF_ORIGINS else "unverified"


def file_identity(path: Path, *, content: bool = False) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    chunks = []
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
            if content:
                chunks.append(chunk)
    result: dict[str, Any] = {"path": str(path), "sha256": digest.hexdigest(), "size_bytes": size}
    if content:
        result["content"] = b"".join(chunks).decode("utf-8")
    return result


def tool_identity(verified: VerifiedVolatility) -> dict[str, Any]:
    executable = verified.path.resolve()
    record: dict[str, Any] = {
        "version": verified.version,
        "executable": file_identity(executable),
        "package_identity_verified": False,
        "identity_scope": "Executable/launcher bytes only; imported packages, plugins and native libraries are not identified by this hash.",
        "interpreter": None,
    }
    with executable.open("rb") as handle:
        first_line = handle.readline(4096)
    if first_line.startswith(b"#!"):
        shebang = first_line[2:].decode("utf-8", errors="replace").strip()
        record["shebang"] = shebang
        parts = shlex.split(shebang)
        if parts:
            interpreter = Path(parts[0])
            record["interpreter"] = file_identity(interpreter.resolve())
            # /usr/bin/env selects another program at launch. Record only simple
            # unambiguous forms; never claim an inferred interpreter is verified.
            if interpreter.name == "env":
                candidate = shutil.which(parts[1]) if len(parts) == 2 and not parts[1].startswith("-") else None
                record["env_interpreter_candidate"] = file_identity(Path(candidate).resolve()) if candidate else None
                record["interpreter_resolution_verified"] = False
            else:
                record["interpreter_resolution_verified"] = True
    return record


def execution_files(stdout: Path, stderr: Path, config: Path, rules: Path | None = None) -> dict[str, Any]:
    files: dict[str, Any] = {}
    for name, path in (("stdout", stdout), ("stderr", stderr), ("saved_config", config), ("yara_rules", rules)):
        if path is None:
            continue
        try:
            files[name] = file_identity(path, content=name in {"saved_config", "yara_rules"})
        except (OSError, UnicodeError) as exc:
            files[name] = {"path": str(path), "sha256": None, "error": str(exc)}
    return files


def has_output(path: Path) -> bool:
    with path.open("rb") as handle:
        return any(chunk.strip() for chunk in iter(lambda: handle.read(65536), b""))
