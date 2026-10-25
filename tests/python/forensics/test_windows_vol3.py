"""Volatility 3 Windows support: os-family branch + PDB symbol sourcing."""

from __future__ import annotations

from astinuslab.forensics.symbols.windows import (
    microsoft_symbol_server_url,
    resolve_windows_symbol_source,
    volatility_windows_symbol_pack_urls,
    windows_symbol_cache_dir,
)
from astinuslab.forensics.vol3_support import os_family_for_distro, requires_isf_build


class TestOsFamilyForDistro:
    def test_windows_server_is_windows(self):
        assert os_family_for_distro("windows-server") == "windows"

    def test_windows_is_windows(self):
        assert os_family_for_distro("windows") == "windows"

    def test_ubuntu_is_linux(self):
        assert os_family_for_distro("ubuntu") == "linux"

    def test_none_is_linux(self):
        assert os_family_for_distro(None) == "linux"


class TestRequiresIsfBuild:
    def test_linux_requires_isf_build(self):
        # Linux guests need a distro-specific ISF built from kernel symbols.
        assert requires_isf_build("linux") is True

    def test_windows_does_not_require_isf_build(self):
        # Windows uses PDB symbols from the Microsoft Symbol Server, not a build.
        assert requires_isf_build("windows") is False


class TestWindowsSymbolSourcing:
    def test_cache_dir_under_windows(self, tmp_path):
        d = windows_symbol_cache_dir(tmp_path)
        assert d == tmp_path / "windows"

    def test_microsoft_symbol_server_url(self):
        assert microsoft_symbol_server_url().startswith("https://msdl.microsoft.com")

    def test_pack_urls_are_https(self):
        urls = volatility_windows_symbol_pack_urls()
        assert urls and all(u.startswith("https://") for u in urls)

    def test_offline_cache_preferred_when_populated(self, tmp_path):
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "ntkrnlmp.pdb.json").write_text("{}")
        source = resolve_windows_symbol_source(offline_cache_dir=cache)
        assert source["source"] == "offline_cache"

    def test_falls_back_to_microsoft_when_cache_empty(self, tmp_path):
        cache = tmp_path / "cache"
        cache.mkdir()
        source = resolve_windows_symbol_source(offline_cache_dir=cache)
        assert source["source"] == "microsoft_symbol_server"
