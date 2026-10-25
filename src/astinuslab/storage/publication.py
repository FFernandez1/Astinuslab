"""Recoverable publication journal for evidence artifacts.

Publishing a capture is four writes — bytes, manifest, sidecar, database row —
and a crash between any two of them used to leave a state nothing could read
back: an Acquisition stuck running, an Artifact row pointing at a staged file
that was never renamed into place, or bytes on disk that no row claims.

This module records the intent before the work starts and advances it as each
stage lands, so startup reconciliation (Task 21) has a durable list of what was
in flight. Task 17 added the journal; Task 18 added the staging and triad
mechanics that write through it.

The staging rules exist for reasons that are easy to lose:

- Staging lives under the shared storage root, never `/tmp`. All three service
  units set `PrivateTmp=true`, so each gets its own `/tmp` that the others and
  libvirtd/qemu cannot see. A file staged there cannot be handed onward.
- The payload is created no-follow and exclusive, so a planted symlink cannot
  redirect a capture into a path the producer never chose.
- The payload's inode is checked after hashing. A swap between hashing and
  manifest writing would otherwise publish a manifest describing bytes nobody
  has.
- Publication links each file into place and fails if the name is taken. Rename
  would silently replace evidence that is already there.
- Cleanup works through a descriptor pinned to the attempt's own directory
  inode, and a cleanup that cannot prove it finished sets `cleanup_incomplete`
  plus an operator-visible event rather than reporting success.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import shutil
import stat
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import (
    PUBLICATION_STATES,
    TERMINAL_PUBLICATION_STATES,
    Acquisition,
    AcquisitionEvent,
    Artifact,
    ArtifactPublicationAttempt,
    JobEvent,
)

STAGING_ROOT_NAME = ".publish"
STAGING_DIR_MODE = 0o700
EVIDENCE_DIR_MODE = 0o750
PAYLOAD_NAME = "payload"
PAYLOAD_MODE = 0o640
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_CHUNK = 1024 * 1024
_RECOVERY_MANIFEST_LIMIT = 16 * 1024 * 1024
_AT_FDCWD = -100

# States an operator or reconciliation pass still has to resolve. `completed` is
# done; `failed` and `cleanup_incomplete` are closed records, the latter kept so
# leftover bytes stay visible rather than silently forgotten.
OPEN_PUBLICATION_STATES = tuple(state for state in PUBLICATION_STATES if state not in TERMINAL_PUBLICATION_STATES)


def _now() -> datetime:
    """Timezone-aware UTC. A naive value here raises when compared with an aware one."""
    return datetime.now(UTC)


def reserve(
    session: Session,
    *,
    producer: str,
    kind: str,
    final_path: str,
    staging_dir: str,
    artifact_id: str | None = None,
    acquisition_id: str | None = None,
    job_id: str | None = None,
    session_id: str | None = None,
) -> ArtifactPublicationAttempt:
    """Claim a destination path before any bytes exist.

    The partial unique index on `final_path` makes the claim exclusive among live
    attempts, so a second producer aiming at the same destination fails here
    rather than halfway through a capture.
    """
    now = _now()
    attempt = ArtifactPublicationAttempt(
        id=uuid.uuid4().hex,
        producer=producer,
        kind=kind,
        final_path=final_path,
        staging_dir=staging_dir,
        artifact_id=artifact_id,
        acquisition_id=acquisition_id,
        job_id=job_id,
        session_id=session_id,
        state="reserved",
        created_at=now,
        updated_at=now,
    )
    session.add(attempt)
    session.flush()
    return attempt


def transition(session: Session, attempt: ArtifactPublicationAttempt, state: str) -> ArtifactPublicationAttempt:
    """Advance an attempt, stamping `completed_at` on terminal states."""
    if state not in PUBLICATION_STATES:
        raise ValueError(f"unknown publication state: {state!r}")
    attempt.state = state
    attempt.updated_at = _now()
    if state in TERMINAL_PUBLICATION_STATES:
        attempt.completed_at = attempt.updated_at
    session.flush()
    return attempt


def fail(session: Session, attempt: ArtifactPublicationAttempt, error_summary: str) -> ArtifactPublicationAttempt:
    """Close an attempt as failed, keeping a summary an operator can act on."""
    attempt.error_summary = error_summary
    return transition(session, attempt, "failed")


def open_attempts(session: Session) -> Sequence[ArtifactPublicationAttempt]:
    """Every attempt that has not reached a terminal state, oldest first."""
    statement = (
        select(ArtifactPublicationAttempt)
        .where(ArtifactPublicationAttempt.state.in_(OPEN_PUBLICATION_STATES))
        .order_by(ArtifactPublicationAttempt.created_at)
    )
    return session.execute(statement).scalars().all()


# ---------------------------------------------------------------------------
# Task 18 — attempt-owned staging and atomic triad creation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StagedTriad:
    """The three files, complete and verified, still inside the attempt directory."""

    sha256: str
    size_bytes: int
    payload_path: Path
    manifest_path: Path
    sidecar_path: Path


@dataclass(frozen=True)
class PublishedTriad:
    """The same three files, now at their final names."""

    artifact_path: Path
    manifest_path: Path
    sidecar_path: Path


def _identity(info: os.stat_result) -> tuple[int, int]:
    return (info.st_dev, info.st_ino)


def _hash_payload(fd: int, path: Path) -> tuple[str, int]:
    """Hash through the open descriptor, never by reopening the name."""
    digest = hashlib.sha256()
    size = 0
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, _CHUNK)
        if not chunk:
            break
        size += len(chunk)
        digest.update(chunk)
    return digest.hexdigest(), size


def _write_verified(directory_fd: int, name: str, payload: bytes) -> None:
    """Write a staging file exclusively and fsync it before anyone depends on it."""
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, PAYLOAD_MODE, dir_fd=directory_fd)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def _remove_staging(staging_dir: Path, identity: tuple[int, int]) -> None:
    """Empty and remove the attempt directory through a descriptor pinned to its inode.

    A path-based rmtree can be steered onto a different directory if ours is
    renamed away between the check and the delete.
    """
    try:
        directory_fd = os.open(staging_dir, _DIRECTORY_FLAGS)
    except FileNotFoundError:
        return
    try:
        if _identity(os.fstat(directory_fd)) != identity:
            raise OSError(f"staging directory identity changed under cleanup: {staging_dir}")
        for name in os.listdir(directory_fd):
            os.unlink(name, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    # Only drop the entry while the name still resolves to the inode just emptied.
    if _identity(os.lstat(staging_dir)) == identity:
        os.rmdir(staging_dir)


RENAME_NOREPLACE = 1


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically move `source` to `destination`, refusing a taken name.

    `os.rename` would silently replace whatever is already there, which for
    evidence is the one thing publication must never do. `renameat2` with
    RENAME_NOREPLACE gives the same atomicity and fails with EEXIST instead.
    """
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:  # pragma: no cover - glibc < 2.28
        raise OSError(errno.ENOSYS, "renameat2 is unavailable")
    result = renameat2(
        ctypes.c_int(_AT_FDCWD),
        ctypes.c_char_p(os.fsencode(source)),
        ctypes.c_int(_AT_FDCWD),
        ctypes.c_char_p(os.fsencode(destination)),
        ctypes.c_uint(RENAME_NOREPLACE),
    )
    if result != 0:
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise FileExistsError(code, os.strerror(code), str(destination))
        raise OSError(code, os.strerror(code), str(destination))


def _move_into_place(source: Path, destination: Path) -> None:
    """Publish one staged file, whoever wrote it.

    A hardlink is preferred: it leaves the staged copy in place until cleanup, so
    a failure midway is trivially reversible. But libvirt writes a core dump as
    root, and with fs.protected_hardlinks=1 - the default on Ubuntu - the service
    user cannot hardlink a file it neither owns nor can write. That is EPERM, and
    it is why every real memory capture failed here until Gate B ran.

    The fallback is renameat2(RENAME_NOREPLACE): same atomicity, same refusal to
    overwrite, and it needs write permission on the directories rather than
    ownership of the file.
    """
    try:
        os.link(source, destination, follow_symlinks=False)
    except PermissionError:
        _rename_noreplace(source, destination)


def _record_operator_event(session: Session, record: ArtifactPublicationAttempt, message: str) -> None:
    """Surface an unresolved attempt where an operator already looks."""
    if record.job_id:
        session.add(JobEvent(job_id=record.job_id, state="publication_cleanup_incomplete", message=message))
    if record.acquisition_id:
        session.add(
            AcquisitionEvent(
                acquisition_id=record.acquisition_id,
                status="publication_cleanup_incomplete",
                message=message,
            )
        )
    try:
        session.flush()
    except Exception:
        # This runs on the failure path. An event that cannot be written is worth
        # less than the exception the caller is already carrying, so the journal
        # state stands on its own and the original error keeps propagating.
        session.rollback()


@dataclass(frozen=True)
class AttemptRecord:
    """Read-only snapshot of a journal row, safe to hold outside a session."""

    id: str
    state: str
    final_path: str
    staging_dir: str
    sha256: str | None
    size_bytes: int | None
    artifact_id: str | None
    acquisition_id: str | None
    error_summary: str | None


def _snapshot(row: ArtifactPublicationAttempt) -> AttemptRecord:
    return AttemptRecord(
        id=row.id,
        state=row.state,
        final_path=row.final_path,
        staging_dir=row.staging_dir,
        sha256=row.sha256,
        size_bytes=row.size_bytes,
        artifact_id=row.artifact_id,
        acquisition_id=row.acquisition_id,
        error_summary=row.error_summary,
    )


class PublicationAttempt:
    """One publication, from reserved destination to completed triad.

    Every database touch opens its own short transaction through the caller's
    session factory. A capture runs for minutes; holding one session open across
    it would keep a SQLite write transaction open the whole time and block every
    other writer, and nothing the journal records would be durable until the end
    — which is precisely what it exists to survive.
    """

    def __init__(
        self,
        session_factory: Callable[[], AbstractContextManager[Session]],
        attempt_id: str,
        staging_dir: Path,
        staging_identity: tuple[int, int],
        final_path: Path,
    ) -> None:
        self._session_factory = session_factory
        self._id = attempt_id
        self._staging_dir = staging_dir
        self._identity = staging_identity
        self._final_path = final_path
        self._staged: StagedTriad | None = None

    @property
    def id(self) -> str:
        return self._id

    @property
    def record(self) -> AttemptRecord:
        with self._session_factory() as session:
            return _snapshot(self._row(session))

    @property
    def staging_dir(self) -> Path:
        return self._staging_dir

    @property
    def payload_path(self) -> Path:
        return self._staging_dir / PAYLOAD_NAME

    def _row(self, session: Session) -> ArtifactPublicationAttempt:
        row = session.get(ArtifactPublicationAttempt, self._id)
        if row is None:  # pragma: no cover - the row is created before the handle exists
            raise RuntimeError(f"publication attempt vanished: {self._id}")
        return row

    def _transition(self, state: str) -> None:
        with self._session_factory() as session:
            transition(session, self._row(session), state)

    @contextmanager
    def open_payload(self) -> Iterator[BinaryIO]:
        """Create the payload no-follow and exclusive, and fsync it on close."""
        self._transition("capturing")
        directory_fd = os.open(self._staging_dir, _DIRECTORY_FLAGS)
        try:
            fd = os.open(
                PAYLOAD_NAME,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                PAYLOAD_MODE,
                dir_fd=directory_fd,
            )
        finally:
            os.close(directory_fd)
        handle = os.fdopen(fd, "wb")
        try:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()

    @contextmanager
    def capture_to_path(self) -> Iterator[Path]:
        """For producers that must create the file themselves.

        libvirt writes a core dump as root through its own code path; it cannot
        be handed an already-open descriptor. The attempt directory is 0700 and
        attempt-owned, so nothing else can plant a name inside it, and the file
        is verified no-follow afterwards.
        """
        self._transition("capturing")
        yield self.payload_path
        directory_fd = os.open(self._staging_dir, _DIRECTORY_FLAGS)
        try:
            info = os.stat(PAYLOAD_NAME, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise OSError(f"producer wrote no payload at {self.payload_path}") from exc
        finally:
            os.close(directory_fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"payload is not a regular file: {self.payload_path}")

    def stage(self, manifest_builder: Callable[[str, int], dict[str, Any]]) -> StagedTriad:
        """Hash the payload, then write the manifest and sidecar beside it."""
        directory_fd = os.open(self._staging_dir, _DIRECTORY_FLAGS)
        try:
            fd = os.open(PAYLOAD_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            try:
                before = os.fstat(fd)
                sha256, size_bytes = _hash_payload(fd, self.payload_path)
                # The descriptor still points at the bytes we hashed; the name may
                # not. If they disagree, the manifest would describe bytes nobody has.
                current = os.stat(PAYLOAD_NAME, dir_fd=directory_fd, follow_symlinks=False)
                if _identity(before) != _identity(current):
                    raise OSError(f"payload inode changed while hashing: {self.payload_path}")
                if size_bytes != current.st_size:
                    raise OSError(f"payload size changed while hashing: {self.payload_path}")
            finally:
                os.close(fd)

            manifest = manifest_builder(sha256, size_bytes)
            manifest_name = f"{PAYLOAD_NAME}.manifest.json"
            sidecar_name = f"{PAYLOAD_NAME}.sha256"
            _write_verified(directory_fd, manifest_name, json.dumps(manifest, indent=2, sort_keys=True).encode())
            _write_verified(directory_fd, sidecar_name, f"{sha256}  {self._final_path.name}\n".encode())
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

        with self._session_factory() as session:
            row = self._row(session)
            row.sha256 = sha256
            row.size_bytes = size_bytes
            row.manifest_path = str(_manifest_destination(self._final_path))
            row.sha256_sidecar_path = str(_sidecar_destination(self._final_path))
            transition(session, row, "staged")

        self._staged = StagedTriad(
            sha256=sha256,
            size_bytes=size_bytes,
            payload_path=self.payload_path,
            manifest_path=self._staging_dir / manifest_name,
            sidecar_path=self._staging_dir / sidecar_name,
        )
        return self._staged

    def adopt(self, existing_path: Path, manifest_builder: Callable[[str, int], dict[str, Any]]) -> StagedTriad:
        """Take responsibility for a file a tool already wrote at its final name.

        Volatility writes its own outputs into the job's vol3 directory;
        AstinusLab does not choose the path. There is nothing to stage, but the
        manifest and sidecar are still built privately so the pair appears
        atomically, and the hash is still taken through our own no-follow
        descriptor rather than by reopening the name.
        """
        self._transition("capturing")
        fd = os.open(existing_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError(f"adopted path is not a regular file: {existing_path}")
            sha256, size_bytes = _hash_payload(fd, existing_path)
            current = os.stat(existing_path, follow_symlinks=False)
            if _identity(info) != _identity(current):
                raise OSError(f"adopted file changed while hashing: {existing_path}")
        finally:
            os.close(fd)

        manifest = manifest_builder(sha256, size_bytes)
        manifest_name = f"{PAYLOAD_NAME}.manifest.json"
        sidecar_name = f"{PAYLOAD_NAME}.sha256"
        directory_fd = os.open(self._staging_dir, _DIRECTORY_FLAGS)
        try:
            _write_verified(directory_fd, manifest_name, json.dumps(manifest, indent=2, sort_keys=True).encode())
            _write_verified(directory_fd, sidecar_name, f"{sha256}  {existing_path.name}\n".encode())
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

        with self._session_factory() as session:
            row = self._row(session)
            row.sha256 = sha256
            row.size_bytes = size_bytes
            row.final_path = str(existing_path)
            row.manifest_path = str(_manifest_destination(existing_path))
            row.sha256_sidecar_path = str(_sidecar_destination(existing_path))
            transition(session, row, "staged")

        self._final_path = existing_path
        self._staged = StagedTriad(
            sha256=sha256,
            size_bytes=size_bytes,
            payload_path=existing_path,
            manifest_path=self._staging_dir / manifest_name,
            sidecar_path=self._staging_dir / sidecar_name,
        )
        return self._staged

    def publish_sidecars(self) -> PublishedTriad:
        """Link the manifest and sidecar beside an adopted file: both or neither."""
        if self._staged is None:
            raise RuntimeError("publish_sidecars() requires an adopted file")
        targets = (
            (self._staged.manifest_path, _manifest_destination(self._final_path)),
            (self._staged.sidecar_path, _sidecar_destination(self._final_path)),
        )
        for _, destination in targets:
            if destination.exists() or destination.is_symlink():
                raise FileExistsError(f"publication destination already exists: {destination}")

        linked: list[Path] = []
        try:
            for source, destination in targets:
                _move_into_place(source, destination)
                linked.append(destination)
        except BaseException:
            # A manifest without its sidecar is a half-published claim; roll back.
            for destination in linked:
                with _suppressed():
                    destination.unlink()
            raise

        directory_fd = os.open(self._final_path.parent, _DIRECTORY_FLAGS)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

        self._transition("published")
        return PublishedTriad(
            artifact_path=self._final_path,
            manifest_path=_manifest_destination(self._final_path),
            sidecar_path=_sidecar_destination(self._final_path),
        )

    def publish(self) -> PublishedTriad:
        """Link the triad into place. A taken name fails the attempt, never overwrites."""
        if self._staged is None:
            raise RuntimeError("publish() requires a staged triad")
        destination_dir = self._final_path.parent
        destination_dir.mkdir(mode=EVIDENCE_DIR_MODE, parents=True, exist_ok=True)

        targets = (
            (self._staged.payload_path, self._final_path),
            (self._staged.manifest_path, _manifest_destination(self._final_path)),
            (self._staged.sidecar_path, _sidecar_destination(self._final_path)),
        )
        # Check every destination before linking any: a half-published triad is a
        # worse outcome than a refusal.
        for _, destination in targets:
            if destination.exists() or destination.is_symlink():
                raise FileExistsError(f"publication destination already exists: {destination}")

        linked: list[Path] = []
        try:
            for source, destination in targets:
                _move_into_place(source, destination)
                linked.append(destination)
        except BaseException:
            for destination in linked:
                with _suppressed():
                    destination.unlink()
            raise

        directory_fd = os.open(destination_dir, _DIRECTORY_FLAGS)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

        self._transition("published")
        return PublishedTriad(
            artifact_path=self._final_path,
            manifest_path=_manifest_destination(self._final_path),
            sidecar_path=_sidecar_destination(self._final_path),
        )

    def complete(self, session: Session, *, artifact_id: str | None = None) -> None:
        """Close the attempt inside the caller's transaction, then drop staging.

        The session is the caller's on purpose: the Artifact row, the sealed
        Acquisition and this transition have to commit together, or a crash
        between them leaves exactly the orphan states this replaces.
        """
        row = self._row(session)
        if artifact_id is not None:
            row.artifact_id = artifact_id
        transition(session, row, "completed")

    def finish(self) -> None:
        """Remove staging after the caller's transaction committed."""
        self._cleanup()

    def abandon(self, reason: str = "attempt abandoned before publication") -> None:
        """Give up without an exception: clean staging and close the journal row."""
        with self._session_factory() as session:
            row = self._row(session)
            if row.state in TERMINAL_PUBLICATION_STATES:
                return
            row.error_summary = reason
            transition(session, row, "failed")
        self._cleanup()

    def _cleanup(self) -> None:
        try:
            _remove_staging(self._staging_dir, self._identity)
        except OSError as exc:
            # Never re-raise here. On the failure path this would replace the
            # diagnosis with a cleanup error; on the success path the evidence is
            # already published and losing it to a cleanup problem helps nobody.
            message = f"publication staging left behind at {self._staging_dir}: {exc}"
            with self._session_factory() as session:
                row = self._row(session)
                row.error_summary = ((row.error_summary or "") + f" | {message}").strip(" |")
                transition(session, row, "cleanup_incomplete")
                _record_operator_event(session, row, message)

    def _fail(self, exc: BaseException) -> None:
        with self._session_factory() as session:
            row = self._row(session)
            if row.state not in TERMINAL_PUBLICATION_STATES:
                row.error_summary = f"{type(exc).__name__}: {exc}"
                transition(session, row, "failed")
        self._cleanup()


def _manifest_destination(final_path: Path) -> Path:
    return final_path.with_suffix(final_path.suffix + ".manifest.json")


def _sidecar_destination(final_path: Path) -> Path:
    return final_path.with_suffix(final_path.suffix + ".sha256")


@contextmanager
def _suppressed() -> Iterator[None]:
    try:
        yield
    except OSError:
        pass


@contextmanager
def attempt(
    session_factory: Callable[[], AbstractContextManager[Session]],
    *,
    producer: str,
    kind: str,
    final_path: Path | str,
    storage_root: Path | str | None = None,
    staging_parent: Path | str | None = None,
    artifact_id: str | None = None,
    acquisition_id: str | None = None,
    job_id: str | None = None,
    session_id: str | None = None,
) -> Iterator[PublicationAttempt]:
    """Reserve a destination, stage privately, and publish or clean up.

    `staging_parent` defaults to `<storage_root>/.publish`. Producers whose bytes
    are written by another process — libvirt dumping a domain as root, qemu-img
    converting a disk — pass the evidence directory instead, because the ACL and
    AppArmor grants that let the hypervisor write are attached to that tree.
    """
    final_path = Path(final_path)
    if staging_parent is None and storage_root is None:
        raise ValueError("attempt() needs either storage_root or staging_parent")
    staging_root = (
        Path(staging_parent) if staging_parent is not None else Path(storage_root) / STAGING_ROOT_NAME  # type: ignore[arg-type]
    )
    staging_root.mkdir(mode=EVIDENCE_DIR_MODE, parents=True, exist_ok=True)

    with session_factory() as session:
        row = reserve(
            session,
            producer=producer,
            kind=kind,
            final_path=str(final_path),
            staging_dir="",
            artifact_id=artifact_id,
            acquisition_id=acquisition_id,
            job_id=job_id,
            session_id=session_id,
        )
        attempt_id = row.id
        staging_dir = staging_root / f"{STAGING_ROOT_NAME}-{attempt_id}"
        # Exclusive create: the attempt id is unique, so a collision means someone
        # else is already inside this directory.
        os.mkdir(staging_dir, mode=STAGING_DIR_MODE)
        os.chmod(staging_dir, STAGING_DIR_MODE)
        identity = _identity(os.lstat(staging_dir))
        row.staging_dir = str(staging_dir)
        session.flush()

    handle = PublicationAttempt(session_factory, attempt_id, staging_dir, identity, final_path)
    try:
        yield handle
    except BaseException as exc:
        handle._fail(exc)
        raise
    else:
        if handle.record.state not in TERMINAL_PUBLICATION_STATES:
            handle.abandon()


# ---------------------------------------------------------------------------
# Task 21 — startup reconciliation
# ---------------------------------------------------------------------------

QUARANTINE_DIR_NAME = "quarantine"


@dataclass
class ReconciliationReport:
    """What a reconciliation pass did, for the operator log."""

    closed: int = 0
    quarantined: int = 0
    completed: int = 0
    integrity_failures: int = 0
    cleanup_incomplete: int = 0
    missing_evidence: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"closed={self.closed} quarantined={self.quarantined} completed={self.completed} "
            f"integrity_failures={self.integrity_failures} cleanup_incomplete={self.cleanup_incomplete} "
            f"missing_evidence={len(self.missing_evidence)}"
        )


def _digest_of(path: Path) -> tuple[str, int]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        return _hash_payload(fd, path)
    finally:
        os.close(fd)


def _quarantine_root(staging_dir: Path, storage_root: Path | str | None) -> Path:
    if storage_root is not None:
        return Path(storage_root) / QUARANTINE_DIR_NAME
    # Without an explicit root, keep the bytes beside where they were staged
    # rather than inventing a location outside the evidence tree.
    return staging_dir.parent / QUARANTINE_DIR_NAME


def _close_unfinished(session: Session, row: ArtifactPublicationAttempt, report: ReconciliationReport) -> None:
    """reserved/capturing: nothing was ever published, so drop the staging."""
    staging = Path(row.staging_dir) if row.staging_dir else None
    row.error_summary = "interrupted before the triad was complete; no bytes were published"
    transition(session, row, "failed")
    if staging is None or not staging.exists():
        report.closed += 1
        return
    try:
        _remove_staging(staging, _identity(os.lstat(staging)))
        report.closed += 1
    except OSError as exc:
        row.error_summary = f"{row.error_summary} | staging left behind at {staging}: {exc}"
        transition(session, row, "cleanup_incomplete")
        _record_operator_event(session, row, f"publication staging left behind at {staging}: {exc}")
        report.cleanup_incomplete += 1


def _quarantine(
    session: Session,
    row: ArtifactPublicationAttempt,
    report: ReconciliationReport,
    storage_root: Path | str | None,
) -> None:
    """staged/db_prepared: a complete triad nobody claimed.

    Resuming would mean inventing the database rows the producer never got to
    write — its case links, its metadata, its report row. Those are not in the
    journal, and a fabricated row is worse than a file an operator has to look
    at. The bytes move somewhere findable and are never deleted.
    """
    staging = Path(row.staging_dir) if row.staging_dir else None
    if staging is None or not staging.exists():
        _close_unfinished(session, row, report)
        return

    destination = _quarantine_root(staging, storage_root) / row.id
    try:
        destination.parent.mkdir(mode=EVIDENCE_DIR_MODE, parents=True, exist_ok=True)
        shutil.move(str(staging), str(destination))
    except OSError as exc:
        row.error_summary = f"staged triad could not be quarantined from {staging}: {exc}"
        transition(session, row, "cleanup_incomplete")
        _record_operator_event(session, row, str(row.error_summary))
        report.cleanup_incomplete += 1
        return

    row.error_summary = (
        f"Publication interrupted in {row.state}; remaining staging retained at {destination}. "
        f"Final destination {row.final_path} may contain files; publication ownership is unresolved "
        "and requires operator review."
    )
    transition(session, row, "failed")
    _record_operator_event(session, row, str(row.error_summary))
    report.quarantined += 1


@dataclass(frozen=True)
class VerifiedPublication:
    sha256: str
    size_bytes: int
    manifest: dict[str, object]


class PublicationIntegrityError(ValueError):
    """A stable reason to preserve evidence for operator reconciliation."""


@contextmanager
def _recovery_file(path: Path, component: str) -> Iterator[tuple[int, os.stat_result]]:
    """Never follow links or block opening a FIFO; verify the opened identity."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise PublicationIntegrityError(f"{component} is not a regular file")
            yield fd, before
            after = os.fstat(fd)
            named = os.lstat(path)
            if (
                not stat.S_ISREG(named.st_mode)
                or _identity(before) != _identity(named)
                or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            ):
                raise PublicationIntegrityError(f"{component} changed during verification")
        finally:
            os.close(fd)
    except OSError as exc:
        raise PublicationIntegrityError(f"{component} could not be read") from exc


def _read_recovery_metadata(path: Path, component: str, limit: int) -> bytes:
    with _recovery_file(path, component) as (fd, before):
        if before.st_size > limit:
            raise PublicationIntegrityError(f"{component} exceeds recovery read limit")
        raw = bytearray()
        while len(raw) <= limit:
            chunk = os.read(fd, min(_CHUNK, limit + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > limit:
            raise PublicationIntegrityError(f"{component} exceeds recovery read limit")
    return bytes(raw)


def _unique_manifest_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PublicationIntegrityError("manifest has duplicate keys")
        result[key] = value
    return result


def _reject_manifest_constant(value: str) -> object:
    raise PublicationIntegrityError("manifest has a non-finite number")


def _verify_published_triad(row: ArtifactPublicationAttempt) -> VerifiedPublication:
    from .artifacts import MANIFEST_SCHEMA_VERSION, manifest_path_for, sha256_sidecar_path_for

    if (
        not isinstance(row.sha256, str)
        or len(row.sha256) != 64
        or any(char not in "0123456789abcdef" for char in row.sha256)
        or type(row.size_bytes) is not int
        or row.size_bytes < 0
    ):
        raise PublicationIntegrityError("journal digest or size is invalid")
    final = Path(row.final_path)
    manifest_path, checksum_path = manifest_path_for(final), sha256_sidecar_path_for(final)
    if row.manifest_path != str(manifest_path) or row.sha256_sidecar_path != str(checksum_path):
        raise PublicationIntegrityError("journal metadata paths do not match payload siblings")
    with _recovery_file(final, "payload") as (fd, _before):
        sha256, size_bytes = _hash_payload(fd, final)
    if sha256 != row.sha256 or size_bytes != row.size_bytes:
        raise PublicationIntegrityError("payload sha256 or size does not match journal")
    raw = _read_recovery_metadata(manifest_path, "manifest", _RECOVERY_MANIFEST_LIMIT)
    try:
        manifest = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_manifest_keys, parse_constant=_reject_manifest_constant
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise PublicationIntegrityError("manifest is invalid JSON") from exc
    if not isinstance(manifest, dict):
        raise PublicationIntegrityError("manifest must be an object")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise PublicationIntegrityError("manifest schema is unsupported")
    allowed = {
        "job_capture": {"memory", "disk"},
        "playground_capture": {"memory", "disk"},
        "manual_upload": {"memory"},
        "vol3_analysis": {"report"},
        "vol3_plugin_run": {"report"},
    }
    if row.kind not in allowed.get(row.producer, set()):
        raise PublicationIntegrityError("journal producer or kind is unsupported")
    expected = {
        "producer": row.producer,
        "kind": row.kind,
        "path": str(final),
        "filename": final.name,
        "sha256": sha256,
        "sha256_sidecar_path": str(checksum_path),
        "job_id": row.job_id,
        "session_id": row.session_id or row.job_id,
        "acquisition_id": row.acquisition_id,
    }
    for key, value in expected.items():
        if key not in manifest or manifest[key] != value:
            raise PublicationIntegrityError(f"manifest {key} does not match journal")
    if type(manifest.get("size_bytes")) is not int or manifest["size_bytes"] != size_bytes:
        raise PublicationIntegrityError("manifest size_bytes does not match payload")
    expected_checksum = f"{sha256}  {final.name}\n".encode()
    if _read_recovery_metadata(checksum_path, "checksum", len(expected_checksum)) != expected_checksum:
        raise PublicationIntegrityError("checksum does not match payload")
    return VerifiedPublication(sha256, size_bytes, manifest)


def _refuse_recovery(
    session: Session, row: ArtifactPublicationAttempt, report: ReconciliationReport, reason: str
) -> None:
    row.error_summary = f"publication integrity failure: {reason}; evidence left untouched; reconciliation required"
    transition(session, row, "failed")
    _record_operator_event(session, row, row.error_summary)
    report.integrity_failures += 1


def _complete_published(session: Session, row: ArtifactPublicationAttempt, report: ReconciliationReport) -> None:
    """published: verify the whole triad before completing the database side."""
    from ..db.repositories import AcquisitionRepository, ArtifactRepository
    from ..db.transactions import reserve_sqlite_write

    final_path = Path(row.final_path)
    try:
        verified = _verify_published_triad(row)
    except PublicationIntegrityError as exc:
        _refuse_recovery(session, row, report, str(exc))
        return
    sha256, size_bytes = verified.sha256, verified.size_bytes
    reserve_sqlite_write(session)
    existing = session.query(Artifact).filter(Artifact.path == str(final_path)).one_or_none()
    if existing is not None:
        expected = {
            "producer": row.producer,
            "kind": row.kind,
            "sha256": sha256,
            "size_bytes": size_bytes,
            "job_id": row.job_id,
            "session_id": row.session_id,
            "acquisition_id": row.acquisition_id,
            "filename": final_path.name,
            "manifest_path": row.manifest_path,
            "sha256_sidecar_path": row.sha256_sidecar_path,
        }
        if row.producer == "playground_capture":
            owner = session.get(Acquisition, row.acquisition_id) if row.acquisition_id else None
            if owner is None:
                _refuse_recovery(session, row, report, "capture owner is missing")
                return
            expected["case_id"] = owner.case_id
        for key, value in expected.items():
            if getattr(existing, key) != value:
                _refuse_recovery(session, row, report, f"existing Artifact {key} conflicts with verified evidence")
                return
    if row.producer == "playground_capture":
        if not row.acquisition_id or not row.session_id:
            _refuse_recovery(session, row, report, "capture owner is missing")
            return
        outcome = AcquisitionRepository(session).recover_playground_capture(
            acquisition_id=row.acquisition_id,
            session_id=row.session_id,
            kind=row.kind,
            path=str(final_path),
            sha256=sha256,
            size_bytes=size_bytes,
        )
        if outcome == "conflict":
            _refuse_recovery(
                session, row, report, "capture Acquisition state or ownership conflicts with verified evidence"
            )
            return
    if existing is None:
        existing = ArtifactRepository(session).create_artifact(
            kind=row.kind,
            producer=row.producer,
            path=str(final_path),
            filename=final_path.name,
            sha256=sha256,
            size_bytes=size_bytes,
            manifest_path=row.manifest_path,
            sha256_sidecar_path=row.sha256_sidecar_path,
            metadata_json=json.dumps({"reconciled": True, "attempt_id": row.id}, sort_keys=True),
            job_id=row.job_id,
            session_id=row.session_id,
            acquisition_id=row.acquisition_id,
        )
    row.artifact_id = existing.id
    transition(session, row, "completed")
    staging = Path(row.staging_dir) if row.staging_dir else None
    if staging is not None and staging.exists():
        try:
            _remove_staging(staging, _identity(os.lstat(staging)))
        except OSError as exc:
            _record_operator_event(session, row, f"publication staging left behind at {staging}: {exc}")
            report.cleanup_incomplete += 1
    report.completed += 1


def reconcile(
    session_factory: Callable[[], AbstractContextManager[Session]],
    *,
    storage_root: Path | str | None = None,
) -> ReconciliationReport:
    """Resolve every attempt an interrupted worker left behind.

    Deterministic by state, and conservative in one direction only: bytes are
    never deleted and a file is never reported complete without being read back
    and checked against what the journal recorded.
    """
    report = ReconciliationReport()
    with session_factory() as session:
        rows = list(
            session.execute(
                select(ArtifactPublicationAttempt).where(ArtifactPublicationAttempt.state.in_(OPEN_PUBLICATION_STATES))
            )
            .scalars()
            .all()
        )
        for row in rows:
            if row.state in {"reserved", "capturing"}:
                _close_unfinished(session, row, report)
            elif row.state in {"staged", "db_prepared"}:
                _quarantine(session, row, report, storage_root)
            elif row.state == "published":
                _complete_published(session, row, report)

        # Completed attempts are history and are not rewritten, but evidence that
        # has gone missing since is surfaced rather than left for a download to
        # discover.
        completed = (
            session.execute(select(ArtifactPublicationAttempt).where(ArtifactPublicationAttempt.state == "completed"))
            .scalars()
            .all()
        )
        for row in completed:
            if row.final_path and not Path(row.final_path).exists():
                report.missing_evidence.append(row.final_path)
                _record_operator_event(session, row, f"published evidence is missing from disk: {row.final_path}")
    return report
