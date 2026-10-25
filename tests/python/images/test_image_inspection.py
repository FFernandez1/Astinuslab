import subprocess
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from astinuslab.images.image_inspection import ImageMetadata, load_sidecar, save_sidecar
from tests.python._support.helpers import autospec_setattr

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _meta(**overrides) -> ImageMetadata:
    base: dict[str, Any] = dict(
        schema_version=1,
        distro="ubuntu",
        version="24.04",
        family="debian",
        default_username="ubuntu",
        os_variant="ubuntu24.04",
        machine_type=None,
        kernel_release="6.8.0-31-generic",
        arch="x86_64",
        cloud_init_profile=None,
        inject_network_config=None,
        source="inspected",
        detected_at="2026-05-02T12:34:00+00:00",
        user_edited_fields=[],
        inspect_error=None,
    )
    base.update(overrides)
    return ImageMetadata(**base)


def test_sidecar_roundtrip(tmp_path: Path):
    image = tmp_path / "noble.qcow2"
    image.write_bytes(b"")
    meta = _meta()
    save_sidecar(image, meta)
    sidecar = image.with_suffix(image.suffix + ".meta.json")
    assert sidecar.exists()
    loaded = load_sidecar(image)
    assert loaded == meta


def test_load_sidecar_missing_returns_none(tmp_path: Path):
    image = tmp_path / "missing.qcow2"
    image.write_bytes(b"")
    assert load_sidecar(image) is None


def test_load_sidecar_corrupt_returns_none(tmp_path: Path):
    image = tmp_path / "corrupt.qcow2"
    image.write_bytes(b"")
    sidecar = image.with_suffix(image.suffix + ".meta.json")
    sidecar.write_text("{not json")
    assert load_sidecar(image) is None


def test_load_sidecar_with_manually_seeded_build_id(tmp_path: Path):
    image = tmp_path / "img.qcow2"
    image.write_bytes(b"")
    image.with_suffix(".qcow2.meta.json").write_text(
        '{"schema_version":1,"distro":"ubuntu","version":"24.04","family":"debian",'
        '"default_username":"ubuntu","os_variant":"ubuntu24.04","machine_type":null,'
        '"kernel_release":"6.8.0-101-generic","arch":"x86_64","cloud_init_profile":null,'
        '"inject_network_config":null,"source":"inspected","detected_at":"2026-05-07T00:00:00+00:00",'
        '"user_edited_fields":[],"inspect_error":null,"build_id":"abcd1234"}',
        encoding="utf-8",
    )

    sidecar = load_sidecar(image)
    assert isinstance(sidecar, ImageMetadata), "sidecar must contain Linux metadata"
    assert sidecar.build_id == "abcd1234"


def test_inspect_image_parses_ubuntu(tmp_path: Path):
    from astinuslab.images.image_inspection import inspect_image

    image = tmp_path / "noble.qcow2"
    image.write_bytes(b"")
    xml = (FIXTURES / "virt_inspector_ubuntu.xml").read_text()
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=xml, stderr="")
    with patch("astinuslab.images.image_inspection.subprocess.run", return_value=completed) as run:
        meta = inspect_image(image)

    run.assert_called_once()
    assert run.call_args.args[0] == ["virt-inspector", "-a", str(image)]
    assert meta.distro == "ubuntu"
    assert meta.version == "24.04"
    assert meta.family == "debian"
    assert meta.os_variant == "ubuntu24.04"
    assert meta.arch == "x86_64"
    assert meta.source == "inspected"
    assert meta.inspect_error is None


def test_inspect_image_normalizes_oracle_linux_metadata(tmp_path: Path):
    from astinuslab.images.image_inspection import inspect_image

    image = tmp_path / "oracle-linux-9.qcow2"
    image.write_bytes(b"")
    xml = """<operatingsystems><operatingsystem>
      <distro>oraclelinux</distro>
      <major_version>9</major_version>
      <arch>x86_64</arch>
      <osinfo>ol9</osinfo>
    </operatingsystem></operatingsystems>"""
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=xml, stderr="")

    with patch("astinuslab.images.image_inspection.subprocess.run", return_value=completed):
        meta = inspect_image(image)

    assert meta.distro == "ol"
    assert meta.family == "redhat"
    assert meta.default_username == "opc"


def test_classify_image_preflight_flags_unknown_and_nonbootable():
    from astinuslab.images.image_inspection import classify_image_preflight

    supported = classify_image_preflight(Path("ubuntu.qcow2"), _meta(distro="ubuntu", arch="x86_64"))
    unknown = classify_image_preflight(Path("mystery.qcow2"), _meta(distro="unknown", arch=None))
    raw = classify_image_preflight(Path("debian.raw"), _meta(distro="debian", arch="x86_64"))

    assert supported["bootable"] is True
    assert supported["isf_supported"] is True
    assert supported["needs_user_action"] is False
    assert unknown["needs_user_action"] is True
    unknown_reasons = unknown["reasons"]
    assert isinstance(unknown_reasons, list)
    assert "distro is unknown" in unknown_reasons
    assert raw["bootable"] is False
    raw_reasons = raw["reasons"]
    assert isinstance(raw_reasons, list)
    assert "extension .raw is not auto-bootable" in raw_reasons


def test_inspect_image_missing_binary_raises_filenotfound(tmp_path: Path):
    from astinuslab.images.image_inspection import inspect_image

    image = tmp_path / "x.qcow2"
    image.write_bytes(b"")
    with patch("astinuslab.images.image_inspection.subprocess.run", side_effect=FileNotFoundError()):
        with pytest.raises(FileNotFoundError):
            inspect_image(image)


def test_inspect_image_timeout_raises(tmp_path: Path):
    from astinuslab.images.image_inspection import inspect_image

    image = tmp_path / "x.qcow2"
    image.write_bytes(b"")
    with patch(
        "astinuslab.images.image_inspection.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="virt-inspector", timeout=1),
    ):
        with pytest.raises(subprocess.TimeoutExpired):
            inspect_image(image, timeout=1)


def test_ensure_metadata_uses_existing_sidecar(tmp_path: Path, monkeypatch):
    from astinuslab.images import image_inspection as M

    image = tmp_path / "noble.qcow2"
    image.write_bytes(b"")
    M.save_sidecar(image, _meta(distro="ubuntu", version="24.04"))

    called = {"v": False}

    def boom(*a, **kw):
        called["v"] = True
        raise AssertionError("inspect_image should not run when sidecar exists")

    monkeypatch.setattr(M, "inspect_image", boom)
    meta = M.ensure_metadata(image)
    assert meta.distro == "ubuntu"
    assert called["v"] is False


def test_ensure_metadata_retries_stale_xml_option_sidecar(tmp_path: Path, monkeypatch):
    from astinuslab.images import image_inspection as M

    image = tmp_path / "noble.qcow2"
    image.write_bytes(b"")
    stale = _meta(
        distro="unknown",
        version="unknown",
        source="filename",
        inspect_error="virt-inspector failed: virt-inspector: unrecognized option '--xml'",
    )
    fresh = _meta(distro="ubuntu", version="24.04", source="inspected", inspect_error=None)
    M.save_sidecar(image, stale)
    autospec_setattr(monkeypatch, M, "inspect_image", lambda *a, **kw: fresh)

    meta = M.ensure_metadata(image)

    assert meta == fresh
    assert M.load_sidecar(image) == fresh


def test_ensure_metadata_falls_back_to_filename_when_binary_missing(tmp_path: Path, monkeypatch):
    from astinuslab.images import image_inspection as M

    image = tmp_path / "ubuntu-24.04-server.qcow2"
    image.write_bytes(b"")

    def missing(*a, **kw):
        raise FileNotFoundError()

    monkeypatch.setattr(M, "inspect_image", missing)
    meta = M.ensure_metadata(image)
    assert meta.source == "filename"
    assert meta.distro == "ubuntu"
    assert meta.version == "24.04"
    assert "virt-inspector" in (meta.inspect_error or "")
    assert M._sidecar_path(image).exists()


def test_ensure_metadata_falls_back_to_resolute_codename(tmp_path: Path, monkeypatch):
    from astinuslab.images import image_inspection as M

    image = tmp_path / "resolute-server-cloudimg-amd64.img"
    image.write_bytes(b"")

    def supermin_failure(*a, **kw):
        raise subprocess.CalledProcessError(
            1,
            "virt-inspector",
            stderr="libguestfs: error: /usr/bin/supermin exited with error status 1.",
        )

    monkeypatch.setattr(M, "inspect_image", supermin_failure)
    meta = M.ensure_metadata(image)

    assert meta.source == "filename"
    assert meta.distro == "ubuntu"
    assert meta.version == "26.04"
    assert meta.default_username == "ubuntu"
    assert meta.os_variant == "generic"


def test_ensure_metadata_retries_stale_unknown_filename_sidecar(tmp_path: Path, monkeypatch):
    from astinuslab.images import image_inspection as M

    image = tmp_path / "resolute-server-cloudimg-amd64.img"
    image.write_bytes(b"")
    M.save_sidecar(
        image,
        _meta(
            distro="unknown",
            version="unknown",
            source="filename",
            inspect_error="virt-inspector failed: libguestfs: error: /usr/bin/supermin exited with error status 1.",
        ),
    )

    def supermin_failure(*a, **kw):
        raise subprocess.CalledProcessError(
            1,
            "virt-inspector",
            stderr="libguestfs: error: /usr/bin/supermin exited with error status 1.",
        )

    monkeypatch.setattr(M, "inspect_image", supermin_failure)
    meta = M.ensure_metadata(image)

    assert meta.source == "filename"
    assert meta.distro == "ubuntu"
    assert meta.version == "26.04"
    written_sidecar = M.load_sidecar(image)
    assert written_sidecar is not None, "sidecar must exist after ensure_metadata writes it"
    assert written_sidecar.distro == "ubuntu"


def test_ensure_metadata_inspect_writes_sidecar(tmp_path: Path, monkeypatch):
    from astinuslab.images import image_inspection as M

    image = tmp_path / "img.qcow2"
    image.write_bytes(b"")
    fake = _meta(distro="debian", version="12", source="inspected")
    autospec_setattr(monkeypatch, M, "inspect_image", lambda *a, **kw: fake)
    meta = M.ensure_metadata(image)
    assert meta == fake
    assert M.load_sidecar(image) == fake


def test_inspect_image_bg_writes_sidecar_and_updates_tracker(tmp_path: Path, monkeypatch):
    from astinuslab.images import image_inspection as M

    img = tmp_path / "x.qcow2"
    img.write_bytes(b"")
    fake = _meta(distro="debian", version="12")
    autospec_setattr(monkeypatch, M, "inspect_image", lambda *a, **kw: fake)

    M.inspect_image_bg(img, join=True)

    assert M.IMAGE_INSPECTIONS[img.name]["status"] == "done"
    assert M.IMAGE_INSPECTIONS[img.name]["meta"].distro == "debian"
    assert M.load_sidecar(img) == fake


def test_inspect_image_bg_force_reinspects(tmp_path: Path, monkeypatch):
    from astinuslab.images import image_inspection as M

    img = tmp_path / "y.qcow2"
    img.write_bytes(b"")
    call_count = {"n": 0}

    def slow(*a, **kw):
        call_count["n"] += 1
        return _meta()

    monkeypatch.setattr(M, "inspect_image", slow)
    M.inspect_image_bg(img, join=True)
    M.inspect_image_bg(img, join=True, force=True)
    assert call_count["n"] == 2


def test_inspect_image_bg_force_waits_then_reinspects(tmp_path: Path, monkeypatch):
    """If a non-force run is in flight, a force=True call must wait then re-run."""
    from astinuslab.images import image_inspection as M

    img = tmp_path / "z.qcow2"
    img.write_bytes(b"")

    started = threading.Event()
    release = threading.Event()
    call_count = {"n": 0}

    def slow(*a, **kw):
        call_count["n"] += 1
        started.set()
        release.wait(timeout=2.0)
        return _meta()

    monkeypatch.setattr(M, "inspect_image", slow)

    # First call (non-force) starts and blocks inside `slow`.
    t1 = M.inspect_image_bg(img)
    assert started.wait(timeout=2.0)

    # Second call (force=True) must NOT piggyback. Spawn a thread that calls it
    # synchronously so we can release the first one and join cleanly.
    holder: dict[str, object] = {}

    def call_force():
        holder["t2"] = M.inspect_image_bg(img, join=True, force=True)

    waiter = threading.Thread(target=call_force, daemon=True)
    waiter.start()

    # The waiter is currently blocked inside inspect_image_bg waiting for t1.
    # Release t1 so it finishes; then the waiter spawns its own inspection.
    release.set()
    t1.join(timeout=2.0)
    waiter.join(timeout=2.0)

    assert call_count["n"] == 2
    assert holder["t2"] is not t1
