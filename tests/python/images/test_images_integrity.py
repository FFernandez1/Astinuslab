from __future__ import annotations

import pytest

from astinuslab.catalog import load_targets
from astinuslab.core.config import settings


def test_enabled_targets_have_images():
    """Verify that all targets marked as enabled actually have their image files present."""
    targets = load_targets()
    enabled_targets = [t for t in targets if t.get("enabled")]

    # If no targets are enabled, we might want to know why, but for integrity
    # we just check those that ARE enabled.
    # Note: load_targets() already sets enabled=False if image is missing.
    # So we should also check if there are ANY enabled targets at all if we expect some.

    if not any(settings.images_dir.glob("*.qcow2")) and not any(settings.images_dir.glob("*.img")):
        pytest.skip(f"No local VM image artifacts present in {settings.images_dir}")

    assert len(enabled_targets) > 0, "No enabled targets found in catalog. Check if images are present in " + str(
        settings.images_dir
    )

    for target in enabled_targets:
        image_ref = target.get("image_ref")
        assert image_ref, f"Target {target['id']} is enabled but has no image_ref"

        image_path = settings.images_dir / image_ref
        assert image_path.exists(), f"Image file {image_path} missing for enabled target {target['id']}"
        assert image_path.is_file(), f"Image path {image_path} is not a file for target {target['id']}"


def test_image_directory_consistency():
    """Check if image_available flag correctly reflects filesystem state for all targets."""
    targets = load_targets()
    for target in targets:
        image_ref = target.get("image_ref")
        if not image_ref:
            assert not target.get("image_available"), (
                f"Target {target['id']} has no image_ref but image_available is True"
            )
            continue

        image_path = settings.images_dir / image_ref
        expected_available = image_path.exists() and image_path.is_file()
        assert target.get("image_available") == expected_available, (
            f"image_available mismatch for {target['id']}: expected {expected_available}, got {target.get('image_available')}"
        )
