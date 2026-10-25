"""Drop the per-page ``<link rel="alternate" hreflang>`` tags from each page.

mkdocs-static-i18n renders page-level hreflang links in every page ``<head>``
(e.g. ``../../es/architecture/overview/``). Material for MkDocs' alternate-version
integration treats each such link as a separate site root and fetches
``<href>/sitemap.xml`` for it, producing a 404 per language on every page — those
sitemaps never exist. Material's sitemap resolution is meant for multi-site /
version setups, not i18n: the visible language switcher already links straight to
the translated page, so the machinery is redundant here and only adds 404 noise.

Removing the ``rel="alternate"`` hreflang links leaves the switcher untouched and
stops the fetches. The RSS ``rel="alternate"`` links carry no ``hreflang`` and are
left in place.
"""

from __future__ import annotations

import re

_ALTERNATE_HREFLANG = re.compile(r'[ \t]*<link\b[^>]*\brel="alternate"[^>]*\bhreflang="[^"]*"[^>]*>\r?\n?')


def on_post_page(output: str, **kwargs) -> str:
    return _ALTERNATE_HREFLANG.sub("", output)
