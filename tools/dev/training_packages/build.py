"""Build passive, image-free draft archives; never provision or start a lab."""

from __future__ import annotations

import argparse
from pathlib import Path

from astinuslab.lab_packages.archive import inspect_archive, write_archive
from astinuslab.lab_packages.models import LabDefinition, validate_checkpoint
from astinuslab.lab_packages.playground_adapter import recipe_from_definition
from astinuslab.playground.recipes import validate_recipe
from astinuslab.virtualization.guest_observation import normalize_observation_path

SOURCE = Path(__file__).resolve().parent
LABS = ("01-single-vulnerability", "02-pivot", "03-firewall")


def build_package(source: Path, destination: Path) -> Path:
    """Validate a draft and write a new archive without overwriting an export."""
    definition = LabDefinition.model_validate_json((source / "lab.json").read_text(encoding="utf-8"))
    if definition.reproducibility != "unlocked":
        raise ValueError("This builder produces unlocked drafts only; use clean-run verification for locked exports")
    runbook = (source / "runbook.md").read_text(encoding="utf-8")
    if not runbook.strip():
        raise ValueError("A non-blank runbook is required")
    validate_recipe(recipe_from_definition(definition))
    vm_ids = {vm.id for vm in definition.vms}
    for checkpoint in definition.experience.checkpoints:
        operation = validate_checkpoint(checkpoint)
        if operation.vm_id not in vm_ids:
            raise ValueError(f"Checkpoint references unknown VM: {operation.vm_id}")
        normalize_observation_path(operation.path)
    payload = write_archive(definition, runbook, required_capabilities=("x86_64", "kvm"))
    inspect_archive(payload)
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / f"{definition.package_id}-{definition.version}.astinuslab"
    with output.open("xb") as handle:
        handle.write(payload)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    for name in LABS:
        output = build_package(SOURCE / name, args.output_dir)
        print(f"DRAFT {output}")


if __name__ == "__main__":
    main()
