import re
from pathlib import Path

STATIC = Path(__file__).parents[3] / "src" / "astinuslab" / "web" / "static"


def test_no_ad_hoc_version_query_in_module_imports():
    """ESM imports must not carry hand-maintained ?v= query strings."""
    offenders = []
    for js in STATIC.rglob("*.js"):
        if js.name == "htmx.min.js":
            continue
        text = js.read_text(encoding="utf-8")
        for match in re.finditer(r'import\s+.*?from\s+["\'][^"\']*\?v=', text):
            offenders.append(f"{js.relative_to(STATIC)}: {match.group(0)}")
    assert not offenders, f"ad-hoc ?v= in ESM imports: {offenders}"
