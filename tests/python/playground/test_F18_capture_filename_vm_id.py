from pathlib import Path
from types import SimpleNamespace

import pytest

from astinuslab import db as database
from astinuslab.jobs.celery_worker import celery_app
from astinuslab.playground import acquisition as pa
from astinuslab.playground import orchestrator as po
from astinuslab.playground import repositories as pr
from tests.python._support.helpers import autospec_setattr


@pytest.fixture
def db(tmp_path, monkeypatch, in_memory_session_scope):
    monkeypatch.setattr(po, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(database, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(
            playground_capture_dir=tmp_path / "captures",
            playground_disk_dir=tmp_path / "disks",
            libvirt_uri="qemu:///system",
            playground_default_vcpus=2,
            playground_default_memory_mb=2048,
            base_dir=tmp_path,
            data_dir=tmp_path,
            storage_dir=tmp_path / "storage",
        ),
    )
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    return tmp_path


def test_F18_capture_uses_vm_directory_and_acquisition_filename(db, monkeypatch, in_memory_session_scope):
    tmp_path = db
    autospec_setattr(monkeypatch, pa, "_dump_memory", lambda d, p: Path(p).write_bytes(b"x"))
    autospec_setattr(monkeypatch, po, "_domain_xml", lambda domain_name: None)
    with in_memory_session_scope() as s:
        pr.create_playground_session(s, job_id="pg-fn")
        pr.register_vm(
            s,
            vm_id="vm-fn",
            job_id="pg-fn",
            label="weblabel",
            image_id="img",
            domain_name="astinuslab-pg-pg-fn-weblabel",
            disk_path="/tmp/x.qcow2",
        )
        pr.update_vm_state(s, "vm-fn", "running")

    po.capture_memory(vm_id="vm-fn", acquisition_id="acq-1")
    capture_dir = tmp_path / "captures" / "pg-fn" / "vm-fn"
    files = list(capture_dir.iterdir())
    payload = capture_dir / "mem-acq-1.elf"
    assert payload.is_file()
    assert payload.with_suffix(".elf.manifest.json").is_file()
    assert payload.with_suffix(".elf.sha256").is_file()
    assert not any("weblabel" in f.name for f in files)
