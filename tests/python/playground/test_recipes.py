from __future__ import annotations

import pytest

from astinuslab.api.schemas import PlaygroundRecipe, PlaygroundRecipeNetwork, PlaygroundRecipeVM
from astinuslab.playground.recipes import PlaygroundRecipeError, validate_recipe


def test_recipe_validation_rejects_unknown_network_ref():
    recipe = PlaygroundRecipe(
        networks=[PlaygroundRecipeNetwork(name="lab", subnet_cidr="10.42.1.0/24")],
        vms=[PlaygroundRecipeVM(label="web", image_id="img", networks=["missing"])],
    )

    with pytest.raises(PlaygroundRecipeError, match="unknown network"):
        validate_recipe(recipe)


def test_recipe_validation_rejects_duplicate_vm_labels():
    recipe = PlaygroundRecipe(
        networks=[PlaygroundRecipeNetwork(name="lab", subnet_cidr="10.42.1.0/24")],
        vms=[
            PlaygroundRecipeVM(label="web", image_id="img", networks=["lab"]),
            PlaygroundRecipeVM(label="web", image_id="img", networks=["lab"]),
        ],
    )

    with pytest.raises(PlaygroundRecipeError, match="duplicate VM"):
        validate_recipe(recipe)


def test_import_rejects_bad_image_before_creating_session(client, monkeypatch):
    from fastapi import HTTPException

    from astinuslab.playground.routes import vms as vms_routes

    def fake_validate(payload):
        if payload.image_id == "missing-img":
            raise HTTPException(status_code=400, detail="image not available: missing-img")
        return {"id": payload.image_id, "username": "ubuntu"}

    monkeypatch.setattr(vms_routes, "_validate_spawn_image", fake_validate)
    monkeypatch.setattr(vms_routes, "_validate_spawn_workload", lambda payload, image=None: None)

    recipe = {
        "schema_version": 1,
        "name": "half",
        "networks": [{"name": "n1", "subnet_cidr": "10.42.9.0/24", "isolated": True}],
        "vms": [
            {"label": "good", "role": "target", "image_id": "ubuntu-22.04", "networks": ["n1"]},
            {"label": "bad", "role": "target", "image_id": "missing-img", "networks": ["n1"]},
        ],
        "topology": [],
    }

    r = client.post("/api/v1/playground/recipes/import", json=recipe)

    assert r.status_code == 400
    assert "missing-img" in r.json()["detail"]
    assert client.get("/api/v1/playground/sessions/current-or-null").json() is None


def test_import_rolls_back_a_new_session_on_late_failure(client, monkeypatch):
    """A recipe that fails after its first reservation leaves nothing behind.

    Admission now happens in one transaction, so the new parent session dies
    with the rollback instead of being handed to asynchronous cleanup.
    """
    from astinuslab.playground import recipe_import
    from astinuslab.playground.routes import vms as vms_routes

    monkeypatch.setattr(vms_routes, "_validate_spawn_image", lambda payload: {"id": payload.image_id})
    monkeypatch.setattr(vms_routes, "_validate_spawn_workload", lambda payload, image=None: None)

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(recipe_import.vms_routes, "_register_pending_vm", boom)

    recipe = {
        "schema_version": 1,
        "name": "x",
        "networks": [{"name": "n1", "subnet_cidr": "10.42.9.0/24", "isolated": True}],
        "vms": [{"label": "a", "role": "target", "image_id": "ubuntu-22.04", "networks": ["n1"]}],
        "topology": [],
    }

    with pytest.raises(RuntimeError, match="boom"):
        client.post("/api/v1/playground/recipes/import", json=recipe)

    assert client.get("/api/v1/playground/sessions/current-or-null").json() is None
