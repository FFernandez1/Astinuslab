from __future__ import annotations

from fastapi import APIRouter

from .routes import networks as networks_routes
from .routes import recipes as recipes_routes
from .routes import vm_actions as vm_actions_routes
from .routes import vms as vms_routes
from .routes.captures import router as captures_router
from .routes.catalog import router as catalog_router
from .routes.metrics import router as metrics_router
from .routes.nics import router as nics_router
from .routes.sessions import router as sessions_router
from .routes.topology import router as topology_router

router = APIRouter(prefix="/api/v1/playground", tags=["playground"])
router.include_router(sessions_router)
router.include_router(catalog_router)
router.include_router(captures_router)
router.include_router(metrics_router)
router.include_router(networks_routes.router)
router.include_router(recipes_routes.router)
router.include_router(nics_router)
router.include_router(topology_router)
router.include_router(vms_routes.router)
router.include_router(vm_actions_routes.router)
