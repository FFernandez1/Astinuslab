"""Task 19 — Playground capture publishes through the recoverable journal.

The defects this closes are both states an operator cannot resolve:

- an Acquisition left permanently `running` because the worker died after the
  Artifact row committed but before the acquisition was sealed, and
- an Artifact row pointing at bytes that were never finished, because capture,
  manifest, sidecar and the database row were four independent commits.

The tests below assert the three properties that make those states impossible:
the triad appears at its final names all at once, a failed capture leaves
nothing behind, and the Artifact row and the sealed Acquisition share one
transaction.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from astinuslab.db import SessionLocal
from astinuslab.db.models import Acquisition, Artifact, ArtifactPublicationAttempt
from astinuslab.jobs.celery_app import celery_app
from tests.python._support.helpers import autospec_setattr, isolated_playground_database


@pytest.fixture(autouse=True)
def _eager(monkeypatch, tmp_path):
    """Same harness the other Playground tests use: run tasks inline, treat the
    catalog's placeholder image paths as present, and isolate its database."""
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    original_is_file = Path.is_file

    def is_file_with_placeholder_images(self):
        path = str(self)
        if path == "/x" or path.startswith("/var/lib/astinuslab/images/"):
            return True
        return original_is_file(self)

    monkeypatch.setattr(Path, "is_file", is_file_with_placeholder_images)
    with isolated_playground_database(monkeypatch, tmp_path, globals()):
        yield


DUMP_BYTES = b"playground memory bytes"


def _setup(monkeypatch, tmp_path, dump_side_effect=None):
    """Bring up a one-VM Playground session with capture wired to a fake dump."""
    from astinuslab.playground import acquisition as pa
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po
    from astinuslab.playground.orchestrator import create_network, create_session, spawn_vm

    fake_lifecycle = MagicMock()
    fake_lifecycle.get_ip.return_value = "10.42.1.10"

    def default_dump(domain_name, output_path):
        output_path.write_bytes(DUMP_BYTES)
        return {"path": str(output_path), "size": len(DUMP_BYTES)}

    fake_dump = MagicMock(side_effect=dump_side_effect or default_dump)
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: fake_lifecycle)
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    monkeypatch.setattr(pa, "_dump_memory", fake_dump)
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda _, *, role="target": {
            "id": "img",
            "path": "/var/lib/astinuslab/images/img.qcow2",
            "format": "qcow2",
            "architecture": "x86_64",
            "distro": "ubuntu",
            "version": "22.04",
            "credentials": {"username": "ubuntu", "password": "ubuntu"},
        },
    )
    monkeypatch.setattr(
        po,
        "settings",
        SimpleNamespace(
            playground_disk_dir=tmp_path / "playground",
            playground_capture_dir=tmp_path / "captures",
            playground_default_vcpus=2,
            playground_default_memory_mb=2048,
            libvirt_uri="qemu:///system",
            base_dir=tmp_path,
            data_dir=tmp_path,
            storage_dir=tmp_path,
        ),
    )

    create_session.delay(job_id="pg-1").get()
    create_network.delay(job_id="pg-1", network_id="n1", name="ab", subnet_cidr="10.42.1.0/24", isolated=True).get()
    spawn_vm.delay(
        job_id="pg-1",
        vm_id="v1",
        label="web-1",
        image_id="img",
        nic_specs=[{"network_id": "n1", "nic_id": "nic1"}],
    ).get()
    return po, fake_dump


def _capture_dir(tmp_path):
    return tmp_path / "captures" / "pg-1" / "v1"


def test_capture_publishes_a_complete_triad_and_records_the_journal(monkeypatch, tmp_path):
    po, fake_dump = _setup(monkeypatch, tmp_path)

    acquisition_id = po.capture_memory.delay(vm_id="v1").get()

    with SessionLocal() as session:
        acquisition = session.query(Acquisition).filter_by(id=acquisition_id).one()
        attempt = session.query(ArtifactPublicationAttempt).filter_by(acquisition_id=acquisition_id).one()
        artifact = session.query(Artifact).filter_by(acquisition_id=acquisition_id).one()

    assert acquisition.path is not None
    final = _capture_dir(tmp_path) / Path(acquisition.path).name
    assert acquisition.status == "completed"
    assert attempt.state == "completed"
    assert attempt.final_path == acquisition.path == artifact.path == str(final)
    assert attempt.sha256 == hashlib.sha256(DUMP_BYTES).hexdigest()

    assert final.exists()
    assert final.with_suffix(final.suffix + ".manifest.json").exists()
    assert final.with_suffix(final.suffix + ".sha256").exists()

    # The capture wrote into an attempt-owned directory, not straight to the
    # published name, and that directory is gone once publication completed.
    _, staged_path = fake_dump.call_args.args
    assert staged_path != final
    assert not staged_path.parent.exists()


def test_capture_failure_leaves_no_partial_evidence(monkeypatch, tmp_path):
    def explode(domain_name, output_path):
        output_path.write_bytes(b"half a dump")
        raise RuntimeError("hypervisor lost the domain")

    po, _ = _setup(monkeypatch, tmp_path, dump_side_effect=explode)

    with pytest.raises(Exception, match="hypervisor lost the domain"):
        po.capture_memory.delay(vm_id="v1").get()

    with SessionLocal() as session:
        acquisition = session.query(Acquisition).filter_by(job_vm_id="v1").one()
        attempt = session.query(ArtifactPublicationAttempt).one()
        assert session.query(Artifact).count() == 0

    assert acquisition.status == "failed"
    assert attempt.state in {"failed", "cleanup_incomplete"}
    published = [path for path in _capture_dir(tmp_path).iterdir()] if _capture_dir(tmp_path).exists() else []
    assert published == [], f"a failed capture must publish nothing, found {published}"


def test_artifact_row_and_sealed_acquisition_commit_together(monkeypatch, tmp_path):
    """A crash between the two commits is what leaves an Acquisition running forever.

    Sealing the acquisition is made to fail after the Artifact row is written. If
    they share a transaction, neither survives; if they do not, the Artifact row
    is orphaned and the acquisition never reaches a terminal state.
    """
    from astinuslab.playground import acquisition as pa

    po, _ = _setup(monkeypatch, tmp_path)
    real_seal = pa._seal_acquisition

    def fail_on_completion(session, **kwargs):
        if kwargs.get("status") == "completed":
            raise RuntimeError("worker died before sealing")
        return real_seal(session, **kwargs)

    monkeypatch.setattr(pa, "_seal_acquisition", fail_on_completion)

    with pytest.raises(Exception, match="worker died before sealing"):
        po.capture_memory.delay(vm_id="v1").get()

    with SessionLocal() as session:
        assert session.query(Artifact).count() == 0, "the Artifact row must not survive alone"
        acquisition = session.query(Acquisition).filter_by(job_vm_id="v1").one()
        assert acquisition.status != "completed"
        assert acquisition.sealed_at is None or acquisition.status == "failed"
