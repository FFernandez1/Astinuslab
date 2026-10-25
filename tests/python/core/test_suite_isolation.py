"""The suite must not read the checkout it runs in as an operator install.

A developer checkout is also a working AstinusLab data root: it holds the
operator's ``astinuslab.toml``, the images this host downloaded, and the live
``config/catalog/*.yaml``. All three change what the application does, so a
suite that reads them passes or fails by machine. These checks pin the
isolation that ``tests/python/conftest.py`` sets up before the first import.
"""

from __future__ import annotations

from pathlib import Path

from astinuslab.catalog import _preferred_catalog_path
from astinuslab.core.config import settings
from astinuslab.images.image_inspection import BOOTABLE_IMAGE_EXTENSIONS

CHECKOUT = Path(__file__).resolve().parents[3]


def _is_inside_checkout(path: Path) -> bool:
    return path.resolve().is_relative_to(CHECKOUT)


def test_data_root_is_not_the_checkout():
    assert not _is_inside_checkout(Path(settings.data_dir))


def test_no_operator_config_file_is_visible():
    """A ``[packages] enabled = true`` in the operator's file adds real routes."""
    assert not (Path(settings.data_dir) / "astinuslab.toml").exists()


def test_image_discovery_sees_no_host_images():
    """``load_targets`` appends one entry per bootable file in this directory."""
    images_dir = Path(settings.images_dir)
    assert not _is_inside_checkout(images_dir)
    discovered = [path.name for path in images_dir.iterdir() if path.suffix.lower() in BOOTABLE_IMAGE_EXTENSIONS]
    assert discovered == []


def test_catalogs_resolve_to_the_seeded_examples():
    """``_preferred_catalog_path`` prefers a live ``*.yaml`` over the example."""
    for configured in (
        settings.targets_catalog_path,
        settings.scenarios_catalog_path,
        settings.workloads_catalog_path,
        settings.attackers_catalog_path,
        settings.network_nodes_catalog_path,
    ):
        resolved = _preferred_catalog_path(Path(configured))
        assert not _is_inside_checkout(resolved)
        assert resolved.name.endswith(".example.yaml")
