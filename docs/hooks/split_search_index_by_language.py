"""Split the combined search index into one file per language.

mkdocs-static-i18n builds a single ``search/search_index.json`` that contains every
locale's pages (English plus the localized fallback copies), so search on any locale
returns results from all four languages. The plugin's ``reconfigure_search`` only
adds lunr stemmers and de-duplicates entries; it never scopes results per locale.

This hook runs after the build and partitions that combined index by the locale
prefix of each entry's ``location`` (e.g. ``es/user/...``). It rewrites the root
``search/search_index.json`` to hold only the default-language entries and writes a
per-locale ``<locale>/search/search_index.json`` for every other built language.
``docs/overrides/main.html`` points each localized page's search request at its own
index, so search stays within the selected language.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from mkdocs.plugins import event_priority

log = logging.getLogger("mkdocs.plugins.i18n_search_split")


def _locale_of(location: str, locales: set[str]) -> str | None:
    """Return the locale prefix of a search entry ``location``, or None for default."""
    first = location.split("/", 1)[0]
    return first if first in locales else None


# Run after every normal plugin (including search, which writes the index).
@event_priority(-100)
def on_post_build(config, **kwargs) -> None:
    i18n = config.plugins.get("i18n")
    if i18n is None:
        return

    languages = i18n.config.languages
    other_locales = {lang.locale for lang in languages if lang.build and not lang.default and lang.locale != "null"}
    if not other_locales:
        return

    site_dir = Path(config.site_dir)
    root_index = site_dir / "search" / "search_index.json"
    if not root_index.exists():
        log.warning("i18n_search_split: %s not found, skipping", root_index)
        return

    data = json.loads(root_index.read_text(encoding="utf-8"))

    buckets: dict[str | None, list] = {locale: [] for locale in other_locales}
    buckets[None] = []
    for entry in data.get("docs", []):
        buckets[_locale_of(entry.get("location", ""), other_locales)].append(entry)

    # Root index keeps only the default-language entries.
    root_index.write_text(
        json.dumps({**data, "docs": buckets[None]}, ensure_ascii=False),
        encoding="utf-8",
    )

    # One index per additional built locale, next to that locale's pages.
    for locale in other_locales:
        target = site_dir / locale / "search" / "search_index.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({**data, "docs": buckets[locale]}, ensure_ascii=False),
            encoding="utf-8",
        )

    log.info(
        "i18n_search_split: root=%d, %s",
        len(buckets[None]),
        ", ".join(f"{loc}={len(buckets[loc])}" for loc in sorted(other_locales)),
    )
