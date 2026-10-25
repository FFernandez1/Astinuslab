"""Tests for image_path containment on JobCreate."""

from __future__ import annotations

import pytest

from astinuslab.api.schemas import JobCreate
from tests.python._support.helpers import autospec_setattr


def _base_payload(**overrides):
    payload = dict(
        target_id="ubuntu-22-04-default",
        scenario_id="immediate-capture",
        execution_mode="immediate",
    )
    payload.update(overrides)
    return payload


class TestImagePathValidation:
    def test_none_is_allowed(self):
        job = JobCreate(**_base_payload())
        assert job.image_path is None

    def test_path_inside_images_dir_allowed(self, monkeypatch, tmp_path):
        from astinuslab.api import schemas

        images = tmp_path / "images"
        images.mkdir()
        image = images / "ubuntu.qcow2"
        image.write_bytes(b"")
        autospec_setattr(monkeypatch, schemas, "_images_dir", lambda: images)

        job = JobCreate(**_base_payload(image_path=str(image)))

        assert job.image_path == str(image.resolve())

    def test_path_outside_images_dir_rejected(self, monkeypatch, tmp_path):
        from astinuslab.api import schemas

        images = tmp_path / "images"
        images.mkdir()
        outside = tmp_path / "outside.qcow2"
        outside.write_bytes(b"")
        autospec_setattr(monkeypatch, schemas, "_images_dir", lambda: images)

        with pytest.raises(ValueError, match="image_path must be inside"):
            JobCreate(**_base_payload(image_path=str(outside)))

    def test_traversal_segments_rejected(self, monkeypatch, tmp_path):
        from astinuslab.api import schemas

        images = tmp_path / "images"
        images.mkdir()
        autospec_setattr(monkeypatch, schemas, "_images_dir", lambda: images)

        traversal = str(images / ".." / ".." / "etc" / "passwd")
        with pytest.raises(ValueError, match="image_path must be inside"):
            JobCreate(**_base_payload(image_path=traversal))

    def test_symlink_escaping_images_dir_rejected(self, monkeypatch, tmp_path):
        from astinuslab.api import schemas

        images = tmp_path / "images"
        images.mkdir()
        outside = tmp_path / "outside.qcow2"
        outside.write_bytes(b"")
        link = images / "evil.qcow2"
        link.symlink_to(outside)
        autospec_setattr(monkeypatch, schemas, "_images_dir", lambda: images)

        with pytest.raises(ValueError, match="image_path must be inside"):
            JobCreate(**_base_payload(image_path=str(link)))
