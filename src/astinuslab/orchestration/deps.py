"""Explicit dependency container for the job-run flows.

``JobRunDeps`` replaces the old ``astinuslab.orchestrator`` module seam. Instead of
monkeypatching orchestrator module globals, tests construct ``JobRunDeps.default()``,
override individual fields, and pass the instance into the flow entrypoints
(``run_boot_capture_job``, ``run_live_session_job``, ``acquire_memory_dump``,
``refresh_runtime``/``control_runtime``) — or call composed helpers directly through
the instance (``deps.provision_workload(...)``).

Two properties keep the old wrapper-chain test semantics working:

* Every default binding is a closure over the ``deps`` instance: a default binding
  that needs another dependency reads it as ``deps.<field>`` at call time, so
  overriding e.g. ``deps.run_command`` is seen by ``deps.parse_guest_ip``,
  ``deps.save_workload_cache``, ... without re-wiring.
* Patch-sensitive process-wide sources (``db.session_scope``,
  ``keystore.get_active_key_paths``, ``vm_support.start_virtiofsd``,
  ``lab_lifecycle.teardown_vm`` ...) are read from their source module at call
  time, so fixtures that monkeypatch the source module keep working.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from importlib import import_module
from pathlib import Path
from typing import Any

from .. import catalog, db
from ..core import compatibility, fake_mode
from ..core.config import settings as _settings
from ..db import repositories
from ..jobs import job_control, job_state_support, runtime_support
from ..storage import keystore, run_path_support
from ..virtualization import (
    boot_staging,
    cloud_init,
    guest_wait,
    host_helpers,
    process_support,
    vm_support,
    windows_prepared,
    windows_runtime,
)
from ..virtualization.vm_lifecycle import VMLifecycle
from ..workloads import workload_cache_support, workload_support
from . import lab_lifecycle
from . import support as orchestration_support

logger = logging.getLogger(__name__)

_WORKLOAD_CACHE_LOCK = threading.Lock()


def _with_test_doubles(deps: JobRunDeps) -> JobRunDeps:
    """The one seam where a test double can replace the production wiring.

    Read at call time, not at import, so a test can flip the flag. Everything
    the runners do is unchanged; only the bindings that reach the host are
    replaced. See ``orchestration/fake_runtime.py`` for what that costs and what
    it deliberately refuses to fake.
    """
    if not fake_mode.FAKE_ORCHESTRATION:
        return deps
    from . import fake_runtime

    return fake_runtime.apply(deps)


@dataclass
class JobRunDeps:
    """Mutable bag of the callables a job run needs. See module docstring."""

    settings: Any
    job_repository_cls: type
    session_scope: Callable[[], Any]
    get_active_key_paths: Callable[[], tuple[Path, Path]]
    is_cancel_requested: Callable[[str], bool]
    run_command: Callable[..., str]
    require_noninteractive_sudo: Callable[[], None]
    check_host_disk_space: Callable[..., None]
    # catalog / target resolution
    target_by_id: Callable[[str], dict]
    scenario_by_id: Callable[[str], dict]
    resolve_target_execution_context: Callable[..., tuple]
    prepare_boot_context: Callable[..., tuple]
    runtime_workload_context_or_raise: Callable[..., dict]
    # VM / guest plumbing
    vm_lifecycle: Callable[[], Any]
    open_libvirt_connection: Callable[[], Any]
    start_virtiofsd: Callable[[str, Path, Path, Path], int]
    libvirt_storage_admin_helper: Callable[[], Path]
    runtime_vm_name: Callable[[str, str], str]
    sync_runtime_record: Callable[..., None]
    read_vm_power_state: Callable[[str], str]
    parse_guest_ip: Callable[..., str]
    wait_for_guest_ssh: Callable[..., None]
    record_verified_ssh_host_key: Callable[..., None]
    wait_for_guest_agent: Callable[..., None]
    wait_for_windows_setup: Callable[..., str]
    prepare_windows_guest: Callable[..., Any]
    bootstrap_windows_guest: Callable[..., Any]
    wait_for_windows_identity: Callable[..., None]
    collect_guest_metadata_via_qga: Callable[[str, str], dict[str, str | None]]
    collect_guest_metadata_via_ssh: Callable[[str, Path, str], dict[str, str | None]]
    write_guest_metadata_json: Callable[[Path, str, dict], None]
    verify_guest_disk_capacity: Callable[[str, str, str, int], None]
    job_disk_image_path: Callable[[str, str], Path]
    target_guest_mac: Callable[[str], str]
    boot_simple_vm: Callable[..., str]
    teardown_vm: Callable[[str], bool | None]
    teardown_attacker_vm: Callable[[str], None]
    create_lab_snapshot: Callable[..., str]
    # cloud-init staging
    resolve_cloud_init_profile: Callable[..., str]
    render_target_user_data: Callable[..., str]
    render_target_meta_data: Callable[..., str]
    render_target_network_config: Callable[..., str | None]
    stage_target_cloud_init: Callable[..., tuple]
    # job state
    load_job_params: Callable[[str], job_state_support.JobParams]
    load_action_payload: Callable[[int | None], dict]
    load_workload_id: Callable[[str], str | None]
    load_attacker_vm_id: Callable[[str], str | None]
    load_capture_runtime_state: Callable[[str], dict]
    handle_exception: Callable[[Exception, str, int | None], None]
    complete_action: Callable[[int | None], None]
    job_cleanup_manager: Callable[[str], job_state_support.JobCleanupManager]
    # workloads
    workload_cache_key: Callable[[str, str, Path], str]
    workload_cache_path: Callable[[str, str, str], Path]
    check_workload_cache: Callable[[str, str, Path], Path | None]
    update_workload_cache_index: Callable[[str, str, str, Path, Path], None]
    save_workload_cache: Callable[[str, str, str, Path, str, str], None]
    run_workload_install: Callable[..., None]
    check_workload_health: Callable[[str, dict, str], None]
    finalize_workload_provisioning: Callable[[str, dict, str], None]
    provision_workload: Callable[..., None]
    provision_stack: Callable[..., None]
    apply_network_isolation: Callable[..., bool]
    remove_network_isolation: Callable[..., None]
    run_post_boot_script: Callable[..., Path | None]
    # flow-level (late-bound so tests can stub whole phases)
    boot_and_prepare: Callable[..., tuple[str, dict]]
    run_isf_only_job: Callable[..., None]
    acquire_and_publish: Callable[..., None]

    @classmethod
    def default(cls) -> JobRunDeps:
        """Build the production wiring. Every closure reads ``deps.<field>`` at call time."""
        deps: JobRunDeps

        def session_scope():
            return db.session_scope()

        def get_active_key_paths():
            return keystore.get_active_key_paths()

        def is_cancel_requested(job_id: str) -> bool:
            return job_control.is_cancel_requested(job_id)

        def run_command(
            cmd: list[str],
            cwd: Path | None = None,
            job_id: str | None = None,
            extra_env: dict[str, str] | None = None,
            timeout: float | None = 3600.0,
            quiet_fail: bool = False,
            capture_output: bool | None = None,
            text: bool | None = None,
        ) -> str:
            _ = (capture_output, text)
            return process_support.run_command(
                logger,
                deps.session_scope,
                deps.job_repository_cls,
                job_control.ACTION_BY_JOB,
                job_control.JOB_PROCESSES,
                job_control.PROCESS_LOCK,
                cmd,
                cwd=cwd,
                job_id=job_id,
                extra_env=extra_env,
                timeout=timeout,
                is_cancel_requested_fn=deps.is_cancel_requested,
                quiet_fail=quiet_fail,
                storage_dir=deps.settings.storage_dir,
            )

        def require_noninteractive_sudo() -> None:
            process_support.require_noninteractive_sudo(deps.run_command, deps.settings)

        def check_host_disk_space(job_id: str, required_gb: int = 6) -> None:
            vm_support.check_host_disk_space(logger, deps.settings, job_id, required_gb)

        def target_by_id(target_id: str) -> dict:
            return vm_support.target_by_id(catalog.load_targets, target_id)

        def scenario_by_id(scenario_id: str) -> dict:
            return vm_support.scenario_by_id(catalog.load_scenarios, scenario_id)

        def resolve_target_execution_context(job_target_id: str, job_scenario_id: str | None = None):
            return run_path_support.resolve_target_execution_context(
                deps.target_by_id,
                deps.scenario_by_id,
                job_target_id,
                job_scenario_id,
            )

        def prepare_boot_context(target, job_image_path, vm_vcpus, vm_memory_mb, vm_disk_gb):
            return run_path_support.prepare_boot_context(
                target,
                deps.settings,
                job_image_path,
                vm_vcpus,
                vm_memory_mb,
                vm_disk_gb,
            )

        def vm_lifecycle() -> VMLifecycle:
            libvirt = import_module("libvirt")
            return VMLifecycle(run_command=deps.run_command, libvirt_module=libvirt)

        def open_libvirt_connection():
            return vm_support.open_libvirt_connection(deps.settings.libvirt_uri)

        def start_virtiofsd(job_id: str, socket_path: Path, share_dir: Path, pid_file_path: Path) -> int:
            return vm_support.start_virtiofsd(logger, job_id, socket_path, share_dir, pid_file_path)

        def libvirt_storage_admin_helper() -> Path:
            return host_helpers.libvirt_storage_admin_helper()

        def sync_runtime_record(job_id: str, role: str, **kwargs) -> None:
            runtime_support.sync_runtime_record(
                deps.session_scope,
                deps.job_repository_cls,
                job_id,
                role,
                **kwargs,
            )

        def read_vm_power_state(vm_name: str) -> str:
            return runtime_support.read_vm_power_state(deps.run_command, deps.settings, vm_name)

        def parse_guest_ip(
            job_id: str,
            libvirt_uri: str | None = None,
            libvirt_network: str | None = None,
        ) -> str:
            return runtime_support.parse_guest_ip(
                job_id,
                deps.settings,
                deps.run_command,
                libvirt_uri=libvirt_uri,
                libvirt_network=libvirt_network,
                subprocess_module=subprocess,
                time_module=time,
                path_cls=Path,
                logger=logger,
            )

        def wait_for_guest_ssh(
            job_id: str,
            guest_ip: str,
            ssh_key: Path,
            guest_user: str,
            max_wait_seconds: int = 600,
            poll_interval_seconds: int = 5,
            known_hosts_path: Path | None = None,
            guest_platform: str = "linux",
        ) -> None:
            return guest_wait.wait_for_guest_ssh(
                deps.is_cancel_requested,
                logger,
                subprocess,
                time,
                job_id,
                guest_ip,
                ssh_key,
                guest_user,
                max_wait_seconds=max_wait_seconds,
                poll_interval_seconds=poll_interval_seconds,
                known_hosts_path=known_hosts_path,
                guest_platform=guest_platform,
            )

        def record_verified_ssh_host_key(
            guest_ip: str,
            known_hosts_path: Path,
            expected_fingerprint: str,
        ) -> None:
            return guest_wait.record_verified_ssh_host_key(
                guest_ip,
                known_hosts_path,
                expected_fingerprint,
                subprocess_module=subprocess,
            )

        def wait_for_guest_agent(
            job_id: str,
            vm_name: str,
            libvirt_uri: str,
            max_wait_seconds: int = 600,
            poll_interval_seconds: int = 2,
        ) -> None:
            return guest_wait.wait_for_guest_agent(
                deps.is_cancel_requested,
                logger,
                job_id,
                vm_name,
                libvirt_uri,
                max_wait_seconds=max_wait_seconds,
                poll_interval_seconds=poll_interval_seconds,
                time_module=time,
            )

        def collect_guest_metadata_via_qga(vm_name: str, libvirt_uri: str) -> dict[str, str | None]:
            return guest_wait.collect_guest_metadata_via_qga(vm_name, libvirt_uri)

        def bootstrap_windows_guest(
            libvirt_uri: str,
            vm_name: str,
            public_key: str,
            computer_name: str,
            *,
            expected_boot_id: str | None = None,
        ):
            return windows_runtime.bootstrap_windows_guest(
                libvirt_uri, vm_name, public_key, computer_name, expected_boot_id=expected_boot_id
            )

        def collect_guest_metadata_via_ssh(guest_ip: str, ssh_key: Path, guest_user: str) -> dict[str, str | None]:
            return guest_wait.collect_guest_metadata_via_ssh(
                guest_ip,
                ssh_key,
                guest_user,
                deps.settings.base_dir / "guest" / "scripts" / "detect_environment.sh",
            )

        def write_guest_metadata_json(path: Path, job_id: str, metadata: dict) -> None:
            boot_staging.write_guest_metadata_json(path, job_id, metadata)

        def verify_guest_disk_capacity(job_id: str, vm_name: str, libvirt_uri: str, vm_disk_gb: int) -> None:
            guest_wait.verify_guest_disk_capacity(
                logger,
                deps.session_scope,
                deps.job_repository_cls,
                job_id,
                vm_name,
                libvirt_uri,
                vm_disk_gb,
            )

        def job_disk_image_path(job_id: str, target_id: str) -> Path:
            return vm_support.job_disk_image_path(job_id, target_id)

        def target_guest_mac(job_id: str) -> str:
            from .runners.boot_capture_runner import target_guest_mac as _target_guest_mac

            return _target_guest_mac(job_id)

        def boot_simple_vm(
            job_id: str,
            vm_suffix: str,
            image_path: Path,
            os_variant: str,
            guest_user: str,
            network_profile: str | None = None,
            vm_vcpus: int | None = None,
            vm_memory_mb: int | None = None,
            vm_disk_gb: int | None = None,
        ) -> str:
            return workload_support.boot_simple_vm(
                deps.session_scope,
                deps.job_repository_cls,
                deps.get_active_key_paths,
                deps.run_command,
                deps.parse_guest_ip,
                deps.wait_for_guest_ssh,
                deps.sync_runtime_record,
                deps.settings,
                logger,
                job_id,
                vm_suffix,
                image_path,
                os_variant,
                guest_user,
                network_profile=network_profile,
                vm_vcpus=vm_vcpus,
                vm_memory_mb=vm_memory_mb,
                vm_disk_gb=vm_disk_gb,
            )

        def teardown_vm(job_id: str) -> bool:
            return lab_lifecycle.teardown_vm(job_id)

        def teardown_attacker_vm(job_id: str) -> None:
            """Destroy the attacker VM for this job. Non-fatal."""
            deps.teardown_vm(f"{job_id}-attacker")

        def create_lab_snapshot(job_id: str, **kwargs) -> str:
            return lab_lifecycle.create_lab_snapshot(job_id, **kwargs)

        def resolve_cloud_init_profile(
            *,
            target_distro: str | None,
            target_family: str | None,
            target_cloud_init_profile: str | None,
        ) -> str:
            return cloud_init.resolve_cloud_init_profile(
                deps.settings,
                target_distro=target_distro,
                target_family=target_family,
                target_cloud_init_profile=target_cloud_init_profile,
            )

        def render_target_user_data(
            *,
            job_target_id: str,
            guest_user: str,
            ssh_public_key_path: Path,
            target_distro: str | None,
            target_family: str | None,
            target_cloud_init_profile: str | None,
            target_guest_mount_path: str | None,
        ) -> str:
            profile = deps.resolve_cloud_init_profile(
                target_distro=target_distro,
                target_family=target_family,
                target_cloud_init_profile=target_cloud_init_profile,
            )
            return cloud_init.render_target_user_data_for_profile(
                deps.settings,
                profile=profile,
                job_target_id=job_target_id,
                guest_user=guest_user,
                ssh_public_key_path=ssh_public_key_path,
                target_guest_mount_path=target_guest_mount_path,
            )

        def render_target_meta_data(
            *,
            job_id: str,
            job_target_id: str,
            cache_instance_id: str | None,
        ) -> str:
            return cloud_init.render_target_meta_data(
                job_id=job_id,
                job_target_id=job_target_id,
                cache_instance_id=cache_instance_id,
            )

        def render_target_network_config(
            *,
            target_distro: str | None,
            target_family: str | None,
            target_cloud_init_profile: str | None,
        ) -> str | None:
            profile = deps.resolve_cloud_init_profile(
                target_distro=target_distro,
                target_family=target_family,
                target_cloud_init_profile=target_cloud_init_profile,
            )
            return cloud_init.render_target_network_config_for_profile(deps.settings, profile=profile)

        def stage_target_cloud_init(**kwargs):
            return boot_staging.stage_target_cloud_init(
                **kwargs,
                render_target_user_data=deps.render_target_user_data,
                render_target_meta_data=deps.render_target_meta_data,
                render_target_network_config=deps.render_target_network_config,
            )

        def load_job_params(job_id: str) -> job_state_support.JobParams:
            return job_state_support.load_job_params(deps.session_scope, deps.job_repository_cls, job_id)

        def load_action_payload(action_id: int | None) -> dict:
            return job_state_support.load_action_payload(deps.session_scope, deps.job_repository_cls, action_id)

        def load_workload_id(job_id: str) -> str | None:
            return job_state_support.load_job_field(deps.session_scope, deps.job_repository_cls, job_id, "workload_id")

        def load_attacker_vm_id(job_id: str) -> str | None:
            return job_state_support.load_job_field(
                deps.session_scope, deps.job_repository_cls, job_id, "attacker_vm_id"
            )

        def load_capture_runtime_state(job_id: str) -> dict:
            return job_state_support.load_capture_runtime_state(deps.session_scope, deps.job_repository_cls, job_id)

        def handle_exception(exc: Exception, job_id: str, action_id: int | None) -> None:
            orchestration_support.handle_exception(
                deps.session_scope, deps.job_repository_cls, logger, exc, job_id, action_id
            )

        def complete_action(action_id: int | None) -> None:
            orchestration_support.complete_action(deps.session_scope, deps.job_repository_cls, action_id)

        def job_cleanup_manager(job_id: str) -> job_state_support.JobCleanupManager:
            return job_state_support.JobCleanupManager(
                job_id,
                logger=logger,
                remove_network_isolation=deps.remove_network_isolation,
                teardown_attacker_vm=deps.teardown_attacker_vm,
                teardown_vm=deps.teardown_vm,
                action_by_job=job_control.ACTION_BY_JOB,
            )

        def workload_cache_key(target_id: str, workload_id: str, image_path: Path) -> str:
            workload = catalog.workload_by_id(workload_id)
            fingerprint = compatibility.compatibility_fingerprint(
                workload or {"id": workload_id}, base_dir=deps.settings.base_dir
            )
            return workload_cache_support.workload_cache_key(
                target_id, workload_id, image_path, workload_fingerprint=fingerprint
            )

        def workload_cache_path(target_id: str, workload_id: str, key: str) -> Path:
            return workload_cache_support.workload_cache_path(deps.settings, target_id, workload_id, key)

        def check_workload_cache(target_id: str, workload_id: str, image_path: Path) -> Path | None:
            return workload_cache_support.check_workload_cache(
                logger,
                deps.settings,
                target_id,
                workload_id,
                image_path,
                cache_key_fn=deps.workload_cache_key,
                cache_path_fn=deps.workload_cache_path,
            )

        def update_workload_cache_index(
            target_id: str, workload_id: str, key: str, cache_path: Path, image_path: Path
        ) -> None:
            workload_cache_support.update_workload_cache_index(
                deps.settings,
                _WORKLOAD_CACHE_LOCK,
                target_id,
                workload_id,
                key,
                cache_path,
                image_path,
            )

        def save_workload_cache(
            job_id: str, target_id: str, workload_id: str, image_path: Path, ip: str, user: str
        ) -> None:
            workload_cache_support.save_workload_cache(
                logger,
                deps.settings,
                deps.run_command,
                deps.job_disk_image_path,
                deps.update_workload_cache_index,
                deps.workload_cache_key,
                deps.workload_cache_path,
                job_id=job_id,
                target_id=target_id,
                workload_id=workload_id,
                image_path=image_path,
                flush_guest_filesystems=partial(workload_support.flush_guest_filesystems, deps, job_id, ip, user),
            )

        def run_workload_install(
            job_id: str, workload: dict, guest_ip: str, guest_user: str, target_context: dict | None = None
        ) -> None:
            workload_support.run_workload_install(
                session_scope=deps.session_scope,
                repo_cls=deps.job_repository_cls,
                get_active_key_paths=deps.get_active_key_paths,
                run_command=deps.run_command,
                settings=deps.settings,
                check_workload_health=deps.check_workload_health,
                is_cancel_requested=deps.is_cancel_requested,
                job_id=job_id,
                workload=workload,
                guest_ip=guest_ip,
                guest_user=guest_user,
                target_context=target_context,
            )

        def check_workload_health(job_id: str, workload: dict, guest_ip: str) -> None:
            workload_support.check_workload_health(
                deps.session_scope,
                deps.job_repository_cls,
                deps.is_cancel_requested,
                logger,
                time,
                job_id,
                workload,
                guest_ip,
            )

        def finalize_workload_provisioning(job_id: str, workload: dict, guest_ip: str) -> None:
            workload_support.finalize_workload_provisioning(
                deps.session_scope, deps.job_repository_cls, job_id, workload, guest_ip
            )

        def provision_workload(
            job_id: str, workload: dict, guest_ip: str, guest_user: str, target_context: dict | None = None
        ) -> None:
            workload_support.provision_workload(
                deps.run_workload_install,
                deps.finalize_workload_provisioning,
                deps.provision_stack,
                job_id,
                workload,
                guest_ip,
                guest_user,
                target_context=target_context,
            )

        def provision_stack(
            job_id: str, stack: dict, guest_ip: str, guest_user: str, target_context: dict | None = None
        ) -> None:
            workload_support.provision_stack(
                session_scope=deps.session_scope,
                repo_cls=deps.job_repository_cls,
                get_active_key_paths=deps.get_active_key_paths,
                run_command=deps.run_command,
                settings=deps.settings,
                workload_by_id_fn=catalog.workload_by_id,
                run_workload_install=deps.run_workload_install,
                finalize_workload_provisioning=deps.finalize_workload_provisioning,
                job_id=job_id,
                stack=stack,
                guest_ip=guest_ip,
                guest_user=guest_user,
                target_context=target_context,
            )

        def apply_network_isolation(job_id: str, guest_ip: str, attacker_ip: str | None = None) -> bool:
            return workload_support.apply_network_isolation(
                deps.session_scope,
                deps.job_repository_cls,
                deps.run_command,
                logger,
                job_id,
                guest_ip,
                attacker_ip=attacker_ip,
            )

        def remove_network_isolation(job_id: str, guest_ip: str, attacker_ip: str | None = None) -> None:
            workload_support.remove_network_isolation(
                deps.session_scope,
                deps.job_repository_cls,
                deps.run_command,
                logger,
                job_id,
                guest_ip,
                attacker_ip=attacker_ip,
            )

        def run_post_boot_script(
            job_id: str,
            guest_ip: str,
            guest_user: str,
            scenario: dict,
            custom_script: str | None,
        ) -> Path | None:
            return run_path_support.run_post_boot_script(
                deps.session_scope,
                deps.job_repository_cls,
                deps.get_active_key_paths,
                deps.run_command,
                deps.is_cancel_requested,
                deps.settings,
                job_id,
                guest_ip,
                guest_user,
                scenario,
                custom_script,
            )

        def runtime_workload_context_or_raise(
            job_id: str,
            workload: dict,
            target: dict,
            metadata: dict[str, str | None],
            workload_map: dict[str, dict] | None = None,
        ) -> dict:
            return catalog.runtime_workload_context_or_raise(
                job_id, workload, target, metadata, workload_map=workload_map
            )

        def boot_and_prepare(*args, **kwargs):
            from .runners.boot_capture_runner import boot_and_prepare as _boot_and_prepare

            kwargs_with_deps = dict(kwargs)
            kwargs_with_deps["deps"] = deps
            return _boot_and_prepare(*args, **kwargs_with_deps)

        def run_isf_only_job(job_id: str, action_id: int | None) -> None:
            from .runners.boot_capture_runner import run_isf_only_job as _run_isf_only_job

            _run_isf_only_job(job_id, action_id, deps=deps)

        def acquire_and_publish(*args, **kwargs) -> None:
            from .runners.boot_capture_runner import acquire_and_publish as _acquire_and_publish

            _acquire_and_publish(*args, deps=deps, **kwargs)

        deps = cls(
            settings=_settings,
            job_repository_cls=repositories.JobRepository,
            session_scope=session_scope,
            get_active_key_paths=get_active_key_paths,
            is_cancel_requested=is_cancel_requested,
            run_command=run_command,
            require_noninteractive_sudo=require_noninteractive_sudo,
            check_host_disk_space=check_host_disk_space,
            target_by_id=target_by_id,
            scenario_by_id=scenario_by_id,
            resolve_target_execution_context=resolve_target_execution_context,
            prepare_boot_context=prepare_boot_context,
            runtime_workload_context_or_raise=runtime_workload_context_or_raise,
            vm_lifecycle=vm_lifecycle,
            open_libvirt_connection=open_libvirt_connection,
            start_virtiofsd=start_virtiofsd,
            libvirt_storage_admin_helper=libvirt_storage_admin_helper,
            runtime_vm_name=runtime_support.runtime_vm_name,
            sync_runtime_record=sync_runtime_record,
            read_vm_power_state=read_vm_power_state,
            parse_guest_ip=parse_guest_ip,
            wait_for_guest_ssh=wait_for_guest_ssh,
            record_verified_ssh_host_key=record_verified_ssh_host_key,
            wait_for_guest_agent=wait_for_guest_agent,
            bootstrap_windows_guest=bootstrap_windows_guest,
            wait_for_windows_setup=windows_runtime.wait_for_windows_setup,
            prepare_windows_guest=windows_prepared.prepare_windows_guest,
            wait_for_windows_identity=windows_runtime.wait_for_windows_identity,
            collect_guest_metadata_via_qga=collect_guest_metadata_via_qga,
            collect_guest_metadata_via_ssh=collect_guest_metadata_via_ssh,
            write_guest_metadata_json=write_guest_metadata_json,
            verify_guest_disk_capacity=verify_guest_disk_capacity,
            job_disk_image_path=job_disk_image_path,
            target_guest_mac=target_guest_mac,
            boot_simple_vm=boot_simple_vm,
            teardown_vm=teardown_vm,
            teardown_attacker_vm=teardown_attacker_vm,
            create_lab_snapshot=create_lab_snapshot,
            resolve_cloud_init_profile=resolve_cloud_init_profile,
            render_target_user_data=render_target_user_data,
            render_target_meta_data=render_target_meta_data,
            render_target_network_config=render_target_network_config,
            stage_target_cloud_init=stage_target_cloud_init,
            load_job_params=load_job_params,
            load_action_payload=load_action_payload,
            load_workload_id=load_workload_id,
            load_attacker_vm_id=load_attacker_vm_id,
            load_capture_runtime_state=load_capture_runtime_state,
            handle_exception=handle_exception,
            complete_action=complete_action,
            job_cleanup_manager=job_cleanup_manager,
            workload_cache_key=workload_cache_key,
            workload_cache_path=workload_cache_path,
            check_workload_cache=check_workload_cache,
            update_workload_cache_index=update_workload_cache_index,
            save_workload_cache=save_workload_cache,
            run_workload_install=run_workload_install,
            check_workload_health=check_workload_health,
            finalize_workload_provisioning=finalize_workload_provisioning,
            provision_workload=provision_workload,
            provision_stack=provision_stack,
            apply_network_isolation=apply_network_isolation,
            remove_network_isolation=remove_network_isolation,
            run_post_boot_script=run_post_boot_script,
            boot_and_prepare=boot_and_prepare,
            run_isf_only_job=run_isf_only_job,
            acquire_and_publish=acquire_and_publish,
        )
        return _with_test_doubles(deps)
