from __future__ import annotations

import hashlib
import io
import zipfile

import pytest

from astinuslab.lab_packages.archive import PackageArchiveError, inspect_archive, write_archive
from astinuslab.lab_packages.canonical import canonical_bytes
from astinuslab.lab_packages.models import (
    HostResources,
    LabDefinition,
    LabExperience,
    LabManifest,
    LabNetwork,
    LabVM,
    ManifestPayload,
    ResourceRecommendations,
)
from astinuslab.lab_packages.signing import (
    LocalTrustPolicy,
    SignatureStatus,
    generate_key_pair,
    public_key_fingerprint,
    sign_archive,
    sign_manifest,
    verify_archive,
    verify_manifest,
)


def _definition() -> LabDefinition:
    return LabDefinition(
        package_id="demo-lab",
        version="1.0.0",
        title="Demo lab",
        creator="tester",
        license="CC-BY-4.0",
        reproducibility="unlocked",
        resources=ResourceRecommendations(
            minimum=HostResources(cpu_threads=2, memory_mb=4096, free_disk_bytes=40 * 1024**3),
            recommended=HostResources(cpu_threads=4, memory_mb=8192, free_disk_bytes=60 * 1024**3),
        ),
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


@pytest.fixture
def unsigned_package() -> bytes:
    return write_archive(_definition(), "# Runbook\n")


def _replace_member(package: bytes, name: str, value: bytes) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(package), "r") as source, zipfile.ZipFile(output, "w") as target:
        for info in source.infolist():
            target.writestr(info, value if info.filename == name else source.read(info.filename))
    return output.getvalue()


def test_unsigned_archive_is_untrusted(unsigned_package: bytes):
    result = verify_archive(unsigned_package)
    assert result.status is SignatureStatus.UNSIGNED
    assert result.fingerprint is None


def test_sign_and_verify_reports_valid_untrusted_key(unsigned_package: bytes):
    private_key, public_key = generate_key_pair()
    package = sign_archive(unsigned_package, private_key, public_key=public_key)

    result = verify_archive(package)

    assert result.status is SignatureStatus.VALID_UNTRUSTED_KEY
    assert result.fingerprint == public_key_fingerprint(public_key)


def test_signing_preserves_manifest_capabilities():
    unsigned_package = write_archive(_definition(), "# Runbook\n", required_capabilities=("kvm", "libvirt"))
    private_key, public_key = generate_key_pair()

    package = sign_archive(unsigned_package, private_key, public_key)

    assert inspect_archive(package).manifest.required_capabilities == ("kvm", "libvirt")


def test_signing_preserves_large_descriptor_assets(monkeypatch: pytest.MonkeyPatch, tmp_path):
    payload = b"clean layer" * 64
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr("astinuslab.lab_packages.archive.PACKAGE_INLINE_PAYLOAD_BYTES", 8)
    source = tmp_path / "source.astinuslab"
    source.write_bytes(
        write_archive(_definition(), "# Runbook\n", assets={digest: payload}, allow_legacy_unreferenced_assets=True)
    )
    private_key, public_key = generate_key_pair()

    signed = sign_archive(source, private_key, public_key)

    inspection = inspect_archive(signed)
    assert verify_archive(signed).valid
    assert inspection.assets[f"assets/sha256/{digest}"] == payload


def test_signing_path_with_inline_assets_keeps_them_in_the_signed_archive(tmp_path):
    payload = b"small asset"
    digest = hashlib.sha256(payload).hexdigest()
    source = tmp_path / "source.astinuslab"
    source.write_bytes(
        write_archive(_definition(), "# Runbook\n", assets={digest: payload}, allow_legacy_unreferenced_assets=True)
    )
    private_key, public_key = generate_key_pair()

    signed = sign_archive(source, private_key, public_key)

    assert verify_archive(signed).valid
    assert inspect_archive(signed).assets[f"assets/sha256/{digest}"] == payload


def test_sign_archive_accepts_a_preinspected_immutable_archive(tmp_path):
    source = tmp_path / "source.astinuslab"
    source.write_bytes(write_archive(_definition(), "# Runbook\n"))
    private_key, public_key = generate_key_pair()

    signed = sign_archive(inspect_archive(source), private_key, public_key)

    assert verify_archive(signed).valid


def test_signing_preserves_exact_manifest_inventory_metadata():
    definition = _definition()
    runbook = b"# Runbook\n"
    notes = b"operator notes\n"
    payloads = {
        "lab.json": canonical_bytes(definition),
        "experience/runbook.md": runbook,
        "experience/notes.txt": notes,
    }
    manifest = LabManifest(
        package_id=definition.package_id,
        version=definition.version,
        payloads=tuple(
            ManifestPayload(
                path=path,
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                media_type="application/problem+json"
                if path == "lab.json"
                else "text/x-runbook"
                if path.endswith("runbook.md")
                else "text/plain",
            )
            for path, content in sorted(payloads.items())
        ),
    )
    unsigned_package = write_archive(
        definition,
        runbook,
        supporting_files={"experience/notes.txt": notes},
        manifest_override=manifest,
    )
    private_key, public_key = generate_key_pair()

    signed_package = sign_archive(unsigned_package, private_key, public_key)

    assert (
        inspect_archive(signed_package).payloads["manifest.json"]
        == inspect_archive(unsigned_package).payloads["manifest.json"]
    )
    assert inspect_archive(signed_package).manifest.payloads == manifest.payloads


def test_local_trust_policy_changes_status_only_for_matching_key(unsigned_package: bytes):
    private_key, public_key = generate_key_pair()
    package = sign_archive(unsigned_package, private_key)

    policy = LocalTrustPolicy.from_public_keys([public_key])

    assert verify_archive(package).status is SignatureStatus.VALID_UNTRUSTED_KEY
    assert verify_archive(package, trust_policy=policy).status is SignatureStatus.VALID_LOCALLY_TRUSTED_KEY


def test_trust_policy_isolation_does_not_trust_unknown_key(unsigned_package: bytes):
    private_key, public_key = generate_key_pair()
    _, other_public = generate_key_pair()
    package = sign_archive(unsigned_package, private_key)

    policy = LocalTrustPolicy.from_public_keys([other_public])

    assert verify_archive(package, trust_policy=policy).status is SignatureStatus.VALID_UNTRUSTED_KEY
    assert public_key_fingerprint(public_key) != public_key_fingerprint(other_public)


def test_fingerprint_policy_is_explicit_and_immutable(unsigned_package: bytes):
    private_key, public_key = generate_key_pair()
    package = sign_archive(unsigned_package, private_key)
    fingerprint = public_key_fingerprint(public_key)
    empty = LocalTrustPolicy.empty()
    policy = empty.with_fingerprint(fingerprint)

    assert verify_archive(package, trust_policy=empty).status is SignatureStatus.VALID_UNTRUSTED_KEY
    assert verify_archive(package, trust_policy=policy).status is SignatureStatus.VALID_LOCALLY_TRUSTED_KEY
    assert empty.trusted_fingerprints == frozenset()


def test_signing_is_deterministic_for_same_manifest_and_key(unsigned_package: bytes):
    private_key, public_key = generate_key_pair()

    assert sign_archive(unsigned_package, private_key, public_key) == sign_archive(
        unsigned_package, private_key, public_key
    )


def test_signature_is_detached_and_not_in_manifest_inventory(unsigned_package: bytes):
    private_key, public_key = generate_key_pair()
    package = sign_archive(unsigned_package, private_key, public_key)
    inspection = inspect_archive(package)

    assert set(inspection.signatures) == {"signatures/manifest.ed25519", "signatures/creator.pub"}
    assert all(not payload.path.startswith("signatures/") for payload in inspection.manifest.payloads)


def test_corrupt_signature_reports_invalid(unsigned_package: bytes):
    private_key, public_key = generate_key_pair()
    package = sign_archive(unsigned_package, private_key, public_key)
    inspection = inspect_archive(package)
    corrupt = bytearray(inspection.signatures["signatures/manifest.ed25519"])
    corrupt[0] ^= 0x01
    package = _replace_member(package, "signatures/manifest.ed25519", bytes(corrupt))

    assert verify_archive(package).status is SignatureStatus.INVALID_SIGNATURE


def test_altered_manifest_bytes_report_invalid(unsigned_package: bytes):
    private_key, public_key = generate_key_pair()
    package = sign_archive(unsigned_package, private_key, public_key)
    inspection = inspect_archive(package)
    altered_manifest = inspection.payloads["manifest.json"].replace(
        b'"media_type":"application/json"', b'"media_type":"application/yaml"'
    )
    result = verify_archive(_replace_member(package, "manifest.json", altered_manifest))
    assert result.status is SignatureStatus.INVALID_SIGNATURE


def test_altered_payload_inventory_is_rejected(unsigned_package: bytes):
    private_key, public_key = generate_key_pair()
    package = sign_archive(unsigned_package, private_key, public_key)
    inspection = inspect_archive(package)
    altered_runbook = inspection.payloads["experience/runbook.md"] + b"tampered"
    with pytest.raises(PackageArchiveError, match="size mismatch|digest mismatch"):
        verify_archive(_replace_member(package, "experience/runbook.md", altered_runbook))


@pytest.mark.parametrize(
    ("signature", "public_key"),
    [(b"short", b"x" * 32), (b"x" * 64, b"short"), (b"x" * 64, b"x" * 31)],
)
def test_malformed_key_and_signature_report_invalid(unsigned_package: bytes, signature: bytes, public_key: bytes):
    manifest = inspect_archive(unsigned_package).payloads["manifest.json"]
    result = verify_manifest(manifest, signature, public_key)
    assert result.status is SignatureStatus.INVALID_SIGNATURE


def test_partial_signature_entries_are_rejected(unsigned_package: bytes):
    private_key, public_key = generate_key_pair()
    package = sign_archive(unsigned_package, private_key, public_key)
    inspection = inspect_archive(package)
    missing_public_key = write_archive(
        inspection.definition,
        inspection.runbook,
        assets={path: content for path, content in inspection.assets.items()},
        allow_legacy_unreferenced_assets=True,
        supporting_files=inspection.supporting_files,
        signatures={"signatures/manifest.ed25519": inspection.signatures["signatures/manifest.ed25519"]},
    )
    with pytest.raises(PackageArchiveError, match="partial"):
        verify_archive(missing_public_key)


def test_mismatched_public_key_reports_invalid(unsigned_package: bytes):
    private_key, public_key = generate_key_pair()
    _, other_public = generate_key_pair()
    manifest = inspect_archive(unsigned_package).payloads["manifest.json"]
    signature = sign_manifest(manifest, private_key)

    result = verify_manifest(manifest, signature, other_public)

    assert result.status is SignatureStatus.INVALID_SIGNATURE
    assert public_key != other_public


def test_fingerprint_is_deterministic_for_raw_key():
    _, public_key = generate_key_pair()

    assert public_key_fingerprint(public_key) == public_key_fingerprint(bytes(public_key))
    assert len(public_key_fingerprint(public_key)) == 64


def test_noncanonical_manifest_cannot_be_signed():
    manifest = canonical_bytes(
        LabManifest.model_validate(
            {
                "package_id": "demo-lab",
                "version": "1.0.0",
                "payloads": (
                    {
                        "path": "lab.json",
                        "sha256": "a" * 64,
                        "size_bytes": 1,
                        "media_type": "application/json",
                    },
                    {
                        "path": "experience/runbook.md",
                        "sha256": "b" * 64,
                        "size_bytes": 1,
                        "media_type": "text/markdown",
                    },
                ),
            }
        )
    )
    noncanonical = manifest.replace(b'"format_version":1,', b'"format_version":1,\n')
    private_key, _ = generate_key_pair()

    with pytest.raises(PackageArchiveError, match="canonical"):
        sign_manifest(noncanonical, private_key)


def test_signature_entries_cannot_be_inventoried():
    manifest = LabManifest(
        package_id="demo-lab",
        version="1.0.0",
        payloads=(
            ManifestPayload(path="lab.json", sha256="a" * 64, size_bytes=1, media_type="application/json"),
            ManifestPayload(
                path="signatures/manifest.ed25519", sha256="b" * 64, size_bytes=1, media_type="application/octet-stream"
            ),
        ),
    )
    private_key, _ = generate_key_pair()

    with pytest.raises(PackageArchiveError, match="cannot inventory"):
        sign_manifest(manifest, private_key)
