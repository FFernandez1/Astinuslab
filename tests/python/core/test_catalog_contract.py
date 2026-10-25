"""Catalog contract: pin the committed Target x Workload compatibility matrix.

Feeds every (target, workload) pair from the committed catalog
(config/catalog/*.example.yaml) through the real compatibility engine and
asserts the concrete verdicts. The non-example catalog files are gitignored
and machine-local, so they are deliberately not used here. A catalog edit
that changes the matrix must consciously update the EXPECTED_* sets below.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from astinuslab.core.compatibility import normalize_compatibility, workload_compatibility_for_target

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
TARGETS_YAML = _REPO_ROOT / "config" / "catalog" / "targets.example.yaml"
WORKLOADS_YAML = _REPO_ROOT / "config" / "catalog" / "workloads.example.yaml"

EXPECTED_DEBIAN_TARGETS = {
    "ubuntu-22-04-default",
    "ubuntu-24.04",
    "ubuntu-20-04-legacy",
    "debian-12",
}
EXPECTED_REDHAT_TARGETS = {"fedora-40"}
EXPECTED_WORKLOADS = {
    "dvwa",
    "wordpress-6.4",
    "full-php-stack",
    "juice-shop",
    "vulnhub-basic-pentesting",
    "gitlab-ce-11",
}


def _load_contract_targets() -> list[dict]:
    raw = yaml.safe_load(TARGETS_YAML.read_text(encoding="utf-8")) or {}
    targets = []
    for item in raw.get("targets", []):
        distro = item.get("distro", {})
        virtualization = item.get("virtualization", {})
        targets.append(
            {
                "id": item["id"],
                "family": distro.get("family", "debian"),
                "distro": distro.get("slug", "unknown"),
                "version": str(distro.get("version", "unknown")),
                "disk_gb": virtualization.get("disk_gb"),
            }
        )
    return targets


def _load_contract_workloads() -> list[dict]:
    raw = yaml.safe_load(WORKLOADS_YAML.read_text(encoding="utf-8")) or {}
    return [
        {
            "id": item["id"],
            "is_stack": item.get("type") == "stack",
            "components": item.get("components", []),
            "compatibility": normalize_compatibility(item.get("compatibility")),
        }
        for item in raw.get("workloads", [])
    ]


def test_catalog_inventory_matches_contract():
    target_ids = {target["id"] for target in _load_contract_targets()}
    workload_ids = {workload["id"] for workload in _load_contract_workloads()}
    assert target_ids == EXPECTED_DEBIAN_TARGETS | EXPECTED_REDHAT_TARGETS, (
        "targets.example.yaml changed; update the expected matrix in this test"
    )
    assert workload_ids == EXPECTED_WORKLOADS, "workloads.example.yaml changed; update the expected matrix in this test"


def test_every_catalog_pair_has_the_expected_verdict():
    targets = _load_contract_targets()
    workloads = _load_contract_workloads()
    workload_map = {workload["id"]: workload for workload in workloads}

    unexpected: list[str] = []
    checked = 0
    for target in targets:
        expected_status = "compatible" if target["id"] in EXPECTED_DEBIAN_TARGETS else "incompatible"
        for workload in workloads:
            checked += 1
            verdict = workload_compatibility_for_target(workload, target, workload_map=workload_map)
            if verdict["compatibility_status"] != expected_status:
                unexpected.append(
                    f"{target['id']} x {workload['id']}: got {verdict['compatibility_status']!r} "
                    f"(reason: {verdict['compatibility_reason']}), expected {expected_status!r}"
                )

    expected_pairs = len(EXPECTED_DEBIAN_TARGETS | EXPECTED_REDHAT_TARGETS) * len(EXPECTED_WORKLOADS)
    assert checked == expected_pairs, f"expected {expected_pairs} catalog pairs, found {checked}"
    assert unexpected == []
