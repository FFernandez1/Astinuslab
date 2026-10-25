from __future__ import annotations

import logging

from fastapi import HTTPException

logger = logging.getLogger(__name__)

ENQUEUE_FAILED_DETAIL = "operation could not be queued"


def task_id(result) -> str | None:
    return getattr(result, "id", None)


def enqueue_or_raise(task, *, on_failure=None, **kwargs) -> str | None:
    """Queue a playground task, compensating and returning a stable 503 on broker errors."""
    try:
        result = task.delay(**kwargs)
    except Exception as exc:
        logger.exception("playground enqueue failed for %s", getattr(task, "name", task))
        if on_failure is not None:
            try:
                on_failure(exc)
            except Exception:
                logger.exception("playground enqueue compensation failed")
        raise HTTPException(status_code=503, detail=ENQUEUE_FAILED_DETAIL) from exc
    return task_id(result)
