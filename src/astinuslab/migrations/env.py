"""Alembic migration environment for AstinusLab.

Uses the application engine (astinuslab.db.engine) so CLI invocations honour
ASTINUSLAB_DB_PATH / astinuslab.toml. Programmatic invocations from
astinuslab.db pass a live Connection via config.attributes["connection"].
"""

from __future__ import annotations

from alembic import context
from sqlalchemy.engine import Connection

config = context.config


def _target_metadata():
    import astinuslab.db.models  # noqa: F401  (registers ORM tables)
    from astinuslab.db import Base

    return Base.metadata


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=_target_metadata(),
        render_as_batch=True,  # SQLite: use batch mode for ALTER in revisions
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection", None)
    if connectable is None:
        # CLI invocation: use the application engine so the DB path comes
        # from astinuslab.core.config (ASTINUSLAB_DB_PATH / astinuslab.toml).
        from astinuslab.db import engine

        connectable = engine
    if isinstance(connectable, Connection):
        # Programmatic invocation: caller owns the transaction.
        _run_migrations(connectable)
    else:
        with connectable.connect() as connection:
            _run_migrations(connection)
            connection.commit()


if context.is_offline_mode():
    raise RuntimeError("offline (--sql) mode is not supported; this app manages a local SQLite file")
run_migrations_online()
