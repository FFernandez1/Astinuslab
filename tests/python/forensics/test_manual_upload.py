"""Tests for operator-supplied (manual) ISF and memory-dump uploads."""

from __future__ import annotations

import hashlib
import io
import json
import types
from contextlib import contextmanager

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from astinuslab.core.config import Settings
from astinuslab.core.config import settings as real_settings

ISF_KERNEL = "5.15.0-91-generic"
ISF_TARGET_ID = "ubuntu-24.04"
VALID_ISF = {
    "metadata": {
        "linux": {
            "symbols": [{"kind": "dwarf", "name": f"vmlinux-{ISF_KERNEL}"}],
            "types": [],
        }
    },
    "symbols": {"init_task": {"address": 1}},
}


def _isf_bytes(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


@pytest.fixture()
def scope_factory():
    import astinuslab.db.models  # noqa: F401 — registers ORM classes with Base.metadata
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

    return _scope


@pytest.fixture()
def manual_env(monkeypatch, tmp_path, scope_factory):
    """Point the manual-upload module + symbol cache at a temp tree and in-memory DB."""
    import astinuslab.forensics.manual_upload as mu
    import astinuslab.forensics.vol3_support as vs

    fake = types.SimpleNamespace(**{field: getattr(real_settings, field) for field in Settings.model_fields})
    fake.vol3_symbols_path = tmp_path / "vol3" / "symbols"
    fake.storage_dir = tmp_path / "storage"
    (fake.vol3_symbols_path / "linux").mkdir(parents=True, exist_ok=True)
    (fake.storage_dir / "dumps").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(mu, "settings", fake)
    monkeypatch.setattr(vs, "settings", fake)
    monkeypatch.setattr(mu, "session_scope", scope_factory)

    # Curated targets are auto-disabled when their image file is absent; on a
    # clean runner no images exist, so force the ISF target enabled/available so
    # these tests exercise the upload flow rather than image provisioning.
    _real_load_targets = mu.load_targets

    def _load_targets_isf_enabled():
        items = [dict(t) for t in _real_load_targets()]
        for item in items:
            if item.get("id") == ISF_TARGET_ID:
                item["enabled"] = True
                item["image_available"] = True
        return items

    monkeypatch.setattr(mu, "load_targets", _load_targets_isf_enabled)
    return types.SimpleNamespace(mu=mu, scope=scope_factory, settings=fake)


def _seed_job(scope, *, job_id="job-1", kernel="5.15.0-91-generic", status="completed"):
    from astinuslab.db.models import Job

    with scope() as session:
        session.add(
            Job(
                id=job_id,
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
                status=status,
                detected_distro="ubuntu",
                detected_version="22.04",
                detected_kernel_release=kernel,
                detected_architecture="x86_64",
            )
        )


def _fake_upload(data: bytes):
    return types.SimpleNamespace(file=io.BytesIO(data))


# ── ISF upload ───────────────────────────────────────────────────────────────


def test_store_manual_isf_valid_creates_selectable_artifact(manual_env):
    _seed_job(manual_env.scope)

    result = manual_env.mu.store_manual_isf("job-1", _isf_bytes(VALID_ISF))

    assert result["status"] == "stored"
    assert result["source"] == "manual_upload"
    from pathlib import Path

    placed = Path(result["isf_path"])
    assert placed.is_file()
    assert placed.stat().st_mode & 0o777 == 0o660
    assert placed.parent == manual_env.settings.vol3_symbols_path / "linux"

    from astinuslab.db.repositories import JobRepository

    with manual_env.scope() as session:
        repo = JobRepository(session)
        artifact = repo.find_valid_isf_by_kernel("5.15.0-91-generic")
        assert artifact is not None
        assert artifact.source == "manual_upload"
        assert artifact.validation_status == "valid"
        assert artifact.sha256 == result["sha256"]
        # Job-scoped selection (used by run_vol3_analysis) resolves it too.
        assert repo.get_valid_isf_artifact("job-1", "5.15.0-91-generic") is not None


def test_store_manual_isf_stamps_known_build_id_and_qualified_slug(manual_env):
    _seed_job(manual_env.scope)
    build_id = "0123456789abcdef0123456789abcdef"
    # A prior build-id-bearing artifact makes get_job_known_build_id return it.
    from astinuslab.db.repositories import JobRepository

    with manual_env.scope() as session:
        JobRepository(session).create_isf_artifact(
            "job-1",
            "5.15.0-91-generic",
            "/legacy/old.json",
            "live_guest",
            build_id=build_id,
            sha256="d" * 64,
        )

    result = manual_env.mu.store_manual_isf("job-1", _isf_bytes(VALID_ISF))

    assert result["build_id"] == build_id
    assert build_id[:16] in result["isf_path"]
    with manual_env.scope() as session:
        artifact = JobRepository(session).find_isf_by_build_id(build_id, "x86_64")
        assert artifact is not None
        assert artifact.source == "manual_upload"


def test_store_manual_isf_rejects_non_json(manual_env):
    _seed_job(manual_env.scope)
    with pytest.raises(HTTPException) as exc:
        manual_env.mu.store_manual_isf("job-1", b"not json at all")
    assert exc.value.status_code == 400


def test_store_manual_isf_rejects_missing_metadata_linux(manual_env):
    _seed_job(manual_env.scope)
    with pytest.raises(HTTPException) as exc:
        manual_env.mu.store_manual_isf("job-1", _isf_bytes({"symbols": {"x": {}}}))
    assert exc.value.status_code == 400


def test_store_manual_isf_rejects_empty_symbols(manual_env):
    _seed_job(manual_env.scope)
    with pytest.raises(HTTPException) as exc:
        manual_env.mu.store_manual_isf("job-1", _isf_bytes({"metadata": {"linux": {}}, "symbols": {}}))
    assert exc.value.status_code == 400


def test_store_manual_isf_requires_detected_kernel(manual_env):
    _seed_job(manual_env.scope, kernel=None)
    with pytest.raises(HTTPException) as exc:
        manual_env.mu.store_manual_isf("job-1", _isf_bytes(VALID_ISF))
    assert exc.value.status_code == 400


def test_store_manual_isf_unknown_job_404(manual_env):
    with pytest.raises(HTTPException) as exc:
        manual_env.mu.store_manual_isf("nope", _isf_bytes(VALID_ISF))
    assert exc.value.status_code == 404


def test_store_manual_isf_rejects_active_job(manual_env):
    _seed_job(manual_env.scope, status="running")
    with pytest.raises(HTTPException) as exc:
        manual_env.mu.store_manual_isf("job-1", _isf_bytes(VALID_ISF))
    assert exc.value.status_code == 409


def test_store_manual_isf_for_target_creates_anchor_job_from_embedded_kernel(manual_env):
    result = manual_env.mu.store_manual_isf_for_target(
        ISF_TARGET_ID,
        "immediate-capture",
        _isf_bytes(VALID_ISF),
    )

    assert result["status"] == "stored"
    assert result["source"] == "manual_upload"
    assert result["kernel_release"] == ISF_KERNEL
    assert result["job_created"] is True

    from pathlib import Path

    assert Path(result["isf_path"]).stat().st_mode & 0o777 == 0o660

    from astinuslab.db.repositories import JobRepository

    with manual_env.scope() as session:
        repo = JobRepository(session)
        job = repo.get_job(result["job_id"])
        assert job is not None
        assert job.target_id == ISF_TARGET_ID
        assert job.scenario_id == "immediate-capture"
        assert job.capture_type == "none"
        assert job.build_vol3_isf is True
        assert job.status == "completed"
        assert job.detected_kernel_release == ISF_KERNEL
        assert repo.find_valid_isf_by_kernel(ISF_KERNEL) is not None


def test_store_manual_isf_for_target_reuses_existing_isf_job(manual_env):
    from astinuslab.api.schemas import JobCreate
    from astinuslab.db.repositories import JobRepository

    with manual_env.scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(
            JobCreate(
                target_id=ISF_TARGET_ID,
                scenario_id="immediate-capture",
                execution_mode="immediate",
                capture_type="none",
                build_vol3_isf=True,
            )
        )
        repo.force_transition(job.id, "completed", detected_kernel_release=ISF_KERNEL)
        existing_job_id = job.id

    result = manual_env.mu.store_manual_isf_for_target(
        ISF_TARGET_ID,
        "immediate-capture",
        _isf_bytes(VALID_ISF),
    )

    assert result["job_id"] == existing_job_id
    assert result["job_created"] is False


def test_store_manual_isf_for_target_rejects_isf_without_kernel_name(manual_env):
    payload = {"metadata": {"linux": {"types": []}}, "symbols": {"init_task": {"address": 1}}}
    with pytest.raises(HTTPException) as exc:
        manual_env.mu.store_manual_isf_for_target(ISF_TARGET_ID, "immediate-capture", _isf_bytes(payload))
    assert exc.value.status_code == 400
    assert "kernel release" in exc.value.detail


# ── Memory-dump upload ───────────────────────────────────────────────────────


def test_store_manual_dump_accepts_non_elf_and_records_artifact(manual_env):
    _seed_job(manual_env.scope)

    result = manual_env.mu.store_manual_dump("job-1", _fake_upload(b"NOT-AN-ELF-DUMP"))

    assert result["status"] == "stored"
    assert result["size_bytes"] == len(b"NOT-AN-ELF-DUMP")
    from pathlib import Path

    stored = Path(result["dump_path"])
    assert stored.is_file()
    assert stored.parent == manual_env.settings.storage_dir / "dumps" / "job-1"
    assert result["dump_filename"].startswith("manual-")

    from astinuslab.db.repositories import ArtifactRepository, JobRepository

    with manual_env.scope() as session:
        artifacts = ArtifactRepository(session).list_artifacts(job_id="job-1")
        assert any(a.kind == "memory" and a.producer == "manual_upload" for a in artifacts)
        job = JobRepository(session).get_job("job-1")
        assert job is not None
        assert job.dump_filename == result["dump_filename"]
        assert job.dump_checksum == result["sha256"]


def test_store_manual_dump_rejects_empty(manual_env):
    _seed_job(manual_env.scope)
    with pytest.raises(HTTPException) as exc:
        manual_env.mu.store_manual_dump("job-1", _fake_upload(b""))
    assert exc.value.status_code == 400


def test_store_manual_dump_unknown_job_404(manual_env):
    with pytest.raises(HTTPException) as exc:
        manual_env.mu.store_manual_dump("nope", _fake_upload(b"data"))
    assert exc.value.status_code == 404


def test_store_manual_dump_rejects_active_job(manual_env):
    _seed_job(manual_env.scope, status="running")
    with pytest.raises(HTTPException) as exc:
        manual_env.mu.store_manual_dump("job-1", _fake_upload(b"data"))
    assert exc.value.status_code == 409


def test_store_manual_dump_rejects_oversize_upload_and_removes_temp(manual_env, monkeypatch):
    _seed_job(manual_env.scope)
    monkeypatch.setattr(manual_env.mu, "_DUMP_MAX_UPLOAD_BYTES", 4)

    with pytest.raises(HTTPException) as exc:
        manual_env.mu.store_manual_dump("job-1", _fake_upload(b"12345"))

    assert exc.value.status_code == 413
    dump_dir = manual_env.settings.storage_dir / "dumps" / "job-1"
    assert not list(dump_dir.glob("*.part"))


def test_store_manual_dump_repeated_uploads_keep_distinct_artifacts(manual_env):
    _seed_job(manual_env.scope)

    first = manual_env.mu.store_manual_dump("job-1", _fake_upload(b"FIRST-DUMP"))
    second = manual_env.mu.store_manual_dump("job-1", _fake_upload(b"SECOND-DUMP"))

    assert first["dump_path"] != second["dump_path"]
    from pathlib import Path

    first_path = Path(first["dump_path"])
    second_path = Path(second["dump_path"])
    assert first_path.read_bytes() == b"FIRST-DUMP"
    assert second_path.read_bytes() == b"SECOND-DUMP"

    from astinuslab.db.repositories import ArtifactRepository

    with manual_env.scope() as session:
        artifacts = [a for a in ArtifactRepository(session).list_artifacts(job_id="job-1") if a.kind == "memory"]
        by_path = {a.path: a.sha256 for a in artifacts}
    assert by_path[str(first_path)] == hashlib.sha256(b"FIRST-DUMP").hexdigest()
    assert by_path[str(second_path)] == hashlib.sha256(b"SECOND-DUMP").hexdigest()


# ── Route wiring (TestClient) ────────────────────────────────────────────────


@pytest.fixture()
def manual_client(client, in_memory_session_scope, monkeypatch, tmp_path):
    """Wire the manual-upload module to the same in-memory DB the client uses."""
    import astinuslab.forensics.manual_upload as mu
    import astinuslab.forensics.vol3_support as vs

    fake = types.SimpleNamespace(**{field: getattr(real_settings, field) for field in Settings.model_fields})
    fake.vol3_symbols_path = tmp_path / "vol3" / "symbols"
    fake.storage_dir = tmp_path / "storage"
    (fake.vol3_symbols_path / "linux").mkdir(parents=True, exist_ok=True)
    (fake.storage_dir / "dumps").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(mu, "settings", fake)
    monkeypatch.setattr(vs, "settings", fake)
    monkeypatch.setattr(mu, "session_scope", in_memory_session_scope)

    # See manual_env: force the ISF target available so the route exercises the
    # upload flow rather than image provisioning on an imageless runner.
    _real_load_targets = mu.load_targets

    def _load_targets_isf_enabled():
        items = [dict(t) for t in _real_load_targets()]
        for item in items:
            if item.get("id") == ISF_TARGET_ID:
                item["enabled"] = True
                item["image_available"] = True
        return items

    monkeypatch.setattr(mu, "load_targets", _load_targets_isf_enabled)
    return client


def test_isf_upload_route_unknown_job_returns_404(manual_client):
    resp = manual_client.post(
        "/api/v1/jobs/missing/isf/upload",
        files={"file": ("symbols.json", _isf_bytes(VALID_ISF), "application/json")},
    )
    assert resp.status_code == 404


def test_target_isf_upload_route_creates_anchor_job(manual_client, in_memory_session_scope):
    resp = manual_client.post(
        f"/api/v1/volatility/targets/{ISF_TARGET_ID}/isf/upload",
        data={"scenario_id": "immediate-capture"},
        files={"file": ("symbols.json", _isf_bytes(VALID_ISF), "application/json")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "stored"
    assert body["kernel_release"] == ISF_KERNEL

    from astinuslab.db.repositories import JobRepository

    with in_memory_session_scope() as session:
        job = JobRepository(session).get_job(body["job_id"])
        assert job is not None
        assert job.target_id == ISF_TARGET_ID
        assert job.capture_type == "none"


def test_dump_upload_route_stores_for_created_job(manual_client, created_job, in_memory_session_scope):
    job_id = created_job["id"]
    from astinuslab.db.repositories import JobRepository

    with in_memory_session_scope() as session:
        JobRepository(session).update_job(job_id, status="completed", current_stage="completed")

    resp = manual_client.post(
        f"/api/v1/jobs/{job_id}/dump/upload",
        files={"file": ("memory.raw", b"RAW-MEMORY-BYTES", "application/octet-stream")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "stored"
    assert body["source"] == "manual_upload"
    from pathlib import Path

    stored = Path(body["dump_path"])
    assert stored.is_file()
    assert stored.read_bytes() == b"RAW-MEMORY-BYTES"


# ---------------------------------------------------------------------------
# Task 20 — manual uploads publish through the recoverable journal
# ---------------------------------------------------------------------------


def test_manual_dump_publishes_a_triad_marked_unverified(manual_env):
    """An operator-supplied dump is still evidence in the vault, so it gets the
    same triad every other artifact has - and a manifest that says plainly what
    it is: unverified bytes that bypassed ELF validation."""
    import hashlib
    import json
    from pathlib import Path

    from astinuslab.db.models import ArtifactPublicationAttempt
    from astinuslab.db.repositories import ArtifactRepository

    _seed_job(manual_env.scope)
    payload = b"NOT-AN-ELF-DUMP"

    result = manual_env.mu.store_manual_dump("job-1", _fake_upload(payload))

    stored = Path(result["dump_path"])
    manifest_path = stored.with_suffix(stored.suffix + ".manifest.json")
    sidecar_path = stored.with_suffix(stored.suffix + ".sha256")
    assert manifest_path.is_file() and sidecar_path.is_file()

    manifest = json.loads(manifest_path.read_text())
    assert manifest["producer"] == "manual_upload"
    assert manifest["sha256"] == hashlib.sha256(payload).hexdigest()
    assert manifest["capture"]["capture_method"] == "manual_upload"
    caveat = (manifest["capture"]["forensic_caveat"] or "").lower()
    assert "unverified" in caveat and "operator-supplied" in caveat
    assert sidecar_path.read_text().startswith(manifest["sha256"])

    with manual_env.scope() as session:
        artifact = [a for a in ArtifactRepository(session).list_artifacts(job_id="job-1")][0]
        attempt = session.query(ArtifactPublicationAttempt).one()

        assert artifact.manifest_path == str(manifest_path)
        assert artifact.sha256_sidecar_path == str(sidecar_path)
        assert attempt.state == "completed"
        assert attempt.artifact_id == artifact.id
        assert attempt.producer == "manual_upload"


def test_manual_dump_publishes_nothing_when_the_stream_is_rejected(manual_env, monkeypatch):
    """An oversize upload must leave no bytes and no half-written attempt."""
    from pathlib import Path

    from astinuslab.db.models import ArtifactPublicationAttempt
    from astinuslab.db.repositories import ArtifactRepository

    _seed_job(manual_env.scope)
    monkeypatch.setattr(manual_env.mu, "_DUMP_MAX_UPLOAD_BYTES", 4)

    with pytest.raises(HTTPException) as exc:
        manual_env.mu.store_manual_dump("job-1", _fake_upload(b"far too many bytes"))
    assert exc.value.status_code == 413

    dump_dir = Path(manual_env.settings.storage_dir) / "dumps" / "job-1"
    leftovers = sorted(p.name for p in dump_dir.iterdir()) if dump_dir.exists() else []
    assert leftovers == [], f"a rejected upload must leave nothing, found {leftovers}"

    with manual_env.scope() as session:
        assert ArtifactRepository(session).list_artifacts(job_id="job-1") == []
        attempt = session.query(ArtifactPublicationAttempt).one()
        assert attempt.state in {"failed", "cleanup_incomplete"}


def test_manual_dump_detects_bytes_changing_under_the_upload(manual_env, monkeypatch):
    """The digest of what was received and the digest of what landed on disk must
    agree; if they do not, something rewrote the file and it is not what the
    operator uploaded."""
    from astinuslab.db.repositories import ArtifactRepository
    from astinuslab.storage import publication

    _seed_job(manual_env.scope)
    payload = b"uploaded bytes"
    monkeypatch.setattr(publication, "_hash_payload", lambda fd, path: ("f" * 64, len(payload)))

    with pytest.raises(HTTPException) as exc:
        manual_env.mu.store_manual_dump("job-1", _fake_upload(payload))
    assert exc.value.status_code == 500

    with manual_env.scope() as session:
        assert ArtifactRepository(session).list_artifacts(job_id="job-1") == []
