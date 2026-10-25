"""Structured API error envelope: {"detail", "code", "context"}."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(self, status_code: int, detail: str, code: str, context: dict | None = None):
        self.status_code = status_code
        self.detail = detail
        self.code = code
        self.context = context
        super().__init__(detail)


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "code": exc.code, "context": exc.context},
    )
