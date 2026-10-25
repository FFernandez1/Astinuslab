from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest

from astinuslab.playground.captures import DiskCapture
from tests.python._support.helpers import autospec_setattr


def test_disk_capture_factory_runner_accepts_bare_command(monkeypatch):
    """Production factory must wire a runner callable as run(cmd)."""
    import inspect

    from astinuslab.core import fake_mode
    from astinuslab.playground import acquisition as pa

    monkeypatch.setattr(fake_mode, "FAKE_PLAYGROUND", False)
    dc = pa._disk_capture()

    inspect.signature(dc._run).bind(["virsh", "suspend", "dom"])


def test_disk_capture_factory_runner_executes_with_check(monkeypatch):
    import subprocess as _subprocess

    from astinuslab.core import fake_mode
    from astinuslab.playground import acquisition as pa
    from astinuslab.playground import orchestrator as po

    monkeypatch.setattr(fake_mode, "FAKE_PLAYGROUND", False)
    calls = []

    def fake_subprocess_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return _subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(po.subprocess, "run", fake_subprocess_run)
    dc = pa._disk_capture()
    dc._run(["virsh", "suspend", "dom"])

    assert calls and calls[0][0] == ["virsh", "suspend", "dom"]
    assert calls[0][1].get("check") is True


@pytest.mark.parametrize("convert_fails", [False, True])
def test_disk_capture_factory_uses_configured_connection_for_suspend_and_resume(monkeypatch, tmp_path, convert_fails):
    import subprocess
    from types import SimpleNamespace

    from astinuslab.core import fake_mode
    from astinuslab.playground import acquisition as pa
    from astinuslab.playground import orchestrator as po

    monkeypatch.setattr(fake_mode, "FAKE_PLAYGROUND", False)
    monkeypatch.setattr(
        po, "settings", SimpleNamespace(libvirt_uri="qemu+ssh://lab/system", playground_capture_timeout_seconds=12)
    )
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "qemu-img" and convert_fails:
            raise RuntimeError("conversion failed")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(pa.subprocess, "run", run)
    capture = pa._disk_capture()
    if convert_fails:
        with pytest.raises(RuntimeError, match="conversion failed"):
            capture.capture(domain_name="dom", source_disk_path="/disk", output_path=tmp_path / "out")
    else:
        capture.capture(domain_name="dom", source_disk_path="/disk", output_path=tmp_path / "out")
    assert [cmd for cmd in calls if cmd[0] == "virsh"] == [
        ["virsh", "-c", "qemu+ssh://lab/system", "suspend", "dom"],
        ["virsh", "-c", "qemu+ssh://lab/system", "resume", "dom"],
    ]


def test_disk_capture_factory_fake_mode_writes_artifact(tmp_path, monkeypatch):
    from astinuslab.core import fake_mode
    from astinuslab.playground import acquisition as pa

    monkeypatch.setattr(fake_mode, "FAKE_PLAYGROUND", True)
    out = tmp_path / "disk.qcow2"
    dc = pa._disk_capture()

    dc.capture(domain_name="dom", source_disk_path="/x.qcow2", output_path=out)

    assert out.read_bytes().startswith(b"astinuslab fake disk image")


def test_disk_capture_factory_runner_defaults_timeout_from_settings(monkeypatch):
    import subprocess as _subprocess
    from types import SimpleNamespace

    from astinuslab.core import fake_mode
    from astinuslab.playground import acquisition as pa
    from astinuslab.playground import orchestrator as po

    monkeypatch.setattr(fake_mode, "FAKE_PLAYGROUND", False)
    monkeypatch.setattr(
        po, "settings", SimpleNamespace(libvirt_uri="qemu:///system", playground_capture_timeout_seconds=12)
    )
    calls = []

    def fake_subprocess_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return _subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(po.subprocess, "run", fake_subprocess_run)
    dc = pa._disk_capture()
    dc._run(["qemu-img", "convert", "-O", "qcow2", "/in", "/out"])

    assert calls[0][1]["timeout"] == 12


def test_disk_capture_runs_blockcopy_with_pivot(tmp_path):
    fake_run = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
    dc = DiskCapture(run_command=fake_run, mode="blockcopy")
    out_path = tmp_path / "disk.qcow2"

    dc.capture(domain_name="astinuslab-pg-x", source_disk="vda", output_path=out_path)

    assert fake_run.call_args_list == [
        call(
            [
                "virsh",
                "-c",
                "qemu:///system",
                "blockcopy",
                "astinuslab-pg-x",
                "vda",
                str(out_path),
                "--wait",
                "--verbose",
                "--format",
                "qcow2",
            ],
            check=True,
            capture_output=True,
            text=True,
        ),
        call(
            ["virsh", "-c", "qemu:///system", "blockjob", "astinuslab-pg-x", "vda", "--pivot"],
            check=True,
            capture_output=True,
            text=True,
        ),
    ]


def test_pause_convert_capture_pauses_then_resumes_around_qemu_img():
    from unittest.mock import MagicMock

    from astinuslab.playground.captures import DiskCapture

    fake_run = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
    dc = DiskCapture(run_command=fake_run, mode="pause_convert")
    dc.capture(domain_name="astinuslab-pg-x", source_disk_path="/var/lib/x.qcow2", output_path="/tmp/out.qcow2")

    cmds = [c.args[0] for c in fake_run.call_args_list]
    joined = [" ".join(cmd) for cmd in cmds]
    assert any("virsh -c qemu:///system suspend" in c for c in joined)
    assert any("qemu-img convert" in c for c in joined)
    assert any("virsh -c qemu:///system resume" in c for c in joined)

    suspend_idx = next(i for i, c in enumerate(joined) if "suspend" in c)
    convert_idx = next(i for i, c in enumerate(joined) if "convert" in c)
    resume_idx = next(i for i, c in enumerate(joined) if "resume" in c)
    assert suspend_idx < convert_idx < resume_idx

    # -U (force-share) is required: a suspended qemu still holds the qcow2
    # write lock, so a plain open would fail with "Failed to get shared write
    # lock". Regression guard for the running-VM disk-capture fix.
    convert_cmd = cmds[convert_idx]
    assert "-U" in convert_cmd
    assert convert_cmd.index("-U") < convert_cmd.index("/var/lib/x.qcow2")


def test_pause_convert_resumes_even_when_convert_fails():
    from unittest.mock import MagicMock

    import pytest as _pytest

    from astinuslab.playground.captures import DiskCapture

    calls = []

    def runner(cmd, **_):
        calls.append(" ".join(cmd))
        if "qemu-img" in calls[-1]:
            raise RuntimeError("simulated failure")
        return MagicMock(returncode=0)

    dc = DiskCapture(run_command=runner, mode="pause_convert")
    with _pytest.raises(RuntimeError):
        dc.capture(domain_name="astinuslab-pg-x", source_disk_path="/var/lib/x.qcow2", output_path="/tmp/out.qcow2")

    assert any("virsh -c qemu:///system resume" in c for c in calls)


def _make_running_vm(s, *, job_id, vm_id, label="lbl"):
    from astinuslab.playground import repositories as pr

    pr.create_playground_session(s, job_id=job_id)
    pr.register_vm(
        s,
        vm_id=vm_id,
        job_id=job_id,
        label=label,
        image_id="img",
        domain_name=f"dom-{vm_id}",
        disk_path="/tmp/x.qcow2",
    )
    pr.update_vm_state(s, vm_id, "running")


def test_memory_capture_filename_includes_acquisition_id(tmp_path, monkeypatch, in_memory_session_scope):
    from types import SimpleNamespace

    from astinuslab import db
    from astinuslab.jobs.celery_worker import celery_app
    from astinuslab.playground import acquisition as pa
    from astinuslab.playground import orchestrator as po

    monkeypatch.setattr(po, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(db, "session_scope", in_memory_session_scope)

    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(
            playground_capture_dir=tmp_path / "caps",
            playground_disk_dir=tmp_path / "disks",
            libvirt_uri="qemu:///system",
            playground_default_vcpus=2,
            playground_default_memory_mb=2048,
            base_dir=tmp_path,
            data_dir=tmp_path,
            storage_dir=tmp_path / "storage",
        ),
    )
    autospec_setattr(monkeypatch, pa, "_dump_memory", lambda d, p: p.write_bytes(b"x"))
    # The publication service writes the manifest and sidecar now; this test only
    # cares about the artifact filename, so the manifest body can be a stub.
    autospec_setattr(
        monkeypatch,
        pa,
        "_build_capture_manifest",
        lambda **kwargs: {"schema_version": "artifact-manifest/v1", "path": str(kwargs["artifact_path"])},
    )

    with in_memory_session_scope() as s:
        _make_running_vm(s, job_id="pg-fn", vm_id="vm-fn")

    po.capture_memory(vm_id="vm-fn", acquisition_id="acq-AAA")
    po.capture_memory(vm_id="vm-fn", acquisition_id="acq-BBB")

    out_dir = tmp_path / "caps" / "pg-fn" / "vm-fn"
    files = [p.name for p in out_dir.iterdir()]
    assert any("acq-AAA" in f for f in files)
    assert any("acq-BBB" in f for f in files)
    assert "mem-acq-AAA.elf" in files
    assert "mem-acq-BBB.elf" in files
    from astinuslab.db.models import Acquisition

    with in_memory_session_scope() as s:
        assert s.get(Acquisition, "acq-AAA").name == "mem-acq-AAA"
    # Both captures coexist even when timestamp collides.
    assert len(files) >= 2


def test_disk_capture_filename_uses_disk_marker(tmp_path, monkeypatch, in_memory_session_scope):
    from types import SimpleNamespace

    from astinuslab import db
    from astinuslab.jobs.celery_worker import celery_app
    from astinuslab.playground import acquisition as pa
    from astinuslab.playground import orchestrator as po

    monkeypatch.setattr(po, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(db, "session_scope", in_memory_session_scope)

    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(
            playground_capture_dir=tmp_path / "caps",
            playground_disk_dir=tmp_path / "disks",
            libvirt_uri="qemu:///system",
            playground_default_vcpus=2,
            playground_default_memory_mb=2048,
            base_dir=tmp_path,
            data_dir=tmp_path,
            storage_dir=tmp_path / "storage",
        ),
    )
    autospec_setattr(
        monkeypatch,
        pa,
        "_disk_capture",
        lambda: SimpleNamespace(capture=lambda **kwargs: kwargs["output_path"].write_bytes(b"x")),
    )
    # The publication service writes the manifest and sidecar now; this test only
    # cares about the artifact filename, so the manifest body can be a stub.
    autospec_setattr(
        monkeypatch,
        pa,
        "_build_capture_manifest",
        lambda **kwargs: {"schema_version": "artifact-manifest/v1", "path": str(kwargs["artifact_path"])},
    )

    with in_memory_session_scope() as s:
        _make_running_vm(s, job_id="pg-disk", vm_id="vm-disk")

    po.capture_disk(vm_id="vm-disk", acquisition_id="acq-DISK")

    out_dir = tmp_path / "caps" / "pg-disk" / "vm-disk"
    files = [p.name for p in out_dir.iterdir()]
    assert "disk-acq-DISK.qcow2" in files
    assert any("acq-DISK" in f for f in files)


def test_capture_lock_blocks_concurrent_capture_from_other_process(tmp_path, monkeypatch, in_memory_session_scope):
    """Process-safe lock must prevent another process from running a capture
    on the same VM concurrently. Simulated by clearing the in-process lock
    map and showing that the file lock still blocks the second attempt."""
    from types import SimpleNamespace

    from astinuslab import db
    from astinuslab.core.locks import capture_lock, clear_in_process_capture_locks
    from astinuslab.jobs.celery_worker import celery_app
    from astinuslab.playground import acquisition as pa
    from astinuslab.playground import orchestrator as po

    monkeypatch.setattr(po, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(db, "session_scope", in_memory_session_scope)

    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(
            playground_capture_dir=tmp_path / "caps",
            playground_disk_dir=tmp_path / "disks",
            libvirt_uri="qemu:///system",
            playground_default_vcpus=2,
            playground_default_memory_mb=2048,
            base_dir=tmp_path,
            data_dir=tmp_path,
            storage_dir=tmp_path / "storage",
        ),
    )

    with in_memory_session_scope() as s:
        _make_running_vm(s, job_id="pg-lock", vm_id="vm-lock")

    with capture_lock("vm-lock", base_dir=tmp_path / "storage" / "tmp" / "playground-capture-locks"):
        # Simulate another worker process by wiping the in-process lock map but
        # keeping the on-disk file lock held by our handle.
        clear_in_process_capture_locks()
        dump_called = []
        autospec_setattr(monkeypatch, pa, "_dump_memory", lambda d, p: dump_called.append(True))
        po.capture_memory(vm_id="vm-lock", acquisition_id="acq-blocked")
        assert dump_called == [], "second capture must not invoke _dump_memory while lock held"


def _make_pending_vm(s, *, job_id, vm_id, label="lbl"):
    """Same VM as _make_running_vm, left in `pending`: never `running`."""
    from astinuslab.playground import repositories as pr

    pr.create_playground_session(s, job_id=job_id)
    pr.register_vm(
        s,
        vm_id=vm_id,
        job_id=job_id,
        label=label,
        image_id="img",
        domain_name=f"dom-{vm_id}",
        disk_path="/tmp/x.qcow2",
    )


def _capture_settings(tmp_path):
    from types import SimpleNamespace

    return SimpleNamespace(
        playground_capture_dir=tmp_path / "caps",
        playground_disk_dir=tmp_path / "disks",
        libvirt_uri="qemu:///system",
        playground_default_vcpus=2,
        playground_default_memory_mb=2048,
        base_dir=tmp_path,
        data_dir=tmp_path,
        storage_dir=tmp_path / "storage",
    )


def test_capture_disk_rejects_non_running_vm_and_fails_the_acquisition(tmp_path, monkeypatch, in_memory_session_scope):
    """A disk capture on a VM that is not running must refuse before touching
    the disk, and must leave the acquisition row `failed` rather than pending.
    The memory path has had this covered since
    test_capture_memory_rejects_non_running_vm_without_acquisition; the disk
    path reached `raise RuntimeError` from no test at all."""
    import pytest

    from astinuslab import db
    from astinuslab.jobs.celery_worker import celery_app
    from astinuslab.playground import acquisition as pa
    from astinuslab.playground import orchestrator as po

    monkeypatch.setattr(po, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(db, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    monkeypatch.setattr(po, "settings", _capture_settings(tmp_path))

    captured = []
    autospec_setattr(monkeypatch, pa, "_disk_capture", lambda: captured.append(True))

    with in_memory_session_scope() as s:
        _make_pending_vm(s, job_id="pg-cold", vm_id="vm-cold", label="web-1")

    with pytest.raises(RuntimeError, match=r"VM vm-cold not running \(state=pending\)"):
        po.capture_disk(vm_id="vm-cold", acquisition_id="acq-cold")

    assert captured == [], "the guard must refuse before the disk capture backend is built"
    from astinuslab.db.models import Acquisition

    with in_memory_session_scope() as s:
        acq = s.get(Acquisition, "acq-cold")
        assert acq is not None, "the acquisition must not be left absent for the UI to poll"
        assert acq.status == "failed"
        assert acq.name == "playground-vm-cold-disk-failed"


def test_capture_disk_without_acquisition_refuses_non_running_vm_silently(
    tmp_path, monkeypatch, in_memory_session_scope
):
    """Same guard with no acquisition to update: it must still refuse, and must
    not invent an Acquisition row."""
    import pytest

    from astinuslab import db
    from astinuslab.jobs.celery_worker import celery_app
    from astinuslab.playground import orchestrator as po

    monkeypatch.setattr(po, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(db, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    monkeypatch.setattr(po, "settings", _capture_settings(tmp_path))

    with in_memory_session_scope() as s:
        _make_pending_vm(s, job_id="pg-cold2", vm_id="vm-cold2")

    with pytest.raises(RuntimeError, match=r"VM vm-cold2 not running \(state=pending\)"):
        po.capture_disk(vm_id="vm-cold2")

    from astinuslab.db.models import Acquisition

    with in_memory_session_scope() as s:
        assert s.query(Acquisition).filter_by(job_vm_id="vm-cold2").one_or_none() is None


def test_capture_disk_marks_already_running_when_another_process_holds_the_lock(
    tmp_path, monkeypatch, in_memory_session_scope
):
    """The disk twin of test_capture_lock_blocks_concurrent_capture_from_other_process.
    Two concurrent disk captures on one VM would both drive virsh against the
    same domain, so the busy path must fail the second acquisition instead of
    starting a capture."""
    from astinuslab import db
    from astinuslab.core.locks import capture_lock, clear_in_process_capture_locks
    from astinuslab.jobs.celery_worker import celery_app
    from astinuslab.playground import acquisition as pa
    from astinuslab.playground import orchestrator as po

    monkeypatch.setattr(po, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(db, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    monkeypatch.setattr(po, "settings", _capture_settings(tmp_path))

    with in_memory_session_scope() as s:
        _make_running_vm(s, job_id="pg-dlock", vm_id="vm-dlock", label="web-1")

    with capture_lock("vm-dlock", base_dir=tmp_path / "storage" / "tmp" / "playground-capture-locks"):
        clear_in_process_capture_locks()
        captured = []
        autospec_setattr(monkeypatch, pa, "_disk_capture", lambda: captured.append(True))

        assert po.capture_disk(vm_id="vm-dlock", acquisition_id="acq-dlock") == "acq-dlock"

        assert captured == [], "second disk capture must not build the capture backend while the lock is held"

    from astinuslab.db.models import Acquisition, AcquisitionEvent

    with in_memory_session_scope() as s:
        acq = s.get(Acquisition, "acq-dlock")
        assert acq.status == "failed"
        assert acq.name == "playground-vm-dlock-disk-failed"
        events = s.query(AcquisitionEvent).filter_by(acquisition_id="acq-dlock").all()
        assert any("capture already in progress for vm-dlock" in event.message for event in events)
