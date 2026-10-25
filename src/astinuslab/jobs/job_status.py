"""Authoritative job status vocabulary and transition graph.

Single source of truth for the job lifecycle. ``JobStatus`` is a ``StrEnum`` so its
members compare and serialize equal to the plain status strings stored in the
database and rendered in templates. ``repositories.JobRepository`` enforces the
graph; this module only defines it.
"""

from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "queued"
    WAITING_FOR_CAPACITY = "waiting_for_capacity"
    PREPARING_IMAGE = "preparing_image"
    WAITING_FOR_GUEST_ACCESS = "waiting_for_guest_access"
    MOUNTING_OUTPUT_SHARE = "mounting_output_share"
    GENERATING_ISF = "generating_isf"
    RUNNING_SCENARIO_SCRIPT = "running_scenario_script"
    PROVISIONING_WORKLOAD = "provisioning_workload"
    WAITING_FOR_CAPTURE = "waiting_for_capture"
    CAPTURE_REQUESTED = "capture_requested"
    ACQUIRING_MEMORY = "acquiring_memory"
    ACQUIRING_DISK = "acquiring_disk"
    PUBLISHING_ARTIFACT = "publishing_artifact"
    CANCEL_REQUESTED = "cancel_requested"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "waiting_for_capacity": {"queued", "cancel_requested", "cancelled", "failed"},
    "queued": {
        "waiting_for_capacity",
        "preparing_image",
        "waiting_for_guest_access",
        "cancel_requested",
        "cancelled",
        "failed",
    },
    "preparing_image": {"waiting_for_guest_access", "cancel_requested", "cancelled", "failed"},
    "waiting_for_guest_access": {"mounting_output_share", "cancel_requested", "cancelled", "failed"},
    "mounting_output_share": {
        "running_scenario_script",
        "provisioning_workload",
        "waiting_for_capture",
        "acquiring_memory",
        "acquiring_disk",
        "generating_isf",
        "cancel_requested",
        "cancelled",
        "failed",
    },
    "generating_isf": {"completed", "cancel_requested", "cancelled", "failed"},
    "running_scenario_script": {
        "waiting_for_capture",
        "acquiring_memory",
        "acquiring_disk",
        "cancel_requested",
        "cancelled",
        "failed",
    },
    "provisioning_workload": {"waiting_for_capture", "cancel_requested", "cancelled", "failed"},
    "waiting_for_capture": {
        "capture_requested",
        "acquiring_memory",
        "acquiring_disk",
        "cancel_requested",
        "cancelled",
        "failed",
    },
    "capture_requested": {"acquiring_memory", "acquiring_disk", "cancelled", "failed"},
    "acquiring_memory": {"publishing_artifact", "acquiring_disk", "cancelled", "failed"},
    "acquiring_disk": {"publishing_artifact", "cancelled", "failed"},
    "publishing_artifact": {"completed", "cancelled", "failed"},
    "cancel_requested": {"cancelled", "failed"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
}

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

# Statuses from which an operator may request cancellation. Derived from the
# graph so it can never drift from ALLOWED_TRANSITIONS: capture/publish
# phases deliberately have no cancel_requested edge — captures are not
# interruptible for forensic consistency.
CANCELLABLE_STATUSES = frozenset(
    status for status, successors in ALLOWED_TRANSITIONS.items() if "cancel_requested" in successors
)

FORENSIC_LIVE_STATUSES = frozenset(
    {
        "queued",
        "waiting_for_capacity",
        "preparing_image",
        "waiting_for_guest_access",
        "mounting_output_share",
        "running_scenario_script",
        "provisioning_workload",
        "waiting_for_capture",
        "capture_requested",
        "acquiring_memory",
        "acquiring_disk",
        "generating_isf",
        "publishing_artifact",
        "cancel_requested",
    }
)

# Phases in which an operator may ask the lifecycle worker to power-cycle a lab
# VM. Only the parked live-lab phase qualifies: from ``capture_requested``
# onwards the guest's volatile state belongs to an acquisition, and every later
# phase has either taken ownership of the VM or already released it.
RUNTIME_CONTROLLABLE_STATUSES = frozenset({JobStatus.WAITING_FOR_CAPTURE.value})
