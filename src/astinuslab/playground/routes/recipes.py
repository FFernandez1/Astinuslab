from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from ... import db as db_mod
from ...api.schemas import PlaygroundRecipe, PlaygroundSessionOut
from .. import recipe_import, recipes
from .. import repositories as pr
from ..serializers import serialize_session
from . import vms as vms_routes
from .sessions import _integrity_as_409

router = APIRouter()


@router.get("/sessions/current/recipe", response_model=PlaygroundRecipe)
def export_current_recipe_endpoint() -> PlaygroundRecipe:
    with db_mod.session_scope() as s:
        job = pr.active_playground_session(s)
        if job is None:
            raise HTTPException(status_code=404, detail="no active playground session")
        return recipes.export_recipe(s, job.id, name=job.id)


def _validate_recipe_vm_images(payload: PlaygroundRecipe) -> dict[str, dict]:
    """Resolve and validate each VM's image and workload before admission opens.

    This reads the catalog only, so it belongs outside the writer transaction;
    the reservation reuses the resolved images to size resources.
    """
    images_by_label: dict[str, dict] = {}
    for vm in payload.vms:
        spawn_payload = vms_routes.PlaygroundCreateIfNeededSpawnVMRequest(
            label=vm.label,
            image_id=vm.image_id,
            role=vm.role,
            network_ids=vm.networks,
            vcpus=vm.vcpus,
            memory_mb=vm.memory_mb,
            disk_gb=vm.disk_gb,
            workload_id=vm.workload_id,
        )
        image = vms_routes._validate_spawn_image(spawn_payload)
        vms_routes._validate_spawn_workload(spawn_payload, image=image)
        images_by_label[vm.label] = image
    return images_by_label


@router.post("/recipes/import", status_code=status.HTTP_202_ACCEPTED, response_model=PlaygroundSessionOut)
def import_recipe_endpoint(payload: PlaygroundRecipe) -> PlaygroundSessionOut:
    try:
        recipes.validate_recipe(payload)
        images_by_label = _validate_recipe_vm_images(payload)
    except recipes.PlaygroundRecipeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # One transaction reserves the parent session, every network, VM, NIC and
    # topology node. A conflict anywhere rolls the whole recipe back, so a
    # rejected import leaves no half-built lab and dispatches nothing.
    with _integrity_as_409("session, network, or VM already exists"):
        with db_mod.session_scope() as s:
            plan = recipe_import.reserve_recipe(s, payload, images_by_label=images_by_label)

    try:
        recipe_import.dispatch_plan(plan)
    except Exception as exc:
        recipe_import.compensate_plan(plan, str(exc))
        raise

    with db_mod.session_scope() as s:
        job = pr.active_playground_session(s)
        if job is None:
            raise HTTPException(status_code=500, detail="session lost after recipe import")
        return serialize_session(s, job)
