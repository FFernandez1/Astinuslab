from __future__ import annotations

from fastapi import APIRouter

from .artifact_routes import router as artifact_router
from .auth_routes import router as auth_router
from .help_routes import router as help_router
from .image_routes import router as image_router
from .jobs_routes import router as jobs_router
from .logs_routes import router as logs_router
from .package_routes import router as package_router
from .volatility_routes import router as volatility_router

__all__ = [
    "artifact_router",
    "auth_router",
    "image_router",
    "jobs_router",
    "logs_router",
    "package_router",
    "ui_router",
    "volatility_router",
]

ui_router = APIRouter()
ui_router.include_router(auth_router)
ui_router.include_router(help_router)
ui_router.include_router(jobs_router)
ui_router.include_router(logs_router)
ui_router.include_router(package_router)
ui_router.include_router(image_router)
ui_router.include_router(artifact_router)
ui_router.include_router(volatility_router)
