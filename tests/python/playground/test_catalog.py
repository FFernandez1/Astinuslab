"""Tests for the playground-mode catalog filter."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import astinuslab.catalog as catalog
from tests.python._support.helpers import autospec_setattr

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_YAML = """
version: 1
targets:
  - id: ubuntu-minimal
    enabled: true
    distro:
      slug: ubuntu
      version: "22.04"
      family: debian
    image:
      source_type: qcow2
      image_ref: ubuntu-22.04-minimal.qcow2
      default_username: ubuntu
      cloud_init: true
    virtualization:
      os_variant: generic
  - id: ubuntu-with-lifecycle
    enabled: true
    distro:
      slug: ubuntu
      version: "24.04"
      family: debian
    image:
      source_type: img
      image_ref: ubuntu-24.04-server.img
      default_username: ubuntu
    virtualization:
      os_variant: ubuntu24.04
    lifecycle:
      destroy_vm_after_job: true
      keep_failed_vm: false
""".strip()

_MISSING_ID_YAML = """
version: 1
targets:
  - enabled: true
    distro:
      slug: ubuntu
      version: "22.04"
      family: debian
    image:
      source_type: qcow2
      image_ref: ubuntu-no-id.qcow2
      default_username: ubuntu
    virtualization:
      os_variant: generic
""".strip()

_MISSING_IMAGE_REF_YAML = """
version: 1
targets:
  - id: ubuntu-no-image-ref
    enabled: true
    distro:
      slug: ubuntu
      version: "22.04"
      family: debian
    image:
      source_type: qcow2
      default_username: ubuntu
    virtualization:
      os_variant: generic
""".strip()

_MISSING_SOURCE_TYPE_YAML = """
version: 1
targets:
  - id: ubuntu-no-source-type
    enabled: true
    distro:
      slug: ubuntu
      version: "22.04"
      family: debian
    image:
      image_ref: ubuntu-no-source-type.qcow2
      default_username: ubuntu
    virtualization:
      os_variant: generic
""".strip()

_MISSING_DISTRO_SLUG_YAML = """
version: 1
targets:
  - id: ubuntu-no-distro
    enabled: true
    distro:
      version: "22.04"
      family: debian
    image:
      source_type: qcow2
      image_ref: ubuntu-no-distro.qcow2
      default_username: ubuntu
    virtualization:
      os_variant: generic
""".strip()

_MISSING_USERNAME_YAML = """
version: 1
targets:
  - id: ubuntu-no-username
    enabled: true
    distro:
      slug: ubuntu
      version: "22.04"
      family: debian
    image:
      source_type: qcow2
      image_ref: ubuntu-no-username.qcow2
    virtualization:
      os_variant: generic
""".strip()

_AVAILABILITY_YAML = """
version: 1
targets:
  - id: ubuntu-present
    enabled: true
    distro:
      slug: ubuntu
      version: "22.04"
      family: debian
    image:
      source_type: qcow2
      image_ref: ubuntu-present.qcow2
      default_username: ubuntu
    virtualization:
      os_variant: generic
  - id: ubuntu-missing
    enabled: true
    distro:
      slug: ubuntu
      version: "22.04"
      family: debian
    image:
      source_type: qcow2
      image_ref: ubuntu-missing.qcow2
      default_username: ubuntu
    virtualization:
      os_variant: generic
  - id: ubuntu-disabled
    enabled: false
    distro:
      slug: ubuntu
      version: "22.04"
      family: debian
    image:
      source_type: qcow2
      image_ref: ubuntu-disabled.qcow2
      default_username: ubuntu
    virtualization:
      os_variant: generic
""".strip()

_NETWORK_NODES_YAML = """
version: 1
network_nodes:
  - id: openwrt
    display_name: OpenWrt Network Node
    image_ref: openwrt.qcow2
    username: root
    os_variant: generic
    cloud_init: false
    enabled: true
    function_tags:
      - firewall
      - router
    defaults:
      vcpus: 1
      memory_mb: 512
      disk_gb: 4
  - id: disabled-node
    display_name: Disabled Node
    image_ref: disabled.qcow2
    enabled: false
  - id: missing-image-node
    display_name: Missing Image Node
    image_ref: missing.qcow2
    enabled: true
""".strip()


def _patch_catalog(
    tmp_path,
    monkeypatch,
    yaml_content: str,
    available_image_refs: tuple[str, ...] = ("ubuntu-22.04-minimal.qcow2", "ubuntu-24.04-server.img"),
):
    """Write yaml_content to a tmp catalog file, patch catalog.settings, clear caches."""
    catalog_yaml = tmp_path / "targets.yaml"
    catalog_yaml.write_text(yaml_content, encoding="utf-8")
    images_dir = tmp_path / "images"
    images_dir.mkdir(exist_ok=True)
    for image_ref in available_image_refs:
        (images_dir / image_ref).write_bytes(b"placeholder image")
    monkeypatch.setattr(
        catalog,
        "settings",
        SimpleNamespace(
            base_dir=tmp_path,
            data_dir=tmp_path,
            targets_catalog_path=catalog_yaml,
            attackers_catalog_path=tmp_path / "attackers.yaml",
            network_nodes_catalog_path=tmp_path / "network_nodes.yaml",
            images_dir=images_dir,
        ),
    )
    autospec_setattr(monkeypatch, catalog, "_supported_os_variants", lambda: {"ubuntu24.04", "generic"})
    autospec_setattr(monkeypatch, catalog, "_scan_images_dir", lambda images_dir: [])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestListPlaygroundUsableImages:
    def test_returns_both_entries_for_valid_minimal_yaml(self, tmp_path, monkeypatch):
        _patch_catalog(tmp_path, monkeypatch, _MINIMAL_YAML)
        results = catalog.list_playground_usable_images()
        ids = [r["id"] for r in results]
        assert "ubuntu-minimal" in ids
        assert "ubuntu-with-lifecycle" in ids

    def test_returns_list_type(self, tmp_path, monkeypatch):
        _patch_catalog(tmp_path, monkeypatch, _MINIMAL_YAML)
        results = catalog.list_playground_usable_images()
        assert isinstance(results, list)

    def test_returned_entries_are_dicts(self, tmp_path, monkeypatch):
        _patch_catalog(tmp_path, monkeypatch, _MINIMAL_YAML)
        for entry in catalog.list_playground_usable_images():
            assert isinstance(entry, dict)

    def test_catalog_path_not_found_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            catalog,
            "settings",
            SimpleNamespace(
                base_dir=tmp_path,
                data_dir=tmp_path,
                targets_catalog_path=tmp_path / "nonexistent.yaml",
                attackers_catalog_path=tmp_path / "attackers.yaml",
                network_nodes_catalog_path=tmp_path / "network_nodes.yaml",
                images_dir=tmp_path / "images",
            ),
        )
        results = catalog.list_playground_usable_images()
        assert results == []

    def test_flat_shape_has_path_key(self, tmp_path, monkeypatch):
        _patch_catalog(tmp_path, monkeypatch, _MINIMAL_YAML)
        results = catalog.list_playground_usable_images()
        for img in results:
            assert "path" in img

    def test_flat_shape_path_includes_image_ref(self, tmp_path, monkeypatch):
        _patch_catalog(tmp_path, monkeypatch, _MINIMAL_YAML)
        results = catalog.list_playground_usable_images()
        minimal = next(r for r in results if r["id"] == "ubuntu-minimal")
        assert minimal["path"].endswith("ubuntu-22.04-minimal.qcow2")

    def test_flat_shape_has_format_key(self, tmp_path, monkeypatch):
        _patch_catalog(tmp_path, monkeypatch, _MINIMAL_YAML)
        results = catalog.list_playground_usable_images()
        minimal = next(r for r in results if r["id"] == "ubuntu-minimal")
        assert minimal["format"] == "qcow2"

    def test_flat_shape_has_architecture_key(self, tmp_path, monkeypatch):
        _patch_catalog(tmp_path, monkeypatch, _MINIMAL_YAML)
        results = catalog.list_playground_usable_images()
        for img in results:
            assert "architecture" in img
            assert img["architecture"] == "x86_64"

    def test_curated_distro_only_images_produce_supported_package_provenance(self, tmp_path, monkeypatch):
        from astinuslab.lab_packages.export_service import image_spec_from_catalog

        _patch_catalog(tmp_path, monkeypatch, _MINIMAL_YAML)

        images = catalog.list_playground_images_for_role("target")

        assert {image["id"] for image in images} == {"ubuntu-minimal", "ubuntu-with-lifecycle"}
        for image in images:
            # Exercise the same catalog adapters and provenance builder used by
            # clean-run verification, including qcow2 and .img source names.
            provenance = image_spec_from_catalog(image["id"], image)
            assert provenance.architecture == "x86_64"
            assert provenance.distro == "ubuntu"

    @pytest.mark.parametrize(
        ("metadata", "expected"),
        [
            ({"architecture": "x86_64"}, "x86_64"),
            ({"arch": "x86_64"}, "x86_64"),
            ({"architecture": "aarch64"}, "aarch64"),
            ({"arch": "aarch64"}, "aarch64"),
            ({"architecture": "aarch64", "arch": "x86_64"}, "aarch64"),
            ({"architecture": "x86_64", "arch": "aarch64"}, "x86_64"),
        ],
    )
    def test_playground_preserves_declared_architecture_for_package_provenance(
        self, tmp_path, monkeypatch, metadata, expected
    ):
        from astinuslab.lab_packages.export_service import PackageExportError, image_spec_from_catalog

        _patch_catalog(tmp_path, monkeypatch, _MINIMAL_YAML)
        entry = catalog.load_targets()[0] | metadata
        # Flat catalog entries may carry the canonical field or the legacy
        # sidecar spelling. Neither can be replaced by the distro or a default.
        autospec_setattr(monkeypatch, catalog, "load_targets", lambda: [entry])

        image = catalog.list_playground_images_for_role("target")[0]

        assert image["architecture"] == expected
        if expected == "x86_64":
            assert image_spec_from_catalog(image["id"], image).architecture == expected
        else:
            with pytest.raises(PackageExportError) as failure:
                image_spec_from_catalog(image["id"], image)
            assert failure.value.code == "unsupported_base_image"

    def test_flat_shape_default_username_top_level(self, tmp_path, monkeypatch):
        _patch_catalog(tmp_path, monkeypatch, _MINIMAL_YAML)
        results = catalog.list_playground_usable_images()
        for img in results:
            assert img.get("default_username") == "ubuntu"

    def test_flat_shape_preserves_cloud_init_flag(self, tmp_path, monkeypatch):
        _patch_catalog(tmp_path, monkeypatch, _MINIMAL_YAML)
        results = catalog.list_playground_usable_images()
        minimal = next(r for r in results if r["id"] == "ubuntu-minimal")
        lifecycle = next(r for r in results if r["id"] == "ubuntu-with-lifecycle")
        assert minimal["cloud_init"] is True
        assert lifecycle["cloud_init"] is False

    def test_excludes_enabled_missing_and_disabled_target_images(self, tmp_path, monkeypatch):
        _patch_catalog(
            tmp_path,
            monkeypatch,
            _AVAILABILITY_YAML,
            available_image_refs=("ubuntu-present.qcow2", "ubuntu-disabled.qcow2"),
        )

        results = catalog.list_playground_usable_images()

        assert [r["id"] for r in results] == ["ubuntu-present"]
        assert {
            "id",
            "path",
            "format",
            "architecture",
            "default_username",
            "image_ref",
            "source_type",
            "distro",
            "version",
            "family",
            "enabled",
            "cloud_init",
        }.issubset(results[0])


class TestPlaygroundImageFieldValidation:
    def test_missing_id_is_excluded(self, tmp_path, monkeypatch):
        _patch_catalog(tmp_path, monkeypatch, _MISSING_ID_YAML)
        results = catalog.list_playground_usable_images()
        assert results == []

    def test_missing_image_ref_is_excluded(self, tmp_path, monkeypatch):
        _patch_catalog(tmp_path, monkeypatch, _MISSING_IMAGE_REF_YAML)
        results = catalog.list_playground_usable_images()
        assert results == []

    def test_missing_source_type_is_excluded(self, tmp_path, monkeypatch):
        _patch_catalog(tmp_path, monkeypatch, _MISSING_SOURCE_TYPE_YAML)
        results = catalog.list_playground_usable_images()
        assert results == []

    def test_missing_distro_slug_is_excluded(self, tmp_path, monkeypatch):
        _patch_catalog(tmp_path, monkeypatch, _MISSING_DISTRO_SLUG_YAML)
        results = catalog.list_playground_usable_images()
        assert results == []

    def test_missing_default_username_is_excluded(self, tmp_path, monkeypatch):
        _patch_catalog(tmp_path, monkeypatch, _MISSING_USERNAME_YAML)
        results = catalog.list_playground_usable_images()
        assert results == []


class TestNetworkNodeCatalog:
    def test_list_playground_images_for_role_network_returns_enabled_available_nodes(self, tmp_path, monkeypatch):
        network_yaml = tmp_path / "network_nodes.yaml"
        network_yaml.write_text(_NETWORK_NODES_YAML, encoding="utf-8")
        images_dir = tmp_path / "images"
        images_dir.mkdir()
        (images_dir / "openwrt.qcow2").write_text("", encoding="utf-8")
        monkeypatch.setattr(
            catalog,
            "settings",
            SimpleNamespace(
                base_dir=tmp_path,
                data_dir=tmp_path,
                targets_catalog_path=tmp_path / "targets.yaml",
                attackers_catalog_path=tmp_path / "attackers.yaml",
                network_nodes_catalog_path=network_yaml,
                images_dir=images_dir,
            ),
        )

        results = catalog.list_playground_images_for_role("network")

        assert [item["id"] for item in results] == ["openwrt"]
        assert results[0]["label"] == "OpenWrt Network Node"
        assert results[0]["role"] == "network"
        assert results[0]["username"] == "root"
        assert results[0]["cloud_init"] is False
        assert results[0]["function_tags"] == ["firewall", "router"]
        assert results[0]["defaults"] == {"vcpus": 1, "memory_mb": 512, "disk_gb": 4}
        assert results[0]["disk_gb"] == 4

    def test_list_playground_images_for_role_rejects_unsupported_role(self):
        with pytest.raises(ValueError):
            catalog.list_playground_images_for_role("sensor")
