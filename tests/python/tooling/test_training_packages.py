"""Exercise the actual archive/recipe consumer contracts for the training drafts."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from astinuslab import db as db_mod
from astinuslab.api import get_session
from astinuslab.lab_packages.api import router
from astinuslab.lab_packages.archive import inspect_archive
from astinuslab.lab_packages.content_store import ContentStore
from astinuslab.lab_packages.models import GuestAssertion, LabDefinition, validate_checkpoint
from astinuslab.lab_packages.package_service import LabPackageService
from astinuslab.lab_packages.playground_adapter import recipe_from_definition
from astinuslab.playground.recipes import validate_recipe
from astinuslab.virtualization.guest_observation import GuestObservationError, normalize_observation_path

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "tools/dev/training_packages"
EXPECTED = {"01-single-vulnerability": 1, "02-pivot": 3, "03-firewall": 2}
pytestmark = pytest.mark.unit


def _builder():
    path = SOURCE / "build.py"
    assert path.is_file(), "Training package builder has not been created"
    spec = importlib.util.spec_from_file_location("training_package_builder", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name,count", EXPECTED.items())
def test_draft_roundtrips_through_archive_and_recipe_consumers(name, count, tmp_path):
    result = _builder().build_package(SOURCE / name, tmp_path)
    inspected = inspect_archive(result)
    definition = inspected.definition
    assert len(definition.vms) == count
    assert all(vm.role != "attacker" for vm in definition.vms)
    assert definition.reproducibility == "unlocked"
    assert not inspected.assets
    assert all(vm.boot_layer is None and vm.base_image is None for vm in definition.vms)
    assert all(vm.image_id.startswith("training-") and vm.workload_id is None for vm in definition.vms)
    assert "DRAFT" in inspected.runbook
    assert "NOT RUN" in inspected.runbook
    assert all(n.isolated and n.reachability == "isolated" and not n.guest_egress for n in definition.networks)
    assert all(n.host_bridge is None for n in definition.networks)
    recipe = recipe_from_definition(definition)
    validate_recipe(recipe)
    assert {v.label: v.networks for v in recipe.vms} == {v.id: list(v.network_ids) for v in definition.vms}
    for checkpoint in definition.experience.checkpoints:
        operation = validate_checkpoint(checkpoint)
        assert isinstance(operation, GuestAssertion)
        assert isinstance(operation.expected, str)
        assert operation.vm_id in {v.id for v in definition.vms}
        assert normalize_observation_path(operation.path).startswith("/var/lib/astinuslab/observations/")


def _network_members(name):
    definition = LabDefinition.model_validate_json((SOURCE / name / "lab.json").read_text())
    return {vm.id: set(vm.network_ids) for vm in definition.vms}


def test_pivot_is_the_only_target_joining_entry_and_internal_networks():
    assert _network_members("02-pivot") == {
        "pivot": {"entry", "internal"},
        "internal-web": {"internal"},
        "internal-db": {"internal"},
    }


def test_firewall_is_the_only_node_joining_entry_and_protected_networks():
    assert _network_members("03-firewall") == {
        "firewall": {"entry", "protected"},
        "protected-web": {"protected"},
    }


@pytest.mark.parametrize("name", EXPECTED)
def test_drafts_can_be_inspected_and_imported_via_api(name, tmp_path, monkeypatch, in_memory_session_scope):
    archive = _builder().build_package(SOURCE / name, tmp_path / "build")
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    monkeypatch.setattr("astinuslab.lab_packages.api.default_package_service", lambda: service)
    monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)
    app = FastAPI()

    def session_dependency():
        with in_memory_session_scope() as session:
            yield session

    app.dependency_overrides[get_session] = session_dependency
    app.include_router(router)
    with TestClient(app) as client:
        inspected = client.post(
            "/api/v1/lab-packages/inspect",
            files={"file": (archive.name, archive.read_bytes(), "application/octet-stream")},
        )
        assert inspected.status_code == 200, inspected.text
        body = inspected.json()
        assert body["signature_status"] == "unsigned"
        assert body["reproducibility"] == "unlocked"
        assert body["assets"] == []
        imported = client.post(f"/api/v1/lab-packages/imports/{body['inspection_id']}/commit")
        assert imported.status_code == 201, imported.text
        assert imported.json()["package"]["package_id"] == body["package_id"]


def test_build_is_deterministic_and_does_not_overwrite_existing_output(tmp_path):
    builder = _builder()
    first = builder.build_package(SOURCE / "02-pivot", tmp_path / "one")
    original = first.read_bytes()
    second = builder.build_package(SOURCE / "02-pivot", tmp_path / "two")
    assert original == second.read_bytes()
    with pytest.raises(FileExistsError):
        builder.build_package(SOURCE / "02-pivot", tmp_path / "one")
    assert first.read_bytes() == original


@pytest.mark.parametrize(
    "fault", ["locked", "unknown-network", "blank-runbook", "checkpoint-host-path", "checkpoint-unknown-vm"]
)
def test_invalid_draft_cannot_produce_an_archive(fault, tmp_path):
    builder = _builder()
    source = tmp_path / "source"
    source.mkdir()
    payload = json.loads((SOURCE / "01-single-vulnerability/lab.json").read_text())
    runbook = "# DRAFT\nLive tests: NOT RUN\n"
    if fault == "locked":
        payload["reproducibility"] = "locked"
    elif fault == "unknown-network":
        payload["vms"][0]["network_ids"] = ["missing"]
    elif fault == "blank-runbook":
        runbook = " \n"
    elif fault == "checkpoint-host-path":
        payload["experience"]["checkpoints"][0]["path"] = "/etc/shadow"
    else:
        payload["experience"]["checkpoints"][0]["vm_id"] = "missing"
    (source / "lab.json").write_text(json.dumps(payload))
    (source / "runbook.md").write_text(runbook)
    with pytest.raises((ValueError, GuestObservationError)):
        builder.build_package(source, tmp_path / "out")
    assert not list((tmp_path / "out").glob("*.astinuslab"))
