from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ... import catalog
from ...api.schemas import PlaygroundWorkloadCatalogOut
from .. import workloads as pw
from ..serializers import playground_image_for

router = APIRouter()


@router.get("/catalog/images")
def list_playground_images_endpoint(role: str = "target") -> list[dict]:
    try:
        return catalog.list_playground_images_for_role(role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/catalog/workloads", response_model=list[PlaygroundWorkloadCatalogOut])
def list_playground_workloads_endpoint(image_id: str) -> list[PlaygroundWorkloadCatalogOut]:
    image = playground_image_for("target", image_id)
    if image is None:
        raise HTTPException(status_code=404, detail="image not found")
    return [PlaygroundWorkloadCatalogOut(**item) for item in pw.annotated_workloads_for_playground_image(image)]
