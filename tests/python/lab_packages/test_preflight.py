from __future__ import annotations

import pytest

from astinuslab.lab_packages.models import (
    AssetReference,
    ManifestPayload,
)
from astinuslab.lab_packages.preflight import (
    HostFacts,
    PreflightIssue,
    issue_warning_consent_token,
    preflight_package,
    reserve_disk_space,
    validate_warning_consent_token,
)
from tests.python._support.lab_packages import preflight_documents as _documents


def test_preflight_is_ready_when_host_meets_minimum_and_capabilities():
    definition, manifest = _documents(("x86_64", "kvm"))
    host = HostFacts(4, 8192, 60 * 1024**3, "x86_64", True, frozenset({"x86_64", "kvm", "offline"}))

    result = preflight_package(definition, manifest, host=host)

    assert result.ready is True
    assert result.errors == ()
    assert result.required_disk_bytes == 10 * 1024**3 + 2


@pytest.mark.parametrize(
    "detail",
    [
        "workload_id 'missing' not found",
        "workload_id is supported for targets only",
        "Workload requires Ubuntu and a 20 GiB disk",
        "",
    ],
)
def test_catalog_refusal_preserves_workload_detail_and_vm(monkeypatch, detail):
    from astinuslab.lab_packages.preflight import _check_unlocked_catalog
    from astinuslab.playground.workloads import PlaygroundWorkloadValidation

    definition, _manifest = _documents()
    monkeypatch.setattr("astinuslab.playground.orchestrator.lookup_playground_image", lambda *a, **k: {"id": "fixture"})
    monkeypatch.setattr(
        "astinuslab.playground.workloads.validate_playground_workload_selection",
        lambda **k: PlaygroundWorkloadValidation(ok=False, detail=detail),
    )
    issues: list[PreflightIssue] = []
    _check_unlocked_catalog(definition, issues)
    assert issues[0].severity == "error" and issues[0].code == "workload-incompatible"
    assert definition.vms[0].id in issues[0].message
    assert (detail or "workload is not compatible") in issues[0].message


def test_preflight_warns_below_recommendation_but_allows_launch():
    definition, manifest = _documents()
    host = HostFacts(2, 4096, 45 * 1024**3, "x86_64", True, frozenset({"x86_64", "kvm"}))

    result = preflight_package(definition, manifest, host=host)

    assert result.ready is True
    assert {issue.code for issue in result.warnings} == {
        "below-recommended-cpu",
        "below-recommended-memory",
        "below-recommended-disk",
        "unlocked-package",
    }


def test_preflight_blocks_insufficient_host_and_missing_capability():
    definition, manifest = _documents(("x86_64", "kvm", "host-bridge"))
    host = HostFacts(1, 1024, 1 * 1024**3, "aarch64", False, frozenset({"offline"}))

    result = preflight_package(definition, manifest, host=host)

    assert result.ready is False
    assert {issue.code for issue in result.errors} == {
        "insufficient-cpu",
        "insufficient-memory",
        "insufficient-disk",
        "kvm-unavailable",
        "unsupported-host-architecture",
        "unsupported-capability",
    }


def test_preflight_rejects_manifest_definition_identity_mismatch():
    definition, manifest = _documents()
    other = manifest.model_copy(update={"version": "2.0.0"})

    with pytest.raises(ValueError, match="identity mismatch"):
        preflight_package(definition, other, host=HostFacts(4, 8192, 60 * 1024**3, "x86_64", True))


def test_preflight_accounts_for_clean_layer_storage_and_cache_presence():
    definition, manifest = _documents()
    clean_layer = AssetReference(
        sha256="c" * 64,
        size_bytes=123,
        media_type="application/x-qcow2",
        license="CC0-1.0",
        redistribution_confirmed=True,
    )
    locked = definition.model_copy(
        update={
            "reproducibility": "locked",
            "vms": (definition.vms[0].model_copy(update={"clean_layer": clean_layer}),),
        }
    )
    manifest = manifest.model_copy(
        update={
            "payloads": manifest.payloads
            + (
                ManifestPayload(
                    path=f"assets/sha256/{clean_layer.sha256}",
                    sha256=clean_layer.sha256,
                    size_bytes=clean_layer.size_bytes,
                    media_type=clean_layer.media_type,
                ),
            )
        }
    )

    result = preflight_package(
        locked,
        manifest,
        host=HostFacts(4, 8192, 60 * 1024**3, "x86_64", True),
        available_assets=frozenset(),
    )

    assert result.ready is False
    # The package retains one embedded copy and materializes one immutable
    # clean-start layer beside it.
    assert result.required_disk_bytes == 10 * 1024**3 + 2 + 2 * clean_layer.size_bytes
    assert {issue.code for issue in result.errors} == {"missing-clean-layer"}


def test_preflight_separates_retained_bytes_from_cached_launch_footprint():
    definition, manifest = _documents()
    clean_layer = AssetReference(
        sha256="d" * 64,
        size_bytes=123,
        media_type="application/x-qcow2",
        license="CC0-1.0",
        redistribution_confirmed=True,
    )
    locked = definition.model_copy(
        update={
            "reproducibility": "locked",
            "vms": (definition.vms[0].model_copy(update={"clean_layer": clean_layer}),),
        }
    )
    manifest = manifest.model_copy(
        update={
            "payloads": manifest.payloads
            + (
                ManifestPayload(
                    path=f"assets/sha256/{clean_layer.sha256}",
                    sha256=clean_layer.sha256,
                    size_bytes=clean_layer.size_bytes,
                    media_type=clean_layer.media_type,
                ),
            )
        }
    )

    result = preflight_package(
        locked,
        manifest,
        host=HostFacts(4, 8192, 60 * 1024**3, "x86_64", True),
        available_assets=frozenset({clean_layer.sha256}),
    )

    assert result.ready is True
    assert result.retained_disk_bytes == 2 + clean_layer.size_bytes
    assert result.launch_disk_bytes == 10 * 1024**3
    assert result.required_disk_bytes == result.retained_disk_bytes + result.launch_disk_bytes


def test_warning_consent_token_binds_warning_values_and_capabilities():
    warnings = (PreflightIssue("warning", "guest-egress", "NAT requires acknowledgement"),)
    token = issue_warning_consent_token(
        package_digest="a" * 64,
        warnings=warnings,
        policy_revision="policy-1",
        requested_capabilities=("guest_egress",),
        nonce="test-nonce",
        now=1_000,
    )

    assert validate_warning_consent_token(
        token,
        package_digest="a" * 64,
        warnings=warnings,
        policy_revision="policy-1",
        requested_capabilities=("guest_egress",),
        now=1_001,
    )
    assert not validate_warning_consent_token(
        token,
        package_digest="a" * 64,
        warnings=(PreflightIssue("warning", "guest-egress", "different value"),),
        policy_revision="policy-1",
        requested_capabilities=("guest_egress",),
        now=1_001,
    )


def test_preflight_does_not_charge_retained_package_bytes_again():
    definition, manifest = _documents()
    definition = definition.model_copy(
        update={
            "resources": definition.resources.model_copy(
                update={
                    "minimum": definition.resources.minimum.model_copy(update={"free_disk_bytes": 1}),
                    "recommended": definition.resources.recommended.model_copy(update={"free_disk_bytes": 1}),
                }
            ),
            "vms": (definition.vms[0].model_copy(update={"disk_gb": 1}),),
        }
    )
    manifest = manifest.model_copy(
        update={
            "payloads": manifest.payloads
            + (
                ManifestPayload(
                    path="assets/sha256/" + "e" * 64,
                    sha256="e" * 64,
                    size_bytes=10 * 1024**3,
                    media_type="application/octet-stream",
                ),
            )
        }
    )

    result = preflight_package(
        definition,
        manifest,
        host=HostFacts(4, 8192, 2 * 1024**3, "x86_64", True, frozenset({"x86_64", "kvm"})),
        retained_present=True,
    )

    assert result.ready is True
    assert result.retained_disk_bytes > result.host.free_disk_bytes
    assert result.launch_disk_bytes == 1 * 1024**3


def test_disk_reservation_uses_owned_files_and_releases_them(tmp_path):
    reservation = reserve_disk_space(((tmp_path, 4096),), owner_id="instance-1")

    assert reservation.allocation_method in {"posix_fallocate", "journaled_write"}
    assert reservation.paths[0].exists()
    assert reservation.paths[0].stat().st_size == 4096
    reservation.release()
    assert not reservation.paths[0].exists()
