import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astinuslab import db as database
from astinuslab.db.models import Artifact
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


def test_F19_manifest_written_next_to_artifact(db, monkeypatch, in_memory_session_scope):
    tmp_path = db
    autospec_setattr(monkeypatch, pa, "_dump_memory", lambda d, p: Path(p).write_bytes(b"x"))

    class _Domain:
        def XMLDesc(self):
            return "<domain><name>d</name></domain>"

    class _Conn:
        def lookupByName(self, name):
            return _Domain()

        def close(self):
            pass

    class _Libvirt:
        @staticmethod
        def open(uri):
            return _Conn()

    monkeypatch.setattr(po, "libvirt", _Libvirt())
    with in_memory_session_scope() as s:
        pr.create_playground_session(s, job_id="pg-mf")
        pr.register_network(
            s,
            network_id="net-mf",
            job_id="pg-mf",
            name="lab",
            subnet_cidr="10.99.0.0/24",
            isolated=True,
            libvirt_net_name="astinuslab-pg-mf-lab",
        )
        pr.update_network_state(s, "net-mf", "active")
        pr.register_network(
            s,
            network_id="net-second",
            job_id="pg-mf",
            name="second",
            subnet_cidr="10.99.1.0/24",
            isolated=True,
            libvirt_net_name="astinuslab-pg-mf-second",
        )
        pr.update_network_state(s, "net-second", "active")
        pr.register_vm(
            s,
            vm_id="vm-mf",
            job_id="pg-mf",
            label="lbl",
            image_id="img",
            domain_name="d",
            disk_path="/tmp/x.qcow2",
        )
        pr.update_vm_state(s, "vm-mf", "running")
        pr.attach_nic(
            s,
            nic_id="nic-mf",
            vm_id="vm-mf",
            network_id="net-mf",
            mac="52:54:00:00:00:01",
            ordinal=1,
        )
        pr.update_nic_ip(s, "nic-mf", "10.99.0.5")
        pr.attach_nic(
            s,
            nic_id="nic-second",
            vm_id="vm-mf",
            network_id="net-second",
            mac="52:54:00:00:00:02",
            ordinal=0,
        )
        pr.update_nic_ip(s, "nic-second", "10.99.1.5")

    po.capture_memory(vm_id="vm-mf", acquisition_id="acq-mf")
    out_dir = tmp_path / "captures" / "pg-mf" / "vm-mf"
    manifests = [p for p in out_dir.iterdir() if p.name.endswith(".manifest.json")]
    assert len(manifests) == 1
    data = json.loads(manifests[0].read_text())
    assert data["vm_id"] == "vm-mf"
    assert data["session_id"] == "pg-mf"
    assert data["kind"] == "memory"
    # The publication service hashes the staged bytes itself, so the manifest
    # now carries a hash that is provably of those bytes rather than one a
    # helper reported.
    assert data["sha256"] == hashlib.sha256(b"x").hexdigest()
    assert data["size_bytes"] == 1
    assert "host_clock_monotonic_ns" not in data
    assert data["captured_at_utc"] is not None
    assert data["capture_finished_at_utc"] is not None
    assert data["captured_at_utc"] <= data["capture_finished_at_utc"]
    assert data["domain_xml_sha256"] == "1c8bc76ceec70e5226d4941a278327c7eb99d09efaa5b4a0af4368da7c23d4e1"
    assert data["domain_xml_excerpt"] == "<domain><name>d</name></domain>"
    # New reproducibility fields
    assert data["vm_state_at_capture_start"] == "running"
    assert data["capture_method"] == "memory_dump"
    artifact_path = Path(str(manifests[0]).removesuffix(".manifest.json"))
    sidecar = artifact_path.with_suffix(artifact_path.suffix + ".sha256")
    assert sidecar.read_text() == f"{hashlib.sha256(b'x').hexdigest()}  {artifact_path.name}\n"
    assert data["schema_version"] == "artifact-manifest/v1"
    assert data["producer"] == "playground_capture"
    assert data["capture"]["capture_method"] == "memory_dump"
    assert data["integrity"]["domain_xml_sha256"] == data["domain_xml_sha256"]
    assert data["target"]["image_id"] == "img"
    assert data["target"]["disk_path"] == "/tmp/x.qcow2"
    assert data["network"]["nics"]
    assert data["network"]["networks"]
    assert data["image_id"] == "img"
    assert data["disk_path"] == "/tmp/x.qcow2"
    nics = data["nics"]
    assert [(nic["ordinal"], nic["nic_id"], nic["network_id"]) for nic in nics] == [
        (0, "nic-second", "net-second"),
        (1, "nic-mf", "net-mf"),
    ]
    assert data["network_ids"] == ["net-second", "net-mf"]
    assert [network["id"] for network in data["networks"]] == ["net-second", "net-mf"]
    networks = {n["id"]: n for n in data["networks"]}
    assert networks["net-mf"]["name"] == "lab"
    assert networks["net-mf"]["subnet_cidr"] == "10.99.0.0/24"
    assert networks["net-mf"]["isolated"] is True
    assert networks["net-mf"]["libvirt_net_name"] == "astinuslab-pg-mf-lab"
    with in_memory_session_scope() as s:
        artifact = s.query(Artifact).filter_by(acquisition_id="acq-mf").one()
        assert artifact.kind == "memory"
        assert artifact.producer == "playground_capture"
        assert artifact.job_id is None
        assert artifact.session_id == "pg-mf"
        assert artifact.path == str(artifact_path)
        assert artifact.sha256 == hashlib.sha256(b"x").hexdigest()
        assert artifact.manifest_path == str(manifests[0])
        assert artifact.sha256_sidecar_path == str(sidecar)
