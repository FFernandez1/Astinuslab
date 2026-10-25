from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import uuid4

from sqlalchemy import case as sql_case
from sqlalchemy import func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from ..api.schemas import JobCreate
from ..jobs.job_status import ALLOWED_TRANSITIONS, TERMINAL_STATUSES
from ..jobs.job_status import FORENSIC_LIVE_STATUSES as FORENSIC_LIVE_STATUSES
from .models import Acquisition, AppSetting, Artifact, Case, HostAction, ISFArtifact, Job, JobEvent, JobRuntime, Report

# Status vocabulary and the transition graph live in job_status; keep the private
# alias so JobRepository internals and the lifecycle test's monkeypatch target are
# unchanged. FORENSIC_LIVE_STATUSES is re-exported for existing importers.
_ALLOWED_TRANSITIONS = ALLOWED_TRANSITIONS
JobHistoryState = Literal["all", "active", "finished"]


def _job_history_query(state: JobHistoryState) -> Select[tuple[Job]]:
    query = select(Job).where(Job.execution_mode != "artifact_analysis")
    if state == "active":
        query = query.where(Job.status.not_in(TERMINAL_STATUSES))
    elif state == "finished":
        query = query.where(Job.status.in_(TERMINAL_STATUSES))
    return query


def _assert_mapped_fields(model_or_instance: Any, fields: dict[str, Any]) -> None:
    allowed = set(model_or_instance.__mapper__.attrs.keys())
    unknown = set(fields) - allowed
    if unknown:
        model_name = (
            model_or_instance.__name__ if isinstance(model_or_instance, type) else type(model_or_instance).__name__
        )
        raise ValueError(f"{model_name}: unknown field(s) {sorted(unknown)}; allowed: {sorted(allowed)}")


class InvalidJobTransitionError(ValueError):
    """Raised when a job status transition is not permitted by the state graph."""

    def __init__(self, from_status: str, to_status: str) -> None:
        super().__init__(f"Invalid job status transition: {from_status!r} → {to_status!r}")
        self.from_status = from_status
        self.to_status = to_status


def clear_artifact_references(session: Session, artifact_id: str) -> None:
    """Implement SET NULL explicitly under the application's SQLite FK policy."""
    from .models import ArtifactPublicationAttempt

    for field in (Report.input_artifact_id, Report.output_artifact_id):
        session.query(Report).filter(field == artifact_id).update({field: None}, synchronize_session="fetch")
    session.query(ArtifactPublicationAttempt).filter(ArtifactPublicationAttempt.artifact_id == artifact_id).update(
        {ArtifactPublicationAttempt.artifact_id: None}, synchronize_session="fetch"
    )
    # Terminal analysis actions are retained, so their live pin is dropped rather
    # than their row. The payload snapshot keeps the historical input identity,
    # and a still-queued action's pin is protected before deletion gets here.
    session.query(HostAction).filter(HostAction.source_artifact_id == artifact_id).update(
        {HostAction.source_artifact_id: None}, synchronize_session="fetch"
    )


class JobRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_jobs(self, *, limit: int | None = None, offset: int = 0) -> list[Job]:
        query = (
            select(Job).where(Job.execution_mode != "artifact_analysis").order_by(Job.created_at.desc(), Job.id.desc())
        )
        if offset:
            query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)
        return list(self.session.scalars(query))

    def count_job_history(self, *, state: JobHistoryState = "all") -> int:
        """Count the selected UI history without loading its Job rows."""
        return self.session.scalar(select(func.count()).select_from(_job_history_query(state).subquery())) or 0

    def list_job_history(self, *, limit: int, offset: int = 0, state: JobHistoryState = "all") -> list[Job]:
        """Bound the UI history, with active Jobs first and stable ordering."""
        query = (
            _job_history_query(state)
            .order_by(sql_case((Job.status.in_(TERMINAL_STATUSES), 1), else_=0), Job.created_at.desc(), Job.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(self.session.scalars(query))

    def prefetch_latest_artifacts(self, jobs: list[Job]) -> None:
        """Resolve latest memory/disk artifacts for many jobs in one query.

        Seeds each job's per-instance memo so dump_* properties do not issue
        per-attribute queries in list endpoints.
        """
        job_ids = [job.id for job in jobs]
        if not job_ids:
            return
        rows = (
            self.session.query(Artifact)
            .filter(Artifact.job_id.in_(job_ids), Artifact.kind.in_(["memory", "disk"]))
            .order_by(Artifact.created_at.asc())
            .all()
        )
        latest: dict[tuple[str, str], Artifact] = {}
        for artifact in rows:
            if artifact.job_id is None:
                continue
            # Ascending order means last write wins: same tie looseness as first().
            latest[(artifact.job_id, artifact.kind)] = artifact
        for job in jobs:
            if job.execution_mode == "artifact_analysis":
                continue  # exact source read-through must never be seeded with an owned dump
            memo = job.__dict__.setdefault("_artifact_memo", {})
            memo.setdefault("memory", latest.get((job.id, "memory")))
            memo.setdefault("disk", latest.get((job.id, "disk")))

    def list_orphaned_jobs(self) -> list[Job]:
        """Jobs in non-terminal states that have already started.

        Used by the startup reconciler to detect jobs left orphaned by a
        crashed worker process. Filters in DB to avoid loading full history.
        """
        from sqlalchemy import or_

        from .models import CapacityReservation

        query = select(Job).where(
            Job.status.not_in(list(self._TERMINAL_STATUSES)),
            Job.status.not_in(("waiting_for_capture", "waiting_for_capacity")),
            or_(
                Job.started_at.is_not(None),
                Job.id.in_(
                    select(CapacityReservation.job_id).where(
                        CapacityReservation.boot_started.is_(True),
                        CapacityReservation.state == "reserved",
                    )
                ),
            ),
        )
        return list(self.session.scalars(query))

    def list_captures(self) -> list[Job]:
        return list(
            self.session.scalars(
                select(Job).where(Job.execution_mode.in_(["immediate", "post_script"])).order_by(Job.created_at.desc())
            )
        )

    def list_jobs_by_acquisition(self, acquisition_id: str) -> list[Job]:
        return list(
            self.session.scalars(
                select(Job).where(Job.acquisition_id == acquisition_id).order_by(Job.created_at.desc())
            )
        )

    def transition_job_status(
        self,
        job_id: str,
        to_status: str,
        **extra_fields,
    ) -> Job:
        """Transition job to a new status, validating against the allowed state graph.

        Also sets current_stage to the same value; other flows may later overwrite current_stage with a finer-grained sub-stage (e.g. acquiring_memory, target_stopped) while status is unchanged — the two columns are NOT guaranteed identical.
        extra_fields are written atomically with the status change.

        Self-transitions (to_status == from_status) are rejected: the state graph
        in _ALLOWED_TRANSITIONS never includes a status in its own successor set.
        Callers that may re-enter the same state must guard with an explicit
        status check before invoking this method (see request_job_cancel,
        enqueue_acquire_memory_dump_service, expire_waiting_captures_task).
        For deliberate overrides to a terminal state use force_transition.

        Raises:
            ValueError: if job not found
            InvalidJobTransitionError: if transition (including any self-transition)
                is not in _ALLOWED_TRANSITIONS
        """
        job = self.get_job(job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id!r}")
        from_status = job.status
        allowed = _ALLOWED_TRANSITIONS.get(from_status, set())
        if to_status not in allowed:
            raise InvalidJobTransitionError(from_status, to_status)
        job.status = to_status
        job.current_stage = to_status
        for key, value in extra_fields.items():
            setattr(job, key, value)
        self.session.add(job)
        self.session.flush()
        return job

    def force_transition(self, job_id: str, to_status: str, **extra_fields) -> Job:
        """Bypasses normal state guards to move a job to a terminal status.
        Also fails all non-terminal actions for this job.
        """
        job = self.get_job(job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id!r}")

        # Fail all non-terminal actions
        from .models import HostAction

        actions = self.session.scalars(
            select(HostAction).where(
                HostAction.job_id == job_id, HostAction.status.not_in(list(self._TERMINAL_STATUSES))
            )
        ).all()
        now = datetime.now(UTC)
        for action in actions:
            action.status = "failed"
            action.error_summary = "Job force-failed by user"
            action.finished_at = now

        job.status = to_status
        job.current_stage = to_status
        for key, value in extra_fields.items():
            setattr(job, key, value)
        self.session.add(job)
        self.session.flush()
        return job

    def get_job(self, job_id: str) -> Job | None:
        return self.session.get(Job, job_id)

    def create_job(self, payload: JobCreate) -> Job:
        job = Job(
            id=f"job-{uuid4().hex[:12]}",
            target_id=payload.target_id,
            scenario_id=payload.scenario_id,
            execution_mode=payload.execution_mode,
            capture_type=payload.capture_type,
            image_mode=payload.image_mode,
            image_path=payload.image_path,
            custom_script=payload.custom_script or None,
            workload_id=payload.workload_id or None,
            attacker_vm_id=payload.attacker_vm_id or None,
            vm_vcpus=payload.vm_vcpus,
            vm_memory_mb=payload.vm_memory_mb,
            vm_disk_gb=payload.vm_disk_gb,
            attacker_vm_vcpus=payload.attacker_vm_vcpus,
            attacker_vm_memory_mb=payload.attacker_vm_memory_mb,
            attacker_vm_disk_gb=payload.attacker_vm_disk_gb,
            build_vol3_isf=payload.build_vol3_isf,
            status="queued",
            current_stage="queued",
        )
        self.session.add(job)
        self.session.flush()
        self.session.add(
            JobRuntime(
                job_id=job.id,
                role="target",
                vm_name=f"astinuslab-{job.id}",
                state="pending",
                power_state="unknown",
                capture_memory_enabled=True,
                capture_disk_enabled=True,
            )
        )
        if payload.attacker_vm_id:
            self.session.add(
                JobRuntime(
                    job_id=job.id,
                    role="attacker",
                    vm_name=f"astinuslab-{job.id}-attacker",
                    state="pending",
                    power_state="unknown",
                    capture_memory_enabled=False,
                    capture_disk_enabled=False,
                )
            )
        self.session.add(JobEvent(job_id=job.id, state="queued", message="Job created"))
        self.session.flush()
        return job

    _TERMINAL_STATUSES = TERMINAL_STATUSES

    def job_has_evidence(self, job_id: str) -> bool:
        job = self.get_job(job_id)
        if job is not None and job.execution_mode == "artifact_analysis" and job.analysis_context_json:
            return True  # source custody and action history persist before first report
        return bool(
            self.session.query(Artifact.id).filter(Artifact.job_id == job_id).first()
            or self.session.query(Report.id).filter(Report.job_id == job_id).first()
        )

    def delete_job(self, job_id: str) -> bool:
        """Delete a job and its events/actions. Only allowed for terminal-state jobs."""
        from .transactions import reserve_sqlite_write

        reserve_sqlite_write(self.session)
        self.session.flush()
        job = self.get_job(job_id)
        if job is None:
            return False
        self.session.refresh(job)
        if job.status not in self._TERMINAL_STATUSES:
            return False
        if self.job_has_evidence(job_id):
            return False
        from .models import CapacityReservation

        reservation = self.session.get(CapacityReservation, job_id)
        if reservation is not None and reservation.state != "released":
            return False
        if any(runtime.state not in {"destroyed", "pending"} for runtime in self.list_runtimes(job_id)):
            return False
        if reservation is not None:
            self.session.delete(reservation)
            self.session.flush()
        self.session.delete(job)
        self.session.flush()
        return True

    def list_events(self, job_id: str, *, limit: int | None = None, offset: int = 0) -> list[JobEvent]:
        query = select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.created_at.asc(), JobEvent.id.asc())
        if offset:
            query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)
        return list(self.session.scalars(query))

    def list_runtimes(self, job_id: str) -> list[JobRuntime]:
        query = select(JobRuntime).where(JobRuntime.job_id == job_id).order_by(JobRuntime.role.asc())
        return list(self.session.scalars(query))

    def get_runtime(self, job_id: str, role: str) -> JobRuntime | None:
        query = select(JobRuntime).where(JobRuntime.job_id == job_id, JobRuntime.role == role).limit(1)
        return self.session.scalars(query).first()

    def upsert_runtime(
        self,
        job_id: str,
        role: str,
        vm_name: str,
        **fields,
    ) -> JobRuntime:
        _assert_mapped_fields(JobRuntime, fields)
        runtime = self.get_runtime(job_id, role)
        now = datetime.now(UTC)
        if runtime is None:
            runtime = JobRuntime(
                job_id=job_id,
                role=role,
                vm_name=vm_name,
                updated_at=now,
                **fields,
            )
        else:
            runtime.vm_name = vm_name
            for key, value in fields.items():
                setattr(runtime, key, value)
            runtime.updated_at = now
        self.session.add(runtime)
        self.session.flush()
        return runtime

    def update_runtime(self, job_id: str, role: str, **fields) -> JobRuntime | None:
        runtime = self.get_runtime(job_id, role)
        if runtime is None:
            return None
        _assert_mapped_fields(runtime, fields)
        for key, value in fields.items():
            setattr(runtime, key, value)
        runtime.updated_at = datetime.now(UTC)
        self.session.add(runtime)
        self.session.flush()
        return runtime

    def add_event(self, job_id: str, state: str, message: str) -> JobEvent:
        event = JobEvent(job_id=job_id, state=state, message=message)
        self.session.add(event)
        self.session.flush()
        return event

    def update_job(self, job_id: str, **fields) -> Job | None:
        job = self.get_job(job_id)
        if job is None:
            return None
        _assert_mapped_fields(job, fields)
        for key, value in fields.items():
            setattr(job, key, value)
        self.session.add(job)
        self.session.flush()
        return job

    def enqueue_action(
        self,
        job_id: str,
        action_type: str,
        payload: dict | None = None,
        *,
        source_artifact_id: str | None = None,
    ) -> HostAction:
        """Queue a host action, optionally pinned to the evidence it analyses.

        ``source_artifact_id`` is keyword-only and defaults to null so every
        non-analysis caller keeps its existing behaviour.
        """
        action = HostAction(
            job_id=job_id,
            action_type=action_type,
            status="queued",
            payload=json.dumps(payload or {}),
            source_artifact_id=source_artifact_id,
        )
        self.session.add(action)
        self.session.flush()
        return action

    def get_action(self, action_id: int) -> HostAction | None:
        return self.session.get(HostAction, action_id)

    def compensate_dispatch_failure(self, job_id: str, action_id: int) -> bool:
        """Fence late deliveries before repairing a never-claimed dispatch intent.

        The conditional write serializes with worker claims. A broker exception
        alone cannot prove rejection: leave a claimed/completed action untouched.
        """
        now = datetime.now(UTC)
        changed = cast(
            CursorResult[Any],
            self.session.execute(
                update(HostAction)
                .where(
                    HostAction.id == action_id,
                    HostAction.job_id == job_id,
                    HostAction.status == "queued",
                    HostAction.started_at.is_(None),
                )
                .values(status="failed", error_summary="Task Queue Unavailable", finished_at=now)
            ),
        ).rowcount
        if not changed:
            return False
        self.session.expire_all()
        action = self.get_action(action_id)
        job = self.get_job(job_id)
        if job is not None and action is not None:
            if action.action_type in {"run_immediate_job", "run_post_script_job", "run_manual_trigger_job"}:
                failed = cast(
                    CursorResult[Any],
                    self.session.execute(
                        update(Job)
                        .where(
                            Job.id == job_id,
                            Job.status == "queued",
                            Job.started_at.is_(None),
                        )
                        .values(
                            status="failed",
                            current_stage="failed",
                            finished_at=now,
                            error_summary="Task Queue Unavailable",
                        )
                    ),
                ).rowcount
                if failed:
                    from ..jobs.capacity import confirm_cleanup

                    confirm_cleanup(self.session, job_id, never_started=True)
                    self.session.execute(
                        update(JobRuntime)
                        .where(
                            JobRuntime.job_id == job_id,
                            JobRuntime.state == "pending",
                        )
                        .values(state="destroyed", power_state="off")
                    )
            elif action.action_type == "acquire_memory_dump":
                # This inverse edge is permitted only after winning the action
                # fence; it is deliberately absent from the general state graph.
                self.session.execute(
                    update(Job)
                    .where(
                        Job.id == job_id,
                        Job.status == "capture_requested",
                    )
                    .values(status="waiting_for_capture", current_stage="waiting_for_capture")
                )
        self.session.expire_all()
        return True

    def get_next_queued_action(self) -> HostAction | None:
        query = select(HostAction).where(HostAction.status == "queued").order_by(HostAction.created_at.asc()).limit(1)
        return self.session.scalars(query).first()

    def list_actions(
        self,
        job_id: str | None = None,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[HostAction]:
        query = select(HostAction).order_by(HostAction.created_at.asc(), HostAction.id.asc())
        if job_id:
            query = query.where(HostAction.job_id == job_id)
        if offset:
            query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)
        return list(self.session.scalars(query))

    def update_action(self, action_id: int, **fields) -> HostAction | None:
        action = self.get_action(action_id)
        if action is None:
            return None
        _assert_mapped_fields(action, fields)
        for key, value in fields.items():
            setattr(action, key, value)
        self.session.add(action)
        self.session.flush()
        return action

    def create_isf_artifact(
        self,
        job_id: str,
        kernel_release: str,
        path: str,
        source: str,
        *,
        distro: str | None = None,
        version: str | None = None,
        sha256: str | None = None,
        build_id: str | None = None,
        validation_status: str = "valid",
        architecture: str = "x86_64",
    ) -> ISFArtifact:
        artifact = ISFArtifact(
            job_id=job_id,
            kernel_release=kernel_release,
            build_id=build_id,
            distro=distro,
            version=version,
            architecture=architecture,
            path=path,
            sha256=sha256,
            source=source,
            validation_status=validation_status,
        )
        self.session.add(artifact)
        self.session.flush()
        return artifact

    def get_latest_isf_artifact(self, job_id: str) -> ISFArtifact | None:
        query = (
            select(ISFArtifact)
            .where(ISFArtifact.job_id == job_id)
            .order_by(ISFArtifact.created_at.desc(), ISFArtifact.id.desc())
            .limit(1)
        )
        return self.session.scalars(query).first()

    def find_isf_by_build_id(self, build_id: str, architecture: str) -> ISFArtifact | None:
        query = (
            select(ISFArtifact)
            .where(
                ISFArtifact.build_id == build_id,
                ISFArtifact.architecture == architecture,
                ISFArtifact.validation_status == "valid",
            )
            .order_by(ISFArtifact.created_at.desc())
            .limit(1)
        )
        return self.session.scalars(query).first()

    def get_valid_isf_artifact(
        self,
        job_id: str,
        kernel_release: str,
        *,
        architecture: str = "x86_64",
    ) -> ISFArtifact | None:
        query = (
            select(ISFArtifact)
            .where(
                ISFArtifact.job_id == job_id,
                ISFArtifact.kernel_release == kernel_release,
                ISFArtifact.architecture == architecture,
                ISFArtifact.validation_status == "valid",
            )
            .order_by(ISFArtifact.created_at.desc())
            .limit(1)
        )
        return self.session.scalars(query).first()

    def find_valid_isf_by_kernel(
        self,
        kernel_release: str,
        *,
        architecture: str = "x86_64",
    ) -> ISFArtifact | None:
        """Find the newest valid ISF for a kernel release across jobs.

        An ISF is keyed by kernel build, not by VM, so a valid ISF for a given
        kernel/architecture resolves symbols for any dump of that kernel — even
        when the ISF was built under a separate (e.g. ISF-only) job. Also used
        by the symbol-cache soft-hit check to discover the build_id recorded for
        the currently cached ISF.
        """
        if not kernel_release:
            return None
        query = (
            select(ISFArtifact)
            .where(
                ISFArtifact.kernel_release == kernel_release,
                ISFArtifact.architecture == architecture,
                ISFArtifact.validation_status == "valid",
            )
            .order_by(ISFArtifact.created_at.desc(), ISFArtifact.id.desc())
            .limit(1)
        )
        return self.session.scalars(query).first()

    def get_job_known_build_id(
        self,
        job_id: str,
        *,
        architecture: str = "x86_64",
    ) -> str | None:
        """Return the most recent non-NULL build_id recorded for a job's kernel.

        Searches across all ISFArtifact rows for the job, preferring rows where
        build_id is not NULL.  Returns None if no such row exists.
        """
        query = (
            select(ISFArtifact.build_id)
            .where(
                ISFArtifact.job_id == job_id,
                ISFArtifact.architecture == architecture,
                ISFArtifact.build_id.is_not(None),
            )
            .order_by(ISFArtifact.created_at.desc())
            .limit(1)
        )
        return self.session.scalars(query).first()

    def resolve_isf_origin_source(self, artifact: ISFArtifact) -> str:
        """Resolve the original provenance of an ISF artifact.

        Cache hits are stamped source="cache", which erases whether the file
        was originally host-fetched (trusted) or built inside the live guest
        (low trust). When the artifact is a cache hit and carries a sha256,
        return the earliest non-cache source recorded for the same content
        hash; otherwise return the artifact's own source. cache_soft_hit rows
        resolve the same way: a content-hash match to a known origin is stronger
        evidence than the missing build_id, so the original source is returned
        when resolvable; otherwise "cache_soft_hit" surfaces in manifests as
        the honest provenance.
        """
        if artifact.source not in ("cache", "cache_soft_hit") or not artifact.sha256:
            return artifact.source
        query = (
            select(ISFArtifact.source)
            .where(
                ISFArtifact.sha256 == artifact.sha256,
                ISFArtifact.source.not_in(["cache", "cache_soft_hit"]),
            )
            .order_by(ISFArtifact.created_at.asc())
            .limit(1)
        )
        origin = self.session.scalars(query).first()
        return origin or artifact.source


def has_active_non_playground_job(session: Session) -> bool:
    """Return True if any non-terminal forensic job exists.

    Used by the playground orchestrator to guard against starting a
    playground session while a forensic capture is in progress. Playground
    sessions live in their own table, so every Job row is a forensic job.
    """
    from .models import CapacityReservation

    return (
        session.query(Job).filter(Job.status.not_in(list(JobRepository._TERMINAL_STATUSES))).count() > 0
        or session.query(CapacityReservation).filter(CapacityReservation.state == "reserved").count() > 0
        or session.query(JobRuntime).filter(JobRuntime.state.not_in(("destroyed", "pending"))).count() > 0
    )


def has_active_playground_session(session: Session) -> bool:
    """Block forensic admission during Playground activity, cleanup or reset.

    Package reset intent spans the gap between removing the old session and
    registering its replacement, so session rows alone cannot prove idleness.
    """
    from ..playground.repositories import ACTIVE_PLAYGROUND_STATES, CLEANUP_BACKLOG_STATES
    from .models import JobVM, LabInstance, PlaygroundSession

    return (
        session.query(PlaygroundSession.id)
        .filter(PlaygroundSession.status.in_(ACTIVE_PLAYGROUND_STATES + CLEANUP_BACKLOG_STATES))
        .first()
        is not None
        or session.query(LabInstance.id).filter(LabInstance.state == "resetting").first() is not None
        or session.query(JobVM.id).filter(JobVM.state != "destroyed").first() is not None
    )


class CaseRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_case(self, name: str) -> Case:
        case = Case(id=f"case-{uuid4().hex[:8]}", name=name, status="open")
        self.session.add(case)
        self.session.flush()
        return case

    def list_cases(self, *, limit: int | None = None, offset: int = 0) -> list[Case]:
        query = select(Case).order_by(Case.created_at.desc(), Case.id.desc())
        if offset:
            query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)
        return list(self.session.scalars(query))

    def get_case(self, case_id: str) -> Case | None:
        return self.session.get(Case, case_id)

    def update_case_status(self, case_id: str, status: str) -> Case:
        case = self.session.get(Case, case_id)
        if case is None:
            raise ValueError(f"Case {case_id!r} not found")
        case.status = status
        if status in {"closed", "sealed"}:
            case.closed_at = datetime.now(UTC)
        self.session.flush()
        return case


class ArtifactRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_artifact(
        self,
        *,
        kind: str,
        producer: str,
        path: str,
        filename: str | None = None,
        sha256: str | None = None,
        size_bytes: int | None = None,
        manifest_path: str | None = None,
        sha256_sidecar_path: str | None = None,
        metadata_json: str | None = None,
        case_id: str | None = None,
        job_id: str | None = None,
        session_id: str | None = None,
        acquisition_id: str | None = None,
    ) -> Artifact:
        from .models import JobVM, PlaygroundSession

        if job_id and session_id:
            raise ValueError("artifact must have only one owner")
        if job_id and self.session.get(Job, job_id) is None:
            raise ValueError("artifact job owner does not exist")
        owner_job = self.session.get(Job, job_id) if job_id else None
        if owner_job and owner_job.execution_mode == "artifact_analysis" and kind in {"memory", "disk"}:
            raise ValueError("analysis contexts cannot own capture artifacts")
        if session_id and self.session.get(PlaygroundSession, session_id) is None:
            raise ValueError("artifact session owner does not exist")
        if acquisition_id and session_id:
            acquisition = self.session.get(Acquisition, acquisition_id)
            if acquisition is None or acquisition.session_id != session_id:
                raise ValueError("artifact acquisition does not belong to session")
            vm = self.session.get(JobVM, acquisition.job_vm_id) if acquisition.job_vm_id else None
            if acquisition.job_vm_id and (vm is None or vm.session_id != session_id):
                raise ValueError("artifact acquisition VM does not belong to session")
            if case_id is not None and acquisition.case_id != case_id:
                raise ValueError("artifact case conflicts with acquisition")
            case_id = acquisition.case_id
        artifact = Artifact(
            id=f"art-{uuid4().hex[:8]}",
            kind=kind,
            producer=producer,
            path=path,
            filename=filename,
            sha256=sha256,
            size_bytes=size_bytes,
            manifest_path=manifest_path,
            sha256_sidecar_path=sha256_sidecar_path,
            metadata_json=metadata_json,
            case_id=case_id,
            job_id=job_id,
            session_id=session_id,
            acquisition_id=acquisition_id,
        )
        self.session.add(artifact)
        self.session.flush()
        return artifact

    def get_artifact(self, artifact_id: str) -> Artifact | None:
        return self.session.get(Artifact, artifact_id)

    def list_artifacts(
        self,
        *,
        case_id: str | None = None,
        job_id: str | None = None,
        acquisition_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Artifact]:
        query = select(Artifact)
        if case_id is not None:
            query = query.where(Artifact.case_id == case_id)
        if job_id is not None:
            query = query.where(Artifact.job_id == job_id)
        if acquisition_id is not None:
            query = query.where(Artifact.acquisition_id == acquisition_id)
        query = query.order_by(Artifact.created_at.desc(), Artifact.id.desc())
        if offset:
            query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)
        return list(self.session.scalars(query))


class ReportRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_report(
        self,
        *,
        report_type: str,
        status: str,
        path: str,
        title: str | None = None,
        summary: str | None = None,
        metadata_json: str | None = None,
        case_id: str | None = None,
        job_id: str | None = None,
        acquisition_id: str | None = None,
        input_artifact_id: str | None = None,
        output_artifact_id: str | None = None,
    ) -> Report:
        report = Report(
            id=f"rep-{uuid4().hex[:8]}",
            report_type=report_type,
            status=status,
            path=path,
            title=title,
            summary=summary,
            metadata_json=metadata_json,
            case_id=case_id,
            job_id=job_id,
            acquisition_id=acquisition_id,
            input_artifact_id=input_artifact_id,
            output_artifact_id=output_artifact_id,
        )
        self.session.add(report)
        self.session.flush()
        return report

    def get_report(self, report_id: str) -> Report | None:
        return self.session.get(Report, report_id)

    def list_reports(
        self,
        *,
        case_id: str | None = None,
        job_id: str | None = None,
        acquisition_id: str | None = None,
        input_artifact_id: str | None = None,
        output_artifact_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Report]:
        query = select(Report)
        if case_id is not None:
            query = query.where(Report.case_id == case_id)
        if job_id is not None:
            query = query.where(Report.job_id == job_id)
        if acquisition_id is not None:
            query = query.where(Report.acquisition_id == acquisition_id)
        if input_artifact_id is not None:
            query = query.where(Report.input_artifact_id == input_artifact_id)
        if output_artifact_id is not None:
            query = query.where(Report.output_artifact_id == output_artifact_id)
        query = query.order_by(Report.created_at.desc(), Report.id.desc())
        if offset:
            query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)
        return list(self.session.scalars(query))


class AcquisitionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_acquisition(self, name: str, case_id: str | None = None) -> Acquisition:
        acq = Acquisition(id=f"acq-{uuid4().hex[:8]}", name=name, status="live", case_id=case_id)
        self.session.add(acq)
        self.session.flush()
        return acq

    def list_acquisitions(self, *, limit: int | None = None, offset: int = 0) -> list[Acquisition]:
        query = select(Acquisition).order_by(Acquisition.created_at.desc(), Acquisition.id.desc())
        if offset:
            query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)
        return list(self.session.scalars(query))

    def list_acquisitions_with_job_counts(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[tuple[Acquisition, int, int]]:
        live_job = sql_case((Job.status.in_(list(FORENSIC_LIVE_STATUSES)), 1), else_=None)
        query = (
            select(Acquisition, func.count(Job.id), func.count(live_job))
            .outerjoin(Job, Job.acquisition_id == Acquisition.id)
            .group_by(Acquisition.id)
            .order_by(Acquisition.created_at.desc(), Acquisition.id.desc())
        )
        if offset:
            query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)
        return [
            (acquisition, int(job_count), int(live_job_count))
            for acquisition, job_count, live_job_count in self.session.execute(query)
        ]

    def get_acquisition(self, acquisition_id: str) -> Acquisition | None:
        return self.session.get(Acquisition, acquisition_id)

    def recover_playground_capture(
        self,
        *,
        acquisition_id: str,
        session_id: str,
        kind: str,
        path: str,
        sha256: str,
        size_bytes: int,
    ) -> Literal["completed", "already_completed", "conflict"]:
        """Seal verified capture metadata in the publication caller's transaction."""
        from .models import AcquisitionEvent
        from .transactions import reserve_sqlite_write

        reserve_sqlite_write(self.session)
        acquisition = self.session.get(Acquisition, acquisition_id, populate_existing=True)
        if acquisition is None or acquisition.session_id != session_id or acquisition.kind != kind:
            return "conflict"
        result = {"path": path, "sha256": sha256, "size_bytes": size_bytes}
        if acquisition.status == "completed":
            if acquisition.sealed_at is not None and all(
                getattr(acquisition, key) == value for key, value in result.items()
            ):
                return "already_completed"
            return "conflict"
        if acquisition.status != "running" or acquisition.sealed_at is not None:
            return "conflict"
        if any(getattr(acquisition, key) not in (None, value) for key, value in result.items()):
            return "conflict"
        changed = self.session.execute(
            update(Acquisition)
            .where(
                Acquisition.id == acquisition_id,
                Acquisition.session_id == session_id,
                Acquisition.kind == kind,
                Acquisition.status == "running",
                Acquisition.sealed_at.is_(None),
                or_(Acquisition.path.is_(None), Acquisition.path == path),
                or_(Acquisition.sha256.is_(None), Acquisition.sha256 == sha256),
                or_(Acquisition.size_bytes.is_(None), Acquisition.size_bytes == size_bytes),
            )
            .values(status="completed", sealed_at=datetime.now(UTC), path=path, sha256=sha256, size_bytes=size_bytes)
            .execution_options(synchronize_session=False)
        )
        if cast(CursorResult[Any], changed).rowcount != 1:
            self.session.refresh(acquisition)
            return "conflict"
        self.session.add(
            AcquisitionEvent(
                acquisition_id=acquisition_id,
                status="completed",
                message=f"Recovered interrupted capture: {acquisition.name}",
            )
        )
        self.session.flush()
        self.session.refresh(acquisition)
        return "completed"

    def update_acquisition_status(self, acquisition_id: str, status: str) -> Acquisition:
        acq = self.session.get(Acquisition, acquisition_id)
        if acq is None:
            raise ValueError(f"Acquisition {acquisition_id!r} not found")
        acq.status = status
        if status == "sealed":
            acq.sealed_at = datetime.now(UTC)
        self.session.flush()
        return acq


class AppSettingsRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, key: str) -> str | None:
        setting = self.session.get(AppSetting, key)
        return setting.value if setting is not None else None

    def set(self, key: str, value: str) -> AppSetting:
        setting = self.session.get(AppSetting, key)
        if setting is None:
            setting = AppSetting(key=key, value=value, updated_at=datetime.now(UTC))
        else:
            setting.value = value
            setting.updated_at = datetime.now(UTC)
        self.session.add(setting)
        self.session.flush()
        return setting
