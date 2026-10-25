"""Offline Ed25519 signatures and explicit local trust for lab packages.

Signatures cover the exact canonical bytes of ``manifest.json``.  The manifest
already contains the complete path, size, and SHA-256 inventory, so signing it
also authenticates the package payload set without ever executing package data.
The archive layer keeps the detached signature pair outside that inventory.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .archive import ArchiveInspection, PackageArchiveError, inspect_archive, write_archive_to
from .canonical import canonical_bytes
from .models import LabManifest

type ArchiveSource = bytes | bytearray | memoryview | BinaryIO | Path

ED25519_PRIVATE_KEY_BYTES: Final = 32
ED25519_PUBLIC_KEY_BYTES: Final = 32
ED25519_SIGNATURE_BYTES: Final = 64
_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_MANIFEST_INVENTORY_PATHS = frozenset({"manifest.json"})


class SignatureStatus(StrEnum):
    """Stable result values for detached package signature verification."""

    UNSIGNED = "unsigned"
    INVALID_SIGNATURE = "invalid-signature"
    VALID_UNTRUSTED_KEY = "valid-untrusted-key"
    VALID_LOCALLY_TRUSTED_KEY = "valid-locally-trusted-key"


@dataclass(frozen=True, slots=True)
class SignatureResult:
    """The verification result and, when available, the creator fingerprint."""

    status: SignatureStatus
    fingerprint: str | None = None

    @property
    def valid(self) -> bool:
        return self.status in {
            SignatureStatus.VALID_UNTRUSTED_KEY,
            SignatureStatus.VALID_LOCALLY_TRUSTED_KEY,
        }


def _raw_key(value: bytes | bytearray | memoryview, expected: int, label: str) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise PackageArchiveError(f"{label} must be raw bytes")
    raw = bytes(value)
    if len(raw) != expected:
        raise PackageArchiveError(f"invalid {label} length")
    return raw


def _raw_signature(value: bytes | bytearray | memoryview) -> bytes:
    return _raw_key(value, ED25519_SIGNATURE_BYTES, "signature")


def public_key_fingerprint(public_key: bytes | bytearray | memoryview) -> str:
    """Return the stable lowercase SHA-256 fingerprint of a raw public key."""

    return hashlib.sha256(_raw_key(public_key, ED25519_PUBLIC_KEY_BYTES, "public key")).hexdigest()


def _validate_fingerprint(value: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT_PATTERN.fullmatch(value) is None:
        raise PackageArchiveError("invalid public key fingerprint")
    return value


@dataclass(frozen=True, slots=True)
class LocalTrustPolicy:
    """Immutable, process-local allow-list for creator public keys.

    Trust is never inferred from a manifest's creator text or any package
    content.  A policy is explicitly constructed by the local operator and can
    match either raw public-key bytes or their SHA-256 fingerprints.
    """

    trusted_public_keys: frozenset[bytes] = field(default_factory=frozenset)
    trusted_fingerprints: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        keys = frozenset(_raw_key(key, ED25519_PUBLIC_KEY_BYTES, "public key") for key in self.trusted_public_keys)
        fingerprints = frozenset(_validate_fingerprint(fingerprint) for fingerprint in self.trusted_fingerprints)
        object.__setattr__(self, "trusted_public_keys", keys)
        object.__setattr__(self, "trusted_fingerprints", fingerprints)

    @classmethod
    def empty(cls) -> LocalTrustPolicy:
        return cls()

    @classmethod
    def from_public_keys(cls, public_keys: Iterable[bytes | bytearray | memoryview]) -> LocalTrustPolicy:
        return cls(
            trusted_public_keys=frozenset(_raw_key(key, ED25519_PUBLIC_KEY_BYTES, "public key") for key in public_keys)
        )

    @classmethod
    def from_fingerprints(cls, fingerprints: Iterable[str]) -> LocalTrustPolicy:
        return cls(trusted_fingerprints=frozenset(fingerprints))

    def trusts(self, public_key: bytes | bytearray | memoryview) -> bool:
        raw_key = _raw_key(public_key, ED25519_PUBLIC_KEY_BYTES, "public key")
        return raw_key in self.trusted_public_keys or public_key_fingerprint(raw_key) in self.trusted_fingerprints

    is_trusted = trusts

    def with_public_key(self, public_key: bytes | bytearray | memoryview) -> LocalTrustPolicy:
        raw_key = _raw_key(public_key, ED25519_PUBLIC_KEY_BYTES, "public key")
        return LocalTrustPolicy(
            trusted_public_keys=self.trusted_public_keys | {raw_key},
            trusted_fingerprints=self.trusted_fingerprints,
        )

    def with_fingerprint(self, fingerprint: str) -> LocalTrustPolicy:
        return LocalTrustPolicy(
            trusted_public_keys=self.trusted_public_keys,
            trusted_fingerprints=self.trusted_fingerprints | {_validate_fingerprint(fingerprint)},
        )


# Both names read naturally at call sites; the alias keeps the policy surface
# intentionally small while allowing applications to call it a trust store.
TrustPolicy = LocalTrustPolicy
TrustStore = LocalTrustPolicy


def generate_key_pair() -> tuple[bytes, bytes]:
    """Generate a raw Ed25519 private/public key pair."""

    private = Ed25519PrivateKey.generate()
    private_bytes = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private_bytes, public_bytes


generate_keypair = generate_key_pair


def public_key_from_private(private_key: bytes | bytearray | memoryview) -> bytes:
    """Derive a raw Ed25519 public key from a raw private key."""

    raw_private = _raw_key(private_key, ED25519_PRIVATE_KEY_BYTES, "private key")
    try:
        return (
            Ed25519PrivateKey.from_private_bytes(raw_private)
            .public_key()
            .public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
        )
    except (TypeError, ValueError) as exc:
        raise PackageArchiveError("invalid private key") from exc


def _canonical_manifest_bytes(manifest: LabManifest | bytes | bytearray | memoryview) -> bytes:
    if isinstance(manifest, LabManifest):
        try:
            canonical = canonical_bytes(manifest)
        except (TypeError, UnicodeError, ValueError) as exc:
            raise PackageArchiveError("invalid lab manifest") from exc
        if any(
            payload.path in _FORBIDDEN_MANIFEST_INVENTORY_PATHS or payload.path.startswith("signatures/")
            for payload in manifest.payloads
        ):
            raise PackageArchiveError("manifest cannot inventory signature or manifest")
        return canonical
    if not isinstance(manifest, (bytes, bytearray, memoryview)):
        raise PackageArchiveError("manifest must be LabManifest or canonical JSON bytes")
    raw = bytes(manifest)
    try:
        parsed = json.loads(raw.decode("utf-8", errors="strict"))
        validated = LabManifest.model_validate(parsed)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
        raise PackageArchiveError("invalid lab manifest") from exc
    try:
        canonical = canonical_bytes(validated)
    except (TypeError, UnicodeError, ValueError) as exc:
        raise PackageArchiveError("invalid lab manifest") from exc
    if any(
        payload.path in _FORBIDDEN_MANIFEST_INVENTORY_PATHS or payload.path.startswith("signatures/")
        for payload in validated.payloads
    ):
        raise PackageArchiveError("manifest cannot inventory signature or manifest")
    if raw != canonical:
        raise PackageArchiveError("manifest bytes must be canonical JSON")
    return raw


def sign_manifest(
    manifest: LabManifest | bytes | bytearray | memoryview,
    private_key: bytes | bytearray | memoryview,
    public_key: bytes | bytearray | memoryview | None = None,
) -> bytes:
    """Sign exact canonical ``manifest.json`` bytes with a raw Ed25519 key."""

    message = _canonical_manifest_bytes(manifest)
    raw_private = _raw_key(private_key, ED25519_PRIVATE_KEY_BYTES, "private key")
    derived_public = public_key_from_private(raw_private)
    if public_key is not None and _raw_key(public_key, ED25519_PUBLIC_KEY_BYTES, "public key") != derived_public:
        raise PackageArchiveError("public key does not match private key")
    try:
        return Ed25519PrivateKey.from_private_bytes(raw_private).sign(message)
    except (TypeError, ValueError) as exc:
        raise PackageArchiveError("could not sign lab manifest") from exc


def _invalid_result() -> SignatureResult:
    return SignatureResult(SignatureStatus.INVALID_SIGNATURE)


def verify_manifest(
    manifest: LabManifest | bytes | bytearray | memoryview,
    signature: bytes | bytearray | memoryview | None,
    public_key: bytes | bytearray | memoryview | None,
    *,
    trust_policy: LocalTrustPolicy | None = None,
) -> SignatureResult:
    """Verify a detached signature and classify local trust explicitly."""

    if signature is None and public_key is None:
        return SignatureResult(SignatureStatus.UNSIGNED)
    if signature is None or public_key is None:
        return _invalid_result()
    try:
        message = _canonical_manifest_bytes(manifest)
        raw_signature = _raw_signature(signature)
        raw_public = _raw_key(public_key, ED25519_PUBLIC_KEY_BYTES, "public key")
        Ed25519PublicKey.from_public_bytes(raw_public).verify(raw_signature, message)
        fingerprint = public_key_fingerprint(raw_public)
    except (InvalidSignature, PackageArchiveError, TypeError, ValueError):
        return _invalid_result()
    trusted = trust_policy is not None and trust_policy.trusts(raw_public)
    status = SignatureStatus.VALID_LOCALLY_TRUSTED_KEY if trusted else SignatureStatus.VALID_UNTRUSTED_KEY
    return SignatureResult(status, fingerprint)


def sign_archive(
    source: ArchiveSource | ArchiveInspection,
    private_key: bytes | bytearray | memoryview,
    public_key: bytes | bytearray | memoryview | None = None,
    *,
    max_archive_bytes: int | None = None,
    max_asset_bytes: int | None = None,
) -> bytes:
    """Return a deterministic package with a detached Ed25519 signature pair."""

    inspection = (
        source
        if isinstance(source, ArchiveInspection)
        else inspect_archive(source, max_archive_bytes=max_archive_bytes, max_asset_bytes=max_asset_bytes)
    )
    output = io.BytesIO()
    sign_archive_to(
        output,
        inspection,
        private_key,
        public_key,
        max_archive_bytes=max_archive_bytes,
        max_asset_bytes=max_asset_bytes,
    )
    return output.getvalue()


def sign_archive_to(
    sink: BinaryIO,
    source: ArchiveSource | ArchiveInspection,
    private_key: bytes | bytearray | memoryview,
    public_key: bytes | bytearray | memoryview | None = None,
    *,
    max_archive_bytes: int | None = None,
    max_asset_bytes: int | None = None,
) -> None:
    """Sign a package into a caller-owned sink without materialising its assets."""

    inspection = (
        source
        if isinstance(source, ArchiveInspection)
        else inspect_archive(source, max_archive_bytes=max_archive_bytes, max_asset_bytes=max_asset_bytes)
    )
    raw_public = public_key_from_private(private_key)
    if public_key is not None and _raw_key(public_key, ED25519_PUBLIC_KEY_BYTES, "public key") != raw_public:
        raise PackageArchiveError("public key does not match private key")
    signature = sign_manifest(inspection.payloads["manifest.json"], private_key, raw_public)
    write_archive_to(
        sink,
        inspection.definition,
        inspection.runbook,
        assets=inspection.assets,
        asset_streams=inspection.asset_descriptors,
        supporting_files=inspection.supporting_files,
        manifest_override=inspection.manifest,
        allow_legacy_unreferenced_assets=(
            inspection.definition.reproducibility == "unlocked"
            and all(
                vm.base_image is None and vm.effective_boot_layer is None and vm.workload is None
                for vm in inspection.definition.vms
            )
        ),
        max_archive_bytes=max_archive_bytes,
        max_asset_bytes=max_asset_bytes,
        signatures={
            "signatures/manifest.ed25519": signature,
            "signatures/creator.pub": raw_public,
        },
    )


def verify_archive(
    source: ArchiveSource | None = None,
    *,
    trust_policy: LocalTrustPolicy | None = None,
    inspection: ArchiveInspection | None = None,
) -> SignatureResult:
    """Inspect and verify a package without extracting or executing it."""

    if inspection is None:
        if source is None:
            raise TypeError("source or inspection is required")
        inspection = inspect_archive(source)
    elif source is not None:
        raise TypeError("pass source or inspection, not both")
    signature_name = "signatures/manifest.ed25519"
    public_name = "signatures/creator.pub"
    has_signature = signature_name in inspection.signatures
    has_public_key = public_name in inspection.signatures
    if not has_signature and not has_public_key:
        return SignatureResult(SignatureStatus.UNSIGNED)
    if has_signature != has_public_key:
        raise PackageArchiveError("partial package signature pair")
    return verify_manifest(
        inspection.payloads["manifest.json"],
        inspection.signatures[signature_name],
        inspection.signatures[public_name],
        trust_policy=trust_policy,
    )
