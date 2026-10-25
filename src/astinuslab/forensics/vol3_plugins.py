"""Curated Volatility 3 plugin catalog."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class PluginInfo:
    name: str
    category: str
    description: str
    os_family: str
    severity_hint: str | None = None


# Ordered category definitions.
CATEGORIES: tuple[dict[str, str], ...] = (
    {"id": "info", "label": "System info"},
    {"id": "proc", "label": "Processes"},
    {"id": "mal", "label": "Malware & injection"},
    {"id": "net", "label": "Network"},
    {"id": "files", "label": "Files & handles"},
    {"id": "persist", "label": "Persistence"},
    {"id": "other", "label": "Other"},
)

CURATED: tuple[PluginInfo, ...] = (
    # ── Linux ──
    PluginInfo("banners.Banners", "info", "Linux kernel banners present in the image.", "linux"),
    PluginInfo("linux.pslist.PsList", "proc", "Active process list from task_struct.", "linux"),
    PluginInfo("linux.pstree.PsTree", "proc", "Process hierarchy as a tree.", "linux"),
    PluginInfo("linux.psscan.PsScan", "proc", "Carve process structures (finds unlinked).", "linux", "warn"),
    PluginInfo("linux.pmap.Pmap", "proc", "Memory mappings per process.", "linux"),
    PluginInfo("linux.malfind.Malfind", "mal", "Detect injected / hidden RWX code regions.", "linux", "warn"),
    PluginInfo("yarascan.YaraScan", "mal", "Scan memory with user-supplied YARA rules.", "linux", "warn"),
    PluginInfo(
        "linux.check_syscall.Check_syscall", "mal", "Verify syscall table against known handlers.", "linux", "warn"
    ),
    PluginInfo(
        "linux.check_modules.Check_modules", "mal", "Compare module lists for hidden kernel modules.", "linux", "warn"
    ),
    PluginInfo("linux.sockstat.Sockstat", "net", "Network connections per socket.", "linux"),
    PluginInfo("linux.lsof.Lsof", "files", "Open file descriptors per process.", "linux"),
    PluginInfo("linux.elfs.Elfs", "files", "ELF binaries mapped into process memory.", "linux"),
    PluginInfo("linux.bash.Bash", "persist", "Recover bash history from heap memory.", "linux"),
    PluginInfo("linux.lsmod.Lsmod", "persist", "Loaded kernel modules.", "linux"),
    # ── Windows ──
    PluginInfo("windows.info.Info", "info", "Image identification and kernel details.", "windows"),
    PluginInfo("windows.pslist.PsList", "proc", "Active processes from the EPROCESS list.", "windows"),
    PluginInfo("windows.pstree.PsTree", "proc", "Process hierarchy as a tree.", "windows"),
    PluginInfo("windows.psscan.PsScan", "proc", "Carve EPROCESS structures (finds hidden).", "windows", "warn"),
    PluginInfo("windows.malfind.Malfind", "mal", "Detect injected code in process space.", "windows", "warn"),
    PluginInfo("yarascan.YaraScan", "mal", "Scan memory with user-supplied YARA rules.", "windows", "warn"),
    PluginInfo("windows.netscan.NetScan", "net", "Carve network artifacts from memory.", "windows"),
    PluginInfo("windows.netstat.NetStat", "net", "Active network connections.", "windows"),
    PluginInfo("windows.cmdline.CmdLine", "persist", "Process command-line arguments.", "windows"),
    PluginInfo("windows.svcscan.SvcScan", "persist", "Windows services recovered from memory.", "windows"),
    PluginInfo("windows.dlllist.DllList", "files", "Loaded modules per process.", "windows"),
    PluginInfo("windows.handles.Handles", "files", "Open handles per process.", "windows"),
)

_CATEGORY_LABELS = {c["id"]: c["label"] for c in CATEGORIES}


def _to_dict(plugin: PluginInfo) -> dict[str, str | None]:
    return {
        "name": plugin.name,
        "category": plugin.category,
        "description": plugin.description,
        "os_family": plugin.os_family,
        "severity_hint": plugin.severity_hint,
    }


def _plugins_for_os(os_family: str) -> list[PluginInfo]:
    os_family = (os_family or "linux").strip().lower()
    return [plugin for plugin in CURATED if plugin.os_family == os_family]


def catalog_for_os(os_family: str) -> dict:
    """Return the categorized plugin catalog for an OS family.

    Shape: ``{os_family, count, categories: [{id, label, plugins: [...]}, ...]}``.
    """
    os_family = (os_family or "linux").strip().lower()
    plugins = _plugins_for_os(os_family)
    categories: list[dict] = []
    for category in CATEGORIES:
        in_cat = [_to_dict(p) for p in plugins if p.category == category["id"]]
        if in_cat:
            categories.append({"id": category["id"], "label": category["label"], "plugins": in_cat})
    return {"os_family": os_family, "count": len(plugins), "categories": categories}


def plugin_names_for_os(os_family: str) -> set[str]:
    """Return the set of plugin names valid for the given OS family."""
    return {p.name for p in _plugins_for_os(os_family)}


def validate_plugin_names(os_family: str, names: Sequence[str]) -> list[str]:
    valid_names = plugin_names_for_os(os_family)
    return [name for name in names if name not in valid_names]


# Plugins that require user-supplied YARA rules.
_YARA_PLUGINS = frozenset({"yarascan.YaraScan"})


def is_yara_plugin(plugin: str) -> bool:
    """True for plugins that require user-supplied YARA rules."""
    return plugin in _YARA_PLUGINS


# ── Severity heuristics ──────────────────────────────────────────────────────
# Volatility 3 output carries no severity. These curated, intentionally narrow
# rules flag rows that commonly warrant attention (e.g. RWX memory regions from
# malfind). They are advisory hints, not threat detection.
@dataclass(frozen=True)
class SeverityRule:
    column_contains: str  # case-insensitive substring identifying the column
    value_contains: str  # case-insensitive substring that must appear in the cell
    severity: str  # "warn" | "crit"


SEVERITY_RULES: dict[str, tuple[SeverityRule, ...]] = {
    "linux.malfind.Malfind": (SeverityRule("protection", "rwx", "warn"),),
    "windows.malfind.Malfind": (SeverityRule("protection", "rwx", "warn"),),
}

_SEVERITY_ORDER = {None: 0, "warn": 1, "crit": 2}


def _max_severity(a: str | None, b: str) -> str:
    return a if a is not None and _SEVERITY_ORDER[a] >= _SEVERITY_ORDER[b] else b


def apply_severity(plugin: str, columns: list[str], rows: list[list]) -> tuple[list[str | None], dict[str, int]]:
    """Return per-row severity flags and aggregate ``{crit, warn}`` counts.

    Plugins without curated rules yield all-``None`` flags and zero counts.
    """
    flags: list[str | None] = [None] * len(rows)
    rules = SEVERITY_RULES.get(plugin, ())
    if rules:
        lowered = [c.lower() for c in columns]
        for row_index, row in enumerate(rows):
            for rule in rules:
                col_index = next((i for i, name in enumerate(lowered) if rule.column_contains in name), None)
                if col_index is None or col_index >= len(row):
                    continue
                if rule.value_contains in str(row[col_index]).lower():
                    flags[row_index] = _max_severity(flags[row_index], rule.severity)
    findings = {
        "crit": sum(1 for f in flags if f == "crit"),
        "warn": sum(1 for f in flags if f == "warn"),
    }
    return flags, findings
