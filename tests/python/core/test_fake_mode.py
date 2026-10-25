from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

from starlette.testclient import TestClient

from astinuslab.core import fake_mode


def test_fake_libvirt_network_lifecycle_is_stateful():
    script = textwrap.dedent(
        """
        import astinuslab
        import libvirt

        xml = "<network><name>astinuslab-pg-test</name></network>"
        connection = libvirt.open(None)
        network = connection.networkDefineXML(xml)
        assert not network.isActive()
        assert network.name() == "astinuslab-pg-test"
        assert network.XMLDesc() == xml

        assert network.create() == 0
        assert network.isActive()

        separate_connection = libvirt.open(None)
        assert separate_connection.networkLookupByName("astinuslab-pg-test") is network
        assert separate_connection.listAllNetworks() == [network]

        assert network.destroy() == 0
        assert not network.isActive()
        assert network.undefine() == 0
        assert separate_connection.listAllNetworks() == []
        try:
            separate_connection.networkLookupByName("astinuslab-pg-test")
        except libvirt.libvirtError as error:
            assert error.get_error_code() == libvirt.VIR_ERR_NO_NETWORK
        else:
            raise AssertionError("missing network lookup unexpectedly succeeded")
        """
    )
    environment = os.environ.copy()
    environment["ASTINUSLAB_FAKE_LIBVIRT"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_fake_mode_banner_is_none_when_real(monkeypatch):
    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    monkeypatch.setattr(fake_mode, "FAKE_PLAYGROUND", False)
    monkeypatch.setattr(fake_mode, "FAKE_ORCHESTRATION", False)
    assert fake_mode.fake_mode_banner() is None


def test_fake_mode_banner_names_active_flags(monkeypatch):
    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    monkeypatch.setattr(fake_mode, "FAKE_PLAYGROUND", True)
    monkeypatch.setattr(fake_mode, "FAKE_ORCHESTRATION", False)
    banner = fake_mode.fake_mode_banner()
    assert banner is not None
    assert "fake_playground" in banner
    assert "SYNTHETIC" in banner


def test_health_omits_fake_mode_when_real(monkeypatch):
    from astinuslab.main import app

    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    monkeypatch.setattr(fake_mode, "FAKE_PLAYGROUND", False)
    monkeypatch.setattr(fake_mode, "FAKE_ORCHESTRATION", False)
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_health_reports_fake_mode(monkeypatch):
    from astinuslab.main import app

    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    monkeypatch.setattr(fake_mode, "FAKE_PLAYGROUND", True)
    monkeypatch.setattr(fake_mode, "FAKE_ORCHESTRATION", False)
    with TestClient(app) as client:
        payload = client.get("/health").json()
    assert payload["status"] == "ok"
    assert payload["fake_mode"]["fake_playground"] is True
    assert "SYNTHETIC" in payload["warning"]


def _build_manifest(tmp_path: Path):
    from astinuslab.storage.artifacts import build_artifact_manifest

    artifact = tmp_path / "memory.dump"
    artifact.write_bytes(b"x")
    return build_artifact_manifest(
        artifact_path=artifact,
        kind="memory",
        producer="job_capture",
        sha256="b" * 64,
        size_bytes=1,
    )


def test_manifest_stamps_fake_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    monkeypatch.setattr(fake_mode, "FAKE_PLAYGROUND", True)
    monkeypatch.setattr(fake_mode, "FAKE_ORCHESTRATION", False)
    manifest = _build_manifest(tmp_path)
    assert manifest["integrity"]["fake_mode"] is True
    assert manifest["integrity"]["fake_mode_flags"]["fake_playground"] is True


def test_manifest_stamps_fake_orchestration(tmp_path, monkeypatch):
    """The job path is not supposed to write a manifest under the fake at all.

    If one ever does, it must be legible as synthetic on disk like every other
    faked artifact -- so the stamp has to carry this flag too, not just the two
    that existed when the rule was written.
    """
    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    monkeypatch.setattr(fake_mode, "FAKE_PLAYGROUND", False)
    monkeypatch.setattr(fake_mode, "FAKE_ORCHESTRATION", True)
    manifest = _build_manifest(tmp_path)
    assert manifest["integrity"]["fake_mode"] is True
    assert manifest["integrity"]["fake_mode_flags"]["fake_orchestration"] is True


def test_manifest_fake_mode_false_when_real(tmp_path, monkeypatch):
    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    monkeypatch.setattr(fake_mode, "FAKE_PLAYGROUND", False)
    monkeypatch.setattr(fake_mode, "FAKE_ORCHESTRATION", False)
    manifest = _build_manifest(tmp_path)
    assert manifest["integrity"]["fake_mode"] is False
    assert manifest["integrity"]["fake_mode_flags"] == {
        "fake_libvirt": False,
        "fake_playground": False,
        "fake_orchestration": False,
    }
