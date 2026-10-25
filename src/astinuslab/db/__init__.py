from __future__ import annotations

import argparse
import hashlib
import logging
import os
import secrets
import stat
import sys
from collections.abc import Generator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ..core.config import settings
from .baseline_snapshot import BASELINE_REVISION, BASELINE_SCHEMA

logger = logging.getLogger(__name__)

# Models import astinuslab.db.Base while python -m astinuslab.db runs this
# file as __main__. Keep both names bound to one module so model metadata is
# registered on the Base used by the CLI initializer.
sys.modules.setdefault("astinuslab.db", sys.modules[__name__])

settings.db_path.parent.mkdir(parents=True, exist_ok=True)


class Base(DeclarativeBase):
    pass


engine = create_engine(f"sqlite:///{Path(settings.db_path)}", future=True)


@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    # SQLite silently leaves FK clauses inert unless this is enabled on every
    # connection. Package ownership and terminal-instance cleanup rely on the
    # database enforcing the same constraints as the ORM.
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


_DROPPED_TABLES: list[str] = []

_DROPPED_COLUMNS: dict[str, list[str]] = {
    # Dump metadata moved to the artifacts table (single source of truth).
    # _migrate_data() backfills artifact rows before these are dropped.
    "jobs": [
        "dump_path",
        "dump_filename",
        "dump_checksum",
        "dump_size_bytes",
        "disk_dump_path",
        "disk_dump_filename",
        "disk_dump_checksum",
        "disk_dump_size_bytes",
    ],
}


def _register_models() -> None:
    import astinuslab.db.models  # noqa: F401


def _migrate_data() -> None:
    """One-time data migrations for the single-host SQLite deployment.

    Frozen pre-Alembic bootstrap: runs only for databases that predate the
    Alembic baseline (no ``alembic_version`` table). Runs after ``create_all``
    (new tables exist) and before ``_migrate_schema`` (legacy columns are
    still readable). Each step is idempotent. New schema changes belong in
    Alembic revisions, not here.
    """
    from uuid import uuid4

    inspector = inspect(engine)
    if not inspector.has_table("jobs"):
        return
    job_columns = {col["name"] for col in inspector.get_columns("jobs")}

    _session_columns = {"execution_mode", "status", "error_summary", "created_at", "started_at", "finished_at"}

    with engine.begin() as conn:
        # Playground sessions used to be jobs rows with sentinel
        # execution_mode='playground'. Move every such row — including
        # historical terminal sessions — into the playground_sessions table.
        if inspector.has_table("playground_sessions") and _session_columns <= job_columns:
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO playground_sessions "
                    "(id, status, error_summary, created_at, started_at, finished_at) "
                    "SELECT id, status, error_summary, created_at, started_at, finished_at "
                    "FROM jobs WHERE execution_mode = 'playground'"
                )
            )
            conn.execute(text("DELETE FROM jobs WHERE execution_mode = 'playground'"))
            # The one-active-session invariant now lives on playground_sessions.
            conn.execute(text("DROP INDEX IF EXISTS ux_jobs_one_active_playground_session"))

        # Backfill artifact rows from the legacy denormalized jobs dump columns
        # so dump metadata survives the column drop in _DROPPED_COLUMNS.
        if inspector.has_table("artifacts") and "dump_path" in job_columns:
            for kind, prefix in (("memory", ""), ("disk", "disk_")):
                rows = conn.execute(
                    text(
                        f"SELECT id, {prefix}dump_path, {prefix}dump_filename, "  # noqa: S608  # prefix is a literal from the loop above, not user input
                        f"{prefix}dump_checksum, {prefix}dump_size_bytes, "
                        "COALESCE(finished_at, created_at) "
                        f"FROM jobs WHERE {prefix}dump_path IS NOT NULL AND NOT EXISTS ("
                        "SELECT 1 FROM artifacts a WHERE a.job_id = jobs.id AND a.kind = :kind)"
                    ),
                    {"kind": kind},
                ).fetchall()
                for job_id, path, filename, sha256, size_bytes, created_at in rows:
                    conn.execute(
                        text(
                            "INSERT INTO artifacts "
                            "(id, job_id, producer, kind, path, filename, sha256, size_bytes, created_at) "
                            "VALUES (:id, :job_id, 'job_capture', :kind, :path, :filename, "
                            ":sha256, :size_bytes, :created_at)"
                        ),
                        {
                            "id": f"art-{uuid4().hex[:8]}",
                            "job_id": job_id,
                            "kind": kind,
                            "path": path,
                            "filename": filename,
                            "sha256": sha256,
                            "size_bytes": size_bytes,
                            "created_at": created_at,
                        },
                    )


def _column_default_sql(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        # Escape single quotes for SQL string literals.
        escaped = value.replace("'", "''")
        return f" DEFAULT '{escaped}'"
    return f" DEFAULT {value}"


def _column_definition(column: dict[str, object]) -> str:
    nullable = " NOT NULL" if not column["nullable"] else ""
    return f'"{column["name"]}" {column["type"]}{nullable}{_column_default_sql(column["default"])}'


def _create_missing_baseline_indexes(table_name: str, table_spec: dict[str, object]) -> None:
    inspector = inspect(engine)
    if not inspector.has_table(table_name):
        return
    with engine.begin() as conn:
        existing_indexes = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = :t"),
                {"t": table_name},
            )
        }
        for sql in table_spec.get("index_sql", []):
            index_name = str(sql).split("INDEX ", 1)[1].split(" ON ", 1)[0].replace('"', "")
            if index_name not in existing_indexes:
                conn.execute(text(str(sql)))


def _create_baseline_missing_tables() -> None:
    """Create baseline tables absent from a pre-Alembic database."""
    inspector = inspect(engine)
    created_tables: list[str] = []
    with engine.begin() as conn:
        for table_name, table_spec in BASELINE_SCHEMA.items():
            if inspector.has_table(table_name):
                continue
            column_defs = [_column_definition(col) for col in table_spec["columns"]]
            primary_keys = [str(col["name"]) for col in table_spec["columns"] if col.get("primary_key")]
            if primary_keys:
                quoted_keys = ", ".join(f'"{name}"' for name in primary_keys)
                column_defs.append(f"PRIMARY KEY ({quoted_keys})")
            conn.execute(text(f'CREATE TABLE "{table_name}" ({", ".join(column_defs)})'))
            created_tables.append(table_name)
    for table_name in created_tables:
        _create_missing_baseline_indexes(table_name, BASELINE_SCHEMA[table_name])


def _migrate_schema() -> None:
    """Sync a pre-Alembic SQLite schema to the frozen baseline snapshot.

    Frozen pre-Alembic bootstrap: runs only for databases that predate the
    Alembic baseline (see ``initialize_database``). Handles three cases:
    - Drop legacy tables listed in _DROPPED_TABLES
    - Drop legacy columns listed in _DROPPED_COLUMNS
    - Add baseline columns present in BASELINE_SCHEMA but absent from the DB
    - Create missing baseline indexes from frozen SQL in BASELINE_SCHEMA

    New schema changes belong in Alembic revisions, not this bootstrap.
    """
    inspector = inspect(engine)
    with engine.begin() as conn:
        # Drop legacy tables that no longer exist in the models
        for table_name in _DROPPED_TABLES:
            if inspector.has_table(table_name):
                conn.execute(text(f'DROP TABLE "{table_name}"'))

        # Drop columns removed from models (SQLite requires recreate; skip if absent)
        for table_name, columns in _DROPPED_COLUMNS.items():
            if not inspector.has_table(table_name):
                continue
            existing = {col["name"] for col in inspector.get_columns(table_name)}
            for col_name in columns:
                if col_name in existing:
                    conn.execute(text(f'ALTER TABLE "{table_name}" DROP COLUMN "{col_name}"'))

        # Add columns present in the frozen baseline but missing from the live DB.
        for table_name, table_spec in BASELINE_SCHEMA.items():
            if not inspector.has_table(table_name):
                continue
            existing = {col["name"] for col in inspector.get_columns(table_name)}
            for column in table_spec["columns"]:
                if column["name"] not in existing:
                    conn.execute(text(f'ALTER TABLE "{table_name}" ADD COLUMN {_column_definition(column)}'))
    for table_name, table_spec in BASELINE_SCHEMA.items():
        _create_missing_baseline_indexes(table_name, table_spec)


def schema_issues() -> list[str]:
    """Return issues using live metadata as status reporting, not bootstrap DDL."""
    _register_models()
    inspector = inspect(engine)
    issues: list[str] = []

    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            issues.append(f"missing table: {table.name}")
            continue
        existing = {col["name"] for col in inspector.get_columns(table.name)}
        for col in table.columns:
            if col.name not in existing:
                issues.append(f"missing column: {table.name}.{col.name}")

    for table_name in _DROPPED_TABLES:
        if inspector.has_table(table_name):
            issues.append(f"legacy table present: {table_name}")

    for table_name, columns in _DROPPED_COLUMNS.items():
        if not inspector.has_table(table_name):
            continue
        existing = {col["name"] for col in inspector.get_columns(table_name)}
        for col_name in columns:
            if col_name in existing:
                issues.append(f"legacy column present: {table_name}.{col_name}")

    return issues


def _run_alembic(command_name: str, target: str = "head") -> None:
    """Run an Alembic command ("stamp" or "upgrade") programmatically.

    Passes a live connection from the (possibly monkeypatched) module engine
    via ``config.attributes`` so alembic/env.py skips its CLI engine fallback.
    """
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(files("astinuslab.migrations").joinpath("alembic.ini")))
    with engine.connect() as connection:
        cfg.attributes["connection"] = connection
        getattr(command, command_name)(cfg, target)
        connection.commit()


def initialize_database() -> list[str]:
    """Create or upgrade the local SQLite database schema (idempotent).

    Routing:
    - Fresh database (no tables): ``create_all`` then stamp the Alembic head.
    - Pre-Alembic database (tables but no ``alembic_version``): create missing
      baseline tables, migrate legacy data/schema using the frozen snapshot,
      stamp the baseline revision, then run ``alembic upgrade head``.
    - Stamped database: ``alembic upgrade head``.
    """
    _register_models()
    before = schema_issues()
    inspector = inspect(engine)
    if inspector.has_table("alembic_version"):
        _run_alembic("upgrade")
        return before

    model_tables_present = any(inspector.has_table(table.name) for table in Base.metadata.sorted_tables)
    if model_tables_present:
        # Legacy bootstrap EOL: remove after release 0.3; pre-Alembic DBs
        # must then upgrade via an intermediate release that still includes it.
        _create_baseline_missing_tables()
        _migrate_data()
        _migrate_schema()
        _run_alembic("stamp", BASELINE_REVISION)
        _run_alembic("upgrade")
        return before

    Base.metadata.create_all(engine)
    _run_alembic("stamp")
    return before


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# Explicitly named revisions only. Alembic accepts relative targets such as
# "-1", which resolve against whatever head happens to be at the time: adding a
# revision or a merge point later silently changes which transition runs. A
# rollback has to name the revision it means.
_RELATIVE_TARGETS = frozenset({"head", "heads", "base", "current"})


def current_revision() -> str | None:
    """Revision the live database is stamped at, or None if it is unstamped."""
    from alembic.runtime.migration import MigrationContext

    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def backup_database(destination: Path) -> dict[str, str]:
    """Take a checkpointed, verified copy of the application database.

    Copies through SQLite's own backup API rather than the filesystem: the
    database runs in WAL mode, so a plain file copy can miss committed rows and
    can tear under a concurrent writer. Only the database is copied, which is
    what keeps SSH private keys and everything else under the storage root out
    of the backup.
    """
    import sqlite3
    from tempfile import TemporaryDirectory

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_path = Path(settings.db_path).resolve(strict=True)
    source_info = source_path.stat()
    sidecar_suffixes = ("-wal", "-shm", "-journal")
    source_sidecars = tuple(source_path.with_name(source_path.name + suffix) for suffix in sidecar_suffixes)
    parent_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    try:

        def refuse_unsafe_destination() -> None:
            pinned_parent = Path(f"/proc/self/fd/{parent_fd}").resolve(strict=True)
            if pinned_parent / destination.name in source_sidecars:
                raise RuntimeError("backup destination must not be a source database sidecar")
            # A valid old WAL or journal can replay over the replacement database.
            # Refuse instead of deleting state that may belong to another owner.
            for suffix in sidecar_suffixes:
                try:
                    os.stat(destination.name + suffix, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                raise RuntimeError("backup destination has a SQLite sidecar; reconcile it before backing up")
            try:
                info = os.stat(destination.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RuntimeError("refusing unsafe backup destination: expected a single-link regular file")
            if (info.st_dev, info.st_ino) == (source_info.st_dev, source_info.st_ino):
                raise RuntimeError("backup destination must not be the source database")
            for sidecar in source_sidecars:
                try:
                    sidecar_info = sidecar.stat()
                except FileNotFoundError:
                    continue
                if (info.st_dev, info.st_ino) == (sidecar_info.st_dev, sidecar_info.st_ino):
                    raise RuntimeError("backup destination must not alias a source database sidecar")

        refuse_unsafe_destination()
        revision = current_revision() or ""
        # Pin the parent through publication and keep SQLite's temporary
        # sidecars inside a private directory. Never truncate a previous copy.
        with TemporaryDirectory(prefix=".astinuslab-backup-", dir=f"/proc/self/fd/{parent_fd}") as temporary:
            staged = Path(temporary) / "backup.db"
            descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
            source = sqlite3.connect(str(settings.db_path))
            try:
                source.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                target = sqlite3.connect(str(staged))
                try:
                    source.backup(target)
                    # A portable backup must contain committed WAL rows in
                    # its one database file, with no required sidecars.
                    target.execute("PRAGMA journal_mode=DELETE")
                finally:
                    target.close()
            finally:
                source.close()

            verify = sqlite3.connect(str(staged))
            try:
                status = verify.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                verify.close()
            if status != "ok":
                raise RuntimeError(f"database backup failed verification: {status}")

            with staged.open("rb") as handle:
                os.fsync(handle.fileno())
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            refuse_unsafe_destination()
            os.replace(staged, destination.name, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
    finally:
        os.close(parent_fd)

    return {
        "path": str(destination),
        "sha256": digest,
        "mode": "0600",
        "revision": revision,
    }


def restore_database(source: Path) -> dict[str, str]:
    """Put a verified backup back in place, leaving the live file alone if it
    does not verify."""
    import sqlite3

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    read_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    write_flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)

    def open_directory(path: Path) -> int:
        absolute = Path(os.path.abspath(path))
        descriptor = os.open(os.sep, directory_flags)
        try:
            for component in absolute.parts[1:]:
                child = os.open(component, directory_flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def verify_regular(descriptor: int, label: str, *, device: int | None = None) -> os.stat_result:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (device is not None and info.st_dev != device):
            raise RuntimeError(f"refusing unsafe {label}")
        return info

    def name_matches(parent_fd: int, name: str, expected: os.stat_result) -> bool:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return (
            stat.S_ISREG(current.st_mode)
            and current.st_nlink == 1
            and (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino)
        )

    def copy_descriptor(source_fd: int, destination_fd: int) -> None:
        os.lseek(source_fd, 0, os.SEEK_SET)
        os.lseek(destination_fd, 0, os.SEEK_SET)
        while True:
            block = os.read(source_fd, 1024 * 1024)
            if not block:
                break
            view = memoryview(block)
            while view:
                view = view[os.write(destination_fd, view) :]

    def digest_descriptor(descriptor: int) -> str:
        digest = hashlib.sha256()
        os.lseek(descriptor, 0, os.SEEK_SET)
        while block := os.read(descriptor, 1024 * 1024):
            digest.update(block)
        return digest.hexdigest()

    source = Path(os.path.abspath(source))
    try:
        source_parent_fd = open_directory(source.parent)
        try:
            source_fd = os.open(source.name, read_flags, dir_fd=source_parent_fd)
        finally:
            os.close(source_parent_fd)
    except FileNotFoundError as exc:
        raise RuntimeError(f"database backup is missing: {source}") from exc
    except OSError as exc:
        raise RuntimeError(f"database backup has unsafe custody: {source}") from exc
    try:
        verify_regular(source_fd, "database backup")
        verify = sqlite3.connect(f"file:/proc/self/fd/{source_fd}?mode=ro&immutable=1", uri=True)
        try:
            status = verify.execute("PRAGMA integrity_check").fetchone()[0]
            if status != "ok":
                raise RuntimeError(f"refusing to restore an unverified backup: {status}")
            try:
                revision = verify.execute("select version_num from alembic_version").fetchone()
            except sqlite3.OperationalError:
                # A backup taken before the first migration has no alembic_version
                # table. That is an unstamped database, not a corrupt one.
                revision = None
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(f"refusing to restore an unreadable backup: {exc}") from exc
        finally:
            verify.close()
    except BaseException:
        os.close(source_fd)
        raise

    engine.dispose()
    target = Path(os.path.abspath(settings.db_path))
    try:
        target_parent_fd = open_directory(target.parent)
    except BaseException:
        os.close(source_fd)
        raise
    parent_device = os.fstat(target_parent_fd).st_dev
    target_fd: int | None = None
    staging_fd: int | None = None
    staging_identity: os.stat_result | None = None
    staging_name = ""
    published = False
    try:
        try:
            target_fd = os.open(target.name, write_flags, dir_fd=target_parent_fd)
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise RuntimeError(f"refusing unsafe live database: {target}") from exc
        else:
            existing = verify_regular(target_fd, "live database", device=parent_device)

        # A failed candidate can write in the storage directory. It must not be
        # able to predict or redirect root's rollback staging file.
        for _attempt in range(32):
            staging_name = f".{target.name}.restore.{secrets.token_hex(12)}"
            try:
                staging_fd = os.open(
                    staging_name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                    dir_fd=target_parent_fd,
                )
            except FileExistsError:
                continue
            break
        else:
            raise RuntimeError("could not allocate exclusive database restore staging")

        if staging_fd is None:  # pragma: no cover - the bounded loop either opens or raises
            raise RuntimeError("database restore staging was not opened")
        staging_identity = verify_regular(staging_fd, "database restore staging", device=parent_device)
        copy_descriptor(source_fd, staging_fd)
        os.ftruncate(staging_fd, os.lseek(staging_fd, 0, os.SEEK_CUR))
        if existing is not None:
            os.fchown(staging_fd, existing.st_uid, existing.st_gid)
        os.fchmod(staging_fd, stat.S_IMODE(existing.st_mode) if existing is not None else 0o600)
        os.fsync(staging_fd)
        if digest_descriptor(source_fd) != digest_descriptor(staging_fd):
            raise RuntimeError("database restore staging failed verification")
        if not name_matches(target_parent_fd, staging_name, staging_identity):
            raise RuntimeError("database restore staging changed identity")

        # SQLite sidecars are removed only by name relative to the bound
        # directory, and only after proving they are regular single-link files.
        for suffix in ("-wal", "-shm"):
            sidecar_name = target.name + suffix
            try:
                sidecar_fd = os.open(sidecar_name, read_flags, dir_fd=target_parent_fd)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise RuntimeError(f"refusing unsafe database sidecar: {sidecar_name}") from exc
            try:
                sidecar_info = verify_regular(sidecar_fd, "database sidecar", device=parent_device)
                if not name_matches(target_parent_fd, sidecar_name, sidecar_info):
                    raise RuntimeError(f"database sidecar changed identity: {sidecar_name}")
                os.unlink(sidecar_name, dir_fd=target_parent_fd)
            finally:
                os.close(sidecar_fd)

        if existing is None:
            try:
                os.stat(target.name, dir_fd=target_parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise RuntimeError("live database appeared during restore")
            os.link(
                staging_name,
                target.name,
                src_dir_fd=target_parent_fd,
                dst_dir_fd=target_parent_fd,
                follow_symlinks=False,
            )
            os.unlink(staging_name, dir_fd=target_parent_fd)
            published = True
        else:
            # Preserve the live inode: the migration's ACL snapshot binds this
            # exact object, and an in-place restore also retains extended ACLs.
            if not name_matches(target_parent_fd, target.name, existing):
                raise RuntimeError("live database changed identity during restore")
            if target_fd is None:  # pragma: no cover - paired with existing metadata above
                raise RuntimeError("live database descriptor was lost")
            os.ftruncate(target_fd, 0)
            copy_descriptor(staging_fd, target_fd)
            os.ftruncate(target_fd, os.lseek(target_fd, 0, os.SEEK_CUR))
            os.fsync(target_fd)
            if not name_matches(target_parent_fd, target.name, existing):
                raise RuntimeError("live database changed identity during restore")
        os.fsync(target_parent_fd)
    finally:
        os.close(source_fd)
        if target_fd is not None:
            os.close(target_fd)
        if staging_fd is not None:
            os.close(staging_fd)
        if (
            staging_name
            and not published
            and staging_identity is not None
            and name_matches(target_parent_fd, staging_name, staging_identity)
        ):
            try:
                os.unlink(staging_name, dir_fd=target_parent_fd)
            except FileNotFoundError:
                pass
        os.close(target_parent_fd)
    return {"path": str(target), "revision": revision[0] if revision else ""}


def downgrade_to(revision: str) -> None:
    """Downgrade to an explicitly named revision."""
    candidate = revision.strip()
    if not candidate or candidate in _RELATIVE_TARGETS or candidate[0] in "+-":
        raise RuntimeError(
            f"refusing a relative downgrade target {revision!r}: name the explicit revision to roll back to"
        )
    _run_alembic("downgrade", candidate)


def clear_job_records() -> dict[str, int]:
    """Delete every forensic job row and the runtime state hanging off it.

    Used by ``astinuslab.sh cleanup`` and tools/dev/clear_active_jobs.sh. Both
    used to shell out to the sqlite3 CLI, which is not a project dependency:
    where it was absent the wipe silently did nothing, and job_runtimes rows
    accumulated until has_active_non_playground_job() reported a permanently
    active forensic job. Deleting through the app engine keeps the ON DELETE
    CASCADE clauses live (PRAGMA foreign_keys is set per connection) and runs
    in one transaction, so children can never outlive their job row.
    """
    counts: dict[str, int] = {}
    with engine.begin() as conn:
        for table in ("job_events", "job_runtimes", "host_actions", "jobs"):
            counts[table] = conn.execute(text(f"DELETE FROM {table}")).rowcount  # noqa: S608 - fixed table list
    return counts


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AstinusLab SQLite database utilities")
    parser.add_argument(
        "command", choices=("init", "status", "backup", "restore", "revision", "downgrade", "clear-jobs")
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="backup/restore destination path, or the explicit revision to downgrade to",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)

    if args.command in {"backup", "restore", "downgrade"} and not args.target:
        logger.error(f"{args.command} requires a target")
        return 2

    if args.command == "backup":
        try:
            record = backup_database(Path(args.target))
        except (OSError, RuntimeError) as exc:
            logger.error(f"database backup failed: {exc}")
            return 1
        # One machine-readable line: the installer records the hash and mode.
        print(
            "backup: path={path} sha256={sha256} mode={mode} revision={revision}".format(**record),
            flush=True,
        )
        return 0

    if args.command == "restore":
        try:
            record = restore_database(Path(args.target))
        except (OSError, RuntimeError) as exc:
            logger.error(f"database restore failed: {exc}")
            return 1
        print("restore: path={path} revision={revision}".format(**record), flush=True)
        return 0

    if args.command == "revision":
        print(current_revision() or "", flush=True)
        return 0

    if args.command == "downgrade":
        try:
            downgrade_to(args.target)
        except (OSError, RuntimeError) as exc:
            logger.error(f"database downgrade failed: {exc}")
            return 1
        print(f"downgrade: revision={current_revision() or ''}", flush=True)
        return 0

    if args.command == "clear-jobs":
        counts = clear_job_records()
        logger.info(f"Cleared job records at {settings.db_path}")
        for table, deleted in counts.items():
            logger.info(f"- {table}: {deleted} row(s)")
        return 0

    if args.command == "init":
        issues = initialize_database()
        if issues:
            logger.info(f"Initialized database at {settings.db_path} ({len(issues)} change(s) applied)")
            for issue in issues:
                logger.info(f"- {issue}")
        else:
            logger.info(f"Database already up to date at {settings.db_path}")
        return 0

    issues = schema_issues()
    if issues:
        logger.info(f"Database requires initialization or migration at {settings.db_path}")
        for issue in issues:
            logger.info(f"- {issue}")
        return 1

    logger.info(f"Database ready at {settings.db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
