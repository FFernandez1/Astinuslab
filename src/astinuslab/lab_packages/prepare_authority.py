"""Local Prepare process custody and short, generation-fenced transactions."""

from __future__ import annotations

import errno
import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import update
from sqlalchemy.orm import Session

from ..db.models import LabPrepareAttempt
from ..db.transactions import reserve_sqlite_write
from .storage_custody import secure_file


class PreparePackageBusy(RuntimeError):
    """Another process owns the package execution/deletion lock."""


class PrepareClaimLost(RuntimeError):
    """This invocation no longer has authority to mutate Prepare state."""


class PrepareTransactionError(RuntimeError):
    """A caller brought unrelated writes into a worker-owned transaction."""


@dataclass(slots=True)
class PreparePackageLock:
    package_record_id: str
    descriptor: int
    active: bool = True

    def require(self, record_id: str) -> None:
        if not self.active or self.descriptor < 0 or self.package_record_id != record_id:
            raise PrepareClaimLost("Prepare requires active custody for the matching package")


@dataclass(frozen=True, slots=True)
class PrepareClaim:
    attempt_id: str
    package_record_id: str
    execution_token: str


@contextmanager
def try_package_lock(path: Path, record_id: str) -> Iterator[PreparePackageLock]:
    """Take a stable shared inode without waiting, including within one process.

    Only flock contention is busy. Permission and unsafe-path failures propagate;
    never unlink this file on release, which would split the authority identity.
    """
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    lock = PreparePackageLock(record_id, descriptor, active=False)
    acquired = False
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("Prepare lock must be a regular single-link file")
        secure_file(descriptor, path)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise PreparePackageBusy("package preparation is active") from exc
            raise
        acquired = lock.active = True
        yield lock
    finally:
        lock.active = False
        lock.descriptor = -1
        try:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def end_read_transaction(session: Session) -> None:
    """End only an unmodified read, never take over flushed caller writes.

    SQLite's DBAPI transaction flag distinguishes a logical SQLAlchemy autobegin
    caused by SELECT from BEGIN/DML. An empty ORM dirty set is insufficient.
    """
    if session.new or session.dirty or session.deleted:
        raise PrepareTransactionError("Prepare requires a clean session without pending writes")
    if session.in_transaction():
        if session.get_bind().dialect.name != "sqlite":
            raise PrepareTransactionError("Prepare requires a fresh worker session")
        connection = session.connection().connection.dbapi_connection
        if getattr(connection, "in_transaction", False):
            raise PrepareTransactionError("Prepare cannot take over an existing database transaction")
        session.rollback()


def require_owned_write(session: Session, claim: PrepareClaim, package_lock: PreparePackageLock) -> None:
    package_lock.require(claim.package_record_id)
    if session.info.get("prepare_owned_write") != claim:
        raise PrepareClaimLost("Prepare mutation requires a current guarded transaction")


@contextmanager
def owned_prepare_write(
    session: Session, claim: PrepareClaim, *, package_lock: PreparePackageLock
) -> Iterator[LabPrepareAttempt]:
    """Fence all related writes under SQLite's writer reservation, then commit."""
    package_lock.require(claim.package_record_id)
    end_read_transaction(session)
    try:
        reserve_sqlite_write(session)
        changed = session.execute(
            update(LabPrepareAttempt)
            .where(
                LabPrepareAttempt.id == claim.attempt_id,
                LabPrepareAttempt.package_record_id == claim.package_record_id,
                LabPrepareAttempt.execution_token == claim.execution_token,
                LabPrepareAttempt.state == "running",
            )
            .values(updated_at=datetime.now(UTC).replace(tzinfo=None))
            .execution_options(synchronize_session=False)
        )
        if getattr(changed, "rowcount", 0) != 1:
            raise PrepareClaimLost("Prepare execution was replaced or terminalized")
        # Cached rows can carry an old cancel flag or old asset/outbox state.
        session.expire_all()
        attempt = session.get(LabPrepareAttempt, claim.attempt_id)
        if attempt is None:
            raise PrepareClaimLost("Prepare execution disappeared")
        session.info["prepare_owned_write"] = claim
        yield attempt
        package_lock.require(claim.package_record_id)
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.info.pop("prepare_owned_write", None)
