from __future__ import annotations

from astinuslab.core.compatibility import normalize_compatibility, workload_compatibility_for_target


def _target(**overrides):
    base = {
        "id": "ubuntu-22-04-default",
        "family": "debian",
        "distro": "ubuntu",
        "version": "22.04",
        "arch": "x86_64",
        "disk_gb": 20,
    }
    base.update(overrides)
    return base


def test_include_family_matches_existing_catalog_shape():
    workload = {
        "id": "wordpress-6.4",
        "compatibility": normalize_compatibility({"include": [{"family": "debian"}]}),
    }

    result = workload_compatibility_for_target(workload, _target())

    assert result["compatibility_status"] == "compatible"
    reason = result["compatibility_reason"]
    assert reason is not None
    assert "family debian" in reason


def test_version_range_blocks_too_new_distro():
    workload = {
        "id": "legacy-php",
        "compatibility": normalize_compatibility(
            {"include": [{"distro": "ubuntu", "version_min": "20.04", "version_max": "22.99"}]}
        ),
    }

    result = workload_compatibility_for_target(workload, _target(version="24.04"))

    assert result["compatibility_status"] == "incompatible"
    reason = result["compatibility_reason"]
    assert reason is not None
    assert "not in the workload compatibility matrix" in reason


def test_exclude_rule_overrides_include_rule():
    workload = {
        "id": "not-noble",
        "compatibility": normalize_compatibility(
            {
                "include": [{"family": "debian"}],
                "exclude": [{"distro": "ubuntu", "version": "24.04"}],
            }
        ),
    }

    result = workload_compatibility_for_target(workload, _target(version="24.04"))

    assert result["compatibility_status"] == "incompatible"
    reason = result["compatibility_reason"]
    assert reason is not None
    assert "Excluded for distro ubuntu / version 24.04" in reason


def test_min_disk_gb_blocks_small_target():
    workload = {
        "id": "gitlab-ce-11",
        "compatibility": normalize_compatibility({"include": [{"family": "debian", "min_disk_gb": 30}]}),
    }

    result = workload_compatibility_for_target(workload, _target(disk_gb=20))

    assert result["compatibility_status"] == "incompatible"
    reason = result["compatibility_reason"]
    assert reason is not None
    assert "requires at least 30GB disk" in reason


def test_arch_selector_blocks_wrong_architecture():
    workload = {
        "id": "x86-only",
        "compatibility": normalize_compatibility({"include": [{"family": "debian", "arch": "x86_64"}]}),
    }

    result = workload_compatibility_for_target(workload, _target(arch="aarch64"))

    assert result["compatibility_status"] == "incompatible"


def test_unknown_when_no_compatibility_declared():
    workload = {"id": "custom", "compatibility": normalize_compatibility(None)}

    result = workload_compatibility_for_target(workload, _target())

    assert result["compatibility_status"] == "unknown"
    assert result["compatibility_reason"] == "Compatibility is not declared for this workload"


def test_stack_missing_component_is_incompatible():
    workload = {
        "id": "stack",
        "is_stack": True,
        "components": ["missing-component"],
        "compatibility": normalize_compatibility({"include": [{"family": "debian"}]}),
    }

    result = workload_compatibility_for_target(workload, _target(), workload_map={})

    assert result["compatibility_status"] == "incompatible"
    reason = result["compatibility_reason"]
    assert reason is not None
    assert "Missing component 'missing-component'" in reason
