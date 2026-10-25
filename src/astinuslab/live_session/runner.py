from __future__ import annotations

import logging

from ..catalog import attacker_by_id, workload_by_id
from ..jobs.job_control import ACTION_BY_JOB
from ..jobs.job_state_support import handle_runner_cleanup_exception as _handle_runner_cleanup_exception_impl
from ..jobs.job_state_support import should_preserve_failed_vm as _should_preserve_failed_vm_impl
from ..orchestration.context import JobRunContext
from ..orchestration.deps import JobRunDeps
from ..orchestration.support import mark_action_running as _mark_action_running_impl
from ..orchestration.support import mark_job_preparing_image as _mark_job_preparing_image_impl
from ..storage.run_path_support import prepare_live_session_boot_context as _prepare_live_session_boot_context_impl
from .support import apply_post_provisioning_network_state as _apply_post_provisioning_network_state_impl
from .support import finish_attacker_boot as _finish_attacker_boot_impl
from .support import finish_attacker_boot_to_cleanup as _finish_attacker_boot_to_cleanup_impl
from .support import guard_runtime_workload_context as _guard_runtime_workload_context_impl
from .support import provision_live_session_workload as _provision_live_session_workload_impl
from .support import resolve_attacker_config as _resolve_attacker_config_impl
from .support import resolve_live_session_workload_cache as _resolve_live_session_workload_cache_impl
from .support import start_attacker_boot as _start_attacker_boot_impl
from .support import wait_for_capture_handoff as _wait_for_capture_handoff_impl

logger = logging.getLogger(__name__)

# Live-session flow. As with boot_capture_runner, dependencies are resolved through
# an explicit ``JobRunDeps`` container (see orchestration.deps.py); tests pass ``deps=``
# with overridden fields instead of monkeypatching module globals.


def run_live_session_job(job_id: str, action_id: int | None = None, deps: JobRunDeps | None = None) -> None:
    ctx = JobRunContext.create(job_id, action_id, deps)
    deps = ctx.deps

    _mark_action_running_impl(deps.session_scope, deps.job_repository_cls, ACTION_BY_JOB, ctx.job_id, ctx.action_id)

    keep_failed_vm = False
    attacker_thread = None
    with deps.job_cleanup_manager(ctx.job_id) as cleanup:
        try:
            params = deps.load_job_params(ctx.job_id)
            target, scenario, guest_user, keep_failed_vm = deps.resolve_target_execution_context(
                params.target_id, params.scenario_id
            )

            deps.check_host_disk_space(ctx.job_id)

            _mark_job_preparing_image_impl(deps.session_scope, deps.job_repository_cls, ctx.job_id)

            image_path, effective_vcpus, effective_memory_mb, effective_disk_gb = (
                _prepare_live_session_boot_context_impl(
                    target,
                    deps.settings,
                    params.image_path,
                    params.vm_vcpus,
                    params.vm_memory_mb,
                    params.vm_disk_gb,
                )
            )

            # ── Resolve workload + cache BEFORE boot so we can use cached image ──
            (
                workload_id,
                workload,
                workload_cache_path,
                boot_cache_image,
                boot_cache_instance_id,
            ) = _resolve_live_session_workload_cache_impl(
                logger,
                deps.session_scope,
                deps.job_repository_cls,
                deps.load_workload_id,
                workload_by_id,
                deps.check_workload_cache,
                deps.workload_cache_key,
                ctx.job_id,
                params.target_id,
                image_path,
            )

            # ── Start Attacker VM early in parallel with Target VM ──
            attacker_thread = None
            attacker_thread_result = None
            attacker_vm_id = deps.load_attacker_vm_id(ctx.job_id)
            if attacker_vm_id or (workload_id and workload is not None and workload.get("attacker_vm")):
                attacker_cfg = _resolve_attacker_config_impl(attacker_by_id, attacker_vm_id, workload)

                # Start attacker VM boot in parallel with the target VM boot.
                attacker_thread, attacker_thread_result = _start_attacker_boot_impl(
                    logger,
                    deps.settings,
                    deps.boot_simple_vm,
                    ctx.job_id,
                    target,
                    attacker_cfg,
                    params.attacker_vm_vcpus,
                    params.attacker_vm_memory_mb,
                    params.attacker_vm_disk_gb,
                )
                cleanup.attacker_booted = True  # Partial boot still owns resources.

            guest_ip, metadata = deps.boot_and_prepare(
                ctx.job_id,
                image_path,
                target.get("os_variant") or "generic",
                params.target_id,
                guest_user=guest_user,
                target_distro=target.get("distro"),
                target_family=target.get("family"),
                target_machine_type=target.get("machine_type"),
                target_network_profile=target.get("network_profile"),
                target_cloud_init_profile=target.get("cloud_init_profile"),
                target_inject_network_config=target.get("inject_network_config"),
                target_filesystem_type=target.get("filesystem_type"),
                vm_vcpus=effective_vcpus,
                vm_memory_mb=effective_memory_mb,
                vm_disk_gb=effective_disk_gb,
                target_guest_mount_path=target.get("guest_mount_path"),
                cache_image=boot_cache_image,
                cache_instance_id=boot_cache_instance_id,
                keep_failed_vm=keep_failed_vm,
            )
            _kernel_release = metadata.get("kernel_release")

            # ── Runtime compatibility guard (after QGA metadata) ───────────────────
            runtime_target = _guard_runtime_workload_context_impl(
                deps.session_scope,
                deps.job_repository_cls,
                deps.runtime_workload_context_or_raise,
                ctx.job_id,
                workload_id,
                workload,
                target,
                metadata,
            )

            # ── Workload provisioning (optional) ───────────────────────────────────
            _provision_live_session_workload_impl(
                deps.session_scope,
                deps.job_repository_cls,
                deps.finalize_workload_provisioning,
                deps.provision_workload,
                deps.save_workload_cache,
                deps.create_lab_snapshot,
                ctx.job_id,
                params.target_id,
                workload_id,
                workload,
                workload_cache_path,
                image_path,
                guest_ip,
                guest_user,
                runtime_target,
            )

            # Join attacker thread — by now it is likely already done.
            _finish_attacker_boot_to_cleanup_impl(
                _finish_attacker_boot_impl,
                logger,
                cleanup,
                ctx.job_id,
                attacker_thread,
                attacker_thread_result,
            )
            cleanup.attacker_booted = bool(attacker_vm_id or attacker_thread)

            _apply_post_provisioning_network_state_impl(
                deps.apply_network_isolation,
                deps.create_lab_snapshot,
                cleanup,
                ctx.job_id,
                workload_id,
                workload,
                guest_ip,
            )

            _wait_for_capture_handoff_impl(
                deps.session_scope,
                deps.job_repository_cls,
                deps.complete_action,
                ctx.job_id,
                scenario or {},
                ctx.action_id,
            )
            cleanup.teardown = False
            return

        except Exception as exc:
            # Do not release a failed target while its paired boot can still
            # create a domain/overlay after cleanup has observed absence.
            if attacker_thread is not None:
                attacker_thread.join()
                cleanup.attacker_booted = True
            _handle_runner_cleanup_exception_impl(
                deps.handle_exception,
                _should_preserve_failed_vm_impl,
                logger,
                cleanup,
                exc,
                ctx.job_id,
                ctx.action_id,
                keep_failed_vm,
            )
