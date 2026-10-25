"""Transaction primitives shared by forensic and Playground admission."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session


def reserve_sqlite_write(session: Session) -> None:
    """Reserve the single writer before checking an invariant and changing rows.

    Call before the guarded reads and retain the transaction through commit.
    Normal SQLite sessions otherwise start their DB transaction only at the
    first write, allowing two successful checks to race before either insert.
    """
    if session.get_bind().dialect.name != "sqlite":
        return
    connection = session.connection().connection.dbapi_connection
    if not getattr(connection, "in_transaction", False):
        session.execute(text("BEGIN IMMEDIATE"))
