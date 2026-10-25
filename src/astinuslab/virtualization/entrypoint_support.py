from __future__ import annotations

import sys


def format_config_error(exc: Exception) -> str:
    """Return compact operator-facing text for entrypoint startup failures."""
    try:
        from pydantic import ValidationError

        if isinstance(exc, ValidationError):
            return "; ".join(
                (".".join(str(part) for part in err["loc"]) or "settings") + ": " + err["msg"] for err in exc.errors()
            )
    except Exception:
        pass
    return str(exc)


def die_config(exc: Exception) -> int:
    print(f"astinuslab: configuration error: {format_config_error(exc)}", file=sys.stderr)
    print(
        "astinuslab: check ASTINUSLAB_* environment variables and astinuslab.toml (docs/usage/configuration.md)",
        file=sys.stderr,
    )
    return 2
