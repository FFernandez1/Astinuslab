"""Contract for the disposable data root the suite runs against.

``tests/python/conftest.py`` seeds a temporary ``ASTINUSLAB_DATA_DIR`` so the
suite stops reading the operator's catalogs, images and ``astinuslab.toml``.
Seeding it is only half the job: several ``Settings`` defaults name a *file*
under that root, and SQLite will not create a file whose parent directory is
missing.

The Celery broker and result backend are the two that bite. Both default to
``<data_root>/storage/<name>.db``, derived from the data root rather than from
``ASTINUSLAB_STORAGE_DIR``, which points somewhere else entirely. With the
directory absent, every task result write failed with

    sqlite3.OperationalError: unable to open database file

and surfaced as an HTTP 500. The whole-suite run hid it, because
``tests/python/lab_packages`` creates that directory as a side effect and sorts
ahead of ``orchestration`` and ``playground`` -- so only the documented
per-folder commands broke, 115 tests at a time.

This module lives in ``core`` so it is collected before ``lab_packages`` and
fails on the seeding itself rather than on whatever ran first.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import pytest

from astinuslab.core.config import settings

pytestmark = pytest.mark.unit


def _sqlite_path(url: str) -> Path:
    """Filesystem path inside a ``sqla+sqlite:///`` or ``db+sqlite:///`` URL."""
    _, _, sqlite_url = url.partition("+")
    return Path(urlsplit(sqlite_url).path)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param(settings.celery_broker_url, id="broker"),
        pytest.param(settings.celery_result_backend, id="result-backend"),
    ],
)
def test_celery_sqlite_parents_exist_under_the_seeded_data_root(url: str) -> None:
    parent = _sqlite_path(url).parent
    assert parent.is_dir(), f"{parent} must exist before SQLite can create {_sqlite_path(url).name}"


def test_the_seeded_data_root_carries_only_the_example_catalogs() -> None:
    """Leaving the live names absent is what makes the catalog fallback deterministic."""
    catalog_dir = settings.data_dir / "config" / "catalog"
    assert catalog_dir.is_dir()
    present = {path.name for path in catalog_dir.glob("*.yaml")}
    assert present, "the seeded root must carry the committed example catalogs"
    live = {name for name in present if not name.endswith(".example.yaml")}
    assert not live, f"the seeded root must not carry live catalog names: {sorted(live)}"


def test_the_seeded_image_directory_is_empty() -> None:
    """``load_targets`` appends one discovered target per bootable file it finds."""
    definitions = settings.data_dir / "images" / "definitions"
    assert definitions.is_dir()
    assert list(definitions.iterdir()) == []
