from __future__ import annotations

from fastapi import APIRouter

from ..core.config import settings as _config_settings
from ..db import session_scope as _db_session_scope
from .artifact_routes import router as artifact_router
from .image_routes import router as image_router
from .job_context import _get_lang as _get_lang
from .jobs_routes import router as jobs_router
from .logs_routes import router as logs_router

settings = _config_settings
session_scope = _db_session_scope

ui_router = APIRouter()
ui_router.include_router(jobs_router)
ui_router.include_router(logs_router)
ui_router.include_router(image_router)
ui_router.include_router(artifact_router)
