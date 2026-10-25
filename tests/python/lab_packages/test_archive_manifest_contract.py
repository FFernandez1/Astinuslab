"""Manifest-inventory contract for the package reader.

The manifest is the archive's own claim about what it contains, and the reader
trusts nothing it says without checking. A payload the manifest never declared
must not be readable, a payload it declares but does not ship must not be
silently skipped, and the manifest must not be allowed to inventory itself or
its own signatures — which is how a crafted package would otherwise get a
signed statement to cover bytes nobody verified.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest

from astinuslab.lab_packages.archive import PackageArchiveError, inspect_archive
from astinuslab.lab_packages.canonical import canonical_bytes
from astinuslab.lab_packages.models import (
    HostResources,
    LabDefinition,
    LabExperience,
    LabManifest,
    LabNetwork,
    LabVM,
    ResourceRecommendations,
)

pytestmark = pytest.mark.unit

RUNBOOK = b"# Runbook\n"


def _definition() -> LabDefinition:
    return LabDefinition(
        package_id="manifest-lab",
        version="1.0.0",
        title="Manifest lab",
        creator="Researcher",
        license="CC-BY-4.0",
        reproducibility="unlocked",
        resources=ResourceRecommendations(
            minimum=HostResources(cpu_threads=2, memory_mb=4096, free_disk_bytes=40 * 1024**3),
            recommended=HostResources(cpu_threads=4, memory_mb=8192, free_disk_bytes=40 * 1024**3),
        ),
        networks=(LabNetwork(id="isolated", subnet_cidr="192.0.2.0/29"),),
        vms=(
            LabVM(
                id="target",
                role="target",
                image_id="ubuntu-24.04",
                vcpus=2,
                memory_mb=2048,
                disk_gb=10,
                network_ids=("isolated",),
            ),
        ),
        experience=LabExperience(),
    )


def _entry(path: str, content: bytes) -> dict:
    return {
        "path": path,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "media_type": "application/json" if path.endswith(".json") else "text/markdown",
    }


def _build(
    *,
    payloads: dict[str, bytes] | None = None,
    entries: list[dict] | None = None,
    drop_members: frozenset[str] = frozenset(),
    manifest_bytes: bytes | None = None,
) -> bytes:
    definition = _definition()
    files = {"lab.json": canonical_bytes(definition), "experience/runbook.md": RUNBOOK}
    files.update(payloads or {})
    inventory = entries if entries is not None else [_entry(name, body) for name, body in files.items()]
    if manifest_bytes is None:
        manifest_bytes = canonical_bytes(
            LabManifest.model_validate(
                {
                    "format_version": 1,
                    "package_id": definition.package_id,
                    "version": definition.version,
                    "required_capabilities": [],
                    "payloads": inventory,
                }
            )
        )
    files["manifest.json"] = manifest_bytes
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in files.items():
            if name in drop_members:
                continue
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o100640 & 0xFFFF) << 16
            info.create_system = 3
            archive.writestr(info, body)
    return buffer.getvalue()


# --- the baseline still reads --------------------------------------------------


def test_a_well_formed_package_inspects():
    inspection = inspect_archive(_build())

    assert inspection.definition.package_id == "manifest-lab"
    assert inspection.payloads["experience/runbook.md"] == RUNBOOK


# --- required members ----------------------------------------------------------


@pytest.mark.parametrize("member", ["lab.json", "experience/runbook.md", "manifest.json"])
def test_a_package_missing_a_required_member_is_refused(member):
    with pytest.raises(PackageArchiveError):
        inspect_archive(_build(drop_members=frozenset({member})))


# --- manifest inventory --------------------------------------------------------


def test_a_manifest_that_declares_the_same_path_twice_is_refused():
    """The model refuses the duplicate, so build the manifest bytes by hand.

    A crafted package would not go through the model on the way in; the reader
    has to reject the raw document.
    """

    definition = _definition()
    duplicate = _entry("lab.json", canonical_bytes(definition))
    manifest = json.dumps(
        {
            "format_version": 1,
            "package_id": definition.package_id,
            "version": definition.version,
            "required_capabilities": [],
            "payloads": [duplicate, duplicate, _entry("experience/runbook.md", RUNBOOK)],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    # The reader reports it as an invalid manifest; the point is that the
    # duplicate never reaches the payload map.
    with pytest.raises(PackageArchiveError, match="invalid lab manifest"):
        inspect_archive(_build(manifest_bytes=manifest))


@pytest.mark.parametrize("path", ["manifest.json", "signatures/manifest.ed25519", "signatures/creator.pub"])
def test_a_manifest_cannot_inventory_itself_or_its_signatures(path):
    definition = _definition()

    with pytest.raises(PackageArchiveError, match="cannot inventory signature or manifest"):
        inspect_archive(
            _build(
                entries=[
                    _entry("lab.json", canonical_bytes(definition)),
                    _entry("experience/runbook.md", RUNBOOK),
                    _entry(path, b"anything"),
                ]
            )
        )


def test_a_manifest_that_declares_a_payload_the_archive_lacks_is_refused():
    definition = _definition()

    with pytest.raises(PackageArchiveError, match="declares missing payload"):
        inspect_archive(
            _build(
                entries=[
                    _entry("lab.json", canonical_bytes(definition)),
                    _entry("experience/runbook.md", RUNBOOK),
                    _entry("experience/absent.md", b"never shipped"),
                ]
            )
        )


def test_a_member_the_manifest_never_declared_is_refused():
    with pytest.raises(PackageArchiveError):
        inspect_archive(
            _build(
                payloads={"experience/undeclared.md": b"smuggled"},
                entries=[
                    _entry("lab.json", canonical_bytes(_definition())),
                    _entry("experience/runbook.md", RUNBOOK),
                ],
            )
        )


def test_a_manifest_whose_digest_disagrees_with_the_bytes_is_refused():
    definition = _definition()
    lying = _entry("experience/runbook.md", RUNBOOK)
    lying["sha256"] = "a" * 64

    with pytest.raises(PackageArchiveError):
        inspect_archive(_build(entries=[_entry("lab.json", canonical_bytes(definition)), lying]))


def test_a_manifest_whose_size_disagrees_with_the_bytes_is_refused():
    definition = _definition()
    lying = _entry("experience/runbook.md", RUNBOOK)
    lying["size_bytes"] = len(RUNBOOK) + 10

    with pytest.raises(PackageArchiveError):
        inspect_archive(_build(entries=[_entry("lab.json", canonical_bytes(definition)), lying]))


def test_a_manifest_for_another_package_identity_is_refused():
    definition = _definition()
    manifest = canonical_bytes(
        LabManifest.model_validate(
            {
                "format_version": 1,
                "package_id": "another-lab",
                "version": "9.9.9",
                "required_capabilities": [],
                "payloads": [
                    _entry("lab.json", canonical_bytes(definition)),
                    _entry("experience/runbook.md", RUNBOOK),
                ],
            }
        )
    )

    with pytest.raises(PackageArchiveError):
        inspect_archive(_build(manifest_bytes=manifest))


def test_an_unreadable_manifest_is_refused():
    with pytest.raises(PackageArchiveError):
        inspect_archive(_build(manifest_bytes=b"{not json"))


# --- supporting files ----------------------------------------------------------


def test_a_supporting_file_inside_the_experience_tree_is_accepted():
    inspection = inspect_archive(_build(payloads={"experience/notes.md": b"# Notes\n"}))

    assert inspection.payloads["experience/notes.md"] == b"# Notes\n"


@pytest.mark.parametrize("name", ["notes.md", "docs/notes.md", "assets/notes.md"])
def test_a_supporting_file_outside_the_experience_tree_is_refused(name):
    with pytest.raises(PackageArchiveError):
        inspect_archive(_build(payloads={name: b"# Notes\n"}))


# --- member ceilings -----------------------------------------------------------


def test_an_archive_over_the_configured_ceiling_is_refused():
    with pytest.raises(PackageArchiveError):
        inspect_archive(_build(), max_archive_bytes=16)
