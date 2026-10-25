"""The Playground acquisition pipeline: capture, manifest, seal, publish.

Split out of `orchestrator.py` so that module stays readable. The two Celery
tasks stay there -- `playground.capture_memory` and `playground.capture_disk`,
with the guards that refuse a capture -- and everything they do once they hold
the capture lock is here.

`settings`, `session_scope`, `libvirt` and the shared primitives this module
needs are reached through the orchestrator module object (`po.<name>`) rather
than imported by name. That is not style. `orchestrator` is what the tests
monkeypatch -- `po.settings`, `po.session_scope`, `po.libvirt` and
`po._create_directory_no_follow` among them -- and a `from .orchestrator import
settings` here would bind the original at import time, so a patched value would
silently not apply to this module. Attribute access resolves at call time and
keeps that working.

Names this module *owns* are called bare, because a bare global resolves through
this module's own namespace at call time; patching `acquisition._dump_memory`
reaches every caller here. Tests that used to patch `orchestrator._dump_memory`,
`_disk_capture`, `_build_capture_manifest` or `_seal_acquisition` must patch this
module instead -- `monkeypatch.setattr` raises on an absent attribute, so that
mistake fails loudly rather than silently running the real capture.

`orchestrator.py` imports this module inside the two task bodies rather than at
module scope, which is what keeps the two from forming an import cycle.
"""

from __future__ import annotations

import logging
import secrets
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from ..core import fake_mode
from ..core.locks import capture_lock_dir
from ..storage import publication
from ..storage.artifacts import build_artifact_manifest
from ..virtualization.host_helpers import (
    EVIDENCE_DIR_MODE,
    ensure_evidence_dir,
    fix_libvirt_path_acl_helper,
    storage_root,
)
from . import orchestrator as po
from .captures import DiskCapture

logger = logging.getLogger(__name__)

_CAPTURE_METHODS = {"memory": "memory_dump", "disk": "pause_convert"}


def _playground_capture_lock_dir() -> Path:
    return capture_lock_dir(po.settings, "playground")


def _dump_memory(domain_name: str, output_path: Path) -> dict:
    if fake_mode.FAKE_PLAYGROUND:
        logger.warning(
            "FAKE MODE: writing synthetic memory dump for %s; this is not evidence",
            domain_name,
        )
        ensure_evidence_dir(output_path.parent, storage_root(po.settings))
        output_path.write_bytes(b"astinuslab fake memory dump\n")
        return {"path": str(output_path), "size": output_path.stat().st_size}
    if po.libvirt is None:
        raise RuntimeError("libvirt is required for playground memory capture")
    from ..forensics.memory_dump import MemoryDumper

    dumper = MemoryDumper(libvirt_module=po.libvirt)
    conn = po.libvirt.open(po.settings.libvirt_uri)
    if conn is None:
        raise RuntimeError(f"failed to open libvirt connection: {po.settings.libvirt_uri}")
    try:
        output_path = Path(output_path)
        ensure_evidence_dir(output_path.parent, storage_root(po.settings))
        # qemu:///system writes the core dump as root; bracket the dump with the
        # privileged ACL helper so the worker user can read the file back to
        # validate and hash it. Mirrors the forensics capture path.
        acl_helper = fix_libvirt_path_acl_helper()
        po._apply_capture_acl(acl_helper, output_path.parent)
        dumper.dump(conn, domain_name, output_path)
        po._apply_capture_acl(acl_helper, output_path.parent)
        dumper.validate(output_path)
        return {"path": str(output_path), "size": output_path.stat().st_size}
    finally:
        close_fn = getattr(conn, "close", None)
        if callable(close_fn):
            close_fn()


def _disk_capture() -> DiskCapture:
    if fake_mode.FAKE_PLAYGROUND:

        def _fake_run(cmd, **kwargs):
            if cmd[:2] == ["qemu-img", "convert"]:
                logger.warning(
                    "FAKE MODE: writing synthetic disk image to %s; this is not evidence",
                    cmd[-1],
                )
                Path(cmd[-1]).write_bytes(b"astinuslab fake disk image\n")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        return DiskCapture(run_command=_fake_run, mode="pause_convert", libvirt_uri=po.settings.libvirt_uri)

    def _run_command(cmd, **kwargs):
        kwargs.pop("job_id", None)
        kwargs.setdefault("check", True)
        kwargs.setdefault("timeout", po.settings.playground_capture_timeout_seconds)
        return subprocess.run(cmd, **kwargs)

    return DiskCapture(run_command=_run_command, mode="pause_convert", libvirt_uri=po.settings.libvirt_uri)


def capture_clean_layer(*, domain_name: str, source_disk_path: str, output_path: Path) -> Path:
    """Capture creator-side clean-start bytes through the pause/convert path."""

    return _disk_capture().capture_clean_layer(
        domain_name=domain_name, source_disk_path=source_disk_path, output_path=output_path
    )


def record_acquisition_event(acquisition_id: str, status: str, message: str) -> None:
    with po.session_scope() as s:
        from ..db.models import AcquisitionEvent

        s.add(AcquisitionEvent(acquisition_id=acquisition_id, status=status, message=message))


def _upsert_playground_acquisition(
    *,
    acquisition_id: str,
    vm_id: str,
    name: str,
    status: str,
    kind: str,
    session_id: str,
    vm_label: str,
    path: str | None = None,
    sha256: str | None = None,
    size_bytes: int | None = None,
) -> None:
    with po.session_scope() as s:
        from ..db.models import Acquisition, JobVM

        acq = s.query(Acquisition).filter_by(id=acquisition_id).one_or_none()
        # ``vm_id`` is normally a real JobVM, but the low-level capture helper
        # is also used to record failures before VM registration completes.
        # Do not violate the acquisition FK in that recovery path; the
        # session/label fields still retain the diagnostic ownership context.
        vm_reference = vm_id if s.get(JobVM, vm_id) is not None else None
        if acq is None:
            acq = Acquisition(id=acquisition_id, name=name, job_vm_id=vm_reference)
            s.add(acq)
        acq.name = name
        acq.status = status
        acq.job_vm_id = vm_reference
        acq.path = path
        acq.sha256 = sha256
        acq.size_bytes = size_bytes
        acq.kind = kind
        acq.session_id = session_id
        acq.vm_label = vm_label
        if status in {"completed", "failed"}:
            acq.sealed_at = datetime.now(UTC)
    record_acquisition_event(acquisition_id, status, name)


def _seal_acquisition(
    session,
    *,
    acquisition_id: str,
    vm_id: str,
    name: str,
    status: str,
    kind: str,
    session_id: str,
    vm_label: str,
    path: str | None = None,
    sha256: str | None = None,
    size_bytes: int | None = None,
) -> None:
    """Move an Acquisition to its terminal state inside the caller's transaction.

    Deliberately takes a session rather than opening one: the Artifact row, this
    terminal state and the terminal event must commit together. Three separate
    commits are what left an Acquisition permanently `running` when a worker died
    between them.
    """
    from ..db.models import Acquisition, AcquisitionEvent

    acq = session.query(Acquisition).filter_by(id=acquisition_id).one_or_none()
    if acq is None:
        acq = Acquisition(id=acquisition_id, name=name, job_vm_id=vm_id)
        session.add(acq)
    acq.name = name
    acq.status = status
    acq.job_vm_id = vm_id
    acq.path = path
    acq.sha256 = sha256
    acq.size_bytes = size_bytes
    acq.kind = kind
    acq.session_id = session_id
    acq.vm_label = vm_label
    if status in {"completed", "failed"}:
        acq.sealed_at = datetime.now(UTC)
    session.add(AcquisitionEvent(acquisition_id=acquisition_id, status=status, message=name))


def _mark_capture_already_running(
    *, acquisition_id: str | None, vm_id: str, job_id: str, vm_label: str, kind: str
) -> str:
    if acquisition_id is None:
        raise RuntimeError(f"capture already in progress for {vm_id}")
    message = f"capture already in progress for {vm_id}"
    _upsert_playground_acquisition(
        acquisition_id=acquisition_id,
        vm_id=vm_id,
        name=f"playground-{vm_id}-{kind}-failed",
        status="failed",
        kind=kind,
        session_id=job_id,
        vm_label=vm_label,
    )
    record_acquisition_event(acquisition_id, "failed", message)
    return acquisition_id


def _build_capture_manifest(
    *,
    artifact_path: Path,
    kind: str,
    vm_id: str,
    session_id: str,
    vm_label: str,
    acquisition_id: str,
    sha256: str,
    size_bytes: int,
    domain_name: str,
    vm_state_at_capture_start: str | None = None,
    captured_at_utc: str | None = None,
    capture_finished_at_utc: str | None = None,
) -> dict:
    """Build the manifest for the *final* artifact path.

    The bytes are still in the attempt directory when this runs, but the manifest
    describes where they will live, so publication is a link and not a rewrite.
    """
    with po.session_scope() as s:
        from ..db.models import JobNetwork, JobVM

        vm = s.query(JobVM).filter_by(id=vm_id).one()
        image_id = vm.image_id
        workload_id = vm.workload_id
        disk_path = vm.disk_path
        ordered_nics = sorted(vm.nics, key=lambda nic: nic.ordinal)
        nics_payload = [
            {
                "nic_id": nic.id,
                "ordinal": nic.ordinal,
                "mac": nic.mac,
                "ip": nic.ip,
                "network_id": nic.job_network_id,
            }
            for nic in ordered_nics
        ]
        # A NIC ordinal is guest-visible interface identity.  Keep the first
        # appearance of each attached network in that same authoritative order
        # rather than replacing it with a lexical database identifier sort.
        network_ids = list(dict.fromkeys(nic.job_network_id for nic in ordered_nics))
        networks_by_id = {
            network.id: network for network in s.query(JobNetwork).filter(JobNetwork.id.in_(network_ids)).all()
        }
        if network_ids:
            networks_payload = [
                {
                    "id": net.id,
                    "name": net.name,
                    "subnet_cidr": net.subnet_cidr,
                    "isolated": net.isolated,
                    "libvirt_net_name": net.libvirt_net_name,
                    "state": net.state,
                }
                for network_id in network_ids
                if (net := networks_by_id.get(network_id)) is not None
            ]
        else:
            networks_payload = []
    domain_xml = po._domain_xml(domain_name)

    manifest = build_artifact_manifest(
        artifact_path=artifact_path,
        kind=kind,
        producer="playground_capture",
        sha256=sha256,
        size_bytes=size_bytes,
        session_id=session_id,
        acquisition_id=acquisition_id,
        vm_id=vm_id,
        vm_label=vm_label,
        domain_name=domain_name,
        image_id=image_id,
        workload_id=workload_id,
        disk_path=disk_path,
        capture_method=_CAPTURE_METHODS.get(kind),
        vm_state_at_capture_start=vm_state_at_capture_start,
        captured_at_utc=captured_at_utc,
        capture_finished_at_utc=capture_finished_at_utc,
        network_ids=network_ids,
        nics=nics_payload,
        networks=networks_payload,
        domain_xml=domain_xml,
    )
    return manifest


def _playground_capture_failure_message(kind: str, exc: Exception) -> str | None:
    if kind != "disk" or not isinstance(exc, subprocess.TimeoutExpired):
        return None
    timeout = exc.timeout
    if isinstance(timeout, int | float):
        timeout_text = f"{timeout:g}s"
    else:
        timeout_text = "the configured timeout"
    return f"Disk capture timed out after {timeout_text}; VM resumed; see Troubleshooting -> A capture appears stuck"


def _capture_locked(
    *,
    kind: str,
    extension: str,
    do_capture,
    vm_id: str,
    acquisition_id: str | None,
    job_id: str,
    vm_label: str,
    domain_name: str,
) -> str:
    po._validate_playground_name("vm_id", vm_id)
    po._validate_playground_name("job_id", job_id)
    capture_root = po.settings.playground_capture_dir.resolve()
    out_dir = po.settings.playground_capture_dir / job_id / vm_id
    if not out_dir.resolve().is_relative_to(capture_root):
        raise RuntimeError(f"playground capture path escapes root: {out_dir}")
    ensure_evidence_dir(capture_root, storage_root(po.settings))
    # `final_mode` reaches the last component only, and the ACL helper inspects
    # both, so the session directory is given its mode by its own call.
    po._create_directory_no_follow(capture_root, job_id, final_mode=EVIDENCE_DIR_MODE)
    po._create_directory_no_follow(capture_root, job_id, vm_id, final_mode=EVIDENCE_DIR_MODE)
    acq_id = acquisition_id or secrets.token_hex(8)
    marker = "mem" if kind == "memory" else "disk"
    name = f"{marker}-{acq_id}"
    out_path = out_dir / f"{name}.{extension}"
    _upsert_playground_acquisition(
        acquisition_id=acq_id,
        vm_id=vm_id,
        name=name,
        status="running",
        kind=kind,
        session_id=job_id,
        vm_label=vm_label,
    )
    try:
        # Staging sits inside the capture directory rather than under
        # <storage_root>/.publish: libvirt writes the dump as root through the
        # ACL and AppArmor grants attached to the evidence tree, and a directory
        # outside that tree would not carry them.
        with publication.attempt(
            po.session_scope,
            producer="playground_capture",
            kind=kind,
            final_path=out_path,
            staging_parent=out_dir,
            acquisition_id=acq_id,
            session_id=job_id,
        ) as attempt:
            captured_at_utc = datetime.now(UTC).isoformat()
            with attempt.capture_to_path() as staged_path:
                do_capture(staged_path)
            capture_finished_at_utc = datetime.now(UTC).isoformat()

            def build_manifest(sha256: str, size_bytes: int) -> dict:
                return _build_capture_manifest(
                    artifact_path=out_path,
                    kind=kind,
                    vm_id=vm_id,
                    session_id=job_id,
                    vm_label=vm_label,
                    acquisition_id=acq_id,
                    sha256=sha256,
                    size_bytes=size_bytes,
                    domain_name=domain_name,
                    vm_state_at_capture_start="running",
                    captured_at_utc=captured_at_utc,
                    capture_finished_at_utc=capture_finished_at_utc,
                )

            triad = attempt.stage(build_manifest)
            published = attempt.publish()

            # One transaction: the Artifact row, the sealed Acquisition, its
            # terminal event, and the journal's completion either all land or
            # none of them do.
            with po.session_scope() as s:
                from ..db.repositories import ArtifactRepository

                artifact = ArtifactRepository(s).create_artifact(
                    kind=kind,
                    producer="playground_capture",
                    path=str(published.artifact_path),
                    filename=published.artifact_path.name,
                    sha256=triad.sha256,
                    size_bytes=triad.size_bytes,
                    manifest_path=str(published.manifest_path),
                    sha256_sidecar_path=str(published.sidecar_path),
                    session_id=job_id,
                    acquisition_id=acq_id,
                )
                _seal_acquisition(
                    s,
                    acquisition_id=acq_id,
                    vm_id=vm_id,
                    name=name,
                    status="completed",
                    kind=kind,
                    session_id=job_id,
                    vm_label=vm_label,
                    path=str(published.artifact_path),
                    sha256=triad.sha256,
                    size_bytes=triad.size_bytes,
                )
                attempt.complete(s, artifact_id=artifact.id)
            attempt.finish()
    except Exception as exc:
        failure_message = _playground_capture_failure_message(kind, exc)
        _upsert_playground_acquisition(
            acquisition_id=acq_id,
            vm_id=vm_id,
            name=name,
            status="failed",
            kind=kind,
            session_id=job_id,
            vm_label=vm_label,
        )
        if failure_message is not None:
            record_acquisition_event(acq_id, "failed", failure_message)
            raise RuntimeError(failure_message) from exc
        raise
    return acq_id


def _capture_memory_locked(
    *, vm_id: str, acquisition_id: str | None, job_id: str, vm_label: str, domain_name: str
) -> str:
    return _capture_locked(
        kind="memory",
        extension="elf",
        do_capture=lambda out_path: _dump_memory(domain_name, out_path),
        vm_id=vm_id,
        acquisition_id=acquisition_id,
        job_id=job_id,
        vm_label=vm_label,
        domain_name=domain_name,
    )


def _capture_disk_locked(
    *, vm_id: str, acquisition_id: str | None, job_id: str, vm_label: str, domain_name: str, disk_path: str
) -> str:
    return _capture_locked(
        kind="disk",
        extension="qcow2",
        do_capture=lambda out_path: _disk_capture().capture(
            domain_name=domain_name,
            source_disk_path=disk_path,
            output_path=out_path,
        ),
        vm_id=vm_id,
        acquisition_id=acquisition_id,
        job_id=job_id,
        vm_label=vm_label,
        domain_name=domain_name,
    )
