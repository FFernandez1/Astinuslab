from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from ..core import fake_mode
from ..core.locks import capture_lock, capture_lock_dir
from ..storage import publication
from ..storage.artifacts import build_artifact_manifest
from ..virtualization.disk_source import current_job_disk
from ..virtualization.host_helpers import ensure_evidence_dir, fix_libvirt_path_acl_helper, storage_root

_LIVE_DISK_CAVEAT = (
    "Acquired through the running guest OS (live, guest-mediated copy): "
    "temporally inconsistent and subject to guest manipulation; not a "
    "point-in-time forensic image."
)


def teardown_post_capture(
    *,
    logger,
    job_id: str,
    guest_ip: str | None,
    attacker_ip: str | None,
    network_isolated: bool,
    attacker_vm_present: bool,
    remove_network_isolation,
    teardown_attacker_vm,
    teardown_vm,
) -> None:
    if network_isolated and guest_ip:
        try:
            remove_network_isolation(job_id, guest_ip, attacker_ip=attacker_ip)
        except Exception as exc:
            logger.warning("[%s] failed to remove network isolation after capture: %s", job_id, exc)
    if attacker_vm_present:
        try:
            teardown_attacker_vm(job_id)
        except Exception as exc:
            logger.warning("[%s] failed to teardown attacker VM after capture: %s", job_id, exc)
    try:
        teardown_vm(job_id)
    except Exception as exc:
        logger.warning("[%s] failed to teardown target VM after capture: %s", job_id, exc)


def _forensic_capture_lock_dir(settings) -> Path:
    return capture_lock_dir(settings, "forensic")


def _safe_segment(value: str | None) -> str:
    """Mirror publish_disk_to_host.sh sanitization (tr -cd 'a-zA-Z0-9.-')."""
    return re.sub(r"[^A-Za-z0-9.-]", "", value or "")


def memory_dump_filename(distro: str, version: str, kernel_release: str | None) -> str:
    """Descriptive memory-dump name matching the sibling disk artifact's pattern."""
    safe_kernel = (kernel_release or "").replace("/", "-")
    return f"{_safe_segment(distro)}-{_safe_segment(version)}-kernel-{safe_kernel}-memory.dump"


def _build_job_capture_manifest(
    *,
    artifact_path: Path,
    kind: str,
    sha256: str,
    size_bytes: int,
    job_id: str,
    target_id: str,
    guest_ip: str,
    vm_name: str,
    distro: str,
    version: str,
    kernel_release: str | None,
    capture_method: str,
    forensic_caveat: str | None = None,
    raw_sha256: str | None = None,
    captured_at_utc: str | None = None,
    capture_finished_at_utc: str | None = None,
) -> dict:
    """Manifest for the *final* artifact path, built while bytes are still staged."""
    return build_artifact_manifest(
        artifact_path=artifact_path,
        kind=kind,
        producer="job_capture",
        sha256=sha256,
        size_bytes=size_bytes,
        job_id=job_id,
        vm_id=vm_name,
        vm_label="target",
        domain_name=vm_name,
        image_id=target_id,
        guest_ip=guest_ip,
        distro=distro,
        version=version,
        kernel_release=kernel_release,
        capture_method=capture_method,
        forensic_caveat=forensic_caveat,
        raw_sha256=raw_sha256,
        captured_at_utc=captured_at_utc,
        capture_finished_at_utc=capture_finished_at_utc,
    )


def _record_artifact(
    session,
    *,
    job_id: str,
    kind: str,
    producer: str,
    artifact_path: Path,
    sha256: str,
    size_bytes: int,
    manifest_path: Path,
    sidecar_path: Path,
    metadata_json: str | None = None,
):
    """Create the Artifact row inside the caller's transaction.

    Takes a session rather than a scope so the row, the journal's completion and
    any sibling state commit together. A separate commit here is what left an
    Artifact row behind when the step after it failed.
    """
    from ..db.repositories import ArtifactRepository

    return ArtifactRepository(session).create_artifact(
        kind=kind,
        producer=producer,
        path=str(artifact_path),
        filename=artifact_path.name,
        sha256=sha256,
        size_bytes=size_bytes,
        manifest_path=str(manifest_path),
        sha256_sidecar_path=str(sidecar_path),
        metadata_json=metadata_json,
        job_id=job_id,
    )


def dispatch_boot_capture_acquisition(
    *,
    run_isf_only_fn,
    acquire_and_publish_fn,
    isf_only: bool,
    job_id: str,
    action_id: int | None,
    target_id: str,
    guest_ip: str,
    runtime_vm_name: str,
    distro: str,
    version: str,
    kernel_release: str | None,
    guest_user: str,
    capture_type: str | None,
) -> None:
    if isf_only:
        run_isf_only_fn(job_id, action_id)
    else:
        acquire_and_publish_fn(
            job_id=job_id,
            target_id=target_id,
            guest_ip=guest_ip,
            vm_name=runtime_vm_name,
            distro=distro,
            version=version,
            action_id=action_id,
            kernel_release=kernel_release,
            guest_user=guest_user,
            capture_type=capture_type,
        )


def acquire_and_publish(
    *,
    session_scope,
    repo_cls,
    get_active_key_paths,
    run_command,
    settings,
    parse_keyed_output,
    check_host_disk_space,
    is_cancel_requested,
    job_disk_image_path,
    memory_dumper,
    conn,
    job_id: str,
    target_id: str,
    guest_ip: str,
    vm_name: str,
    distro: str,
    version: str,
    action_id: int | None,
    kernel_release: str | None = None,
    guest_user: str = "ubuntu",
    capture_type: str = "memory",
) -> None:
    """Acquire memory and/or disk artifacts under the shared per-VM capture lock."""
    with capture_lock(vm_name, base_dir=_forensic_capture_lock_dir(settings)):
        _acquire_and_publish_locked(
            session_scope=session_scope,
            repo_cls=repo_cls,
            get_active_key_paths=get_active_key_paths,
            run_command=run_command,
            settings=settings,
            parse_keyed_output=parse_keyed_output,
            check_host_disk_space=check_host_disk_space,
            is_cancel_requested=is_cancel_requested,
            job_disk_image_path=job_disk_image_path,
            memory_dumper=memory_dumper,
            conn=conn,
            job_id=job_id,
            target_id=target_id,
            guest_ip=guest_ip,
            vm_name=vm_name,
            distro=distro,
            version=version,
            action_id=action_id,
            kernel_release=kernel_release,
            guest_user=guest_user,
            capture_type=capture_type,
        )


def _domain_power_state(conn, vm_name: str) -> str:
    """Observe libvirt under the capture lock; cached runtime rows can lag shutdown."""
    try:
        active = conn.lookupByName(vm_name).isActive()
    except Exception as exc:
        raise RuntimeError(f"Cannot determine target VM power state: {vm_name}") from exc
    if active not in (0, 1):
        raise RuntimeError(f"Cannot determine target VM power state: {vm_name}")
    return "on" if active else "off"


def _acquire_and_publish_locked(
    session_scope,
    repo_cls,
    get_active_key_paths,
    run_command,
    settings,
    parse_keyed_output,
    check_host_disk_space,
    is_cancel_requested,
    job_disk_image_path,
    memory_dumper,
    conn,
    job_id: str,
    target_id: str,
    guest_ip: str,
    vm_name: str,
    distro: str,
    version: str,
    action_id: int | None,
    *,
    kernel_release: str | None = None,
    guest_user: str = "ubuntu",
    capture_type: str = "memory",
) -> None:
    """Acquire memory and/or disk artifacts, then publish completion state."""
    windows_guest = distro.strip().lower().startswith("win")
    if windows_guest and capture_type == "both":
        # The live disk helper executes Linux shell commands. Reject the whole
        # request before publishing memory from an unsupported combined capture.
        raise RuntimeError(
            "Windows combined memory/disk capture is unavailable; capture memory while running, "
            "then shut down the VM cleanly for a separate disk capture"
        )
    ssh_priv, _ = get_active_key_paths()

    if capture_type in ("memory", "both"):
        if not fake_mode.FAKE_LIBVIRT and _domain_power_state(conn, vm_name) == "off":
            raise RuntimeError("Target VM is powered off; memory capture is unavailable")
        with session_scope() as session:
            repo = repo_cls(session)
            repo.transition_job_status(job_id, "acquiring_memory")
            repo.add_event(job_id, "acquiring_memory", "Starting native memory dump")

        if is_cancel_requested(job_id):
            raise RuntimeError("cancel_requested")

        check_host_disk_space(job_id)

        dump_dir = Path(settings.storage_dir) / "dumps" / job_id
        ensure_evidence_dir(dump_dir, storage_root(settings))

        acl_helper: Path | None = None
        if not fake_mode.FAKE_LIBVIRT:
            acl_helper = fix_libvirt_path_acl_helper()
            run_command(
                ["sudo", "-n", str(acl_helper), str(dump_dir)],
                job_id=job_id,
            )

        output_path = dump_dir / memory_dump_filename(distro, version, kernel_release)

        capture_started_at = datetime.now(UTC).isoformat()
        # Staging sits inside the job's dump directory, not under
        # <storage_root>/.publish: libvirt writes the dump through the ACL grants
        # applied to that tree, and a directory outside it would not carry them.
        with publication.attempt(
            session_scope,
            producer="job_capture",
            kind="memory",
            final_path=output_path,
            staging_parent=dump_dir,
            job_id=job_id,
        ) as attempt:
            if acl_helper is not None:
                run_command(
                    ["sudo", "-n", str(acl_helper), str(attempt.staging_dir)],
                    job_id=job_id,
                )
            with attempt.capture_to_path() as staged_path:
                memory_dumper.dump(conn, vm_name, staged_path, cancel_check=lambda: is_cancel_requested(job_id))
            capture_finished_at = datetime.now(UTC).isoformat()

            # Refresh directory ACLs now that the dump file exists so the helper
            # can apply file-level ACLs without being passed a file path.
            if acl_helper is not None:
                run_command(
                    ["sudo", "-n", str(acl_helper), str(attempt.staging_dir)],
                    job_id=job_id,
                )

            memory_dumper.validate(staged_path)

            if capture_type == "memory":
                # memory-only: advance to publishing_artifact now
                with session_scope() as session:
                    repo = repo_cls(session)
                    repo.transition_job_status(job_id, "publishing_artifact")
                    repo.add_event(job_id, "publishing_artifact", "Memory dump written to host storage")
            else:
                # "both": disk capture follows — stay in acquiring_memory, add event only
                with session_scope() as session:
                    repo = repo_cls(session)
                    repo.add_event(job_id, "acquiring_memory", "Memory dump written to host storage")

            def build_memory_manifest(sha256: str, size_bytes: int) -> dict:
                return _build_job_capture_manifest(
                    artifact_path=output_path,
                    kind="memory",
                    sha256=sha256,
                    size_bytes=size_bytes,
                    job_id=job_id,
                    target_id=target_id,
                    guest_ip=guest_ip,
                    vm_name=vm_name,
                    distro=distro,
                    version=version,
                    kernel_release=kernel_release,
                    capture_method="memory_dump",
                    captured_at_utc=capture_started_at,
                    capture_finished_at_utc=capture_finished_at,
                )

            triad = attempt.stage(build_memory_manifest)
            published = attempt.publish()

            # One transaction: the Artifact row and the journal's completion land
            # together or not at all.
            with session_scope() as session:
                artifact = _record_artifact(
                    session,
                    job_id=job_id,
                    kind="memory",
                    producer="job_capture",
                    artifact_path=published.artifact_path,
                    sha256=triad.sha256,
                    size_bytes=triad.size_bytes,
                    manifest_path=published.manifest_path,
                    sidecar_path=published.sidecar_path,
                )
                attempt.complete(session, artifact_id=artifact.id)
            attempt.finish()
        if is_cancel_requested(job_id):
            raise RuntimeError("cancel_requested")

    if capture_type in ("disk", "both"):
        with session_scope() as session:
            repo = repo_cls(session)
            repo.transition_job_status(job_id, "acquiring_disk")
            repo.add_event(job_id, "acquiring_disk", "Starting disk acquisition")

        if is_cancel_requested(job_id):
            raise RuntimeError("cancel_requested")

        disk_capture_started_at = datetime.now(UTC).isoformat()

        if fake_mode.FAKE_LIBVIRT:
            with session_scope() as session:
                repo = repo_cls(session)
                target_runtime = repo.get_runtime(job_id, "target")
                target_power_state = target_runtime.power_state if target_runtime is not None else None
        else:
            # Shutdown is asynchronous: the runtime row may still say "on"
            # after libvirt has stopped the domain. Recheck after memory capture
            # too, so combined acquisition does not reuse an earlier observation.
            target_power_state = _domain_power_state(conn, vm_name)

        if windows_guest and target_power_state != "off":
            raise RuntimeError(
                "Windows disk capture requires the VM to be shut down cleanly; live capture is unavailable"
            )

        if target_power_state == "off":
            disk_source = job_disk_image_path(job_id, target_id)
            if not fake_mode.FAKE_LIBVIRT:
                disk_source = current_job_disk(conn, vm_name, disk_source)
            disk_output = run_command(
                [
                    "bash",
                    str(Path(settings.base_dir) / "ops" / "host" / "acquire_disk_host.sh"),
                    job_id,
                    target_id,
                    distro,
                    version,
                    kernel_release or "",
                    str(disk_source),
                ],
                cwd=settings.base_dir,
                job_id=job_id,
                extra_env={"ASTINUSLAB_STORAGE_DIR": str(settings.storage_dir)},
            )
            with session_scope() as session:
                repo = repo_cls(session)
                repo.add_event(job_id, "acquiring_disk", "Disk acquisition running in offline host mode")
        else:
            disk_output = run_command(
                [
                    "bash",
                    str(Path(settings.base_dir) / "ops" / "host" / "acquire_disk_remote.sh"),
                    job_id,
                    guest_ip,
                    str(ssh_priv),
                    distro,
                    version,
                    kernel_release or "",
                ],
                cwd=settings.base_dir,
                job_id=job_id,
                extra_env={"GUEST_USER": guest_user, "ASTINUSLAB_STORAGE_DIR": str(settings.storage_dir)},
            )
            with session_scope() as session:
                repo = repo_cls(session)
                repo.add_event(
                    job_id,
                    "acquiring_disk",
                    "Disk acquisition ran through the LIVE guest (non-forensic copy): "
                    "filesystem was mounted and guest-mediated during read",
                )
        disk_capture_finished_at = datetime.now(UTC).isoformat()
        disk_parsed = parse_keyed_output(disk_output)
        if not disk_parsed.get("disk_final_path"):
            raise RuntimeError("Disk acquisition did not report disk_final_path")

        # disk_publish_stage is informational (for the log message) but must NOT
        # gate the state transition. The state machine requires:
        #   acquiring_disk → publishing_artifact → completed
        # If this transition is skipped, the final transition to 'completed' below
        # would attempt acquiring_disk → completed, which is not an allowed edge
        # and raises InvalidJobTransitionError, leaving the job stuck permanently.
        disk_publish_stage = disk_parsed.get("disk_publish_stage")
        publish_msg = f"Disk artifact ready: {disk_publish_stage}" if disk_publish_stage else "Disk artifact ready"
        with session_scope() as session:
            repo = repo_cls(session)
            repo.transition_job_status(job_id, "publishing_artifact")
            repo.add_event(job_id, "publishing_artifact", publish_msg)

        disk_dump_path = disk_parsed.get("disk_final_path")
        if disk_dump_path:
            disk_stat = Path(disk_dump_path).stat()
            if disk_stat.st_size == 0:
                raise RuntimeError(f"Disk artifact rejected: zero-length dump: {disk_dump_path}")
            disk_raw_sha256 = disk_parsed.get("disk_raw_sha256")
            disk_artifact_path = Path(disk_dump_path)
            disk_capture_method = "offline_disk" if target_power_state == "off" else "remote_disk"

            # The acquisition script writes the disk image at a path of its own
            # choosing - it runs on the host, and over SSH in the remote case - so
            # the attempt adopts that file instead of staging it. What the journal
            # adds here is a durable record of the publication and a manifest and
            # sidecar that appear as a pair.
            def build_disk_manifest(sha256: str, size_bytes: int) -> dict:
                return _build_job_capture_manifest(
                    artifact_path=disk_artifact_path,
                    kind="disk",
                    sha256=sha256,
                    size_bytes=size_bytes,
                    job_id=job_id,
                    target_id=target_id,
                    guest_ip=guest_ip,
                    vm_name=vm_name,
                    distro=distro,
                    version=version,
                    kernel_release=kernel_release,
                    capture_method=disk_capture_method,
                    forensic_caveat=_LIVE_DISK_CAVEAT if disk_capture_method == "remote_disk" else None,
                    raw_sha256=disk_raw_sha256,
                    captured_at_utc=disk_capture_started_at,
                    capture_finished_at_utc=disk_capture_finished_at,
                )

            with publication.attempt(
                session_scope,
                producer="job_capture",
                kind="disk",
                final_path=disk_artifact_path,
                staging_parent=disk_artifact_path.parent,
                job_id=job_id,
            ) as disk_attempt:
                disk_triad = disk_attempt.adopt(disk_artifact_path, build_disk_manifest)
                disk_published = disk_attempt.publish_sidecars()

                with session_scope() as session:
                    disk_artifact = _record_artifact(
                        session,
                        job_id=job_id,
                        kind="disk",
                        producer="job_capture",
                        artifact_path=disk_artifact_path,
                        sha256=disk_triad.sha256,
                        size_bytes=disk_triad.size_bytes,
                        manifest_path=disk_published.manifest_path,
                        sidecar_path=disk_published.sidecar_path,
                        metadata_json=json.dumps(
                            {
                                "capture_method": disk_capture_method,
                                "live_acquisition": disk_capture_method == "remote_disk",
                                "raw_sha256": disk_raw_sha256,
                            },
                            sort_keys=True,
                        ),
                    )
                    disk_attempt.complete(session, artifact_id=disk_artifact.id)
                disk_attempt.finish()
            if is_cancel_requested(job_id):
                raise RuntimeError("cancel_requested")

    if is_cancel_requested(job_id):
        raise RuntimeError("cancel_requested")

    # Dump metadata is persisted as Artifact rows by _record_artifact above —
    # the Artifact table is the single source of truth for capture artifacts.
    with session_scope() as session:
        repo = repo_cls(session)
        repo.transition_job_status(
            job_id,
            "completed",
            finished_at=datetime.now(UTC),
        )
        repo.add_event(job_id, "completed", "Job completed")
        if action_id is not None:
            repo.update_action(action_id, status="completed", finished_at=datetime.now(UTC), active_pid=None)
