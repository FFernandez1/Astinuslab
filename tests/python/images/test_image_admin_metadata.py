from types import SimpleNamespace

from astinuslab.images import image_admin_support
from astinuslab.images.image_inspection import SIDECAR_SCHEMA_VERSION, ImageMetadata, save_sidecar


def test_list_images_surfaces_source_and_os_variant(tmp_path, monkeypatch):
    monkeypatch.setattr(image_admin_support, "settings", SimpleNamespace(images_dir=tmp_path))
    img = tmp_path / "ubuntu-22.04-server.qcow2"
    img.write_bytes(b"")
    save_sidecar(
        img,
        ImageMetadata(
            schema_version=SIDECAR_SCHEMA_VERSION,
            distro="ubuntu",
            version="22.04",
            family="debian",
            default_username="ubuntu",
            os_variant="ubuntu22.04",
            machine_type=None,
            kernel_release=None,
            arch="x86_64",
            cloud_init_profile=None,
            inject_network_config=None,
            source="inspected",
            detected_at="2026-05-02T00:00:00+00:00",
            user_edited_fields=[],
            inspect_error=None,
        ),
    )

    rows = image_admin_support.list_images()
    assert len(rows) == 1
    row = rows[0]
    assert row["distro"] == "ubuntu"
    assert row["version"] == "22.04"
    assert row["source"] == "inspected"
    assert row["os_variant"] == "ubuntu22.04"
    assert row["default_username"] == "ubuntu"
    assert row["inspect_error"] is None
    assert row["bootable"] is True
    assert row["isf_supported"] is True
    assert row["needs_user_action"] is False


def test_list_images_hides_filename_fallback_inspection_error(tmp_path, monkeypatch):
    monkeypatch.setattr(image_admin_support, "settings", SimpleNamespace(images_dir=tmp_path))
    img = tmp_path / "debian-12-genericcloud-amd64.qcow2"
    img.write_bytes(b"")
    save_sidecar(
        img,
        ImageMetadata(
            schema_version=SIDECAR_SCHEMA_VERSION,
            distro="debian",
            version="12",
            family="debian",
            default_username="debian",
            os_variant="debian12",
            machine_type="pc",
            kernel_release=None,
            arch=None,
            cloud_init_profile=None,
            inject_network_config=None,
            source="filename",
            detected_at="2026-05-02T00:00:00+00:00",
            user_edited_fields=[],
            inspect_error="virt-inspector not installed",
        ),
    )

    rows = image_admin_support.list_images()

    assert rows[0]["source"] == "filename"
    assert rows[0]["inspect_error"] is None


def test_list_images_marks_raw_as_non_auto_bootable(tmp_path, monkeypatch):
    monkeypatch.setattr(image_admin_support, "settings", SimpleNamespace(images_dir=tmp_path))
    img = tmp_path / "debian-12.raw"
    img.write_bytes(b"")
    save_sidecar(
        img,
        ImageMetadata(
            schema_version=SIDECAR_SCHEMA_VERSION,
            distro="debian",
            version="12",
            family="debian",
            default_username="debian",
            os_variant="debian12",
            machine_type="pc",
            kernel_release=None,
            arch="x86_64",
            cloud_init_profile=None,
            inject_network_config=None,
            source="filename",
            detected_at="2026-05-02T00:00:00+00:00",
            user_edited_fields=[],
            inspect_error=None,
        ),
    )

    row = image_admin_support.list_images()[0]

    assert row["filename"] == "debian-12.raw"
    assert row["bootable"] is False
    assert row["isf_supported"] is True
    assert row["needs_user_action"] is True
    assert "extension .raw is not auto-bootable" in row["preflight_reasons"]
