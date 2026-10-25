"""One memory artifact per queued analysis, chosen at enqueue and proven at use.

A queued plugin or batch run used to read whatever memory artifact its Job
presented when the worker happened to wake up. Uploading a second dump to the
same completed job therefore re-pointed already-queued work at the new bytes,
and the published report named the new artifact as the input it had analysed.

An action now carries two records of the evidence it was queued against:

- ``HostAction.source_artifact_id``, the live foreign key, which deletion
  clears (``SET NULL``) so an artifact can still be disposed of once its
  analysis has reached a terminal status;
- an immutable snapshot of that artifact's recorded identity in the action
  payload, which survives the deletion and keeps a historical report readable.

Both must agree before execution. Nothing here ever falls back to the newest
dump: an action whose source cannot be proven fails and asks to be requeued.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Artifact, HostAction, Job
from ..db.transactions import reserve_sqlite_write
from .vol3_support import _sha256_file

#: Versioned payload key. Existing plugin/run/YARA payload fields are untouched.
SOURCE_SNAPSHOT_KEY = "analysis_source"
SOURCE_SNAPSHOT_VERSION = 1

#: Action types that must always name the artifact they analyse.
ANALYSIS_ACTION_TYPES = frozenset({"run_vol3_analysis", "run_vol3_plugin"})

REQUEUE_MESSAGE = (
    "This analysis was queued before its input was pinned to a specific memory dump "
    "artifact and its evidence cannot be proven; requeue it against the intended dump"
)


class AnalysisInputError(Exception):
    """The action's memory input cannot be proven, so nothing may be analysed."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class SourceSnapshot:
    """What the enqueue transaction recorded about the artifact it selected."""

    artifact_id: str
    sha256: str | None = None
    size_bytes: int | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": SOURCE_SNAPSHOT_VERSION,
            "artifact_id": self.artifact_id,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def of(cls, artifact: Artifact) -> SourceSnapshot:
        return cls(artifact_id=artifact.id, sha256=artifact.sha256, size_bytes=artifact.size_bytes)

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> SourceSnapshot | None:
        raw = (payload or {}).get(SOURCE_SNAPSHOT_KEY)
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise AnalysisInputError("Queued analysis source snapshot is unreadable")
        if raw.get("version") != SOURCE_SNAPSHOT_VERSION:
            raise AnalysisInputError(f"Queued analysis source snapshot version {raw.get('version')!r} is not supported")
        artifact_id = raw.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            raise AnalysisInputError("Queued analysis source snapshot names no artifact")
        sha256 = raw.get("sha256")
        size_bytes = raw.get("size_bytes")
        if sha256 is not None and not isinstance(sha256, str):
            raise AnalysisInputError("Queued analysis source snapshot checksum is unreadable")
        if size_bytes is not None and not isinstance(size_bytes, int):
            raise AnalysisInputError("Queued analysis source snapshot size is unreadable")
        return cls(artifact_id=artifact_id, sha256=sha256, size_bytes=size_bytes)


@dataclass(frozen=True)
class ResolvedInput:
    """The exact evidence an action may read, resolved from its own pin."""

    artifact_id: str
    path: str
    filename: str | None
    sha256: str | None
    size_bytes: int | None
    legacy: bool = False

    @property
    def verified(self) -> bool:
        """A pinned artifact with no recorded checksum stays explicitly unverified."""
        return bool(self.sha256)


def action_payload(action: HostAction | None) -> dict[str, Any]:
    if action is None:
        return {}
    try:
        payload = json.loads(action.payload or "{}")
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def pinned_source_id(action: HostAction | None, payload: dict[str, Any] | None = None) -> str | None:
    """Best-known source id for labelling a result, including a failed one.

    Never a selection: resolution below decides what may actually be read.
    """
    if action is None:
        return None
    if action.source_artifact_id:
        return action.source_artifact_id
    payload = action_payload(action) if payload is None else payload
    raw = payload.get(SOURCE_SNAPSHOT_KEY)
    if isinstance(raw, dict) and isinstance(raw.get("artifact_id"), str):
        return raw["artifact_id"]
    legacy = payload.get("source_artifact_id")
    return legacy if isinstance(legacy, str) and legacy else None


def select_source_artifact(session: Session, job: Job) -> Artifact:
    """Choose the one memory artifact an action queued now must analyse.

    Reserves the SQLite writer first: the eligibility read and the action insert
    have to be one serialized unit, or an upload or a deletion committed between
    them silently changes what was queued.
    """
    reserve_sqlite_write(session)
    if job.execution_mode == "artifact_analysis":
        from .artifact_context import source_artifact

        if not job.source_artifact_id:
            raise AnalysisInputError("Analysis source was removed; select retained evidence")
        return source_artifact(session, job.source_artifact_id)
    artifact = session.scalars(
        select(Artifact)
        .where(Artifact.job_id == job.id, Artifact.kind == "memory")
        .order_by(Artifact.created_at.desc(), Artifact.id.desc())
        .limit(1)
    ).first()
    if artifact is None:
        raise AnalysisInputError("Vol3 analysis requires a completed memory dump")
    return artifact


def resolve_analysis_input(session: Session, job: Job, action: HostAction | None) -> ResolvedInput:
    """Resolve, and prove, the memory artifact this action was queued against.

    Raises :class:`AnalysisInputError` rather than returning anything the caller
    could mistake for the job's current dump.
    """
    if action is None:
        raise AnalysisInputError(REQUEUE_MESSAGE)
    if action.job_id != job.id:
        raise AnalysisInputError("Queued analysis belongs to another job")
    payload = action_payload(action)
    snapshot = SourceSnapshot.from_payload(payload)
    pinned = action.source_artifact_id

    if snapshot is None and pinned is None:
        return _resolve_legacy_input(session, job, payload)
    if snapshot is None:
        raise AnalysisInputError("Queued analysis carries a source reference without its identity snapshot")
    if pinned is None:
        raise AnalysisInputError(
            f"Pinned analysis source {snapshot.artifact_id} is no longer retained; requeue the analysis"
        )
    if pinned != snapshot.artifact_id:
        raise AnalysisInputError(
            f"Queued analysis source reference {pinned} disagrees with its snapshot {snapshot.artifact_id}"
        )

    artifact = session.get(Artifact, pinned)
    if artifact is None:
        raise AnalysisInputError(f"Pinned analysis source {pinned} is no longer retained; requeue the analysis")
    _require_eligible(session, job, artifact)
    if artifact.sha256 != snapshot.sha256 or artifact.size_bytes != snapshot.size_bytes:
        raise AnalysisInputError(
            f"Pinned analysis source {pinned} no longer matches the identity recorded when it was queued"
        )
    return ResolvedInput(
        artifact_id=artifact.id,
        path=artifact.path,
        filename=artifact.filename,
        sha256=artifact.sha256,
        size_bytes=artifact.size_bytes,
    )


def _resolve_legacy_input(session: Session, job: Job, payload: dict[str, Any]) -> ResolvedInput:
    """Accept only a pin an older action already made explicit.

    Artifact-analysis actions have always carried their source id and their Job
    binds the same artifact, so that pin is provable. An ordinary action has no
    such record: the newest dump and the absence of an upload event are both
    guesses, and a guess here attributes a report to evidence nobody chose.
    """
    if job.execution_mode != "artifact_analysis":
        raise AnalysisInputError(REQUEUE_MESSAGE)
    legacy_id = payload.get("source_artifact_id")
    if not isinstance(legacy_id, str) or not legacy_id:
        raise AnalysisInputError(REQUEUE_MESSAGE)
    if legacy_id != job.source_artifact_id:
        raise AnalysisInputError("Queued source artifact identity no longer matches the analysis context")
    artifact = session.get(Artifact, legacy_id)
    if artifact is None:
        raise AnalysisInputError(f"Pinned analysis source {legacy_id} is no longer retained; requeue the analysis")
    _require_eligible(session, job, artifact)
    return ResolvedInput(
        artifact_id=artifact.id,
        path=artifact.path,
        filename=artifact.filename,
        sha256=artifact.sha256,
        size_bytes=artifact.size_bytes,
        legacy=True,
    )


def _require_eligible(session: Session, job: Job, artifact: Artifact) -> None:
    if artifact.kind != "memory":
        raise AnalysisInputError(f"Pinned analysis source {artifact.id} is not memory evidence")
    if job.execution_mode == "artifact_analysis":
        if artifact.id != job.source_artifact_id:
            raise AnalysisInputError("Queued source artifact identity no longer matches the analysis context")
        return
    if artifact.job_id != job.id:
        raise AnalysisInputError(f"Pinned analysis source {artifact.id} no longer belongs to job {job.id}")


def verify_source_bytes(resolved: ResolvedInput) -> str:
    """Hash the pinned bytes at use and refuse anything but what was recorded.

    Returns the computed digest. A pinned artifact with no recorded checksum is
    still readable; the caller reports it as unverified rather than as verified.
    """
    path = Path(resolved.path)
    size_at_use = path.stat().st_size
    if resolved.size_bytes is not None and size_at_use != resolved.size_bytes:
        raise AnalysisInputError(
            f"memory dump size mismatch: recorded {resolved.size_bytes}, "
            f"found {size_at_use} — the dump changed since capture"
        )
    sha256_at_use = _sha256_file(path)
    if resolved.sha256 and sha256_at_use != resolved.sha256:
        raise AnalysisInputError(
            f"memory dump hash mismatch: recorded {resolved.sha256}, "
            f"computed {sha256_at_use} — the dump changed since capture"
        )
    return sha256_at_use


def require_unchanged_input(
    session: Session, job: Job | None, action: HostAction | None, expected: ResolvedInput
) -> None:
    """Check the live pin again in the transaction that publishes a report."""
    if job is None or resolve_analysis_input(session, job, action) != expected:
        raise AnalysisInputError("Analysis source identity changed before publication")


__all__ = [
    "ANALYSIS_ACTION_TYPES",
    "REQUEUE_MESSAGE",
    "SOURCE_SNAPSHOT_KEY",
    "SOURCE_SNAPSHOT_VERSION",
    "AnalysisInputError",
    "ResolvedInput",
    "SourceSnapshot",
    "action_payload",
    "pinned_source_id",
    "resolve_analysis_input",
    "select_source_artifact",
    "verify_source_bytes",
]
