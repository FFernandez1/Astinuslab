from __future__ import annotations

import builtins
import importlib
import re
import sys
from pathlib import Path
from types import ModuleType

from astinuslab.core.config import Settings
from astinuslab.forensics import vol3_plugins

ROOT = Path(__file__).resolve().parents[3]
FRONTEND_CATALOG = ROOT / "src" / "astinuslab" / "web" / "static" / "volatility" / "catalog.js"
EXPECTED_LINUX_PLUGINS = (
    "banners.Banners",
    "linux.pslist.PsList",
    "linux.pstree.PsTree",
    "linux.psscan.PsScan",
    "linux.pmap.Pmap",
    "linux.malfind.Malfind",
    "yarascan.YaraScan",
    "linux.check_syscall.Check_syscall",
    "linux.check_modules.Check_modules",
    "linux.sockstat.Sockstat",
    "linux.lsof.Lsof",
    "linux.elfs.Elfs",
    "linux.bash.Bash",
    "linux.lsmod.Lsmod",
)
EXPECTED_WINDOWS_PLUGINS = (
    "windows.info.Info",
    "windows.pslist.PsList",
    "windows.pstree.PsTree",
    "windows.psscan.PsScan",
    "windows.malfind.Malfind",
    "yarascan.YaraScan",
    "windows.netscan.NetScan",
    "windows.netstat.NetStat",
    "windows.cmdline.CmdLine",
    "windows.svcscan.SvcScan",
    "windows.dlllist.DllList",
    "windows.handles.Handles",
)
EXPECTED_WINDOWS_CATALOG_PLUGINS = (
    "windows.info.Info",
    "windows.pslist.PsList",
    "windows.pstree.PsTree",
    "windows.psscan.PsScan",
    "windows.malfind.Malfind",
    "yarascan.YaraScan",
    "windows.netscan.NetScan",
    "windows.netstat.NetStat",
    "windows.dlllist.DllList",
    "windows.handles.Handles",
    "windows.cmdline.CmdLine",
    "windows.svcscan.SvcScan",
)
EXPECTED_CATEGORY_ORDER = ("info", "proc", "mal", "net", "files", "persist")


def _catalog_names(os_family: str) -> tuple[str, ...]:
    catalog = vol3_plugins.catalog_for_os(os_family)
    return tuple(plugin["name"] for category in catalog["categories"] for plugin in category["plugins"])


def test_import_and_catalog_use_never_import_volatility3(monkeypatch):
    attempted_imports: list[str] = []
    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "volatility3" or name.startswith("volatility3."):
            attempted_imports.append(name)
            raise AssertionError(f"unexpected in-process Volatility import: {name}")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    # monkeypatch (not sys.modules.pop) so the original module object is restored:
    # a later test must not end up holding a different instance than it imported.
    monkeypatch.delitem(sys.modules, "astinuslab.forensics.vol3_plugins", raising=False)

    imported = importlib.import_module("astinuslab.forensics.vol3_plugins")
    imported.catalog_for_os("linux")
    imported.catalog_for_os("windows")

    assert attempted_imports == []


def test_repeated_catalog_calls_return_identical_ordered_data():
    for os_family, expected_plugins in (
        ("linux", EXPECTED_LINUX_PLUGINS),
        ("windows", EXPECTED_WINDOWS_CATALOG_PLUGINS),
    ):
        first = vol3_plugins.catalog_for_os(os_family)
        second = vol3_plugins.catalog_for_os(os_family)

        assert first == second
        assert tuple(category["id"] for category in first["categories"]) == EXPECTED_CATEGORY_ORDER
        assert (
            tuple(plugin["name"] for category in first["categories"] for plugin in category["plugins"])
            == expected_plugins
        )


def test_plugins_for_os_preserves_curated_source_order():
    assert tuple(plugin.name for plugin in vol3_plugins._plugins_for_os("linux")) == EXPECTED_LINUX_PLUGINS
    assert tuple(plugin.name for plugin in vol3_plugins._plugins_for_os("windows")) == EXPECTED_WINDOWS_PLUGINS


def test_catalog_for_os_linux_shape():
    cat = vol3_plugins.catalog_for_os("linux")
    assert cat["os_family"] == "linux"
    assert cat["count"] >= 1
    for category in cat["categories"]:
        assert {"id", "label", "plugins"} <= category.keys()
        for plugin in category["plugins"]:
            assert {"name", "category", "description", "os_family"} <= plugin.keys()
            assert plugin["os_family"] == "linux"
    names = {p["name"] for c in cat["categories"] for p in c["plugins"]}
    assert "linux.pslist.PsList" in names
    assert not any(n.startswith("windows.") for n in names)


def test_catalog_for_os_windows_filters_linux():
    cat = vol3_plugins.catalog_for_os("windows")
    names = {p["name"] for c in cat["categories"] for p in c["plugins"]}
    assert "windows.pslist.PsList" in names
    assert not any(n.startswith("linux.") for n in names)


def test_linux_and_windows_catalogs_contain_only_curated_v1_entries():
    assert _catalog_names("linux") == EXPECTED_LINUX_PLUGINS
    assert _catalog_names("windows") == EXPECTED_WINDOWS_CATALOG_PLUGINS


def test_pslist_grouped_in_proc_category():
    cat = vol3_plugins.catalog_for_os("linux")
    proc = next(c for c in cat["categories"] if c["id"] == "proc")
    assert "linux.pslist.PsList" in {p["name"] for p in proc["plugins"]}


def test_plugin_names_for_os_returns_known_set():
    names = vol3_plugins.plugin_names_for_os("linux")
    assert "linux.pslist.PsList" in names
    assert "windows.pslist.PsList" not in names


def test_yarascan_is_curated_for_linux_and_windows():
    assert "yarascan.YaraScan" in vol3_plugins.plugin_names_for_os("linux")
    assert "yarascan.YaraScan" in vol3_plugins.plugin_names_for_os("windows")


def test_is_yara_plugin_identifies_yara_plugins():
    assert vol3_plugins.is_yara_plugin("yarascan.YaraScan") is True
    assert vol3_plugins.is_yara_plugin("windows.vadyarascan.VadYaraScan") is False
    assert vol3_plugins.is_yara_plugin("linux.pslist.PsList") is False


def test_every_yara_plugin_is_in_the_curated_catalog():
    curated_names = set(EXPECTED_LINUX_PLUGINS) | set(EXPECTED_WINDOWS_PLUGINS)
    assert vol3_plugins._YARA_PLUGINS <= curated_names


def test_frontend_yara_plugins_match_backend_curated_yara_plugins():
    source = FRONTEND_CATALOG.read_text(encoding="utf-8")
    function = re.search(r"export function isYaraPlugin\(name\)\s*\{(?P<body>.*?)\}", source, re.DOTALL)
    assert function is not None, f"Missing isYaraPlugin in {FRONTEND_CATALOG.relative_to(ROOT)}"
    frontend_yara_plugins = frozenset(re.findall(r"""name\s*===\s*["']([^"']+)["']""", function.group("body")))
    curated_names = {plugin.name for plugin in vol3_plugins.CURATED}

    assert frontend_yara_plugins == vol3_plugins._YARA_PLUGINS
    assert frontend_yara_plugins <= curated_names


def test_injected_volatility_plugin_never_appears_in_catalog(monkeypatch):
    injected_name = "linux.injected.Injected"
    fake_package = ModuleType("volatility3")
    fake_plugins = ModuleType("volatility3.plugins")
    fake_framework = ModuleType("volatility3.framework")
    setattr(fake_framework, "import_files", lambda *_args: None)
    setattr(fake_framework, "list_plugins", lambda: {injected_name: object()})
    setattr(fake_package, "plugins", fake_plugins)
    setattr(fake_package, "framework", fake_framework)
    monkeypatch.setitem(sys.modules, "volatility3", fake_package)
    monkeypatch.setitem(sys.modules, "volatility3.plugins", fake_plugins)
    monkeypatch.setitem(sys.modules, "volatility3.framework", fake_framework)
    monkeypatch.delitem(sys.modules, "astinuslab.forensics.vol3_plugins", raising=False)

    imported = importlib.import_module("astinuslab.forensics.vol3_plugins")
    catalog = imported.catalog_for_os("linux")
    names = {plugin["name"] for category in catalog["categories"] for plugin in category["plugins"]}

    assert injected_name not in names


def test_default_plugins_are_in_curated_linux_catalog():
    defaults = [name.strip() for name in Settings().vol3_default_plugins.split(",") if name.strip()]
    assert set(defaults) <= set(EXPECTED_LINUX_PLUGINS)


def test_validate_plugin_names_accepts_curated_names_and_preserves_unknown_order():
    names = [
        "linux.pslist.PsList",
        "custom.first.Plugin",
        "linux.bash.Bash",
        "custom.second.Plugin",
        "custom.first.Plugin",
    ]
    assert vol3_plugins.validate_plugin_names("linux", names) == [
        "custom.first.Plugin",
        "custom.second.Plugin",
        "custom.first.Plugin",
    ]


def test_validate_plugin_names_computes_valid_names_once(monkeypatch):
    calls = 0
    real_plugin_names_for_os = vol3_plugins.plugin_names_for_os

    def counted_plugin_names_for_os(os_family):
        nonlocal calls
        calls += 1
        return real_plugin_names_for_os(os_family)

    monkeypatch.setattr(vol3_plugins, "plugin_names_for_os", counted_plugin_names_for_os)

    vol3_plugins.validate_plugin_names("linux", ["unknown.one", "unknown.two", "unknown.three"])

    assert calls == 1


def test_apply_severity_flags_malfind_rwx():
    cols = ["PID", "Process", "Protection"]
    rows = [[8842, "miner", "rwx"], [1, "systemd", "r--"]]
    flags, findings = vol3_plugins.apply_severity("linux.malfind.Malfind", cols, rows)
    assert flags == ["warn", None]
    assert findings == {"crit": 0, "warn": 1}


def test_apply_severity_noop_for_plugin_without_rules():
    flags, findings = vol3_plugins.apply_severity("linux.pslist.PsList", ["PID"], [[1], [2]])
    assert flags == [None, None]
    assert findings == {"crit": 0, "warn": 0}
