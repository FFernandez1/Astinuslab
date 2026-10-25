"""Compatibility links to the separate documentation website."""

from __future__ import annotations

import sysconfig
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from ..core.config import settings
from ..i18n import SUPPORTED_LANGS
from .job_context import _get_lang

router = APIRouter()
_SECTIONS = frozenset({"user", "usage", "reference", "api", "architecture", "development", "community"})
_ROOT_PAGES = frozenset({"index", "LICENSE", "THIRD_PARTY_NOTICES"})


def docs_root() -> Path:
    """Source deployments keep docs beside config; wheels install them as data."""
    source = settings.base_dir / "docs"
    if (source / "user").is_dir():
        return source
    return Path(sysconfig.get_path("data")) / "share" / "astinuslab" / "docs"


def _published(path: str) -> bool:
    if any(ord(character) < 32 for character in path):
        return False
    parts = PurePosixPath(path).parts
    if not parts or path.startswith("/") or "\\" in path or ".." in parts or not path.endswith(".md"):
        return False
    stem = parts[-1][:-3]
    if stem.rsplit(".", 1)[-1] in SUPPORTED_LANGS:
        stem = stem.rsplit(".", 1)[0]
    return (len(parts) == 1 and stem in _ROOT_PAGES) or (len(parts) == 2 and parts[0] in _SECTIONS)


@router.get("/help", include_in_schema=False)
def help_index(request: Request) -> RedirectResponse:
    return help_page(request, "usage/getting-started.md")


@router.get("/help/{doc_path:path}", include_in_schema=False)
def help_page(request: Request, doc_path: str) -> RedirectResponse:
    if not _published(doc_path):
        raise HTTPException(status_code=404, detail="Guide not found")
    root = docs_root().resolve()
    path = root / doc_path
    resolved = path.resolve()
    if resolved != path or not resolved.is_relative_to(root) or not resolved.is_file():
        raise HTTPException(status_code=404, detail="Guide not found")
    # MkDocs turns suffix translations into language directories, with English
    # at the site root and English fallback content at untranslated locale URLs.
    language = _get_lang(request)
    stem = path.stem
    if stem.rsplit(".", 1)[-1] in SUPPORTED_LANGS:
        stem, language = stem.rsplit(".", 1)
    target = PurePosixPath(doc_path).with_name(stem)
    if stem == "index":
        target = target.parent
    relative_url = "" if str(target) == "." else quote(str(target), safe="/") + "/"
    prefix = "" if language == "en" else language + "/"
    return RedirectResponse(f"http://localhost:{settings.docs_port}/{prefix}{relative_url}", status_code=303)
