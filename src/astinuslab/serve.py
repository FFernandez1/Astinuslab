"""Entrypoint wrapper: print friendly config errors, then start the API server."""

from __future__ import annotations

import os

from astinuslab.virtualization.entrypoint_support import die_config


def main() -> int:
    try:
        import astinuslab.main  # noqa: F401 - exercise full app import chain under guard
        from astinuslab.core.config import settings
    except Exception as exc:
        return die_config(exc)

    import uvicorn

    reload_enabled = os.environ.get("ASTINUSLAB_DEV", "0") == "1"
    uvicorn.run(
        "astinuslab.main:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.api_log_level,
        workers=1,
        reload=reload_enabled,
        reload_dirs=[str(settings.base_dir / "src" / "astinuslab")] if reload_enabled else None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
