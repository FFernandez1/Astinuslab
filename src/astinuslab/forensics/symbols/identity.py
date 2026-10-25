from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SymbolIdentity:
    architecture: str
    kernel_release: str
    build_id: str | None
    source: str
    distro: str | None = None
    version: str | None = None


def _safe_part(value: str | None) -> str:
    value = value or "unknown"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "unknown"


def isf_filename(identity: SymbolIdentity) -> str:
    if identity.build_id:
        parts = [identity.architecture, identity.kernel_release, identity.build_id, identity.source]
    else:
        parts = [
            identity.architecture,
            identity.kernel_release,
            identity.distro or "",
            identity.version or "",
            identity.source,
        ]
    return "__".join(_safe_part(part) for part in parts) + ".json"
