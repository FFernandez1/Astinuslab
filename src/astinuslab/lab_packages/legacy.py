"""Compatibility adapter for the original Playground scenario ZIP format."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .. import catalog
from ..api.schemas import PlaygroundRecipe
from ..playground.recipes import PlaygroundRecipeError, validate_recipe
from .models import (
    HostResources,
    LabDefinition,
    LabExperience,
    LabNetwork,
    LabTopologyNode,
    LabVM,
    ResourceRecommendations,
)

LEGACY_RUNBOOK = """# Legacy scenario package

This package was imported from a legacy scenario archive. The source contained
infrastructure only; it did not contain a forensic exercise or reproducible
content.
"""


class LegacyScenarioPackageError(ValueError):
    """Raised when a legacy scenario cannot be converted to a lab definition."""


@dataclass(frozen=True)
class LegacyScenarioImport:
    definition: LabDefinition
    runbook: str = LEGACY_RUNBOOK


def _safe_package_id(value: str) -> str:
    candidate = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    if len(candidate) < 3:
        candidate = f"legacy-{candidate or 'lab'}"
    return candidate[:128]


def _resources(recipe: PlaygroundRecipe) -> ResourceRecommendations:
    vm_cpu = sum(vm.vcpus or 1 for vm in recipe.vms)
    vm_memory = sum(vm.memory_mb or 512 for vm in recipe.vms)
    vm_disk = sum(vm.disk_gb or 20 for vm in recipe.vms)
    minimum = HostResources(
        cpu_threads=max(2, vm_cpu),
        memory_mb=max(4096, vm_memory),
        free_disk_bytes=max(40 * 1024**3, vm_disk * 1024**3),
    )
    recommended = HostResources(
        cpu_threads=max(minimum.cpu_threads, 4),
        memory_mb=max(minimum.memory_mb, 8192),
        free_disk_bytes=max(minimum.free_disk_bytes, 60 * 1024**3),
    )
    return ResourceRecommendations(minimum=minimum, recommended=recommended)


def adapt_recipe(
    recipe: PlaygroundRecipe,
    *,
    package_id: str,
    version: str = "1.0.0",
    creator: str = "Legacy scenario package",
    license: str = "NOASSERTION",
    title: str | None = None,
    description: str = "Imported from a legacy Playground scenario package.",
) -> LabDefinition:
    try:
        validate_recipe(recipe)
    except PlaygroundRecipeError as exc:
        raise LegacyScenarioPackageError(str(exc)) from exc
    try:
        networks = tuple(
            LabNetwork(id=network.name, subnet_cidr=network.subnet_cidr, isolated=network.isolated)
            for network in recipe.networks
        )
        vms = tuple(
            LabVM(
                id=vm.label,
                role=vm.role,
                image_id=vm.image_id,
                workload_id=vm.workload_id,
                vcpus=vm.vcpus or 1,
                memory_mb=vm.memory_mb or 512,
                disk_gb=vm.disk_gb or 20,
                network_ids=tuple(vm.networks),
                clean_layer=None,
            )
            for vm in recipe.vms
        )
        topology = tuple(
            LabTopologyNode(node_id=node.node_id, node_type=node.node_type, x=node.x, y=node.y)
            for node in recipe.topology
        )
        return LabDefinition(
            package_id=package_id,
            version=version,
            title=title or recipe.name,
            description=description,
            creator=creator,
            license=license,
            reproducibility="unlocked",
            resources=_resources(recipe),
            networks=networks,
            vms=vms,
            topology=topology,
            experience=LabExperience(objectives=("The source contained infrastructure only.",)),
        )
    except ValidationError as exc:
        raise LegacyScenarioPackageError("legacy scenario does not satisfy the portable lab contract") from exc


def definition_from_scenario_package(
    data: bytes,
    *,
    package_id: str | None = None,
    version: str = "1.0.0",
    creator: str = "Legacy scenario package",
    license: str = "NOASSERTION",
    title: str | None = None,
    description: str = "Imported from a legacy Playground scenario package.",
) -> LabDefinition:
    """Read and validate a legacy ZIP, returning an unlocked logical definition."""
    try:
        package = catalog.read_scenario_package(data)
        recipe = PlaygroundRecipe.model_validate(package["recipe"])
    except (catalog.ScenarioPackageError, ValidationError) as exc:
        raise LegacyScenarioPackageError("invalid legacy scenario package") from exc
    resolved_package_id = _safe_package_id(package_id or recipe.name)
    return adapt_recipe(
        recipe,
        package_id=resolved_package_id,
        version=version,
        creator=creator,
        license=license,
        title=title,
        description=description,
    )


def import_scenario_package(data: bytes, **kwargs: Any) -> LegacyScenarioImport:
    """Return both the unlocked definition and the neutral generated runbook."""
    return LegacyScenarioImport(definition=definition_from_scenario_package(data, **kwargs))


def legacy_runbook() -> str:
    return LEGACY_RUNBOOK


lab_definition_from_scenario_package = definition_from_scenario_package
