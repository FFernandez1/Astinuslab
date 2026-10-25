from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator, model_validator

from ..playground.resources import (
    DISK_GB_MAX,
    DISK_GB_MIN,
    MEMORY_MB_MAX,
    MEMORY_MB_MIN,
    VCPUS_MAX,
    VCPUS_MIN,
)

_PositiveInt = Annotated[int, Field(ge=1)]

CaptureType = Literal["memory", "disk", "both", "none"]


def _images_dir() -> Path:
    from ..core.config import settings

    return Path(settings.images_dir).resolve()


def _validate_image_path_in_images_dir(value: str | None) -> str | None:
    if value is None:
        return None

    candidate = Path(value).resolve()
    images_dir = _images_dir()
    try:
        candidate.relative_to(images_dir)
    except ValueError as exc:
        raise ValueError(
            f"image_path must be inside the configured images_dir ({images_dir}); got {candidate}"
        ) from exc
    return str(candidate)


class TargetOut(BaseModel):
    id: str
    distro: str
    version: str
    family: str | None = None
    enabled: bool
    image_available: bool = True
    source_type: str
    supports_cloud_image: bool
    supports_iso_autoinstall: bool
    image_ref: str | None = None
    os_variant: str | None = None
    display_name: str | None = None


class ScenarioOut(BaseModel):
    id: str
    mode: str
    description: str
    enabled: bool
    script_path: str | None = None


class WorkloadServiceOut(BaseModel):
    name: str
    port: int
    url_path: str = "/"
    credentials: dict | None = None


class WorkloadOut(BaseModel):
    id: str
    display_name: str
    tags: list[str] = Field(default_factory=list)
    services: list[WorkloadServiceOut] = Field(default_factory=list)
    cve_ids: list[str] = Field(default_factory=list)
    cve_context: str | None = None
    is_stack: bool = False
    components: list[str] = Field(default_factory=list)
    compatibility_status: str = "unknown"
    compatibility_reason: str | None = None


class JobCreate(BaseModel):
    target_id: str
    scenario_id: str
    execution_mode: str
    capture_type: CaptureType = "memory"
    image_mode: str | None = None
    image_path: str | None = None
    custom_script: str | None = None
    workload_id: str | None = None
    attacker_vm_id: str | None = None
    vm_vcpus: _PositiveInt | None = None
    vm_memory_mb: _PositiveInt | None = None
    vm_disk_gb: _PositiveInt | None = None
    attacker_vm_vcpus: _PositiveInt | None = None
    attacker_vm_memory_mb: _PositiveInt | None = None
    attacker_vm_disk_gb: _PositiveInt | None = None
    build_vol3_isf: bool = False

    @field_validator("image_path")
    @classmethod
    def _check_image_path(cls, value: str | None) -> str | None:
        return _validate_image_path_in_images_dir(value)


class AcquisitionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class AcquisitionStatusUpdate(BaseModel):
    status: Literal["live", "paused", "sealed"]


class CaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class CaseStatusUpdate(BaseModel):
    status: Literal["open", "closed", "sealed"]


class CaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    status: str
    created_at: datetime
    closed_at: datetime | None = None


class ArtifactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    case_id: str | None = None
    job_id: str | None = None
    session_id: str | None = None
    ownership_reconciliation_json: str | None = None
    acquisition_id: str | None = None
    producer: str
    kind: str
    filename: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    created_at: datetime


class ReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    case_id: str | None = None
    job_id: str | None = None
    acquisition_id: str | None = None
    input_artifact_id: str | None = None
    output_artifact_id: str | None = None
    report_type: str
    status: str
    path: str
    title: str | None = None
    summary: str | None = None
    created_at: datetime


class AcquisitionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    status: str
    created_at: datetime
    sealed_at: datetime | None = None
    job_count: int = 0
    live_job_count: int = 0


class LabDefinition(BaseModel):
    target_id: str
    scenario_id: str
    mode: Literal["immediate", "post_script", "manual_trigger"]
    capture_type: CaptureType = "memory"
    post_boot_script: str | None = None
    workload_id: str | None = None
    attacker_vm_id: str | None = None
    vm_vcpus: _PositiveInt | None = None
    vm_memory_mb: _PositiveInt | None = None
    vm_disk_gb: _PositiveInt | None = None
    attacker_vm_vcpus: _PositiveInt | None = None
    attacker_vm_memory_mb: _PositiveInt | None = None
    attacker_vm_disk_gb: _PositiveInt | None = None


JobStatus = Literal[
    "queued",
    "waiting_for_capacity",
    "preparing_image",
    "waiting_for_guest_access",
    "mounting_output_share",
    "generating_isf",
    "running_scenario_script",
    "provisioning_workload",
    "waiting_for_capture",
    "capture_requested",
    "acquiring_memory",
    "acquiring_disk",
    "publishing_artifact",
    "cancel_requested",
    "completed",
    "failed",
    "cancelled",
]


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    target_id: str
    scenario_id: str
    execution_mode: str
    capture_type: str
    status: JobStatus
    capacity_reason: str | None = None
    image_mode: str | None = None
    guest_ip: str | None = None
    detected_distro: str | None = None
    detected_version: str | None = None
    detected_kernel_release: str | None = None
    detected_architecture: str | None = None
    dump_filename: str | None = None
    dump_checksum: str | None = None
    dump_size_bytes: int | None = None
    disk_dump_filename: str | None = None
    disk_dump_checksum: str | None = None
    disk_dump_size_bytes: int | None = None
    has_memory_dump: bool = False
    has_disk_dump: bool = False

    @model_validator(mode="after")
    def _derive_dump_flags(self) -> JobOut:
        self.has_memory_dump = self.dump_filename is not None or self.dump_checksum is not None
        self.has_disk_dump = self.disk_dump_filename is not None or self.disk_dump_checksum is not None
        return self

    workload_id: str | None = None
    workload_provisioned_at: datetime | None = None
    lab_url: str | None = None
    lab_snapshot_name: str | None = None
    network_isolated: bool | None = None
    attacker_vm_id: str | None = None
    attacker_vm_name: str | None = None
    attacker_ip: str | None = None
    exploit_tags: list[str] = Field(default_factory=list)
    exploit_notes: str | None = None
    custom_script: str | None = None

    @field_validator("exploit_tags", mode="before")
    @classmethod
    def _parse_tags(cls, v: object) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [t.strip() for t in v.split(",") if t.strip()]
        return list(cast(Iterable[str], v))

    vm_vcpus: int | None = None
    vm_memory_mb: int | None = None
    vm_disk_gb: int | None = None
    attacker_vm_vcpus: int | None = None
    attacker_vm_memory_mb: int | None = None
    attacker_vm_disk_gb: int | None = None
    error_summary: str | None = None
    current_stage: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    build_vol3_isf: bool = False


class JobRuntimeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: str
    role: str
    vm_name: str
    ip_address: str | None
    state: str
    power_state: str
    snapshot_name: str | None
    capture_memory_enabled: bool
    capture_disk_enabled: bool
    created_at: datetime
    updated_at: datetime


class JobEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: str
    state: str
    message: str
    created_at: datetime


class HostActionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: str
    action_type: str
    status: str
    payload: str | None
    active_pid: int | None
    error_summary: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ControlRuntimeOut(BaseModel):
    status: str
    role: str
    action: str


class CveOut(BaseModel):
    cve_id: str
    workload_ids: list[str]


class LabServiceOut(BaseModel):
    name: str
    port: int
    url: str
    credentials: dict | None = None


class LabInfoOut(BaseModel):
    workload_id: str
    lab_url: str | None
    guest_ip: str
    snapshot_available: bool
    services: list[LabServiceOut]


class ResetLabOut(BaseModel):
    status: str


class CaptureRequest(BaseModel):
    exploit_tags: list[str] = Field(default_factory=list)
    exploit_notes: str | None = None
    capture_type: CaptureType | None = None


class ImageDownloadConfig(BaseModel):
    source_policy: str = "official_only"
    network_policy: str = "safe"
    allowed_domains: list[str] = Field(default_factory=list)
    connect_timeout_seconds: int = 10
    read_timeout_seconds: int = 60
    max_size_gb: int = 20


# ---------------------------------------------------------------------------
# Playground schemas
# ---------------------------------------------------------------------------


PlaygroundVMRole = Literal["target", "attacker", "network"]


class PlaygroundNicSpec(BaseModel):
    network_id: str = Field(min_length=1)


class PlaygroundSpawnVMRequest(BaseModel):
    label: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    image_id: str = Field(min_length=1)
    nics: list[PlaygroundNicSpec] = Field(min_length=1)
    vcpus: int | None = Field(default=None, ge=VCPUS_MIN, le=VCPUS_MAX)
    memory_mb: int | None = Field(default=None, ge=MEMORY_MB_MIN, le=MEMORY_MB_MAX)
    disk_gb: int | None = Field(default=None, ge=DISK_GB_MIN, le=DISK_GB_MAX)
    role: PlaygroundVMRole = "target"
    workload_id: str | None = None


def _validate_cidr_str(v: str) -> str:
    """Strict CIDR parse plus minimum 4 addresses. Shared by network schemas."""
    try:
        network = ipaddress.ip_network(v, strict=True)
    except ValueError as exc:
        raise ValueError(f"invalid CIDR: {exc}") from exc
    if network.num_addresses < 4:
        raise ValueError("CIDR must contain at least 4 addresses")
    return v


class PlaygroundCreateNetworkRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    subnet_cidr: str
    isolated: bool = True

    @field_validator("subnet_cidr")
    @classmethod
    def _validate_cidr(cls, v: str) -> str:
        return _validate_cidr_str(v)


class PlaygroundCaptureRequest(BaseModel):
    kind: Literal["memory", "disk"]


class PlaygroundVMActionRequest(BaseModel):
    action: Literal["start", "stop", "reboot"]


class PlaygroundRecipeNetwork(PlaygroundCreateNetworkRequest):
    """Alias of PlaygroundCreateNetworkRequest used in recipe documents."""


class PlaygroundRecipeVM(BaseModel):
    label: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    role: PlaygroundVMRole = "target"
    image_id: str = Field(min_length=1)
    workload_id: str | None = None
    vcpus: int | None = Field(default=None, ge=VCPUS_MIN, le=VCPUS_MAX)
    memory_mb: int | None = Field(default=None, ge=MEMORY_MB_MIN, le=MEMORY_MB_MAX)
    disk_gb: int | None = Field(default=None, ge=DISK_GB_MIN, le=DISK_GB_MAX)
    networks: list[str] = Field(default_factory=list)


class PlaygroundTopologyNode(BaseModel):
    node_id: str = Field(min_length=1, max_length=128)
    node_type: Literal["vm", "network"]
    x: FiniteFloat
    y: FiniteFloat


class PlaygroundRecipeTopologyNode(PlaygroundTopologyNode):
    """Alias of PlaygroundTopologyNode used in recipe documents."""


class PlaygroundRecipe(BaseModel):
    schema_version: Literal[1] = 1
    name: str = Field(default="playground", min_length=1, max_length=128)
    networks: list[PlaygroundRecipeNetwork] = Field(default_factory=list)
    vms: list[PlaygroundRecipeVM] = Field(default_factory=list)
    topology: list[PlaygroundRecipeTopologyNode] = Field(default_factory=list)


class PlaygroundNicOut(BaseModel):
    id: str
    job_network_id: str
    ordinal: int = Field(ge=0)
    network_id: str | None = None
    network_name: str | None = None
    network_state: str | None = None
    network_isolated: bool | None = None
    mac: str
    ip: str | None
    state: str | None = None
    diagnostic: str | None = None


class PlaygroundServiceOut(BaseModel):
    name: str
    port: int
    url_path: str = "/"
    url: str | None = None
    credentials: dict | None = None
    status: str = "planned"


class PlaygroundWorkloadCatalogOut(BaseModel):
    id: str
    display_name: str
    tags: list[str] = Field(default_factory=list)
    service_labels: list[str] = Field(default_factory=list)
    services: list[WorkloadServiceOut] = Field(default_factory=list)
    cve_ids: list[str] = Field(default_factory=list)
    is_stack: bool = False
    compatibility_status: str = "unknown"
    compatibility_reason: str | None = None


def _mirror_playground_session_ids(data):
    if isinstance(data, dict):
        if data.get("session_id") is None and data.get("job_id") is not None:
            data["session_id"] = data["job_id"]
        elif data.get("job_id") is None and data.get("session_id") is not None:
            data["job_id"] = data["session_id"]
    return data


class PlaygroundVMOut(BaseModel):
    id: str
    job_id: str = Field(
        description="Deprecated alias of session_id; will be removed in the next minor version.",
        json_schema_extra={"deprecated": True},
    )
    session_id: str
    label: str
    image_id: str
    os: str | None = None
    state: str
    domain_name: str
    role: str
    function_tags: list[str] = Field(default_factory=list)
    vcpus: int | None = None
    memory_mb: int | None = None
    disk_gb: int | None = None
    created_at: datetime
    workload_id: str | None = None
    workload_display_name: str | None = None
    workload_state: str | None = None
    workload_error: str | None = None
    error_summary: str | None = None
    services: list[PlaygroundServiceOut] = Field(default_factory=list)
    nics: list[PlaygroundNicOut]
    can_capture_memory: bool = False
    can_capture_disk: bool = False
    can_destroy: bool = False
    can_start: bool = False
    can_stop: bool = False
    can_reboot: bool = False
    action_block_reason: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _mirror_session_id(cls, data):
        return _mirror_playground_session_ids(data)


class PlaygroundVMMetricsOut(BaseModel):
    vm_id: str
    state: str
    cpu_pct: float
    mem_pct: float
    mem_used_mb: int
    mem_total_mb: int
    rx_bytes: dict[str, int] = Field(default_factory=dict)
    tx_bytes: dict[str, int] = Field(default_factory=dict)
    sampled_at: datetime


class PlaygroundCidrSuggestionOut(BaseModel):
    subnet_cidr: str


class PlaygroundNetworkOut(BaseModel):
    id: str
    job_id: str = Field(
        description="Deprecated alias of session_id; will be removed in the next minor version.",
        json_schema_extra={"deprecated": True},
    )
    session_id: str
    name: str
    subnet_cidr: str
    isolated: bool
    libvirt_net_name: str
    state: str
    attached_vm_count: int = 0
    can_delete: bool = False
    action_block_reason: str | None = None
    cidr_substituted_from: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _mirror_session_id(cls, data):
        return _mirror_playground_session_ids(data)


class PlaygroundTopologyLayout(BaseModel):
    nodes: list[PlaygroundTopologyNode] = Field(default_factory=list)


class PlaygroundSessionOut(BaseModel):
    job_id: str = Field(
        description="Deprecated alias of session_id; will be removed in the next minor version.",
        json_schema_extra={"deprecated": True},
    )
    session_id: str
    status: str
    started_at: datetime | None
    vms: list[PlaygroundVMOut]
    networks: list[PlaygroundNetworkOut]

    @model_validator(mode="before")
    @classmethod
    def _mirror_session_id(cls, data):
        return _mirror_playground_session_ids(data)


class PlaygroundAttachNicRequest(BaseModel):
    network_id: str
    nic_id: str | None = None
