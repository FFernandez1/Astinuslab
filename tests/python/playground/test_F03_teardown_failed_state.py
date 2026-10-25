from unittest.mock import MagicMock

import pytest

from astinuslab.db.models import JobVM
from astinuslab.playground import orchestrator as po
from astinuslab.playground import repositories as pr
from tests.python._support.helpers import autospec_setattr


@pytest.fixture
def session(monkeypatch, in_memory_session_scope):
    monkeypatch.setattr(po, "session_scope", in_memory_session_scope)
    return in_memory_session_scope


def _seed_vm(session_scope) -> str:
    with session_scope() as s:
        pr.create_playground_session(s, job_id="pg-1")
        pr.register_vm(
            s,
            vm_id="vm-1",
            job_id="pg-1",
            label="x",
            image_id="img",
            domain_name="astinuslab-pg-pg-1-x",
            disk_path="/tmp/x.qcow2",
        )
        pr.update_vm_state(s, "vm-1", "running")
    return "vm-1"


def test_F03_destroy_vm_marks_teardown_failed_on_libvirt_error(session, monkeypatch):
    _seed_vm(session)
    lifecycle = MagicMock()
    lifecycle.destroy.side_effect = RuntimeError("libvirt died")
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: lifecycle)
    with pytest.raises(RuntimeError, match="teardown incomplete: libvirt died"):
        po.destroy_vm(vm_id="vm-1")
    with session() as s:
        vm = s.query(JobVM).filter_by(id="vm-1").one()
        assert vm.state == "teardown_failed"
        assert "libvirt died" in (vm.error_summary or "")
