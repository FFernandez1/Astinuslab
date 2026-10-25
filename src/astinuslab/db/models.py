from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    event,
)
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column, object_session, relationship

from . import Base


class AdminBrowserSession(Base):
    __tablename__ = "admin_browser_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    admin_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)


class Case(Base):
    __tablename__ = "cases"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Acquisition(Base):
    __tablename__ = "acquisitions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="live")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id", ondelete="SET NULL"), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vm_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    job_vm_id: Mapped[str | None] = mapped_column(
        ForeignKey("job_vms.id", ondelete="SET NULL", use_alter=True, name="fk_acquisitions_job_vm_id"),
        nullable=True,
    )

    jobs: Mapped[list[Job]] = relationship(back_populates="acquisition")


class AcquisitionEvent(Base):
    __tablename__ = "acquisition_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    acquisition_id: Mapped[str] = mapped_column(ForeignKey("acquisitions.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (Index("ix_acquisition_events_acquisition_id", "acquisition_id"),)


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id", ondelete="SET NULL"), nullable=True)
    # Historical mixed values are reconciled by a039. New writes use one
    # explicit owner; job_id remains additive-migration compatible.
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("playground_sessions.id", ondelete="RESTRICT"), nullable=True
    )
    ownership_reconciliation_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    acquisition_id: Mapped[str | None] = mapped_column(
        ForeignKey("acquisitions.id", ondelete="SET NULL"), nullable=True
    )
    producer: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    manifest_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256_sidecar_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (
        Index("ix_artifacts_case_id", "case_id"),
        Index("ix_artifacts_job_id", "job_id"),
        Index("ix_artifacts_session_id", "session_id"),
        Index("ix_artifacts_acquisition_id", "acquisition_id"),
        Index("ix_artifacts_kind", "kind"),
    )


# Publication journal states. A capture writes bytes, a manifest, a sidecar and a
# database row; the journal records which of those have happened so a crash between
# any two is recoverable instead of merely visible.
PUBLICATION_STATES = (
    "reserved",
    "capturing",
    "staged",
    "db_prepared",
    "published",
    "completed",
    "failed",
    "cleanup_incomplete",
)
# States that still own their destination path. A failed or cleanup_incomplete
# attempt has released it, so a retry can reserve the same final_path.
LIVE_PUBLICATION_STATES = (
    "reserved",
    "capturing",
    "staged",
    "db_prepared",
    "published",
    "completed",
)
# Terminal states stamp completed_at. `published` is not terminal: the bytes are in
# place but the database side of the triad is not yet finished.
TERMINAL_PUBLICATION_STATES = ("completed", "failed", "cleanup_incomplete")


class ArtifactPublicationAttempt(Base):
    __tablename__ = "artifact_publication_attempts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id", ondelete="SET NULL"), nullable=True)
    acquisition_id: Mapped[str | None] = mapped_column(
        ForeignKey("acquisitions.id", ondelete="SET NULL"), nullable=True
    )
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    producer: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    staging_dir: Mapped[str] = mapped_column(Text, nullable=False)
    final_path: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256_sidecar_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="reserved")
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "state IN (" + ", ".join(f"'{state}'" for state in PUBLICATION_STATES) + ")",
            name="ck_artifact_publication_attempts_state",
        ),
        # Partial unique index: two producers must never both believe they own the
        # same destination, but a dead attempt must not poison that path forever.
        Index(
            "ux_artifact_publication_attempts_live_final_path",
            "final_path",
            unique=True,
            sqlite_where=sql_text("state IN (" + ", ".join(f"'{state}'" for state in LIVE_PUBLICATION_STATES) + ")"),
        ),
        Index("ix_artifact_publication_attempts_state", "state"),
        Index("ix_artifact_publication_attempts_job_id", "job_id"),
        Index("ix_artifact_publication_attempts_acquisition_id", "acquisition_id"),
    )


class EvidenceTombstone(Base):
    """What was destroyed, by whom, and why — written before the bytes go.

    A deleted artifact leaves no trace in the artifacts table by definition, so
    the custody record has to outlive it. Written in the same transaction that
    drops the Artifact row, and before any unlink, so a crash mid-deletion still
    leaves an operator something that names the files.
    """

    __tablename__ = "evidence_tombstones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    artifact_id: Mapped[str] = mapped_column(String(64), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    producer: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    acquisition_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    case_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    file_set: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    forced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    protection_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    cleanup_state: Mapped[str] = mapped_column(String(16), nullable=False, default="legacy", server_default="legacy")
    cleanup_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (
        Index("ix_evidence_tombstones_artifact_id", "artifact_id"),
        Index("ix_evidence_tombstones_job_id", "job_id"),
    )


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id", ondelete="SET NULL"), nullable=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=True)
    acquisition_id: Mapped[str | None] = mapped_column(
        ForeignKey("acquisitions.id", ondelete="SET NULL"), nullable=True
    )
    input_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"), nullable=True
    )
    output_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"), nullable=True
    )
    report_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="completed")
    path: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (
        Index("ix_reports_case_id", "case_id"),
        Index("ix_reports_job_id", "job_id"),
        Index("ix_reports_acquisition_id", "acquisition_id"),
        Index("ix_reports_input_artifact_id", "input_artifact_id"),
        Index("ix_reports_output_artifact_id", "output_artifact_id"),
    )


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"), nullable=True
    )
    analysis_context_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    scenario_id: Mapped[str] = mapped_column(String(128), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(64), nullable=False)
    image_mode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    image_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="queued")
    capacity_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    guest_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detected_distro: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detected_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detected_kernel_release: Mapped[str | None] = mapped_column(String(255), nullable=True)
    detected_architecture: Mapped[str | None] = mapped_column(String(64), nullable=True)
    capture_type: Mapped[str] = mapped_column(String(32), nullable=False, default="memory")
    custom_script: Mapped[str | None] = mapped_column(Text, nullable=True)
    vm_vcpus: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vm_memory_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vm_disk_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attacker_vm_vcpus: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attacker_vm_memory_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attacker_vm_disk_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    workload_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    workload_provisioned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    capture_wait_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lab_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    lab_snapshot_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    network_isolated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    attacker_vm_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attacker_vm_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attacker_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exploit_tags: Mapped[str | None] = mapped_column(Text, nullable=True)  # comma-separated
    exploit_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    build_vol3_isf: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    acquisition_id: Mapped[str | None] = mapped_column(
        ForeignKey("acquisitions.id", ondelete="SET NULL"), nullable=True
    )
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    current_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Indexes for the two most-queried columns:
    #   - status: filtered in list_orphaned_jobs() on every Celery worker start
    #   - created_at: default sort key in list_jobs()
    # Without these, both queries do full table scans as the job history grows.
    __table_args__ = (
        Index("ix_jobs_status", "status"),
        Index("ux_jobs_source_artifact_id", "source_artifact_id", unique=True),
        Index("ix_jobs_created_at", "created_at"),
        Index("ix_jobs_acquisition_id", "acquisition_id"),
    )

    events: Mapped[list[JobEvent]] = relationship(back_populates="job", cascade="all, delete-orphan")
    actions: Mapped[list[HostAction]] = relationship(back_populates="job", cascade="all, delete-orphan")
    runtimes: Mapped[list[JobRuntime]] = relationship(back_populates="job", cascade="all, delete-orphan")
    isf_artifacts: Mapped[list[ISFArtifact]] = relationship(back_populates="job", cascade="all, delete-orphan")
    acquisition: Mapped[Acquisition | None] = relationship(back_populates="jobs")

    # Dump metadata lives in the Artifact table (single source of truth,
    # written at capture time). These read-through properties keep the
    # historical Job attribute names working for the API (JobOut), templates,
    # and services. They memoize per instance; list endpoints should seed the
    # memo with JobRepository.prefetch_latest_artifacts() to avoid N+1 queries.
    def _latest_artifact(self, kind: str) -> Artifact | None:
        cache = self.__dict__.setdefault("_artifact_memo", {})
        if kind in cache:
            return cache[kind]
        session = object_session(self)
        if session is None:
            return None
        if self.execution_mode == "artifact_analysis":
            source = session.get(Artifact, self.source_artifact_id) if self.source_artifact_id else None
            return source if source is not None and source.kind == kind else None
        artifact = (
            session.query(Artifact)
            .filter(Artifact.job_id == self.id, Artifact.kind == kind)
            .order_by(Artifact.created_at.desc())
            .first()
        )
        cache[kind] = artifact
        return artifact

    @property
    def dump_path(self) -> str | None:
        artifact = self._latest_artifact("memory")
        return artifact.path if artifact else None

    @property
    def dump_filename(self) -> str | None:
        artifact = self._latest_artifact("memory")
        return artifact.filename if artifact else None

    @property
    def dump_checksum(self) -> str | None:
        artifact = self._latest_artifact("memory")
        return artifact.sha256 if artifact else None

    @property
    def dump_size_bytes(self) -> int | None:
        artifact = self._latest_artifact("memory")
        return artifact.size_bytes if artifact else None

    @property
    def disk_dump_path(self) -> str | None:
        artifact = self._latest_artifact("disk")
        return artifact.path if artifact else None

    @property
    def disk_dump_filename(self) -> str | None:
        artifact = self._latest_artifact("disk")
        return artifact.filename if artifact else None

    @property
    def disk_dump_checksum(self) -> str | None:
        artifact = self._latest_artifact("disk")
        return artifact.sha256 if artifact else None

    @property
    def disk_dump_size_bytes(self) -> int | None:
        artifact = self._latest_artifact("disk")
        return artifact.size_bytes if artifact else None


class PlaygroundSession(Base):
    """A Playground lab session (own lifecycle, separate from forensic jobs).

    Historically playground sessions were stored as ``Job`` rows with sentinel
    ``target_id="__playground__"``. They now live in their own table; child
    resources (JobVM, JobNetwork, PlaygroundTopologyLayout) keep their
    ``job_id`` column name for migration compatibility, but for playground
    rows it holds a ``playground_sessions.id`` (``pg-<hex>``).
    """

    __tablename__ = "playground_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Nullable: ordinary authored sessions are not created from a package.
    # Kept separate from the canonical package id because package identity is
    # ``package_id@version`` while this is a local immutable record reference.
    lab_package_record_id: Mapped[str | None] = mapped_column(
        ForeignKey("lab_packages.id", ondelete="SET NULL", use_alter=True, name="fk_playground_sessions_lab_package"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="running")
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # At most one session may be in an active state at a time. The unique
    # index over the constant expression (1) applies only to rows matched by
    # the partial WHERE clause, so concurrent creators race on an
    # IntegrityError which the API maps to 409.
    __table_args__ = (
        Index(
            "ux_playground_sessions_one_active",
            sql_text("(1)"),
            unique=True,
            sqlite_where=sql_text(
                "status IN ('pending', 'queued', 'running', 'cancel_requested', "
                "'destroy_requested', 'destroying', 'destroy_failed', 'failure_cleanup', "
                "'authoring_closed')"
            ),
        ),
    )


class JobEvent(Base):
    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    state: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))

    job: Mapped[Job] = relationship(back_populates="events")


class HostAction(Base):
    __tablename__ = "host_actions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="queued")
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dispatch_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    owner_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The memory artifact an analysis action was queued against. Null for every
    # non-analysis action, and cleared when the artifact is deleted so terminal
    # actions are retained; the payload keeps the historical identity snapshot.
    source_artifact_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("artifacts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    active_pid: Mapped[int | None] = mapped_column(nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    job: Mapped[Job] = relationship(back_populates="actions")


class JobRuntime(Base):
    __tablename__ = "job_runtimes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    vm_name: Mapped[str] = mapped_column(String(128), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    power_state: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    snapshot_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    capture_memory_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    capture_disk_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))

    job: Mapped[Job] = relationship(back_populates="runtimes")


class ISFArtifact(Base):
    __tablename__ = "isf_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    kernel_release: Mapped[str] = mapped_column(String(255), nullable=False)
    # Build ID extracted from vmlinux (readelf -n) or from /sys/kernel/notes inside the guest.
    # Nullable for backward compatibility with rows created before F02 review fix.
    build_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    distro: Mapped[str | None] = mapped_column(String(64), nullable=True)
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    architecture: Mapped[str] = mapped_column(String(16), nullable=False, default="x86_64")
    path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # "host_fetch", "live_guest", "cache"
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False, default="valid")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))

    job: Mapped[Job] = relationship(back_populates="isf_artifacts")


class JobVM(Base):
    __tablename__ = "job_vms"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Playground-owned session foreign key; renamed from historical job_id in revision a017_session_ids.
    session_id: Mapped[str] = mapped_column(ForeignKey("playground_sessions.id", ondelete="CASCADE"), nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    image_id: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="target", server_default="target")
    workload_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    workload_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    workload_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    domain_name: Mapped[str] = mapped_column(String(128), nullable=False)
    disk_path: Mapped[str] = mapped_column(Text, nullable=False)
    vcpus: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memory_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    disk_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        Index("ix_job_vms_session_id", "session_id"),
        Index("ix_job_vms_state", "state"),
        Index("ux_job_vms_session_id_label", "session_id", "label", unique=True),
    )

    playground_session: Mapped[PlaygroundSession] = relationship()
    nics: Mapped[list[JobVMNic]] = relationship(
        back_populates="vm", cascade="all, delete-orphan", order_by="JobVMNic.ordinal"
    )


class JobNetwork(Base):
    __tablename__ = "job_networks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Playground-owned session foreign key; renamed from historical job_id in revision a017_session_ids.
    session_id: Mapped[str] = mapped_column(ForeignKey("playground_sessions.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    subnet_cidr: Mapped[str] = mapped_column(String(64), nullable=False)
    isolated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    libvirt_net_name: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # A name is unique among the networks a session still holds. Destroyed rows
    # stay for the audit trail but leave the index, so a session may reuse the
    # name of a network it tore down - matching create_network, which has always
    # compared a requested name against non-destroyed rows only.
    __table_args__ = (
        Index("ix_job_networks_session_id", "session_id"),
        Index(
            "ux_job_networks_session_id_name",
            "session_id",
            "name",
            unique=True,
            sqlite_where=sql_text("state != 'destroyed'"),
        ),
    )

    playground_session: Mapped[PlaygroundSession] = relationship()


class PlaygroundTopologyLayout(Base):
    __tablename__ = "playground_topology_layout"

    # Playground-owned session foreign key; renamed from historical job_id in revision a017_session_ids.
    session_id: Mapped[str] = mapped_column(ForeignKey("playground_sessions.id", ondelete="CASCADE"), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    node_type: Mapped[str] = mapped_column(String(32), nullable=False)
    x: Mapped[float] = mapped_column(Float, nullable=False)
    y: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    __table_args__ = (Index("ix_playground_topology_layout_session_id", "session_id"),)

    playground_session: Mapped[PlaygroundSession] = relationship()


class JobVMNic(Base):
    __tablename__ = "job_vm_nics"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_vm_id: Mapped[str] = mapped_column(ForeignKey("job_vms.id", ondelete="CASCADE"), nullable=False)
    job_network_id: Mapped[str] = mapped_column(ForeignKey("job_networks.id", ondelete="CASCADE"), nullable=False)
    mac: Mapped[str] = mapped_column(String(32), nullable=False)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # This is a guest-visible interface identity, not a display sort key.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    # Detach requests are queued before the Celery task is enqueued.  The
    # token and lease make the queued -> host-mutating transition exclusive
    # across duplicate deliveries and restart recovery.
    detach_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detach_claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (
        Index("ix_job_vm_nics_vm", "job_vm_id"),
        Index("ix_job_vm_nics_net", "job_network_id"),
        Index("ix_job_vm_nics_state", "state"),
        Index("ux_job_vm_nics_mac", "mac", unique=True),
        Index("ux_job_vm_nics_vm_ordinal", "job_vm_id", "ordinal", unique=True),
    )

    vm: Mapped[JobVM] = relationship(back_populates="nics")
    network: Mapped[JobNetwork] = relationship()


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))


class LabPackage(Base):
    """An inert, locally retained portable lab package.

    The archive and canonical documents are immutable once registered. Runtime
    Playground identities deliberately live in their own tables; this row only
    makes a shared package discoverable and restart-safe on the local host.
    """

    __tablename__ = "lab_packages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    package_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    creator: Mapped[str] = mapped_column(String(160), nullable=False)
    license: Mapped[str] = mapped_column(String(256), nullable=False)
    reproducibility: Mapped[str] = mapped_column(String(16), nullable=False)
    archive_path: Mapped[str] = mapped_column(Text, nullable=False)
    archive_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    archive_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    definition_json: Mapped[str] = mapped_column(Text, nullable=False)
    required_capabilities_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    signature_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="unsigned", server_default="unsigned"
    )
    creator_key_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ux_lab_packages_package_id_version", "package_id", "version", unique=True),
        Index("ix_lab_packages_title", "title"),
        Index("ix_lab_packages_signature_status", "signature_status"),
        CheckConstraint("archive_size_bytes > 0", name="ck_lab_packages_archive_size_positive"),
        CheckConstraint("reproducibility IN ('locked', 'unlocked')", name="ck_lab_packages_reproducibility"),
        CheckConstraint(
            "signature_status IN ('unsigned', 'invalid-signature', 'valid-untrusted-key', 'valid-locally-trusted-key')",
            name="ck_lab_packages_signature_status",
        ),
    )


_LAB_PACKAGE_IMMUTABLE_FIELDS = (
    "id",
    "package_id",
    "version",
    "title",
    "description",
    "creator",
    "license",
    "reproducibility",
    "archive_path",
    "archive_sha256",
    "archive_size_bytes",
    "manifest_sha256",
    "manifest_json",
    "definition_json",
    "required_capabilities_json",
    "signature_status",
    "creator_key_fingerprint",
    "imported_at",
)


@event.listens_for(LabPackage, "before_update")
def _reject_lab_package_mutation(_mapper, _connection, target: LabPackage) -> None:
    state = sqlalchemy_inspect(target)
    changed = [name for name in _LAB_PACKAGE_IMMUTABLE_FIELDS if state.attrs[name].history.has_changes()]
    if changed:
        raise ValueError(f"lab package records are immutable; attempted to change: {', '.join(changed)}")


# Package storage is deliberately journaled independently of the immutable
# package record.  A package import writes bytes and several rows; the records
# below make each interruption observable and recoverable rather than leaving
# an unowned file in the local store.
PACKAGE_PUBLICATION_STATES = ("reserved", "published", "completed", "failed", "cleanup_incomplete")
PACKAGE_LIVE_PUBLICATION_STATES = ("reserved", "published", "completed")
PACKAGE_INSPECTION_STATES = ("reserved", "staged", "committed", "expired", "failed")
PACKAGE_INSTANCE_STATES = (
    "registered",
    "creating",
    "ready",
    "failed",
    "cancel_requested",
    "rollback_pending",
    "cleanup_incomplete",
    "resetting",
    "destroying",
    "destroyed",
)


class LabContentObject(Base):
    __tablename__ = "lab_content_objects"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    filesystem_fingerprint: Mapped[str | None] = mapped_column(String(256), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint("size_bytes > 0", name="ck_lab_content_objects_size_positive"),
        Index("ix_lab_content_objects_last_used_at", "last_used_at"),
    )


class LabContentPublicationAttempt(Base):
    """Durable ownership of an individual content-store publication.

    Content-addressed bytes may be atomically published before an enclosing
    package transaction can reference them.  This journal names the bytes
    first, so startup reconciliation can adopt or quarantine them safely.
    """

    __tablename__ = "lab_content_publication_attempts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    final_path: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="reserved")
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "state IN ('reserved', 'published', 'completed', 'failed', 'quarantined')",
            name="ck_lab_content_publications_state",
        ),
        Index(
            "ux_lab_content_publications_digest_live",
            "sha256",
            "size_bytes",
            unique=True,
            sqlite_where=sql_text("state IN ('reserved', 'published')"),
        ),
        Index("ix_lab_content_publications_state", "state"),
    )


class LabPackageAsset(Base):
    __tablename__ = "lab_package_assets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    package_record_id: Mapped[str] = mapped_column(ForeignKey("lab_packages.id", ondelete="CASCADE"), nullable=False)
    logical_path: Mapped[str] = mapped_column(String(512), nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False, default="asset")
    expected_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str] = mapped_column(String(256), nullable=False)
    origin: Mapped[str] = mapped_column(String(32), nullable=False, default="embedded")
    source_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Resolution is deliberately separate from ``origin``.  ``origin`` is
    # provenance declared by the package, while these fields describe the
    # last verified materialisation on this host.  A missing/failed asset can
    # therefore be retried without ever treating a partial file as content.
    prepare_state: Mapped[str] = mapped_column(String(32), nullable=False, default="missing", server_default="missing")
    prepare_attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prepare_error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    prepared_origin: Mapped[str | None] = mapped_column(String(32), nullable=True)
    prepared_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (
        ForeignKeyConstraint(
            ["content_sha256", "content_size_bytes"],
            ["lab_content_objects.sha256", "lab_content_objects.size_bytes"],
            ondelete="RESTRICT",
        ),
        Index("ux_lab_package_assets_path_role", "package_record_id", "logical_path", "role", unique=True),
        Index("ix_lab_package_assets_content", "content_sha256", "content_size_bytes"),
        CheckConstraint("expected_size_bytes > 0", name="ck_lab_package_assets_expected_size_positive"),
        CheckConstraint(
            "(content_sha256 IS NULL AND content_size_bytes IS NULL) OR "
            "(content_sha256 IS NOT NULL AND content_size_bytes IS NOT NULL)",
            name="ck_lab_package_assets_content_pair",
        ),
        CheckConstraint(
            "content_sha256 IS NULL OR (content_sha256 = expected_sha256 AND content_size_bytes = expected_size_bytes)",
            name="ck_lab_package_assets_content_matches_expected",
        ),
        CheckConstraint(
            "prepare_state IN ('missing', 'resolving', 'ready', 'failed', 'cancelled')",
            name="ck_lab_package_assets_prepare_state",
        ),
    )


class LabPackageInspection(Base):
    __tablename__ = "lab_package_inspections"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    staged_path: Mapped[str] = mapped_column(Text, nullable=False)
    archive_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    archive_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    inspection_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    signature_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    creator_key_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="reserved")
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (
        CheckConstraint(
            "state IN (" + ", ".join(f"'{state}'" for state in PACKAGE_INSPECTION_STATES) + ")",
            name="ck_lab_package_inspections_state",
        ),
        Index("ix_lab_package_inspections_expiry", "expires_at"),
        Index("ix_lab_package_inspections_state", "state"),
    )


class LabPackagePublicationAttempt(Base):
    __tablename__ = "lab_package_publication_attempts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    inspection_id: Mapped[str | None] = mapped_column(
        ForeignKey("lab_package_inspections.id", ondelete="SET NULL"), nullable=True
    )
    package_record_id: Mapped[str | None] = mapped_column(
        ForeignKey("lab_packages.id", ondelete="SET NULL"), nullable=True
    )
    # Reserve this canonical identity before the final archive name is made
    # visible.  Digest/path locks alone allow two conflicting versions to race.
    package_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(128), nullable=False)
    archive_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    staging_path: Mapped[str] = mapped_column(Text, nullable=False)
    final_path: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="reserved")
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "state IN (" + ", ".join(f"'{state}'" for state in PACKAGE_PUBLICATION_STATES) + ")",
            name="ck_lab_package_publications_state",
        ),
        Index(
            "ux_lab_package_publications_live_final",
            "final_path",
            unique=True,
            sqlite_where=sql_text("state IN ('reserved', 'published', 'completed')"),
        ),
        # One canonical identity has one durable journal owner from reservation
        # through its permanent completed ledger.  Failed attempts alone may
        # release it for a retry; disjoint partial indexes leave a race gap.
        Index(
            "ux_lab_package_publications_identity_nonfailed",
            "package_id",
            "version",
            unique=True,
            sqlite_where=sql_text("state != 'failed'"),
        ),
        Index("ix_lab_package_publications_state", "state"),
    )


class LabPackageExportAttempt(Base):
    __tablename__ = "lab_package_export_attempts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    package_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(128), nullable=False)
    definition_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    staging_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    archive_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="reserved")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "state IN ('reserved', 'published', 'completed', 'failed')", name="ck_lab_package_exports_state"
        ),
        CheckConstraint(
            "state != 'completed' OR archive_sha256 IS NOT NULL",
            name="ck_lab_package_exports_completed_digest",
        ),
        Index(
            "ux_lab_package_exports_live_identity",
            "package_id",
            "version",
            unique=True,
            sqlite_where=sql_text("state IN ('reserved', 'published')"),
        ),
        Index(
            "ux_lab_package_exports_final_identity",
            "package_id",
            "version",
            unique=True,
            sqlite_where=sql_text("state = 'completed'"),
        ),
    )


class LabDraft(Base):
    __tablename__ = "lab_drafts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    definition_json: Mapped[str] = mapped_column(Text, nullable=False)
    definition_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # The definition digest is deliberately accompanied by the authored
    # experience inputs.  A verification must run from the exact saved draft,
    # never from whatever happens to be in the singleton Playground later.
    runbook: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    supporting_files_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}", server_default="{}")
    authored_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))


class LabInstance(Base):
    __tablename__ = "lab_instances"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    package_record_id: Mapped[str] = mapped_column(ForeignKey("lab_packages.id", ondelete="RESTRICT"), nullable=False)
    playground_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("playground_sessions.id", ondelete="SET NULL"), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="registered")
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (
        CheckConstraint(
            "state IN (" + ", ".join(f"'{state}'" for state in PACKAGE_INSTANCE_STATES) + ")",
            name="ck_lab_instances_state",
        ),
        Index("ux_lab_instances_package_idempotency", "package_record_id", "idempotency_key", unique=True),
        Index("ix_lab_instances_package_state", "package_record_id", "state"),
    )


class LabInstanceAction(Base):
    __tablename__ = "lab_instance_actions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    instance_id: Mapped[str] = mapped_column(ForeignKey("lab_instances.id", ondelete="CASCADE"), nullable=False)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (Index("ix_lab_instance_actions_instance", "instance_id"),)


class LabOperationOutbox(Base):
    __tablename__ = "lab_operation_outbox"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    operation_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}", server_default="{}")
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint("state IN ('pending', 'claimed', 'completed', 'failed')", name="ck_lab_operation_outbox_state"),
        Index("ux_lab_operation_outbox_kind_operation", "operation_kind", "operation_id", unique=True),
        Index("ix_lab_operation_outbox_state", "state"),
    )


class LabPackageAttestation(Base):
    __tablename__ = "lab_package_attestations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    input_draft_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    locked_definition_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    input_digests_json: Mapped[str] = mapped_column(Text, nullable=False)
    runtime_json: Mapped[str] = mapped_column(Text, nullable=False)
    result_json: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (Index("ix_lab_package_attestations_definition", "locked_definition_sha256"),)


class LabVerificationAttempt(Base):
    __tablename__ = "lab_verification_attempts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    draft_id: Mapped[str] = mapped_column(ForeignKey("lab_drafts.id", ondelete="RESTRICT"), nullable=False)
    # The frozen authoring session this attempt owns. A draft is deduplicated
    # by snapshot digest and can outlive several authoring sessions, so the
    # binding that startup reconciliation follows has to live on the attempt.
    authored_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    definition_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="reserved")
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (
        CheckConstraint(
            "state IN ('reserved', 'running', 'verified', 'failed', 'cancelled')",
            name="ck_lab_verification_attempts_state",
        ),
        Index("ix_lab_verification_attempts_draft", "draft_id"),
        # One frozen authoring session can back exactly one verification.  The
        # nullable column keeps legacy/sessionless attempts valid while the
        # unique index is the durable guard behind start-verification retries.
        Index("ux_lab_verification_attempts_authored_session", "authored_session_id", unique=True),
        Index(
            "ux_lab_verification_attempts_draft_live",
            "draft_id",
            unique=True,
            # A digest-identical draft may be verified again from a later
            # authored session. Only work that can still mutate is singleton;
            # historical verified attempts retain their session-bound result.
            sqlite_where=sql_text("state IN ('reserved', 'running')"),
        ),
    )


class LabPrepareAttempt(Base):
    __tablename__ = "lab_prepare_attempts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    package_record_id: Mapped[str] = mapped_column(ForeignKey("lab_packages.id", ondelete="CASCADE"), nullable=False)
    definition_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="reserved")
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, default=lambda: f"legacy-{uuid4().hex}")
    progress_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    execution_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    network_opt_in: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (
        CheckConstraint(
            "state IN ('reserved', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_lab_prepare_attempts_state",
        ),
        CheckConstraint("progress_percent >= 0 AND progress_percent <= 100", name="ck_lab_prepare_attempts_progress"),
        Index("ix_lab_prepare_attempts_package", "package_record_id"),
        Index("ux_lab_prepare_attempts_package_idempotency", "package_record_id", "idempotency_key", unique=True),
        Index("ix_lab_prepare_attempts_state", "state"),
    )


class LabStorageLease(Base):
    __tablename__ = "lab_storage_leases"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    resource_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    operation_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (
        Index("ux_lab_storage_leases_resource_owner", "resource_kind", "resource_id", "owner_id", unique=True),
        Index("ix_lab_storage_leases_expiry", "expires_at"),
    )


class LabPackageTombstone(Base):
    __tablename__ = "lab_package_tombstones"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    package_record_id: Mapped[str] = mapped_column(String(64), nullable=False)
    archive_path: Mapped[str] = mapped_column(Text, nullable=False)
    archive_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (Index("ix_lab_package_tombstones_state", "state"),)


class CapacityReservation(Base):
    __tablename__ = "capacity_reservations"

    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="RESTRICT"), primary_key=True)
    vm_count: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="waiting", index=True)
    preserve_failed_vm: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    boot_started: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
