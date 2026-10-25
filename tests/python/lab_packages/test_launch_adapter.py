from __future__ import annotations

import pytest

from astinuslab.lab_packages.models import (
    HostResources,
    LabDefinition,
    LabExperience,
    LabNetwork,
    LabVM,
    ResourceRecommendations,
)
from astinuslab.lab_packages.playground_adapter import PackageLaunchError, recipe_from_definition


def _definition(**network_changes: object) -> LabDefinition:
    return LabDefinition(
        package_id="launch-lab",
        version="1.0.0",
        title="Launch lab",
        creator="Researcher",
        license="CC-BY-4.0",
        reproducibility="unlocked",
        resources=ResourceRecommendations(
            minimum=HostResources(cpu_threads=2, memory_mb=4096, free_disk_bytes=40 * 1024**3),
            recommended=HostResources(cpu_threads=4, memory_mb=8192, free_disk_bytes=40 * 1024**3),
        ),
        networks=(LabNetwork.model_validate({"id": "isolated", "subnet_cidr": "192.0.2.0/29", **network_changes}),),
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


def test_unlocked_definition_maps_to_recipe():
    recipe = recipe_from_definition(_definition())

    assert recipe.name == "Launch lab"
    assert recipe.networks[0].name == "isolated"
    assert recipe.vms[0].label == "target"
    assert recipe.vms[0].vcpus == 2
    assert recipe.vms[0].networks == ["isolated"]


def test_locked_definition_requires_clean_start_lifecycle():
    with pytest.raises(PackageLaunchError, match="locked packages"):
        recipe_from_definition(_definition().model_copy(update={"reproducibility": "locked"}))


def test_host_bridge_is_not_silently_dropped():
    with pytest.raises(PackageLaunchError, match="host bridge"):
        recipe_from_definition(_definition(isolated=False, host_bridge="br0"))
