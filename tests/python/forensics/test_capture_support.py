"""Tests for capture_support.py — Phase 3 native memory dump path."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from astinuslab.core import fake_mode
from astinuslab.core.locks import CaptureLockBusy, capture_lock

_DISK_RAW_SHA256 = "cafe1234cafe1234cafe1234cafe1234cafe1234cafe1234cafe1234cafe1234"


def _domain_connection(active=1):
    conn = MagicMock()
    conn.lookupByName.return_value.isActive.return_value = active
    return conn


def _make_args(
    tmp_path,
    *,
    capture_type="memory",
    memory_dumper=None,
    conn=None,
    vm_name="astinuslab-job-test",
    is_cancel_requested=None,
):
    """Build keyword arguments for acquire_and_publish."""
    settings = MagicMock()
    settings.base_dir = tmp_path
    settings.data_dir = tmp_path
    settings.storage_dir = tmp_path / "storage"

    @contextmanager
    def session_scope():
        yield MagicMock()

    if memory_dumper is None:

        def _write_elf(c, v, path, *, cancel_check=lambda: False):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\x7fELF" + b"\x00" * 100)

        memory_dumper = MagicMock()
        memory_dumper.dump.side_effect = _write_elf
        memory_dumper.validate.return_value = None

    if conn is None:
        conn = MagicMock()
        conn.lookupByName.return_value.isActive.return_value = 1
    return dict(
        session_scope=session_scope,
        repo_cls=lambda s: MagicMock(),
        get_active_key_paths=lambda: (tmp_path / "id", tmp_path / "id.pub"),
        run_command=lambda cmd, **kwargs: "abc123  memory.dump",
        settings=settings,
        parse_keyed_output=lambda o: {},
        check_host_disk_space=lambda job_id: None,
        is_cancel_requested=is_cancel_requested or (lambda job_id: False),
        job_disk_image_path=lambda j, t: tmp_path / "disk.qcow2",
        memory_dumper=memory_dumper,
        conn=conn or MagicMock(),
        job_id="job-test",
        target_id="target-ubuntu",
        guest_ip="10.0.0.1",
        vm_name=vm_name,
        distro="ubuntu",
        version="22.04",
        action_id=None,
        capture_type=capture_type,
    )


def test_acquire_and_publish_rejects_when_shared_capture_lock_is_held(tmp_path):
    from astinuslab.forensics.capture_support import acquire_and_publish

    memory_dumper = MagicMock()
    args = _make_args(tmp_path, memory_dumper=memory_dumper, vm_name="astinuslab-job-locked")
    args["settings"].storage_dir = tmp_path / "storage"

    with capture_lock("astinuslab-job-locked", base_dir=tmp_path / "storage" / "tmp" / "forensic-capture-locks"):
        with pytest.raises(CaptureLockBusy):
            acquire_and_publish(**args)

    memory_dumper.dump.assert_not_called()


@pytest.mark.parametrize("distro", ["ubuntu", "windows-server"])
def test_memory_branch_calls_memory_dumper(tmp_path, distro):
    from astinuslab.forensics.capture_support import acquire_and_publish

    memory_dumper = MagicMock()

    def _write_elf(conn, vm_name, path, *, cancel_check=lambda: False):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x7fELF" + b"\x00" * 100)

    memory_dumper.dump.side_effect = _write_elf
    memory_dumper.validate.return_value = None

    args = _make_args(tmp_path, memory_dumper=memory_dumper)
    args["distro"] = distro
    acquire_and_publish(**args)

    memory_dumper.dump.assert_called_once()
    call_args = memory_dumper.dump.call_args
    assert call_args.args[1] == "astinuslab-job-test"
    # Since Task 20 the dumper writes into an attempt directory; the published
    # name is asserted below.
    staged = call_args.args[2]
    assert staged.parent.name.startswith(".publish-")
    assert (tmp_path / "storage" / "dumps" / "job-test" / f"{distro}-22.04-kernel--memory.dump").exists()
    memory_dumper.validate.assert_called_once()


def test_memory_manifest_records_capture_window(tmp_path):
    from astinuslab.forensics.capture_support import acquire_and_publish

    memory_dumper = MagicMock()

    def _write_elf(conn, vm_name, path, *, cancel_check=lambda: False):
        Path(path).write_bytes(b"ELF")

    memory_dumper.dump.side_effect = _write_elf
    memory_dumper.validate.return_value = None

    acquire_and_publish(**_make_args(tmp_path, memory_dumper=memory_dumper))

    manifest_path = tmp_path / "storage" / "dumps" / "job-test" / "ubuntu-22.04-kernel--memory.dump.manifest.json"
    data = json.loads(manifest_path.read_text())
    assert data["captured_at_utc"] is not None
    assert data["capture_finished_at_utc"] is not None
    assert data["captured_at_utc"] <= data["capture_finished_at_utc"]
    assert "host_clock_monotonic_ns" not in data


def test_memory_branch_raises_on_cancel(tmp_path):
    from astinuslab.forensics.capture_support import acquire_and_publish

    args = _make_args(tmp_path, is_cancel_requested=lambda job_id: True)
    with pytest.raises(RuntimeError, match="cancel_requested"):
        acquire_and_publish(**args)


def test_memory_branch_output_path_is_under_job_dir(tmp_path):
    from astinuslab.forensics.capture_support import acquire_and_publish

    written_paths: list[Path] = []

    memory_dumper = MagicMock()

    def _capture_path(conn, vm_name, path, *, cancel_check=lambda: False):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x7fELF" + b"\x00" * 100)
        written_paths.append(path)

    memory_dumper.dump.side_effect = _capture_path
    memory_dumper.validate.return_value = None

    acquire_and_publish(**_make_args(tmp_path, memory_dumper=memory_dumper))

    assert len(written_paths) == 1
    # The attempt directory lives inside the job dump directory, and the
    # published artifact lands in the job directory itself.
    assert written_paths[0].parent.parent.name == "job-test"
    assert written_paths[0].parent.name.startswith(".publish-")
    published = tmp_path / "storage" / "dumps" / "job-test" / "ubuntu-22.04-kernel--memory.dump"
    assert published.exists()


def test_capture_honors_storage_dir_override(tmp_path):
    from astinuslab.forensics.capture_support import acquire_and_publish

    captured = {}

    def _capture_path(conn, vm_name, path, *, cancel_check=lambda: False):
        captured["path"] = path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x7fELF" + b"\x00" * 100)

    memory_dumper = MagicMock()
    memory_dumper.dump.side_effect = _capture_path
    memory_dumper.validate.return_value = None

    args = _make_args(tmp_path, memory_dumper=memory_dumper)
    args["settings"].storage_dir = tmp_path / "alt-root"

    acquire_and_publish(**args)

    job_dir = tmp_path / "alt-root" / "dumps" / "job-test"
    assert captured["path"].parent.parent == job_dir
    assert (job_dir / "ubuntu-22.04-kernel--memory.dump").exists()


def test_capture_and_analysis_share_job_dir_under_override(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import astinuslab.forensics.vol3_analysis as analysis

    monkeypatch.setattr(
        analysis,
        "settings",
        SimpleNamespace(storage_dir=tmp_path / "alt-root"),
    )
    output_dir = analysis._safe_output_dir("job-test")
    assert output_dir == (tmp_path / "alt-root" / "dumps" / "job-test" / "vol3").resolve()


def test_memory_branch_acl_helper_receives_job_dir_twice(tmp_path, monkeypatch):
    # Exercises the real privileged path: the suite runs under
    # ASTINUSLAB_FAKE_LIBVIRT=1, where the ACL helper is deliberately not invoked.
    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    from astinuslab.forensics.capture_support import acquire_and_publish

    calls: list[list[str]] = []

    def _fake_run_command(cmd, **kwargs):
        calls.append(cmd)
        if cmd and cmd[0] == "sha256sum":
            return f"abc123  {cmd[1]}"
        return ""

    memory_dumper = MagicMock()

    def _write_elf(conn, vm_name, path, *, cancel_check=lambda: False):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x7fELF" + b"\x00" * 100)

    memory_dumper.dump.side_effect = _write_elf
    memory_dumper.validate.return_value = None

    args = _make_args(tmp_path, memory_dumper=memory_dumper)
    args["run_command"] = _fake_run_command
    args["settings"].acl_allowed_storage_roots = [tmp_path]

    acquire_and_publish(**args)

    acl_helper_calls = [
        cmd
        for cmd in calls
        if len(cmd) >= 3
        and cmd[:3]
        == [
            "sudo",
            "-n",
            "/usr/local/libexec/astinuslab/fix_libvirt_path_acl.sh",
        ]
    ]
    job_dir = str(tmp_path / "storage" / "dumps" / "job-test")

    # The job directory is prepared once, then the attempt directory the
    # hypervisor actually writes into is bracketed before and after the dump.
    assert len(acl_helper_calls) == 3
    assert acl_helper_calls[0][3] == job_dir
    staged_dirs = [cmd[3] for cmd in acl_helper_calls[1:]]
    assert all(Path(directory).parent == Path(job_dir) for directory in staged_dirs)
    assert all(Path(directory).name.startswith(".publish-") for directory in staged_dirs)
    assert staged_dirs[0] == staged_dirs[1]


def test_disk_only_capture_completes_without_disk_publish_stage(tmp_path, in_memory_session_scope):
    """Disk-only capture must reach 'completed' even when the acquisition script
    does not emit a 'disk_publish_stage' key in its output.

    Regression test for:
        acquire_and_publish (disk) → acquiring_disk → (missing transition) → completed
        State machine only allows acquiring_disk → publishing_artifact → completed.
        Omitting the intermediate transition raises InvalidJobTransitionError.
    """
    from unittest.mock import MagicMock

    from astinuslab.api.schemas import JobCreate
    from astinuslab.db.repositories import JobRepository
    from astinuslab.forensics.capture_support import acquire_and_publish

    # Create a job and advance it to the pre-capture state
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="post_script",
                capture_type="disk",
            )
        )
        job_id = job.id
        for status in [
            "waiting_for_guest_access",
            "mounting_output_share",
            "running_scenario_script",
            "waiting_for_capture",
            "capture_requested",
        ]:
            repo.transition_job_status(job_id, status)

    # Create a fake non-empty disk file for the code to stat and checksum
    disk_file = tmp_path / "disk.qcow2"
    disk_file.write_bytes(b"QCOW" * 256)

    def _fake_run_command(cmd, **kwargs):
        if cmd and cmd[0] == "sha256sum":
            return f"deadbeef01234567  {cmd[1]}"
        return ""

    def _fake_parse_keyed(output):
        # Intentionally omit disk_publish_stage — this is the regression case
        return {
            "disk_final_path": str(disk_file),
            "disk_filename": "disk.qcow2",
        }

    settings_mock = MagicMock()
    settings_mock.base_dir = tmp_path
    settings_mock.data_dir = tmp_path
    settings_mock.storage_dir = tmp_path / "storage"
    settings_mock.acl_allowed_storage_roots = [tmp_path]

    # Must not raise InvalidJobTransitionError
    acquire_and_publish(
        session_scope=in_memory_session_scope,
        repo_cls=JobRepository,
        get_active_key_paths=lambda: (tmp_path / "key", tmp_path / "key.pub"),
        run_command=_fake_run_command,
        settings=settings_mock,
        parse_keyed_output=_fake_parse_keyed,
        check_host_disk_space=lambda job_id: None,
        is_cancel_requested=lambda job_id: False,
        job_disk_image_path=lambda j, t: tmp_path / "disk.qcow2",
        memory_dumper=MagicMock(),
        conn=_domain_connection(),
        job_id=job_id,
        target_id="ubuntu-22-04-default",
        guest_ip="10.0.0.1",
        vm_name="astinuslab-test",
        distro="ubuntu",
        version="22.04",
        action_id=None,
        capture_type="disk",
    )

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        stored_job = repo.get_job(job_id)
        assert stored_job is not None
        status = stored_job.status
        disk_dump_path = stored_job.disk_dump_path
        dump_checksum = stored_job.dump_checksum

    assert status == "completed", f"Expected 'completed', got '{status}'"
    assert disk_dump_path == str(disk_file)
    # The service hashes the adopted file; the stubbed `sha256sum` no longer
    # decides what the sidecar claims.
    disk_sha256 = hashlib.sha256(disk_file.read_bytes()).hexdigest()
    assert (disk_file.with_suffix(".qcow2.sha256")).read_text() == f"{disk_sha256}  {disk_file.name}\n"
    manifest = json.loads((disk_file.with_suffix(".qcow2.manifest.json")).read_text())
    assert manifest["schema_version"] == "artifact-manifest/v1"
    assert manifest["producer"] == "job_capture"
    assert manifest["job_id"] == job_id
    assert manifest["kind"] == "disk"
    assert manifest["sha256"] == disk_sha256
    assert manifest["capture"]["capture_method"] == "remote_disk"
    # Memory was not captured — checksum must be None, not empty string
    assert dump_checksum is None, f"dump_checksum should be None for disk-only capture, got {dump_checksum!r}"


def _run_disk_capture_with_power_state(
    tmp_path, in_memory_session_scope, power_state: str, *, conn=None, commands=None, distro="ubuntu"
):
    from astinuslab.api.schemas import JobCreate
    from astinuslab.db.repositories import JobRepository
    from astinuslab.forensics.capture_support import acquire_and_publish

    if conn is not None:
        source = tmp_path / "disk.qcow2"
        source.write_bytes(b"synthetic current disk")
        conn.lookupByName.return_value.XMLDesc.return_value = (
            f'<domain><devices><disk type="file" device="disk">'
            f'<source file="{source}"/><target dev="vda"/></disk></devices></domain>'
        )

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="post_script",
                capture_type="disk",
            )
        )
        job_id = job.id
        for status in [
            "waiting_for_guest_access",
            "mounting_output_share",
            "running_scenario_script",
            "waiting_for_capture",
            "capture_requested",
        ]:
            repo.transition_job_status(job_id, status)
        repo.upsert_runtime(job_id, "target", "astinuslab-test", power_state=power_state)

    disk_file = tmp_path / f"disk-{power_state}.qcow2"
    disk_file.write_bytes(b"QCOW" * 256)

    def _fake_run_command(cmd, **kwargs):
        if commands is not None:
            commands.append(cmd)
        if cmd and cmd[0] == "sha256sum":
            return f"deadbeef01234567  {cmd[1]}"
        return ""

    def _fake_parse_keyed(output):
        return {
            "disk_final_path": str(disk_file),
            "disk_filename": disk_file.name,
            "disk_raw_sha256": _DISK_RAW_SHA256,
        }

    settings_mock = MagicMock()
    settings_mock.base_dir = tmp_path
    settings_mock.data_dir = tmp_path
    settings_mock.storage_dir = tmp_path / "storage"
    settings_mock.acl_allowed_storage_roots = [tmp_path]

    acquire_and_publish(
        session_scope=in_memory_session_scope,
        repo_cls=JobRepository,
        get_active_key_paths=lambda: (tmp_path / "key", tmp_path / "key.pub"),
        run_command=_fake_run_command,
        settings=settings_mock,
        parse_keyed_output=_fake_parse_keyed,
        check_host_disk_space=lambda job_id: None,
        is_cancel_requested=lambda job_id: False,
        job_disk_image_path=lambda j, t: tmp_path / "disk.qcow2",
        memory_dumper=MagicMock(),
        conn=conn if conn is not None else _domain_connection(int(power_state != "off")),
        job_id=job_id,
        target_id="ubuntu-22-04-default",
        guest_ip="10.0.0.1",
        vm_name="astinuslab-test",
        distro=distro,
        version="22.04",
        action_id=None,
        capture_type="disk",
    )
    return job_id, disk_file


def test_live_disk_artifact_is_flagged_non_forensic(tmp_path, in_memory_session_scope):
    from astinuslab.db.models import Artifact, JobEvent

    job_id, _disk_file = _run_disk_capture_with_power_state(tmp_path, in_memory_session_scope, "on")

    with in_memory_session_scope() as session:
        artifact = session.query(Artifact).filter(Artifact.job_id == job_id, Artifact.kind == "disk").one()
        events = [
            (event.state, event.message) for event in session.query(JobEvent).filter(JobEvent.job_id == job_id).all()
        ]
        metadata_json = artifact.metadata_json
        manifest_path = artifact.manifest_path

    assert metadata_json is not None
    assert json.loads(metadata_json) == {
        "capture_method": "remote_disk",
        "live_acquisition": True,
        "raw_sha256": _DISK_RAW_SHA256,
    }
    assert manifest_path is not None
    manifest = json.loads(Path(manifest_path).read_text())
    assert manifest["capture"]["capture_method"] == "remote_disk"
    assert "guest-mediated" in manifest["capture"]["forensic_caveat"]
    assert manifest["integrity"]["raw_sha256"] == _DISK_RAW_SHA256
    assert any(state == "acquiring_disk" and "LIVE guest" in message for state, message in events)


def test_offline_disk_artifact_is_not_flagged(tmp_path, in_memory_session_scope):
    from astinuslab.db.models import Artifact, JobEvent

    job_id, _disk_file = _run_disk_capture_with_power_state(tmp_path, in_memory_session_scope, "off")

    with in_memory_session_scope() as session:
        artifact = session.query(Artifact).filter(Artifact.job_id == job_id, Artifact.kind == "disk").one()
        events = [
            (event.state, event.message) for event in session.query(JobEvent).filter(JobEvent.job_id == job_id).all()
        ]
        metadata_json = artifact.metadata_json
        manifest_path = artifact.manifest_path

    assert metadata_json is not None
    assert json.loads(metadata_json)["live_acquisition"] is False
    assert json.loads(metadata_json)["raw_sha256"] == _DISK_RAW_SHA256
    assert manifest_path is not None
    manifest = json.loads(Path(manifest_path).read_text())
    assert manifest["capture"]["capture_method"] == "offline_disk"
    assert manifest["capture"]["forensic_caveat"] is None
    assert manifest["integrity"]["raw_sha256"] == _DISK_RAW_SHA256
    assert not any("LIVE guest" in message for _state, message in events)


def test_memory_only_capture_leaves_disk_checksum_none(tmp_path, in_memory_session_scope):
    """When capture_type='memory', disk_dump_checksum must be None in the DB, not ''.

    An empty string is indistinguishable from a captured-but-empty checksum.
    None is the correct sentinel for 'not captured'.
    """
    from unittest.mock import MagicMock

    from astinuslab.api.schemas import JobCreate
    from astinuslab.db.repositories import JobRepository
    from astinuslab.forensics.capture_support import acquire_and_publish

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
                capture_type="memory",
            )
        )
        job_id = job.id
        for status in [
            "waiting_for_guest_access",
            "mounting_output_share",
            "waiting_for_capture",
            "capture_requested",
        ]:
            repo.transition_job_status(job_id, status)

    def _fake_run_command(cmd, **kwargs):
        if cmd and cmd[0] == "sha256sum":
            return f"aabbccdd00112233  {cmd[1]}"
        return ""

    def _fake_memory_dumper_dump(conn, vm_name, path, *, cancel_check=lambda: False):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x7fELF" + b"\x00" * 64)

    memory_dumper = MagicMock()
    memory_dumper.dump.side_effect = _fake_memory_dumper_dump
    memory_dumper.validate.return_value = None

    settings_mock = MagicMock()
    settings_mock.base_dir = tmp_path
    settings_mock.data_dir = tmp_path
    settings_mock.storage_dir = tmp_path / "storage"
    settings_mock.acl_allowed_storage_roots = [tmp_path]

    acquire_and_publish(
        session_scope=in_memory_session_scope,
        repo_cls=JobRepository,
        get_active_key_paths=lambda: (tmp_path / "key", tmp_path / "key.pub"),
        run_command=_fake_run_command,
        settings=settings_mock,
        parse_keyed_output=lambda o: {},
        check_host_disk_space=lambda job_id: None,
        is_cancel_requested=lambda job_id: False,
        job_disk_image_path=lambda j, t: tmp_path / "disk.qcow2",
        memory_dumper=memory_dumper,
        conn=_domain_connection(),
        job_id=job_id,
        target_id="ubuntu-22-04-default",
        guest_ip="10.0.0.1",
        vm_name="astinuslab-test",
        distro="ubuntu",
        version="22.04",
        action_id=None,
        capture_type="memory",
    )

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        stored_job = repo.get_job(job_id)
        assert stored_job is not None
        status = stored_job.status
        disk_dump_checksum = stored_job.disk_dump_checksum
        disk_dump_path = stored_job.disk_dump_path

    assert status == "completed"
    assert disk_dump_checksum is None
    assert disk_dump_path is None

    memory_path = tmp_path / "storage" / "dumps" / job_id / "ubuntu-22.04-kernel--memory.dump"
    sidecar_path = memory_path.with_suffix(".dump.sha256")
    manifest_path = memory_path.with_suffix(".dump.manifest.json")
    # The service hashes the staged bytes; the stubbed `sha256sum` no longer
    # decides what the sidecar claims.
    memory_sha256 = hashlib.sha256(b"\x7fELF" + b"\x00" * 64).hexdigest()
    assert sidecar_path.read_text() == f"{memory_sha256}  {memory_path.name}\n"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["schema_version"] == "artifact-manifest/v1"
    assert manifest["producer"] == "job_capture"
    assert manifest["job_id"] == job_id
    assert manifest["kind"] == "memory"
    assert manifest["sha256"] == memory_sha256
    assert manifest["target"]["guest_ip"] == "10.0.0.1"
    assert manifest["target"]["distro"] == "ubuntu"
    assert manifest["capture"]["capture_method"] == "memory_dump"


def test_both_capture_type_completes_with_memory_and_disk(tmp_path, in_memory_session_scope):
    """capture_type='both' must produce completed job with both memory and disk checksums set.

    This path is the most complex: memory is acquired first (status stays in
    acquiring_memory), then disk is acquired (acquiring_disk), then
    publishing_artifact, then completed. Both checksums must be non-None.
    """
    from unittest.mock import MagicMock

    from astinuslab.api.schemas import JobCreate
    from astinuslab.db.repositories import JobRepository
    from astinuslab.forensics.capture_support import acquire_and_publish

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="post_script",
                capture_type="both",
            )
        )
        job_id = job.id
        for status in [
            "waiting_for_guest_access",
            "mounting_output_share",
            "running_scenario_script",
            "waiting_for_capture",
            "capture_requested",
        ]:
            repo.transition_job_status(job_id, status)

    disk_file = tmp_path / "disk.qcow2"
    disk_file.write_bytes(b"QCOW" * 256)

    def _fake_run_command(cmd, **kwargs):
        if cmd and cmd[0] == "sha256sum":
            return f"cafebabe12345678  {cmd[1]}"
        return ""

    def _fake_memory_dumper_dump(conn, vm_name, path, *, cancel_check=lambda: False):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x7fELF" + b"\x00" * 64)

    memory_dumper = MagicMock()
    memory_dumper.dump.side_effect = _fake_memory_dumper_dump
    memory_dumper.validate.return_value = None

    settings_mock = MagicMock()
    settings_mock.base_dir = tmp_path
    settings_mock.data_dir = tmp_path
    settings_mock.storage_dir = tmp_path / "storage"
    settings_mock.acl_allowed_storage_roots = [tmp_path]

    acquire_and_publish(
        session_scope=in_memory_session_scope,
        repo_cls=JobRepository,
        get_active_key_paths=lambda: (tmp_path / "key", tmp_path / "key.pub"),
        run_command=_fake_run_command,
        settings=settings_mock,
        parse_keyed_output=lambda o: {
            "disk_final_path": str(disk_file),
            "disk_filename": "disk.qcow2",
            # disk_publish_stage intentionally absent (regression guard)
        },
        check_host_disk_space=lambda job_id: None,
        is_cancel_requested=lambda job_id: False,
        job_disk_image_path=lambda j, t: tmp_path / "disk.qcow2",
        memory_dumper=memory_dumper,
        conn=_domain_connection(),
        job_id=job_id,
        target_id="ubuntu-22-04-default",
        guest_ip="10.0.0.1",
        vm_name="astinuslab-test",
        distro="ubuntu",
        version="22.04",
        action_id=None,
        capture_type="both",
    )

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        stored_job = repo.get_job(job_id)
        assert stored_job is not None
        status = stored_job.status
        dump_checksum = stored_job.dump_checksum
        disk_dump_checksum = stored_job.disk_dump_checksum
        dump_path = stored_job.dump_path
        disk_dump_path = stored_job.disk_dump_path

    assert status == "completed", f"Expected 'completed', got '{status}'"
    assert dump_checksum is not None, "Memory checksum must be set for 'both' capture"
    assert disk_dump_checksum is not None, "Disk checksum must be set for 'both' capture"
    assert dump_path is not None
    assert disk_dump_path == str(disk_file)


# ---------------------------------------------------------------------------
# Task 20 — Job memory capture publishes through the recoverable journal
# ---------------------------------------------------------------------------


def _job_ready_for_capture(session_scope, capture_type="memory"):
    from astinuslab.api.schemas import JobCreate
    from astinuslab.db.repositories import JobRepository

    with session_scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="post_script",
                capture_type=capture_type,
            )
        )
        job_id = job.id
        for status in [
            "waiting_for_guest_access",
            "mounting_output_share",
            "running_scenario_script",
            "waiting_for_capture",
            "capture_requested",
        ]:
            repo.transition_job_status(job_id, status)
    return job_id


def _memory_args(tmp_path, session_scope, job_id, dumper=None):
    from unittest.mock import MagicMock

    from astinuslab.db.repositories import JobRepository

    settings_mock = MagicMock()
    settings_mock.base_dir = tmp_path
    settings_mock.data_dir = tmp_path
    settings_mock.storage_dir = tmp_path / "storage"

    if dumper is None:

        def _write_elf(conn, vm, path, *, cancel_check=lambda: False):
            path.write_bytes(ELF_BYTES)

        dumper = MagicMock()
        dumper.dump.side_effect = _write_elf
        dumper.validate.return_value = None

    return dict(
        session_scope=session_scope,
        repo_cls=JobRepository,
        get_active_key_paths=lambda: (tmp_path / "key", tmp_path / "key.pub"),
        run_command=lambda cmd, **kwargs: "unused  memory.dump",
        settings=settings_mock,
        parse_keyed_output=lambda output: {},
        check_host_disk_space=lambda job_id: None,
        is_cancel_requested=lambda job_id: False,
        job_disk_image_path=lambda j, t: tmp_path / "disk.qcow2",
        memory_dumper=dumper,
        conn=_domain_connection(),
        job_id=job_id,
        target_id="ubuntu-22-04-default",
        guest_ip="10.0.0.1",
        vm_name="astinuslab-job-test",
        distro="ubuntu",
        version="22.04",
        action_id=None,
        capture_type="memory",
        kernel_release="5.15.0-91-generic",
    )


ELF_BYTES = b"\x7fELF" + b"\x00" * 512


def test_memory_capture_publishes_through_the_journal(tmp_path, in_memory_session_scope):
    import hashlib

    from astinuslab.db.models import Artifact, ArtifactPublicationAttempt
    from astinuslab.forensics.capture_support import acquire_and_publish

    job_id = _job_ready_for_capture(in_memory_session_scope)

    acquire_and_publish(**_memory_args(tmp_path, in_memory_session_scope, job_id))

    dump_dir = tmp_path / "storage" / "dumps" / job_id
    published = dump_dir / "ubuntu-22.04-kernel-5.15.0-91-generic-memory.dump"
    assert published.exists(), sorted(p.name for p in dump_dir.iterdir())
    assert published.with_suffix(published.suffix + ".manifest.json").exists()
    assert published.with_suffix(published.suffix + ".sha256").exists()
    assert not any(entry.name.startswith(".publish-") for entry in dump_dir.iterdir())

    # Read inside the scope: committing expires attributes on the instances.
    with in_memory_session_scope() as session:
        attempt = session.query(ArtifactPublicationAttempt).one()
        artifact = session.query(Artifact).filter_by(job_id=job_id, kind="memory").one()

        assert attempt.state == "completed"
        assert attempt.final_path == str(published) == artifact.path
        assert attempt.artifact_id == artifact.id
        # The hash is of the staged bytes, computed by the service - not whatever
        # a `sha256sum` subprocess reported about a path.
        assert artifact.sha256 == hashlib.sha256(ELF_BYTES).hexdigest()


def test_memory_capture_failure_publishes_nothing(tmp_path, in_memory_session_scope):
    from unittest.mock import MagicMock

    from astinuslab.db.models import Artifact, ArtifactPublicationAttempt
    from astinuslab.forensics.capture_support import acquire_and_publish

    job_id = _job_ready_for_capture(in_memory_session_scope)

    def _die(conn, vm, path, *, cancel_check=lambda: False):
        path.write_bytes(b"half an image")
        raise RuntimeError("dump aborted")

    dumper = MagicMock()
    dumper.dump.side_effect = _die

    with pytest.raises(RuntimeError, match="dump aborted"):
        acquire_and_publish(**_memory_args(tmp_path, in_memory_session_scope, job_id, dumper=dumper))

    dump_dir = tmp_path / "storage" / "dumps" / job_id
    leftovers = sorted(p.name for p in dump_dir.iterdir()) if dump_dir.exists() else []
    assert leftovers == [], f"a failed capture must leave nothing behind, found {leftovers}"

    with in_memory_session_scope() as session:
        assert session.query(Artifact).count() == 0
        attempt = session.query(ArtifactPublicationAttempt).one()
        assert attempt.state in {"failed", "cleanup_incomplete"}


def test_memory_artifact_row_and_journal_completion_commit_together(tmp_path, in_memory_session_scope):
    """The Artifact row must not survive a failure of the completing transaction."""
    from astinuslab.db.models import Artifact, ArtifactPublicationAttempt
    from astinuslab.forensics import capture_support

    job_id = _job_ready_for_capture(in_memory_session_scope)

    real_record = capture_support._record_artifact

    def explode(*args, **kwargs):
        real_record(*args, **kwargs)
        raise RuntimeError("commit interrupted")

    capture_support._record_artifact = explode
    try:
        with pytest.raises(RuntimeError, match="commit interrupted"):
            acquire = capture_support.acquire_and_publish
            acquire(**_memory_args(tmp_path, in_memory_session_scope, job_id))
    finally:
        capture_support._record_artifact = real_record

    with in_memory_session_scope() as session:
        assert session.query(Artifact).count() == 0, "the Artifact row must not survive alone"
        attempt = session.query(ArtifactPublicationAttempt).one()
        assert attempt.state != "completed"


def test_disk_capture_is_journalled_and_gets_paired_sidecars(tmp_path, in_memory_session_scope):
    """Disk bytes are produced by the acquisition script at a path it chooses, so
    the attempt adopts the file rather than staging it. The journal record and the
    manifest/sidecar pair are what the migration adds."""
    import hashlib
    from unittest.mock import MagicMock

    from astinuslab.db.models import Artifact, ArtifactPublicationAttempt
    from astinuslab.db.repositories import JobRepository
    from astinuslab.forensics.capture_support import acquire_and_publish

    job_id = _job_ready_for_capture(in_memory_session_scope, capture_type="disk")

    disk_bytes = b"QCOW" * 512
    disk_file = tmp_path / "storage" / "dumps" / job_id / "disk.img.gz"
    disk_file.parent.mkdir(parents=True, exist_ok=True)
    disk_file.write_bytes(disk_bytes)

    settings_mock = MagicMock()
    settings_mock.base_dir = tmp_path
    settings_mock.data_dir = tmp_path
    settings_mock.storage_dir = tmp_path / "storage"

    acquire_and_publish(
        session_scope=in_memory_session_scope,
        repo_cls=JobRepository,
        get_active_key_paths=lambda: (tmp_path / "key", tmp_path / "key.pub"),
        run_command=lambda cmd, **kwargs: "",
        settings=settings_mock,
        parse_keyed_output=lambda output: {
            "disk_final_path": str(disk_file),
            "disk_filename": disk_file.name,
            "disk_publish_stage": "host",
        },
        check_host_disk_space=lambda job_id: None,
        is_cancel_requested=lambda job_id: False,
        job_disk_image_path=lambda j, t: tmp_path / "disk.qcow2",
        memory_dumper=MagicMock(),
        conn=_domain_connection(),
        job_id=job_id,
        target_id="ubuntu-22-04-default",
        guest_ip="10.0.0.1",
        vm_name="astinuslab-job-test",
        distro="ubuntu",
        version="22.04",
        action_id=None,
        capture_type="disk",
    )

    manifest_path = disk_file.with_suffix(disk_file.suffix + ".manifest.json")
    sidecar_path = disk_file.with_suffix(disk_file.suffix + ".sha256")
    assert manifest_path.is_file() and sidecar_path.is_file()
    assert not list(disk_file.parent.glob(".publish-*")), "the attempt directory must not survive"

    with in_memory_session_scope() as session:
        artifact = session.query(Artifact).filter_by(job_id=job_id, kind="disk").one()
        attempt = session.query(ArtifactPublicationAttempt).filter_by(kind="disk").one()

        assert attempt.state == "completed"
        assert attempt.artifact_id == artifact.id
        assert attempt.final_path == str(disk_file)
        # Hashed by the service from the bytes on disk, not parsed out of a
        # `sha256sum` subprocess.
        assert artifact.sha256 == hashlib.sha256(disk_bytes).hexdigest()
        assert artifact.manifest_path == str(manifest_path)


@pytest.mark.parametrize(
    "cached_state,active,method,script",
    [("on", 0, "offline_disk", "acquire_disk_host.sh"), ("off", 1, "remote_disk", "acquire_disk_remote.sh")],
)
def test_real_disk_capture_routes_from_current_domain_state(
    tmp_path, in_memory_session_scope, monkeypatch, cached_state, active, method, script
):
    from astinuslab.db.models import Artifact

    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    conn = MagicMock()
    conn.lookupByName.return_value.isActive.return_value = active
    commands: list[list[str]] = []
    job_id, _ = _run_disk_capture_with_power_state(
        tmp_path, in_memory_session_scope, cached_state, conn=conn, commands=commands
    )
    assert any(Path(cmd[1]).name == script for cmd in commands if len(cmd) > 1)
    with in_memory_session_scope() as session:
        artifact = session.query(Artifact).filter_by(job_id=job_id, kind="disk").one()
        manifest = json.loads(Path(artifact.manifest_path).read_text())
    assert manifest["capture"]["capture_method"] == method
    assert all(call.args == ("astinuslab-test",) for call in conn.lookupByName.call_args_list)


@pytest.mark.parametrize("capture_type", ["memory", "both"])
def test_real_memory_capture_refuses_currently_inactive_domain(tmp_path, monkeypatch, capture_type):
    from astinuslab.forensics.capture_support import acquire_and_publish

    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    conn = MagicMock()
    conn.lookupByName.return_value.isActive.return_value = 0
    args = _make_args(tmp_path, capture_type=capture_type, conn=conn)
    args["run_command"] = MagicMock()
    with pytest.raises(RuntimeError, match="powered off"):
        acquire_and_publish(**args)
    args["memory_dumper"].dump.assert_not_called()
    args["run_command"].assert_not_called()
    assert not (tmp_path / "storage" / "dumps").exists()


@pytest.mark.parametrize("capture_type", ["memory", "disk"])
def test_real_capture_refuses_unobservable_domain_state(tmp_path, monkeypatch, capture_type):
    from astinuslab.forensics.capture_support import acquire_and_publish

    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    conn = MagicMock()
    conn.lookupByName.side_effect = RuntimeError("libvirt unavailable")
    args = _make_args(tmp_path, capture_type=capture_type, conn=conn)
    args["run_command"] = MagicMock()
    with pytest.raises(RuntimeError, match="Cannot determine.*power state"):
        acquire_and_publish(**args)
    args["memory_dumper"].dump.assert_not_called()
    args["run_command"].assert_not_called()


def test_combined_capture_rechecks_power_after_memory(tmp_path, monkeypatch):
    from astinuslab.forensics.capture_support import acquire_and_publish

    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    conn = _domain_connection()
    conn.lookupByName.return_value.isActive.side_effect = [1, 0]
    disk_file = tmp_path / "offline-after-memory.img.gz"
    disk_file.write_bytes(b"offline disk")
    conn.lookupByName.return_value.XMLDesc.return_value = (
        f'<domain><devices><disk type="file" device="disk">'
        f'<source file="{disk_file}"/><target dev="vda"/></disk></devices></domain>'
    )
    args = _make_args(tmp_path, capture_type="both", conn=conn)
    args["settings"].acl_allowed_storage_roots = [tmp_path]
    args["run_command"] = MagicMock(return_value="")
    args["parse_keyed_output"] = lambda _: {"disk_final_path": str(disk_file), "disk_raw_sha256": _DISK_RAW_SHA256}
    acquire_and_publish(**args)
    args["memory_dumper"].dump.assert_called_once()
    assert conn.lookupByName.return_value.isActive.call_count == 2
    commands = [call.args[0] for call in args["run_command"].call_args_list]
    assert any(len(cmd) > 1 and Path(cmd[1]).name == "acquire_disk_host.sh" for cmd in commands)
    assert not any(len(cmd) > 1 and Path(cmd[1]).name == "acquire_disk_remote.sh" for cmd in commands)


def test_real_capture_refuses_invalid_libvirt_active_result(tmp_path, monkeypatch):
    from astinuslab.forensics.capture_support import acquire_and_publish

    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    args = _make_args(tmp_path, capture_type="disk", conn=_domain_connection(-1))
    args["run_command"] = MagicMock()
    with pytest.raises(RuntimeError, match="Cannot determine.*power state"):
        acquire_and_publish(**args)
    args["run_command"].assert_not_called()


@pytest.mark.parametrize("simulated", [False, True])
@pytest.mark.parametrize("capture_type", ["disk", "both"])
def test_windows_live_disk_refused_before_any_capture(tmp_path, monkeypatch, simulated, capture_type):
    from astinuslab.forensics.capture_support import acquire_and_publish

    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", simulated)
    args = _make_args(tmp_path, capture_type=capture_type, conn=_domain_connection(1))
    args["distro"] = "windows-server"
    args["run_command"] = MagicMock()
    repo = MagicMock()
    repo.get_runtime.return_value.power_state = "on"
    args["repo_cls"] = lambda _: repo

    with pytest.raises(RuntimeError, match="Windows.*disk capture"):
        acquire_and_publish(**args)

    args["run_command"].assert_not_called()
    args["memory_dumper"].dump.assert_not_called()
    assert not (tmp_path / "storage" / "dumps").exists()


def test_windows_offline_disk_uses_host_capture(tmp_path, in_memory_session_scope, monkeypatch):
    from astinuslab.db.models import Artifact

    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    commands: list[list[str]] = []
    job_id, _ = _run_disk_capture_with_power_state(
        tmp_path,
        in_memory_session_scope,
        "on",  # A stale runtime row must not override the actual shut-down state.
        conn=_domain_connection(0),
        commands=commands,
        distro="windows-server",
    )
    assert any(len(cmd) > 1 and Path(cmd[1]).name == "acquire_disk_host.sh" for cmd in commands)
    assert not any(len(cmd) > 1 and Path(cmd[1]).name == "acquire_disk_remote.sh" for cmd in commands)
    with in_memory_session_scope() as session:
        artifact = session.query(Artifact).filter_by(job_id=job_id, kind="disk").one()
        manifest = json.loads(Path(artifact.manifest_path).read_text())
    assert manifest["capture"]["capture_method"] == "offline_disk"


def test_stopped_capture_uses_current_snapshot_leaf(tmp_path, monkeypatch):
    from astinuslab.forensics.capture_support import acquire_and_publish

    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    original = tmp_path / "disk.qcow2"
    original.write_bytes(b"before snapshot")
    current = tmp_path / "disk.lab-clean"
    current.write_bytes(b"guest writes after snapshot")
    conn = _domain_connection(0)
    conn.lookupByName.return_value.XMLDesc.return_value = (
        f'<domain><devices><disk type="file" device="disk"><driver type="qcow2"/>'
        f'<source file="{current}"/><target dev="vda"/></disk></devices></domain>'
    )
    output = tmp_path / "acquired.img.gz"
    args = _make_args(tmp_path, capture_type="disk", conn=conn)
    args["settings"].acl_allowed_storage_roots = [tmp_path]

    def acquire(cmd, **kwargs):
        if len(cmd) > 1 and Path(cmd[1]).name == "acquire_disk_host.sh":
            output.write_bytes(Path(cmd[-1]).read_bytes())
        return ""

    args["run_command"] = acquire
    args["parse_keyed_output"] = lambda _: {"disk_final_path": str(output)}
    acquire_and_publish(**args)
    assert output.read_bytes() == b"guest writes after snapshot"


def test_disk_capture_requires_reported_artifact_before_completion(tmp_path):
    from astinuslab.forensics.capture_support import acquire_and_publish

    args = _make_args(tmp_path, capture_type="disk")
    repo = MagicMock()
    args["repo_cls"] = lambda _: repo
    with pytest.raises(RuntimeError, match="disk_final_path"):
        acquire_and_publish(**args)
    assert "completed" not in [call.args[1] for call in repo.transition_job_status.call_args_list]


@pytest.mark.parametrize("source_kind", ["missing", "relative", "foreign", "directory", "symlink"])
def test_current_job_disk_rejects_unbound_sources(tmp_path, source_kind):
    from astinuslab.virtualization.disk_source import current_job_disk

    owned = tmp_path / "job"
    owned.mkdir()
    foreign = tmp_path / "other.qcow2"
    foreign.write_bytes(b"unowned disk")
    link = owned / "redirect.qcow2"
    link.symlink_to(foreign)
    source = {"missing": "", "relative": "relative.qcow2", "foreign": foreign, "directory": owned, "symlink": link}[
        source_kind
    ]
    conn = _domain_connection(0)
    conn.lookupByName.return_value.XMLDesc.return_value = (
        f'<domain><devices><disk type="file" device="disk">'
        f'<source file="{source}"/><target dev="vda"/></disk></devices></domain>'
    )
    with pytest.raises(RuntimeError, match="Cannot determine current Job disk"):
        current_job_disk(conn, "astinuslab-test", owned / "original.qcow2")
