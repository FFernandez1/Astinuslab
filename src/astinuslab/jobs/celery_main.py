from __future__ import annotations

import sys

from ..virtualization.entrypoint_support import die_config

try:
    # Import worker-only signal handlers and task modules at worker startup.
    from ..playground import orchestrator as _playground_orchestrator  # noqa: F401
    from . import celery_worker_signals as _celery_worker_signals  # noqa: F401
    from .celery_app import celery_app
except Exception as exc:  # pragma: no cover - exercised through subprocess entrypoint tests
    sys.exit(die_config(exc))

app = celery_app
