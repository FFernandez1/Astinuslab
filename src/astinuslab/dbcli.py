"""Entrypoint wrapper for database CLI friendly startup errors."""

from __future__ import annotations

import sys

from astinuslab.virtualization.entrypoint_support import die_config


def main() -> int:
    try:
        from astinuslab import db
    except Exception as exc:
        return die_config(exc)
    return db.main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
