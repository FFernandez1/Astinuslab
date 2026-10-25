"""Windows PDB symbol sourcing for Volatility 3.

Unlike Linux, Windows Volatility 3 analysis does not build a per-kernel ISF from
DWARF. It resolves an Intermediate Symbol Format table for the guest's kernel
build from PDB symbols, which Volatility 3 downloads from the Microsoft Symbol
Server (or pre-packaged ISF symbol zips). For air-gapped labs an offline PDB/ISF
cache directory is used instead.

These are pure config/resolution helpers so the workflow can decide a symbol
source without contacting the network during unit tests.
"""

from __future__ import annotations

from pathlib import Path

MICROSOFT_SYMBOL_SERVER = "https://msdl.microsoft.com/download/symbols"

# Pre-built Volatility 3 Windows ISF symbol packs (mirrors of the upstream
# symbol repository). Volatility 3 also auto-downloads these by default.
VOLATILITY_WINDOWS_ISF_PACKS = ("https://downloads.volatilityfoundation.org/volatility3/symbols/windows.zip",)


def windows_symbol_cache_dir(symbols_root: Path) -> Path:
    """Return the Windows symbol cache directory under the Vol3 symbols root.

    Linux ISF tables live under ``<symbols_root>/linux``; Windows tables live
    under ``<symbols_root>/windows`` to keep the two symbol stores separate.
    """
    return symbols_root / "windows"


def microsoft_symbol_server_url() -> str:
    """Return the Microsoft Symbol Server base URL for PDB downloads."""
    return MICROSOFT_SYMBOL_SERVER


def volatility_windows_symbol_pack_urls() -> list[str]:
    """Return the Volatility 3 prebuilt Windows ISF symbol pack URLs."""
    return list(VOLATILITY_WINDOWS_ISF_PACKS)


def resolve_windows_symbol_source(*, offline_cache_dir: Path | None) -> dict[str, str]:
    """Choose where Windows symbols come from for an analysis run.

    A populated offline cache is preferred (works air-gapped); otherwise the
    Microsoft Symbol Server is used.
    """
    if offline_cache_dir and offline_cache_dir.exists() and any(offline_cache_dir.iterdir()):
        return {"source": "offline_cache", "location": str(offline_cache_dir)}
    return {"source": "microsoft_symbol_server", "location": MICROSOFT_SYMBOL_SERVER}
