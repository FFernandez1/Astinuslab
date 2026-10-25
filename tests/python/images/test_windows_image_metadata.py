from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from astinuslab.images import image_inspection as M
from tests.python._support.windows_images import windows_payload as _windows_payload
from tests.python._support.windows_images import write_windows_image


@pytest.mark.parametrize("use_override", [False, True])
@pytest.mark.parametrize("provenance", ["missing", "malformed", "specialized", "running", "linux", "bad-type"])
def test_windows_boot_context_rejects_selected_image_without_reusable_provenance(tmp_path, use_override, provenance):
    from astinuslab.storage.run_path_support import prepare_boot_context

    selected = write_windows_image(tmp_path / "selected.qcow2")
    sidecar = selected.with_suffix(".qcow2.meta.json")
    if provenance == "missing":
        sidecar.unlink()
    elif provenance == "malformed":
        sidecar.write_text("[]", encoding="utf-8")
    elif provenance == "linux":
        sidecar.write_text('{"platform": "linux"}', encoding="utf-8")
    elif provenance == "bad-type":
        write_windows_image(selected, arch=123)
    elif provenance == "running":
        write_windows_image(selected, shutdown_state="running")
    else:
        write_windows_image(selected, generalization="specialized")
    before = sidecar.read_bytes() if sidecar.exists() else None
    write_windows_image(tmp_path / "catalog.qcow2")
    target = {
        "platform": "windows",
        "family": "windows",
        "reusable": True,
        "image_ref": "catalog.qcow2" if use_override else selected.name,
    }

    with pytest.raises(ValueError, match="Windows image preflight"):
        prepare_boot_context(
            target, SimpleNamespace(images_dir=tmp_path), str(selected) if use_override else None, None, None, None
        )

    assert (sidecar.read_bytes() if sidecar.exists() else None) == before


def test_windows_boot_context_accepts_valid_selected_override(tmp_path):
    from astinuslab.storage.run_path_support import prepare_boot_context

    selected = write_windows_image(tmp_path / "selected.qcow2")
    target = {"platform": "windows", "family": "windows", "reusable": True, "image_ref": "catalog.qcow2"}
    result = prepare_boot_context(target, SimpleNamespace(images_dir=tmp_path), str(selected), None, None, None)

    assert result[0] == selected.resolve()
    assert result[3] == 40


def _windows_meta(tmp_path: Path, **overrides):
    image = tmp_path / "windows-server-2022.qcow2"
    image.with_suffix(image.suffix + ".meta.json").write_text(
        json.dumps(_windows_payload(**overrides)), encoding="utf-8"
    )
    meta = M.load_sidecar(image)
    assert meta is not None
    return image, meta


def test_windows_sidecar_roundtrip_preserves_provenance(tmp_path: Path):
    image = tmp_path / "windows-server-2022.qcow2"
    image.write_bytes(b"placeholder")
    payload = _windows_payload()
    image.with_suffix(image.suffix + ".meta.json").write_text(json.dumps(payload), encoding="utf-8")

    expected = M.load_sidecar(image)
    assert expected is not None
    M.save_sidecar(image, expected)

    loaded = M.load_sidecar(image)

    assert loaded is not None
    assert asdict(loaded) == payload
    assert asdict(loaded)["firmware"]["nvram"] == "per_instance_copy"


def test_valid_windows_sidecar_is_reusable(tmp_path: Path):
    image = tmp_path / "windows-server-2022.qcow2"
    image, meta = _windows_meta(tmp_path)

    result = M.classify_image_preflight(image, meta)

    assert result["bootable"] is True
    assert result["reusable"] is True
    assert result["needs_user_action"] is False
    assert result["family"] == "windows"
    assert result["isf_supported"] is False


@pytest.mark.parametrize(
    "changes", [{"schema_version": 2}, {"first_boot": None}, {"first_boot": {"profile": "unknown"}}]
)
def test_windows_cannot_bypass_prepared_first_boot_contract(tmp_path, changes):
    image, meta = _windows_meta(tmp_path, **changes)
    result = M.classify_image_preflight(image, meta)
    assert result["reusable"] is False
    assert result["needs_user_action"] is True


def test_metadata_api_rejects_unknown_or_credential_shaped_first_boot_fields():
    from pydantic import ValidationError

    from astinuslab.ui.image_routes import _MetadataPayload

    extras: tuple[dict[str, object], ...] = ({"password": "synthetic"}, {"parent_acls": []}, {"profile": "unknown"})
    for extra in extras:
        value = _windows_payload()
        value["first_boot"].update(extra)
        with pytest.raises(ValidationError):
            _MetadataPayload(**value)


@pytest.mark.parametrize("username", ["administrator", "ADMINISTRATOR"])
def test_windows_preflight_preserves_ascii_username_case_tolerance(tmp_path, username):
    image = write_windows_image(tmp_path / "windows.qcow2", default_username=username)

    assert M.require_reusable_windows_image(image, allowed_dir=tmp_path) == image


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("generalization", "specialized"),
        ("shutdown_state", "running"),
        ("virtual_size_bytes", 41 * 1024**3),
        ("firmware", {"machine": "i440fx", "firmware": "ovmf", "nvram": "per_instance_copy"}),
        (
            "firmware",
            {"machine": "q35", "firmware": "ovmf", "nvram": "/shared/win.vars"},
        ),
        (
            "guest_access",
            {
                "method": "openssh",
                "bootstrap": "password",
                "key_in_image": False,
                "host_keys": "regenerate_per_instance",
            },
        ),
        (
            "guest_access",
            {"method": "openssh", "bootstrap": "qga", "key_in_image": True, "host_keys": "regenerate_per_instance"},
        ),
        (
            "guest_access",
            {"method": "openssh", "bootstrap": "qga", "key_in_image": False, "host_keys": "preserve"},
        ),
    ],
)
def test_invalid_windows_provenance_is_not_reusable(tmp_path: Path, field: str, value):
    image, meta = _windows_meta(tmp_path, **{field: value})

    result = M.classify_image_preflight(image, meta)

    assert result["reusable"] is False
    assert result["needs_user_action"] is True
    assert result["reasons"]


def test_catalog_scans_valid_windows_sidecar_without_cloud_init(tmp_path: Path, monkeypatch):
    import astinuslab.catalog as catalog

    image = tmp_path / "windows-server-2022.qcow2"
    image.write_bytes(b"placeholder")
    image.with_suffix(image.suffix + ".meta.json").write_text(json.dumps(_windows_payload()), encoding="utf-8")
    monkeypatch.setattr(
        catalog,
        "settings",
        SimpleNamespace(images_dir=tmp_path, base_dir=tmp_path, data_dir=tmp_path),
    )

    entries = catalog._scan_images_dir(tmp_path)

    assert len(entries) == 1
    entry = entries[0]
    assert entry["platform"] == "windows"
    assert entry["family"] == "windows"
    assert entry["distro"] == "windows-server"
    assert entry["version"] == "2022"
    assert entry["default_username"] == "Administrator"
    assert entry["cloud_init"] is False
    assert entry["reusable"] is True


def test_windows_metadata_payload_requires_explicit_windows_platform():
    from astinuslab.ui.image_routes import _MetadataPayload

    payload = _MetadataPayload(
        platform="windows",
        distro="windows-server",
        version="2022",
        family="windows",
        default_username="Administrator",
        os_variant="win2k22",
        edition="Standard Evaluation",
        build="20348",
        revision=5622,
        source="operator_prepared",
        generalization="generalized",
        shutdown_state="shut_off",
        virtual_size_bytes=40 * 1024**3,
        firmware={"machine": "q35"},
        guest_access={"bootstrap": "qga"},
        windows_update={"policy": "optional", "pending_count": 0},
        sha256="a" * 64,
    )

    assert payload.platform == "windows"
    assert payload.family == "windows"


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema_version": 2.0},
        {"virtual_size_bytes": float(40 * 1024**3)},
        {"revision": True},
        {"windows_update": {"policy": "optional", "pending_count": True}},
        {"edition": "  "},
        {"edition": 123},
        {"build": []},
        {"build": "  "},
        {"sha256": 123},
        {"arch": 123},
        {"architecture": "aarch64"},
        {"default_username": "ubuntu"},
        {"default_username": None},
        {"default_username": "admini\u017ftrator"},
        {"default_username": "admini\ufb06rator"},
        {"os_variant": "ubuntu22.04"},
        {"machine_type": "pc"},
        {"cloud_init_profile": "ubuntu"},
        {"inject_network_config": True},
        {"kernel_release": "6.8.0-linux"},
    ],
)
def test_windows_provenance_rejects_contradictions_consistently(tmp_path, monkeypatch, overrides):
    from astinuslab import catalog
    from astinuslab.images import image_admin_support
    from astinuslab.ui import image_routes

    image = write_windows_image(tmp_path / "windows.qcow2", **overrides)
    settings = SimpleNamespace(images_dir=tmp_path, base_dir=tmp_path, data_dir=tmp_path)
    monkeypatch.setattr(catalog, "settings", settings)
    monkeypatch.setattr(image_admin_support, "settings", settings)
    meta = M.load_sidecar(image)
    assert isinstance(meta, M.WindowsImageMetadata)
    expected = M.classify_image_preflight(image, meta)
    assert expected["reusable"] is False
    assert expected["needs_user_action"] is True
    assert expected["reasons"]
    entries = [
        image_routes._metadata_response(image, meta),
        image_admin_support.list_images()[0],
        catalog._scan_images_dir(tmp_path)[0],
    ]
    for entry in entries:
        assert entry["reusable"] is False
        assert entry["preflight_reasons"] == expected["reasons"]
    assert entries[-1]["enabled"] is False
    with pytest.raises(ValueError, match="Windows image preflight"):
        M.require_reusable_windows_image(image, allowed_dir=tmp_path)


def test_windows_metadata_get_exposes_reusable_without_requiring_updates(tmp_path, monkeypatch):
    from astinuslab.ui import image_routes

    image = write_windows_image(tmp_path / "windows.qcow2", windows_update={"policy": "optional", "pending_count": 12})
    monkeypatch.setattr(image_routes, "settings", SimpleNamespace(images_dir=tmp_path))
    response = image_routes.admin_image_metadata_get(image.name)
    assert response["reusable"] is True
    assert response["windows_update"]["pending_count"] == 12


@pytest.mark.parametrize(
    "overrides",
    [
        {"arch": "aarch64", "architecture": "aarch64"},
        {"architecture": "aarch64"},
        {"cloud_init_profile": "ubuntu", "inject_network_config": True},
    ],
)
def test_windows_metadata_put_does_not_discard_conflicting_input(tmp_path, monkeypatch, overrides):
    from astinuslab.ui import image_routes

    image = write_windows_image(tmp_path / "windows.qcow2")
    monkeypatch.setattr(image_routes, "settings", SimpleNamespace(images_dir=tmp_path))
    payload = image_routes._MetadataPayload(**_windows_payload(**overrides))
    response = image_routes.admin_image_metadata_put(image.name, payload)
    assert response["reusable"] is False
    saved_meta = M.load_sidecar(image)
    assert isinstance(saved_meta, M.WindowsImageMetadata)
    saved = asdict(saved_meta)
    for field, value in overrides.items():
        assert saved[field] == value


def test_windows_metadata_put_does_not_inherit_linux_kernel_or_arch(tmp_path, monkeypatch):
    from astinuslab.ui import image_routes

    image = tmp_path / "windows.qcow2"
    image.write_bytes(b"synthetic image")
    old = M._filename_fallback(image, None)
    old.arch = "aarch64"
    old.kernel_release = "6.8.0-linux"
    M.save_sidecar(image, old)
    monkeypatch.setattr(image_routes, "settings", SimpleNamespace(images_dir=tmp_path))
    response = image_routes.admin_image_metadata_put(image.name, image_routes._MetadataPayload(**_windows_payload()))
    assert response["arch"] == "x86_64"
    assert response["architecture"] == "x86_64"
    assert response["kernel_release"] is None
    assert response["reusable"] is True


@pytest.mark.parametrize(
    "field,value", [("revision", True), ("revision", 1.0), ("virtual_size_bytes", float(40 * 1024**3))]
)
def test_windows_metadata_api_rejects_coerced_integer_provenance(field, value):
    from pydantic import ValidationError

    from astinuslab.ui.image_routes import _MetadataPayload

    with pytest.raises(ValidationError):
        _MetadataPayload(**_windows_payload(**{field: value}))


def test_windows_metadata_edit_history_tracks_saved_values(tmp_path, monkeypatch):
    from astinuslab.ui import image_routes

    image = write_windows_image(tmp_path / "windows.qcow2")
    monkeypatch.setattr(image_routes, "settings", SimpleNamespace(images_dir=tmp_path))
    payload = _windows_payload(revision=5623)
    payload.pop("arch")
    payload.pop("architecture")
    response = image_routes.admin_image_metadata_put(image.name, image_routes._MetadataPayload(**payload))
    assert response["arch"] == "x86_64"
    assert response["architecture"] == "x86_64"
    assert response["user_edited_fields"] == ["revision"]


@pytest.mark.parametrize(
    "provenance", ["valid", "missing", "truncated", "list", "incomplete", "extra-field", "legacy-windows", "symlink"]
)
@pytest.mark.parametrize("operation", ["ensure", "force"])
def test_inspection_preserves_windows_and_invalid_provenance(tmp_path, monkeypatch, provenance, operation):
    image = write_windows_image(tmp_path / "windows-server-2022.qcow2")
    sidecar = image.with_suffix(".qcow2.meta.json")
    if provenance == "missing":
        sidecar.unlink()
    elif provenance == "truncated":
        sidecar.write_text('{"platform":"windows",')
    elif provenance == "list":
        sidecar.write_text("[]")
    elif provenance == "incomplete":
        sidecar.write_text('{"platform":"windows","schema_version":2}')
    elif provenance == "extra-field":
        sidecar.write_text(json.dumps(_windows_payload(unrecognized="synthetic-data")))
    elif provenance == "legacy-windows":
        legacy = M._filename_fallback(tmp_path / "unknown.qcow2", None)
        legacy.distro = "windows"
        legacy.os_variant = "win2k22"
        M.save_sidecar(image, legacy)
    elif provenance == "symlink":
        other = tmp_path / "operator-record.json"
        sidecar.rename(other)
        sidecar.symlink_to(other)
    before = sidecar.read_bytes() if sidecar.exists() else None
    monkeypatch.setattr(M, "inspect_image", lambda *a, **k: pytest.fail("protected provenance reached inspector"))
    if operation == "ensure":
        meta = M.ensure_metadata(image)
        preflight = M.classify_image_preflight(image, meta)
        assert preflight["reusable"] is (provenance == "valid")
        from astinuslab.ui import image_routes

        monkeypatch.setattr(image_routes, "settings", SimpleNamespace(images_dir=tmp_path))
        assert image_routes.admin_image_metadata_get(image.name)["reusable"] is (provenance == "valid")
    else:
        M.inspect_image_bg(image, force=True, join=True)
        state = M.IMAGE_INSPECTIONS[image.name]
        assert state["status"] == "error"
        assert state["error"]
    assert (sidecar.read_bytes() if sidecar.exists() else None) == before
    assert sidecar.is_symlink() is (provenance == "symlink")


def test_unidentified_invalid_sidecar_is_visible_but_disabled_and_not_rewritten(tmp_path, monkeypatch):
    from astinuslab import catalog
    from astinuslab.images import image_admin_support
    from astinuslab.ui import image_routes

    image = tmp_path / "opaque.qcow2"
    image.write_bytes(b"synthetic image")
    sidecar = image.with_suffix(".qcow2.meta.json")
    sidecar.write_text("{broken-record")
    for module in (catalog, image_admin_support, image_routes):
        monkeypatch.setattr(
            module, "settings", SimpleNamespace(images_dir=tmp_path, base_dir=tmp_path, data_dir=tmp_path)
        )
    monkeypatch.setattr(M, "inspect_image", lambda *a, **k: pytest.fail("invalid sidecar reached inspector"))
    target = catalog._scan_images_dir(tmp_path)[0]
    assert target["enabled"] is False
    for entry in (target, image_admin_support.list_images()[0], image_routes.admin_image_metadata_get(image.name)):
        assert entry["reusable"] is False
        assert entry["isf_supported"] is False
        assert entry["preflight_reasons"]
    assert sidecar.read_text() == "{broken-record"


@pytest.mark.parametrize("operation", ["ensure", "force"])
def test_inspector_windows_result_never_becomes_linux_sidecar(tmp_path, monkeypatch, operation):
    from astinuslab.core import config

    image = tmp_path / "opaque.qcow2"
    image.write_bytes(b"synthetic image")
    monkeypatch.setattr(config, "settings", config.settings.model_copy(update={"image_inspection_enabled": True}))
    monkeypatch.setattr(
        M.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            stdout="<operatingsystems><operatingsystem><name>Windows</name><type>windows</type><distro>windows</distro><arch>x86_64</arch></operatingsystem></operatingsystems>"
        ),
    )
    if operation == "ensure":
        result = M.classify_image_preflight(image, M.ensure_metadata(image))
        assert result["reusable"] is False
        assert result["family"] == "windows"
        assert result["needs_user_action"] is True
    else:
        M.inspect_image_bg(image, force=True, join=True)
        assert M.IMAGE_INSPECTIONS[image.name]["status"] == "error"
    assert not image.with_suffix(".qcow2.meta.json").exists()
