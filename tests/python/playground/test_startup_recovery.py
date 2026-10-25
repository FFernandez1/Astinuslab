from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from unittest.mock import MagicMock

from tests.python._support.helpers import autospec_setattr


def test_libvirt_open_uses_settings_libvirt_uri(monkeypatch):
    """_libvirt_open must respect settings.libvirt_uri, not a hardcoded URI."""
    from astinuslab.jobs import startup_recovery

    captured: dict[str, str] = {}

    def fake_open(uri):
        captured["uri"] = uri
        return MagicMock()

    libvirt_mod = sys.modules["libvirt"]
    monkeypatch.setattr(libvirt_mod, "open", fake_open)

    fake_settings = types.SimpleNamespace(libvirt_uri="qemu:///session")
    monkeypatch.setattr(startup_recovery, "settings", fake_settings)

    startup_recovery._libvirt_open()

    assert captured["uri"] == "qemu:///session"


def test_startup_recovery_purges_in_flight_playground(monkeypatch, tmp_path):
    from astinuslab.jobs import startup_recovery
    from astinuslab.playground import cleanup as playground_cleanup
    from astinuslab.playground.repositories import create_playground_session

    fake_conn = MagicMock()
    dom1, dom2 = MagicMock(), MagicMock()
    dom1.name.return_value = "astinuslab-pg-pg-1-web"
    dom2.name.return_value = "astinuslab-pg-pg-1-db"
    dom1.isActive.return_value = True
    dom2.isActive.return_value = False
    disk_root = tmp_path / "storage" / "playground"
    disk_file = disk_root / "pg-1" / "web.qcow2"
    disk_file.parent.mkdir(parents=True)
    disk_file.write_bytes(b"qcow2")
    dom1.XMLDesc.return_value = (
        f"<domain><devices><disk type='file'><source file='{disk_file}'/></disk></devices></domain>"
    )
    fake_conn.listAllDomains.return_value = [dom1, dom2]
    net1 = MagicMock()
    net1.name.return_value = "astinuslab-pg-pg-1-ab"
    net1.isActive.return_value = True
    fake_conn.listAllNetworks.return_value = [net1]
    autospec_setattr(monkeypatch, startup_recovery, "_libvirt_open", lambda: fake_conn)
    monkeypatch.setattr(
        startup_recovery,
        "settings",
        types.SimpleNamespace(
            libvirt_uri="qemu:///system",
            playground_disk_dir=disk_root,
            playground_capture_dir=tmp_path / "storage" / "playground-captures",
            base_dir=tmp_path,
            data_dir=tmp_path,
            storage_dir=tmp_path / "storage",
        ),
    )

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import astinuslab.db.models  # noqa
    from astinuslab.db import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    @contextmanager
    def _scope():
        s = Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(startup_recovery, "_pg_session_scope", _scope)
    monkeypatch.setattr(playground_cleanup, "session_scope", _scope)

    # Insert an in-flight playground job
    with _scope() as s:
        create_playground_session(s, job_id="pg-1")

    startup_recovery.recover_playground_sessions()

    # libvirt calls
    dom1.destroy.assert_called_once()
    dom1.undefine.assert_called_once()
    dom2.destroy.assert_not_called()  # isActive was False
    dom2.undefine.assert_called_once()
    # C1: disk overlays for ACTIVE sessions are no longer swept by
    # startup_recovery; the DB-purge path (purge_session_resources ->
    # destroy_vm) is now solely responsible for their removal.
    assert disk_file.exists()
    net1.destroy.assert_called_once()
    net1.undefine.assert_called_once()

    # DB state
    with _scope() as s:
        from astinuslab.db.models import PlaygroundSession

        playground_session = s.query(PlaygroundSession).filter_by(id="pg-1").one()
        assert playground_session.status == "failed"
        assert playground_session.finished_at is not None
        assert "startup" in (playground_session.error_summary or "").lower()


def test_startup_recovery_delegates_db_playground_sessions_to_shared_purge(monkeypatch, tmp_path):
    from astinuslab.jobs import startup_recovery
    from astinuslab.playground import cleanup as playground_cleanup
    from astinuslab.playground.repositories import create_playground_session

    fake_conn = MagicMock()
    fake_conn.listAllDomains.return_value = []
    fake_conn.listAllNetworks.return_value = []
    autospec_setattr(monkeypatch, startup_recovery, "_libvirt_open", lambda: fake_conn)
    monkeypatch.setattr(
        startup_recovery,
        "settings",
        types.SimpleNamespace(
            libvirt_uri="qemu:///system",
            playground_disk_dir=tmp_path / "storage" / "playground",
            playground_capture_dir=tmp_path / "storage" / "playground-captures",
            base_dir=tmp_path,
            data_dir=tmp_path,
            storage_dir=tmp_path / "storage",
        ),
    )

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import astinuslab.db.models  # noqa
    from astinuslab.db import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    @contextmanager
    def _scope():
        s = Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(startup_recovery, "_pg_session_scope", _scope)
    calls = []

    def fake_purge_session_resources(*, job_id: str, final_status: str, error_summary: str | None = None):
        calls.append((job_id, final_status, error_summary))

    monkeypatch.setattr(playground_cleanup, "purge_session_resources", fake_purge_session_resources)

    with _scope() as s:
        create_playground_session(s, job_id="pg-1")

    startup_recovery.recover_playground_sessions()

    assert calls == [("pg-1", "failed", "purged on backend startup")]


def test_startup_recovery_preserves_host_resources_owned_by_frozen_verification(monkeypatch, tmp_path):
    """The generic host sweep must not race a fresh clean-run verification."""
    from astinuslab.jobs import startup_recovery
    from astinuslab.playground import repositories as pr

    disk_root = tmp_path / "storage" / "playground"
    disk_path = disk_root / "pg-aabbccddeeff" / "target.qcow2"
    disk_path.parent.mkdir(parents=True)
    disk_path.write_bytes(b"frozen overlay")

    domain = MagicMock()
    domain.name.return_value = "astinuslab-pg-pg-aabbccddeeff-target"
    domain.isActive.return_value = True
    network = MagicMock()
    network.name.return_value = "astinuslab-pg-pg-aabbccddeeff-isolated-cafe"
    network.isActive.return_value = True

    stale_domain = MagicMock()
    stale_domain.name.return_value = "astinuslab-pg-pg-deaddeaddead-target"
    stale_domain.isActive.return_value = True
    stale_network = MagicMock()
    stale_network.name.return_value = "astinuslab-pg-pg-deaddeaddead-isolated-1234"
    stale_network.isActive.return_value = True

    fake_conn = MagicMock()
    fake_conn.listAllDomains.return_value = [domain, stale_domain]
    fake_conn.listAllNetworks.return_value = [network, stale_network]
    autospec_setattr(monkeypatch, startup_recovery, "_libvirt_open", lambda: fake_conn)
    monkeypatch.setattr(
        startup_recovery,
        "settings",
        types.SimpleNamespace(
            libvirt_uri="qemu:///system",
            playground_disk_dir=disk_root,
            playground_capture_dir=tmp_path / "storage" / "playground-captures",
            storage_dir=tmp_path / "storage",
        ),
    )

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import astinuslab.db.models  # noqa: F401
    from astinuslab.db import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    @contextmanager
    def _scope():
        session = Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(startup_recovery, "_pg_session_scope", _scope)
    with _scope() as session:
        authored = pr.create_playground_session(session, job_id="pg-aabbccddeeff")
        net = pr.register_network(
            session,
            network_id="net-frozen",
            job_id=authored.id,
            name="isolated",
            subnet_cidr="192.0.2.0/29",
            isolated=True,
            libvirt_net_name=network.name(),
        )
        net.state = "active"
        pr.register_vm(
            session,
            vm_id="vm-frozen",
            job_id=authored.id,
            label="target",
            image_id="ubuntu-24.04",
            domain_name=domain.name(),
            disk_path=str(disk_path),
        )
        pr.update_vm_state(session, "vm-frozen", "running")
        pr.close_authored_session(session, authored.id)

    startup_recovery.cleanup_stale_playground_resources()

    domain.destroy.assert_not_called()
    domain.undefine.assert_not_called()
    network.destroy.assert_not_called()
    network.undefine.assert_not_called()
    stale_domain.destroy.assert_called_once()
    stale_domain.undefine.assert_called_once()
    stale_network.destroy.assert_called_once()
    stale_network.undefine.assert_called_once()
    assert disk_path.read_bytes() == b"frozen overlay"


def test_completed_capture_survives_worker_restart(monkeypatch, tmp_path):
    """C1: a completed capture (Acquisition row + on-disk artifact) must
    survive a Celery worker restart unchanged. The startup sweep is forbidden
    from touching settings.playground_capture_dir.
    """
    import hashlib

    from astinuslab.jobs import startup_recovery
    from astinuslab.playground import cleanup as playground_cleanup

    fake_conn = MagicMock()
    fake_conn.listAllDomains.return_value = []
    fake_conn.listAllNetworks.return_value = []
    autospec_setattr(monkeypatch, startup_recovery, "_libvirt_open", lambda: fake_conn)

    disk_root = tmp_path / "storage" / "playground"
    capture_root = tmp_path / "storage" / "playground-captures"
    monkeypatch.setattr(
        startup_recovery,
        "settings",
        types.SimpleNamespace(
            libvirt_uri="qemu:///system",
            playground_disk_dir=disk_root,
            playground_capture_dir=capture_root,
            base_dir=tmp_path,
            data_dir=tmp_path,
            storage_dir=tmp_path / "storage",
        ),
    )

    # Create a real capture artifact under <capture_root>/pg-XXX/vm-YYY/
    job_id = "pg-aabbccddeeff"
    vm_id = "vm-1122334455"
    capture_dir = capture_root / job_id / vm_id
    capture_dir.mkdir(parents=True)
    capture_path = capture_dir / "memory-vm-1122334455-20260101T000000Z-deadbeef.elf"
    capture_payload = b"ELF\x7fcompleted-capture-bytes"
    capture_path.write_bytes(capture_payload)
    expected_sha = hashlib.sha256(capture_payload).hexdigest()
    expected_size = len(capture_payload)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import astinuslab.db.models  # noqa: F401 — registers ORM classes
    from astinuslab.db import Base
    from astinuslab.db.models import Acquisition, JobVM

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    @contextmanager
    def _scope():
        s = Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(startup_recovery, "_pg_session_scope", _scope)
    monkeypatch.setattr(playground_cleanup, "session_scope", _scope)

    # The session is already terminal (completed); recovery must not
    # re-purge anything for it.
    from astinuslab.db.models import PlaygroundSession

    with _scope() as s:
        s.add(
            PlaygroundSession(
                id=job_id,
                status="completed",
            )
        )
        s.add(
            JobVM(
                id=vm_id,
                session_id=job_id,
                label="target",
                image_id="ubuntu-22-04-test",
                domain_name=f"astinuslab-{job_id}-target",
                disk_path=str(disk_root / job_id / "target.qcow2"),
                state="destroyed",
            )
        )
        s.add(
            Acquisition(
                id="acq-1",
                name="playground-vm-1122334455-memory-20260101T000000Z",
                status="completed",
                path=str(capture_path),
                sha256=expected_sha,
                size_bytes=expected_size,
                kind="memory",
                session_id=job_id,
                vm_label="target",
                job_vm_id=vm_id,
            )
        )

    startup_recovery.recover_playground_sessions()

    # The capture file must still exist and be byte-identical.
    assert capture_path.exists(), "capture artifact was deleted by startup sweep"
    assert hashlib.sha256(capture_path.read_bytes()).hexdigest() == expected_sha

    # The Acquisition DB row must be unchanged.
    with _scope() as s:
        acq = s.query(Acquisition).filter_by(id="acq-1").one()
        assert acq.status == "completed"
        assert acq.path == str(capture_path)
        assert acq.sha256 == expected_sha
        assert acq.size_bytes == expected_size
