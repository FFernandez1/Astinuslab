"""Release-owned metadata for external operator-supplied forensic tools."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AuditedBinary:
    name: str
    version: str
    commit: str
    architecture: str
    download_url: str
    sha256: str


DWARF2JSON_AMD64 = AuditedBinary(
    name="dwarf2json-linux-amd64",
    version="0.9.0",
    commit="9f14607e0d339d463ea725fbd5c08aa7b7d40f75",
    architecture="linux-amd64",
    download_url=("https://github.com/volatilityfoundation/dwarf2json/releases/download/v0.9.0/dwarf2json-linux-amd64"),
    sha256="e2e75ba5bc9c22bc38a48edffbb050456d089d54998d54f46cb580223d9fbc6b",
)
