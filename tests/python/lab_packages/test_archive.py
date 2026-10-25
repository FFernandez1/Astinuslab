from __future__ import annotations

import hashlib
import io
import json
import stat
import struct
import zipfile
import zlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from astinuslab.lab_packages.archive import (
    AssetDescriptor,
    PackageArchiveError,
    inspect_archive,
    write_archive,
    write_archive_to,
)
from astinuslab.lab_packages.canonical import canonical_bytes
from astinuslab.lab_packages.models import (
    AssetReference,
    HostResources,
    LabDefinition,
    LabExperience,
    LabImageSpec,
    LabManifest,
    LabNetwork,
    LabVM,
    ResourceRecommendations,
)


def _definition() -> LabDefinition:
    resources = ResourceRecommendations(
        minimum=HostResources(cpu_threads=2, memory_mb=4096, free_disk_bytes=40 * 1024**3),
        recommended=HostResources(cpu_threads=4, memory_mb=8192, free_disk_bytes=60 * 1024**3),
    )

    return LabDefinition(
        package_id="demo-lab",
        version="1.0.0",
        title="Demo lab",
        description="A test package",
        creator="tester",
        license="CC-BY-4.0",
        reproducibility="unlocked",
        resources=resources,
        networks=(LabNetwork(id="lab", subnet_cidr="10.42.1.0/24"),),
        vms=(
            LabVM(
                id="target",
                role="target",
                image_id="ubuntu-24.04",
                vcpus=2,
                memory_mb=2048,
                disk_gb=20,
                network_ids=("lab",),
            ),
        ),
        experience=LabExperience(objectives=("Find the flag",)),
    )


def test_read_only_descriptor_inspection_streams_assets_without_retaining_them(monkeypatch):
    import os

    content = os.urandom(32_768)
    member = "assets/sha256/" + hashlib.sha256(content).hexdigest()
    payload = _valid_package(changes={member: content})
    monkeypatch.setattr("astinuslab.lab_packages.archive.PACKAGE_MAX_INSPECTED_PAYLOAD_BYTES", 8192)
    with pytest.raises(PackageArchiveError, match="budget|memory|payload"):
        inspect_archive(io.BytesIO(payload))
    handle = io.BytesIO(payload)
    inspection = inspect_archive(handle, retain_assets=False)
    assert inspection.runbook == "# Runbook\n"
    assert not inspection.assets
    assert not inspection.asset_descriptors
    assert not handle.closed
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        damaged = _zip(
            [
                (name, b"x" * len(content) if name == member else archive.read(name), None, 0)
                for name in archive.namelist()
            ]
        )
    with pytest.raises(PackageArchiveError, match="digest|checksum"):
        inspect_archive(io.BytesIO(damaged), retain_assets=False)


def _zip(members: Sequence[tuple[str, bytes, int | None, int]]) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content, mode, flags in members:
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.flag_bits |= flags
            if mode is not None:
                info.external_attr = mode << 16
            archive.writestr(info, content)
    return payload.getvalue()


def _valid_package(
    *,
    member_mode: int | None = None,
    encrypted: bool = False,
    changes: dict[str, bytes] | None = None,
) -> bytes:
    definition = _definition()
    runbook = b"# Runbook\n"
    payloads = {
        "lab.json": canonical_bytes(definition),
        "experience/runbook.md": runbook,
    }
    payloads.update(changes or {})
    entries = [
        {
            "path": path,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            "media_type": "application/json" if path.endswith(".json") else "text/markdown",
        }
        for path, content in payloads.items()
    ]
    manifest = {
        "format_version": 1,
        "package_id": definition.package_id,
        "version": definition.version,
        "required_capabilities": [],
        "payloads": entries,
    }
    payloads["manifest.json"] = canonical_bytes(LabManifest.model_validate(manifest))
    return _zip(
        [
            (
                name,
                content,
                member_mode if member_mode is not None else None,
                0x1 if encrypted and name == "lab.json" else 0,
            )
            for name, content in payloads.items()
        ]
    )


@pytest.mark.parametrize(
    "name", ["/etc/passwd", "../escape", "assets//blob", "assets/../blob", "C:/escape", r"assets\blob"]
)
def test_inspection_rejects_unsafe_member_names(name: str):
    package = _valid_package()
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        members = [(info.filename, archive.read(info.filename), None, 0) for info in archive.infolist()]
    members.append((name, b"payload", None, 0))
    with pytest.raises(PackageArchiveError, match="unsafe package path"):
        inspect_archive(_zip(members))


def test_inspection_rejects_duplicate_and_casefold_colliding_members():
    package = _valid_package()
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        members = [(info.filename, archive.read(info.filename), None, 0) for info in archive.infolist()]
    members.append(("LAB.JSON", b"duplicate", None, 0))
    with pytest.raises(PackageArchiveError, match="duplicate|collision"):
        inspect_archive(_zip(members))


def test_inspection_rejects_unicode_normalization_collision():
    package = _valid_package()
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        members = [(info.filename, archive.read(info.filename), None, 0) for info in archive.infolist()]
    members.append(("assets/é", b"one", None, 0))
    members.append(("assets/e\u0301", b"two", None, 0))
    with pytest.raises(PackageArchiveError, match="collision"):
        inspect_archive(_zip(members))


@pytest.mark.parametrize(
    "mode",
    [stat.S_IFLNK | 0o777, stat.S_IFCHR | 0o600, stat.S_IFBLK | 0o600, stat.S_IFIFO | 0o600, stat.S_IFSOCK | 0o600],
)
def test_inspection_rejects_special_members(mode: int):
    with pytest.raises(PackageArchiveError, match="unsupported package member type"):
        inspect_archive(_valid_package(member_mode=mode))


def test_inspection_rejects_encrypted_members():
    with pytest.raises(PackageArchiveError, match="encrypted"):
        raw = bytearray(_valid_package())
        local = raw.index(b"PK\x03\x04")
        central = raw.index(b"PK\x01\x02")
        struct.pack_into("<H", raw, local + 6, 0x1)
        struct.pack_into("<H", raw, central + 8, 0x1)
        inspect_archive(bytes(raw))


def test_inspection_rejects_undeclared_payload():
    package = _valid_package()
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        members = [(info.filename, archive.read(info.filename), None, 0) for info in archive.infolist()]
    members.append(("notes.txt", b"not listed", None, 0))
    with pytest.raises(PackageArchiveError, match="undeclared"):
        inspect_archive(_zip(members))


def test_inspection_rejects_excessive_path_length_or_depth():
    package = _valid_package()
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        members = [(info.filename, archive.read(info.filename), None, 0) for info in archive.infolist()]
    members.append(("a/" * 17 + "payload", b"payload", None, 0))
    with pytest.raises(PackageArchiveError, match="unsafe package path"):
        inspect_archive(_zip(members))

    members[-1] = ("a" * 1025, b"payload", None, 0)
    with pytest.raises(PackageArchiveError, match="unsafe package path"):
        inspect_archive(_zip(members))


def test_inspection_rejects_invalid_asset_shape_before_reading():
    package = _valid_package(changes={"assets/not-a-digest": b"payload"})
    with pytest.raises(PackageArchiveError, match="invalid embedded asset path"):
        inspect_archive(package)


def test_inspection_rejects_unexpected_signature_entry():
    package = _valid_package()
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        members = [(info.filename, archive.read(info.filename), None, 0) for info in archive.infolist()]
    members.append(("signatures/other.sig", b"signature", None, 0))
    with pytest.raises(PackageArchiveError, match="unexpected package signature"):
        inspect_archive(_zip(members))


def test_allowed_signature_members_are_not_manifest_payloads():
    package = write_archive(
        _definition(),
        "# Runbook\n",
        signatures={"signatures/manifest.ed25519": b"sig", "signatures/creator.pub": b"pub"},
    )
    inspected = inspect_archive(package)
    assert inspected.signatures == {"signatures/creator.pub": b"pub", "signatures/manifest.ed25519": b"sig"}


@pytest.mark.parametrize(
    ("limit_name", "member_name", "member_value"),
    [
        ("PACKAGE_MAX_MANIFEST_BYTES", "manifest.json", None),
        ("PACKAGE_MAX_DEFINITION_BYTES", "lab.json", None),
        ("PACKAGE_MAX_RUNBOOK_BYTES", "experience/runbook.md", b"12345"),
    ],
)
def test_inspection_rejects_manifest_definition_or_runbook_overflow(
    monkeypatch: pytest.MonkeyPatch, limit_name: str, member_name: str, member_value: bytes | None
):
    monkeypatch.setattr("astinuslab.lab_packages.archive." + limit_name, 4)
    package = _valid_package(changes={member_name: member_value} if member_value is not None else None)
    with pytest.raises(PackageArchiveError, match="too large"):
        inspect_archive(package)


def test_inspection_rejects_total_uncompressed_overflow(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("astinuslab.lab_packages.archive.PACKAGE_MAX_TOTAL_UNCOMPRESSED_BYTES", 10)
    with pytest.raises(PackageArchiveError, match="uncompressed payload"):
        inspect_archive(_valid_package())


def test_inspection_rejects_excessive_compression_ratio(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("astinuslab.lab_packages.archive.PACKAGE_MAX_COMPRESSION_RATIO", 2.0)
    with pytest.raises(PackageArchiveError, match="compression ratio"):
        inspect_archive(_valid_package(changes={"experience/runbook.md": b"A" * 4096}))


def test_inspection_rejects_excessive_retained_payload_memory(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("astinuslab.lab_packages.archive.PACKAGE_MAX_INSPECTED_PAYLOAD_BYTES", 100)
    with pytest.raises(PackageArchiveError, match="inspected package payload"):
        inspect_archive(_valid_package())


def test_inspection_normalizes_malformed_deflate_error(monkeypatch: pytest.MonkeyPatch):
    package = _valid_package()

    def corrupt_open(*args: object, **kwargs: object) -> object:
        raise zlib.error("invalid distance too far back")

    monkeypatch.setattr(zipfile.ZipFile, "open", corrupt_open)
    with pytest.raises(PackageArchiveError, match="invalid package member"):
        inspect_archive(package)


def test_inspection_rejects_noncanonical_lab_json():
    package = write_archive(_definition(), "# Runbook\n")
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        members = {info.filename: archive.read(info.filename) for info in archive.infolist()}
    definition_data = json.loads(members["lab.json"])
    members["lab.json"] = json.dumps(definition_data, separators=(",", ":"), indent=2).encode()
    manifest_data = json.loads(members["manifest.json"])
    for payload in manifest_data["payloads"]:
        if payload["path"] == "lab.json":
            payload["sha256"] = hashlib.sha256(members["lab.json"]).hexdigest()
            payload["size_bytes"] = len(members["lab.json"])
    members["manifest.json"] = canonical_bytes(LabManifest.model_validate(manifest_data))
    with pytest.raises(PackageArchiveError, match="lab.json is not canonical"):
        inspect_archive(_zip([(name, content, None, 0) for name, content in members.items()]))


def test_inspection_rejects_coerced_fields_in_canonical_legacy_locked_json():
    raw_definition = json.loads(canonical_bytes(_definition()))
    raw_definition["reproducibility"] = "locked"
    raw_definition["vms"][0]["vcpus"] = "2"
    payload = json.dumps(raw_definition, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    with pytest.raises(PackageArchiveError, match="lab.json is not canonical JSON"):
        inspect_archive(_valid_package(changes={"lab.json": payload}))


def test_inspection_downgrades_a_canonical_unprovable_legacy_locked_claim():
    raw_definition = json.loads(canonical_bytes(_definition()))
    raw_definition["reproducibility"] = "locked"
    payload = json.dumps(raw_definition, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    inspection = inspect_archive(_valid_package(changes={"lab.json": payload}))

    assert inspection.definition.reproducibility == "unlocked"


def test_inspection_rejects_duplicate_json_keys():
    package = _valid_package()
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        members = [(info.filename, archive.read(info.filename), None, 0) for info in archive.infolist()]
    duplicate_manifest = b'{"format_version":1,"format_version":1}'
    members = [
        (name, duplicate_manifest if name == "manifest.json" else content, mode, flags)
        for name, content, mode, flags in members
    ]
    with pytest.raises(PackageArchiveError, match="invalid JSON"):
        inspect_archive(_zip(members))


def test_inspection_rejects_non_utf8_json():
    package = _valid_package()
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        members = [(info.filename, archive.read(info.filename), None, 0) for info in archive.infolist()]
    members = [
        (name, b"\xff" if name == "manifest.json" else content, mode, flags) for name, content, mode, flags in members
    ]
    with pytest.raises(PackageArchiveError, match="invalid JSON"):
        inspect_archive(_zip(members))


def test_inspection_does_not_write_outside_temp_root(tmp_path):
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"keep")
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    inspect_archive(write_archive(_definition(), "# Runbook\n"), temp_root=tmp_path / "inspection")
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before
    assert sentinel.read_bytes() == b"keep"


def test_supporting_files_are_preserved_in_inspection_payloads():
    package = write_archive(_definition(), "# Runbook\n", supporting_files={"experience/guide.txt": b"guide"})
    loaded = inspect_archive(package)
    assert loaded.supporting_files == {"experience/guide.txt": b"guide"}
    assert loaded.payloads["experience/guide.txt"] == b"guide"
    assert loaded.payloads["manifest.json"]


def test_writer_rejects_empty_runbook_and_reader_limit_overflows(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(PackageArchiveError, match="empty"):
        write_archive(_definition(), "")
    with pytest.raises(PackageArchiveError, match="valid UTF-8"):
        write_archive(_definition(), b"\xff")

    monkeypatch.setattr("astinuslab.lab_packages.archive.PACKAGE_MAX_MEMBER_BYTES", 4)
    with pytest.raises(PackageArchiveError, match="member too large"):
        write_archive(_definition(), "# Runbook\n")


def test_writer_rejects_total_member_count_and_archive_size_overflows(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("astinuslab.lab_packages.archive.PACKAGE_MAX_TOTAL_UNCOMPRESSED_BYTES", 10)
    with pytest.raises(PackageArchiveError, match="uncompressed payload"):
        write_archive(_definition(), "# Runbook\n")

    monkeypatch.setattr("astinuslab.lab_packages.archive.PACKAGE_MAX_TOTAL_UNCOMPRESSED_BYTES", 40 * 1024**3)
    monkeypatch.setattr("astinuslab.lab_packages.archive.PACKAGE_MAX_MEMBERS", 2)
    with pytest.raises(PackageArchiveError, match="too many members"):
        write_archive(_definition(), "# Runbook\n")

    monkeypatch.setattr("astinuslab.lab_packages.archive.PACKAGE_MAX_MEMBERS", 4096)
    monkeypatch.setattr("astinuslab.lab_packages.archive.PACKAGE_MAX_ARCHIVE_BYTES", 1)
    with pytest.raises(PackageArchiveError, match="archive is too large"):
        write_archive(_definition(), "# Runbook\n")


def test_writer_rejects_reader_memory_overflow(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("astinuslab.lab_packages.archive.PACKAGE_MAX_INSPECTED_PAYLOAD_BYTES", 100)
    with pytest.raises(PackageArchiveError, match="inspected package payload"):
        write_archive(_definition(), "# Runbook\n")


def test_writer_rejects_supporting_path_normalization_collisions():
    with pytest.raises(PackageArchiveError, match="path collision"):
        write_archive(
            _definition(),
            "# Runbook\n",
            supporting_files={"experience/é.txt": b"one", "experience/e\u0301.txt": b"two"},
        )


def test_writer_uses_stored_members_when_deflate_ratio_would_be_rejected(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("astinuslab.lab_packages.archive.PACKAGE_MAX_COMPRESSION_RATIO", 2.0)
    package = write_archive(_definition(), "A" * 4096)
    loaded = inspect_archive(package)
    assert loaded.runbook == "A" * 4096
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        assert archive.getinfo("experience/runbook.md").compress_type == zipfile.ZIP_STORED


def test_writer_is_deterministic_and_round_trips():
    first = write_archive(_definition(), "# Runbook\n", {"a.bin": b"abc"}, allow_legacy_unreferenced_assets=True)
    second = write_archive(_definition(), "# Runbook\n", {"a.bin": b"abc"}, allow_legacy_unreferenced_assets=True)
    assert first == second
    loaded = inspect_archive(first)
    assert loaded.definition == _definition()
    assert loaded.runbook == "# Runbook\n"
    assert loaded.assets[f"assets/sha256/{hashlib.sha256(b'abc').hexdigest()}"] == b"abc"


def test_writer_rejects_orphan_assets_for_nonlegacy_definitions():
    payload = b"orphan"
    digest = hashlib.sha256(payload).hexdigest()
    definition = _definition().model_copy(
        update={
            "vms": (
                _definition()
                .vms[0]
                .model_copy(
                    update={
                        "base_image": LabImageSpec(
                            image_id="ubuntu-24.04",
                            sha256="a" * 64,
                            size_bytes=1,
                            media_type="application/octet-stream",
                        )
                    }
                ),
            )
        }
    )

    with pytest.raises(PackageArchiveError, match="no declared asset reference"):
        write_archive(definition, "# Runbook\n", {digest: payload})


def test_writer_rejects_missing_embedded_boot_layer_payload():
    layer = AssetReference(
        sha256="b" * 64,
        size_bytes=4,
        media_type="application/octet-stream",
        embedded=True,
        license="CC-BY-4.0",
        redistribution_confirmed=True,
    )
    definition = _definition().model_copy(
        update={"reproducibility": "locked", "vms": (_definition().vms[0].model_copy(update={"clean_layer": layer}),)}
    )

    with pytest.raises(PackageArchiveError, match="no declared asset reference"):
        write_archive(definition, "# Runbook\n")


def test_writer_rejects_conflicting_embedded_references_for_one_digest():
    common = hashlib.sha256(b"data").hexdigest()
    first = AssetReference(
        sha256=common,
        size_bytes=4,
        media_type="application/octet-stream",
        embedded=True,
        license="CC-BY-4.0",
        redistribution_confirmed=True,
    )
    second = first.model_copy(update={"media_type": "application/x-qcow2"})
    vm = _definition().vms[0]
    definition = _definition().model_copy(
        update={
            "reproducibility": "locked",
            "vms": (
                vm.model_copy(update={"clean_layer": first}),
                vm.model_copy(update={"id": "other", "clean_layer": second}),
            ),
        }
    )

    with pytest.raises(PackageArchiveError, match="references disagree"):
        write_archive(definition, "# Runbook\n", {common: b"data"})


def test_writer_rejects_manifest_override_with_wrong_asset_media_type():
    payload = b"legacy asset"
    digest = hashlib.sha256(payload).hexdigest()
    layer = AssetReference(
        sha256=digest,
        size_bytes=len(payload),
        media_type="application/octet-stream",
        embedded=True,
        license="CC-BY-4.0",
        redistribution_confirmed=True,
    )
    definition = _definition().model_copy(
        update={"reproducibility": "locked", "vms": (_definition().vms[0].model_copy(update={"clean_layer": layer}),)}
    )
    original = inspect_archive(write_archive(definition, "# Runbook\n", {digest: payload}))
    override = original.manifest.model_copy(
        update={
            "payloads": tuple(
                entry.model_copy(update={"media_type": "application/x-qcow2"})
                if entry.path == f"assets/sha256/{digest}"
                else entry
                for entry in original.manifest.payloads
            )
        }
    )

    with pytest.raises(PackageArchiveError, match="embedded asset metadata mismatch"):
        write_archive(
            definition,
            "# Runbook\n",
            {digest: payload},
            manifest_override=override,
        )


def test_large_embedded_asset_is_a_reopenable_descriptor_not_inline(tmp_path, monkeypatch: pytest.MonkeyPatch):
    payload = b"0123456789" * 64
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr("astinuslab.lab_packages.archive.PACKAGE_INLINE_PAYLOAD_BYTES", 32)
    archive_path = tmp_path / "large.astinuslab"
    archive_path.write_bytes(
        write_archive(_definition(), "# Runbook\n", {digest: payload}, allow_legacy_unreferenced_assets=True)
    )

    inspection = inspect_archive(archive_path)

    member = f"assets/sha256/{digest}"
    assert member not in inspection.assets
    assert isinstance(inspection.asset_descriptors[member], AssetDescriptor)
    with inspection.open_asset(member) as source:
        assert source.read() == payload


def test_write_archive_to_streams_asset_without_reading_it_all(tmp_path):
    payload = b"streamed asset" * 128
    digest = hashlib.sha256(payload).hexdigest()
    destination = tmp_path / "streamed.astinuslab"
    with destination.open("w+b") as sink:
        write_archive_to(
            sink,
            _definition(),
            "# Runbook\n",
            asset_streams={digest: io.BytesIO(payload)},
            allow_legacy_unreferenced_assets=True,
        )

    inspection = inspect_archive(destination)
    assert inspection.assets[f"assets/sha256/{digest}"] == payload


def test_descriptor_streams_close_their_zip_file_descriptors(tmp_path, monkeypatch: pytest.MonkeyPatch):
    if not Path("/proc/self/fd").exists():
        pytest.skip("descriptor accounting is Linux-specific")
    payload = b"asset" * 64
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr("astinuslab.lab_packages.archive.PACKAGE_INLINE_PAYLOAD_BYTES", 8)
    archive_path = tmp_path / "asset.astinuslab"
    archive_path.write_bytes(
        write_archive(_definition(), "# Runbook\n", {digest: payload}, allow_legacy_unreferenced_assets=True)
    )
    descriptor = inspect_archive(archive_path).asset_descriptors[f"assets/sha256/{digest}"]
    before = len(list(Path("/proc/self/fd").iterdir()))

    for _ in range(16):
        with descriptor.open() as source:
            assert source.read() == payload

    assert len(list(Path("/proc/self/fd").iterdir())) <= before + 1


def test_descriptor_stream_supports_readinto_iteration_and_zero_reads(tmp_path, monkeypatch: pytest.MonkeyPatch):
    payload = b"first\nsecond\n"
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr("astinuslab.lab_packages.archive.PACKAGE_INLINE_PAYLOAD_BYTES", 1)
    archive_path = tmp_path / "asset.astinuslab"
    archive_path.write_bytes(
        write_archive(_definition(), "# Runbook\n", {digest: payload}, allow_legacy_unreferenced_assets=True)
    )
    descriptor = inspect_archive(archive_path).asset_descriptors[f"assets/sha256/{digest}"]

    with descriptor.open() as source:
        assert source.read(0) == b""
        buffer = bytearray(6)
        assert cast(Any, source).readinto(buffer) == 6
        assert bytes(buffer) == b"first\n"
        assert list(source) == [b"second\n"]


def test_descriptor_open_failure_closes_its_zip_file(monkeypatch: pytest.MonkeyPatch, tmp_path):
    payload = b"asset" * 64
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr("astinuslab.lab_packages.archive.PACKAGE_INLINE_PAYLOAD_BYTES", 8)
    archive_path = tmp_path / "asset.astinuslab"
    archive_path.write_bytes(
        write_archive(_definition(), "# Runbook\n", {digest: payload}, allow_legacy_unreferenced_assets=True)
    )
    descriptor = inspect_archive(archive_path).asset_descriptors[f"assets/sha256/{digest}"]
    closed = False
    original_close = zipfile.ZipFile.close

    def closing_failure(self):
        nonlocal closed
        closed = True
        return original_close(self)

    def fail_open(self, *args, **kwargs):
        raise OSError("cannot open member")

    monkeypatch.setattr(zipfile.ZipFile, "close", closing_failure)
    monkeypatch.setattr(zipfile.ZipFile, "open", fail_open)
    with pytest.raises(PackageArchiveError, match="cannot open package member"):
        descriptor.open()
    assert closed
