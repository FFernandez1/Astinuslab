import os
import platform
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import Mock, create_autospec

import pytest


def configure_csrf_client(client):
    """Give integration-test clients a matching CSRF cookie/header pair."""
    csrf_token = "backend-test-csrf-token"
    client.cookies.set("astinuslab_csrf", csrf_token)
    client.headers["X-CSRF-Token"] = csrf_token
    return client


def autospec_setattr(monkeypatch, obj, name, value, raising=True):
    """
    A wrapper around monkeypatch.setattr that uses create_autospec to ensure
    that the mock strictly adheres to the signature of the real function it replaces.
    """
    try:
        real_attr = getattr(obj, name)
    except AttributeError:
        if not raising:
            monkeypatch.setattr(obj, name, value, raising=raising)
            return
        raise

    # If the attribute is already a Mock (e.g., patched by an earlier fixture),
    # attempting to autospec it again will raise an InvalidSpecError.
    # In this case, we simply overwrite the attribute.
    if isinstance(real_attr, Mock):
        monkeypatch.setattr(obj, name, value, raising=raising)
        return

    if callable(real_attr):
        try:
            mock_func = create_autospec(real_attr)
        except Exception as e:
            if "Cannot spec a Mock object" in str(e):
                monkeypatch.setattr(obj, name, value, raising=raising)
                return
            raise

        if callable(value):
            mock_func.side_effect = value
        else:
            mock_func.return_value = value
        monkeypatch.setattr(obj, name, mock_func, raising=raising)
    else:
        # For non-callable properties (e.g., settings objects), just use normal setattr
        monkeypatch.setattr(obj, name, value, raising=raising)


def assert_fresh_ubuntu_kvm() -> None:
    contract = "package lifecycle acceptance requires Ubuntu 24.04 LTS on x86_64 with KVM"
    machine = platform.machine()
    if machine.lower() not in {"x86_64", "amd64"}:
        raise AssertionError(f"{contract}; found architecture {machine!r}")
    try:
        release: dict[str, str] = {}
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                release[key] = value.strip().strip('"')
    except OSError as exc:
        raise AssertionError(f"{contract}; could not inspect /etc/os-release") from exc
    if release.get("ID") != "ubuntu" or release.get("VERSION_ID") != "24.04":
        raise AssertionError(f"{contract}; found ID={release.get('ID')!r}, VERSION_ID={release.get('VERSION_ID')!r}")
    kvm = Path("/dev/kvm")
    if not kvm.exists() or not os.access(kvm, os.R_OK | os.W_OK):
        raise AssertionError(f"{contract}; /dev/kvm is not readable and writable by the acceptance runner")


def real_host_worker_command() -> list[str]:
    """Build the test worker command with every queue the host_real tier exercises.

    Shared here rather than in the host_real conftest because the topology rule
    allows exactly one cross-suite import path, and two suites need this: the
    host_real fixture that launches the worker, and the host test that asserts
    the launched worker consumes both queues.
    """
    from astinuslab.jobs.celery_app import celery_app

    default_queue = celery_app.conf.task_default_queue
    analysis_queue = celery_app.conf.task_routes["run_vol3_plugin"]["queue"]
    queues = ",".join(dict.fromkeys((default_queue, analysis_queue)))
    return [
        sys.executable,
        "-m",
        "celery",
        "-A",
        "astinuslab.jobs.celery_main",
        "worker",
        "--concurrency=1",
        "--pool=threads",
        "--loglevel=INFO",
        "--queues",
        queues,
    ]


@contextmanager
def isolated_playground_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, namespace: dict[str, Any]
) -> Iterator[None]:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from astinuslab import db

    # Separate SQLite connections preserve the concurrent claim tests. No
    # global engine or table cleanup is needed when each test owns its file.
    test_engine = create_engine(f"sqlite:///{tmp_path / 'playground.sqlite'}", connect_args={"timeout": 10})
    db.Base.metadata.create_all(test_engine)
    factory = sessionmaker(bind=test_engine, autoflush=False)
    try:
        with monkeypatch.context() as local:
            local.setattr(db, "SessionLocal", factory)
            if "SessionLocal" in namespace:
                local.setitem(namespace, "SessionLocal", factory)
            # SQL race fixtures must observe their own database connection;
            # leave db.engine itself untouched for the durable sentinel.
            if "engine" in namespace:
                local.setitem(namespace, "engine", test_engine)
            yield
    finally:
        test_engine.dispose()


def create_evidence_triad(session, tmp_path, *, job_id=None, acquisition_id=None, case_id=None, name="memory.dump"):
    """Write a triad on disk and the Artifact row that owns it."""
    import json

    from astinuslab.db import models

    dumps = tmp_path / "dumps"
    dumps.mkdir(parents=True, exist_ok=True)
    artifact_path = dumps / name
    artifact_path.write_bytes(b"evidence bytes")
    manifest_path = artifact_path.with_suffix(artifact_path.suffix + ".manifest.json")
    manifest_path.write_text(json.dumps({"sha256": "abc"}))
    sidecar_path = artifact_path.with_suffix(artifact_path.suffix + ".sha256")
    sidecar_path.write_text(f"abc  {artifact_path.name}\n")

    artifact = models.Artifact(
        id=f"art-{name}",
        kind="memory",
        producer="job_capture",
        path=str(artifact_path),
        filename=artifact_path.name,
        sha256="abc",
        size_bytes=artifact_path.stat().st_size,
        manifest_path=str(manifest_path),
        sha256_sidecar_path=str(sidecar_path),
        job_id=job_id,
        acquisition_id=acquisition_id,
        case_id=case_id,
    )
    session.add(artifact)
    session.flush()
    return artifact, (artifact_path, manifest_path, sidecar_path)


def create_terminal_job(session, job_id="job-1", status="completed"):
    from astinuslab.api.schemas import JobCreate
    from astinuslab.db.repositories import JobRepository

    repo = JobRepository(session)
    job = repo.create_job(
        JobCreate(target_id="ubuntu-22-04-default", scenario_id="immediate-capture", execution_mode="immediate")
    )
    job.id = job_id
    job.status = status
    session.flush()
    return job


def run_with_test_database(db: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["ASTINUSLAB_DB_PATH"] = str(db)
    return subprocess.run(
        [sys.executable, *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


ALEMBIC_UPGRADE_SCRIPT = """
from alembic import command
from alembic.config import Config
from pathlib import Path
from astinuslab.db import engine
import astinuslab.db.models  # noqa: F401

cfg = Config(str(Path("src/astinuslab/migrations/alembic.ini").resolve()))
with engine.connect() as connection:
    cfg.attributes["connection"] = connection
    command.upgrade(cfg, "{target}")
    connection.commit()
"""
