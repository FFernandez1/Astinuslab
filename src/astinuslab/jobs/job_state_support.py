from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class JobParams:
    target_id: str
    scenario_id: str
    image_path: str | None
    custom_script: str | None
    vm_vcpus: int | None
    vm_memory_mb: int | None
    vm_disk_gb: int | None
    attacker_vm_vcpus: int | None
    attacker_vm_memory_mb: int | None
    attacker_vm_disk_gb: int | None
    capture_type: str


def load_job_params(session_scope, repo_cls, job_id: str) -> JobParams:
    """Return target/scenario paths plus target+attacker resources and capture_type."""
    with session_scope() as session:
        repo = repo_cls(session)
        job = repo.get_job(job_id)
        if job is None:
            raise RuntimeError(f"Job {job_id} not found")
        return JobParams(
            target_id=job.target_id,
            scenario_id=job.scenario_id,
            image_path=job.image_path,
            custom_script=job.custom_script,
            vm_vcpus=job.vm_vcpus,
            vm_memory_mb=job.vm_memory_mb,
            vm_disk_gb=job.vm_disk_gb,
            attacker_vm_vcpus=job.attacker_vm_vcpus,
            attacker_vm_memory_mb=job.attacker_vm_memory_mb,
            attacker_vm_disk_gb=job.attacker_vm_disk_gb,
            capture_type=job.capture_type or "memory",
        )


def load_action_payload(session_scope, repo_cls, action_id: int | None) -> dict:
    if action_id is None:
        return {}
    with session_scope() as session:
        repo = repo_cls(session)
        action = repo.get_action(action_id)
        if action is None:
            return {}
        return json.loads(action.payload or "{}")


class JobCleanupManager:
    """Context manager for idempotent cleanup of job resources to avoid leaks on panic."""

    def __init__(
        self,
        job_id: str,
        *,
        logger,
        remove_network_isolation,
        teardown_attacker_vm,
        teardown_vm,
        action_by_job: dict[str, int],
    ):
        self.job_id = job_id
        self.teardown = True
        self.isolated_guest_ip: str | None = None
        self.attacker_ip_for_isolation: str | None = None
        self.attacker_booted = False
        self.tmp_script_path: Path | None = None
        self._logger = logger
        self._remove_network_isolation = remove_network_isolation
        self._teardown_attacker_vm = teardown_attacker_vm
        self._teardown_vm = teardown_vm
        self._action_by_job = action_by_job

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.teardown and self.isolated_guest_ip:
            try:
                self._remove_network_isolation(
                    self.job_id, self.isolated_guest_ip, attacker_ip=self.attacker_ip_for_isolation
                )
            except Exception as exc:
                self._logger.warning("[%s] failed to remove network isolation during cleanup: %s", self.job_id, exc)
        if self.teardown and self.attacker_booted:
            try:
                self._teardown_attacker_vm(self.job_id)
            except Exception as exc:
                self._logger.warning("[%s] failed to teardown attacker VM: %s", self.job_id, exc)
        if self.teardown:
            try:
                self._teardown_vm(self.job_id)
            except Exception as exc:
                self._logger.warning("[%s] failed to teardown target VM: %s", self.job_id, exc)
        if self.tmp_script_path and getattr(self.tmp_script_path, "exists", lambda: False)():
            try:
                self.tmp_script_path.unlink(missing_ok=True)
            except Exception as exc:
                self._logger.warning("[%s] failed to remove tmp script: %s", self.job_id, exc)
        finalize_action_tracking(self._action_by_job, self.job_id)


def load_job_field(session_scope, repo_cls, job_id: str, field_name: str):
    with session_scope() as session:
        repo = repo_cls(session)
        job = repo.get_job(job_id)
        return getattr(job, field_name) if job else None


def load_capture_runtime_state(session_scope, repo_cls, job_id: str) -> dict:
    with session_scope() as session:
        repo = repo_cls(session)
        job = repo.get_job(job_id)
        if job is None:
            raise RuntimeError(f"job not found: {job_id}")
        return {
            "guest_ip": job.guest_ip,
            "kernel_release": job.detected_kernel_release,
            "attacker_ip": job.attacker_ip,
            "network_isolated": bool(job.network_isolated),
            "attacker_vm_present": bool(job.attacker_vm_name),
        }


def should_preserve_failed_vm(logger, exc: Exception, job_id: str, keep_failed_vm: bool) -> bool:
    is_cancel = str(exc) == "cancel_requested" or (
        isinstance(exc, subprocess.CalledProcessError) and exc.returncode == -15
    )
    if keep_failed_vm and not is_cancel:
        logger.info("[%s] VM preserved for debugging (keep_failed_vm=true)", job_id)
        return True
    return False


def handle_runner_cleanup_exception(
    handle_exception_fn,
    should_preserve_failed_vm_fn,
    logger,
    cleanup,
    exc: Exception,
    job_id: str,
    action_id: int | None,
    keep_failed_vm: bool,
) -> None:
    handle_exception_fn(exc, job_id, action_id)
    if should_preserve_failed_vm_fn(logger, exc, job_id, keep_failed_vm):
        cleanup.teardown = False


def finalize_action_tracking(action_by_job: dict[str, int], job_id: str) -> None:
    action_by_job.pop(job_id, None)


def persist_guest_access_and_metadata(
    session_scope,
    repo_cls,
    sync_runtime_record,
    *,
    job_id: str,
    vm_name: str,
    guest_ip: str,
    metadata: dict,
) -> None:
    with session_scope() as session:
        repo = repo_cls(session)
        repo.transition_job_status(job_id, "waiting_for_guest_access", guest_ip=guest_ip)
        repo.add_event(job_id, "waiting_for_guest_access", f"Guest IP detected: {guest_ip}")
    sync_runtime_record(
        job_id,
        "target",
        vm_name=vm_name,
        ip_address=guest_ip,
        state="running",
        power_state="on",
        capture_memory_enabled=True,
        capture_disk_enabled=True,
    )
    with session_scope() as session:
        repo = repo_cls(session)
        repo.update_job(
            job_id,
            detected_distro=metadata.get("distro_id"),
            detected_version=metadata.get("distro_version"),
            detected_kernel_release=metadata.get("kernel_release"),
            detected_architecture=metadata.get("arch"),
        )
        repo.add_event(
            job_id, "waiting_for_guest_access", f"Guest metadata collected: kernel={metadata.get('kernel_release')}"
        )
    with session_scope() as session:
        repo = repo_cls(session)
        repo.transition_job_status(job_id, "mounting_output_share")
        repo.add_event(job_id, "mounting_output_share", "Mounting output share on guest")
