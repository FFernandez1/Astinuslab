"""Shared package builders and database scopes for package integration tests."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from astinuslab.db import Base
from astinuslab.lab_packages.archive import write_archive
from astinuslab.lab_packages.models import (
    HostResources,
    LabDefinition,
    LabExperience,
    LabManifest,
    LabNetwork,
    LabVM,
    ManifestPayload,
    ResourceRecommendations,
)


def preflight_documents(required_capabilities: tuple[str, ...] = ()) -> tuple[LabDefinition, LabManifest]:
    definition = LabDefinition(
        package_id="preflight-lab",
        version="1.0.0",
        title="Preflight lab",
        creator="Researcher",
        license="CC-BY-4.0",
        reproducibility="unlocked",
        resources=ResourceRecommendations(
            minimum=HostResources(cpu_threads=2, memory_mb=4096, free_disk_bytes=40 * 1024**3),
            recommended=HostResources(cpu_threads=4, memory_mb=8192, free_disk_bytes=50 * 1024**3),
        ),
        networks=(LabNetwork(id="isolated", subnet_cidr="192.0.2.0/29"),),
        vms=(
            LabVM(
                id="target",
                role="target",
                image_id="ubuntu-24.04",
                vcpus=2,
                memory_mb=2048,
                disk_gb=10,
                network_ids=("isolated",),
            ),
        ),
        experience=LabExperience(),
    )
    manifest = LabManifest(
        package_id=definition.package_id,
        version=definition.version,
        required_capabilities=required_capabilities,
        payloads=(
            ManifestPayload(path="lab.json", sha256="a" * 64, size_bytes=1, media_type="application/json"),
            ManifestPayload(path="experience/runbook.md", sha256="b" * 64, size_bytes=1, media_type="text/markdown"),
        ),
    )
    return definition, manifest


def launch_definition() -> LabDefinition:
    return LabDefinition(
        package_id="launch-lab",
        version="1.0.0",
        title="Launch lab",
        creator="Researcher",
        license="CC-BY-4.0",
        reproducibility="unlocked",
        resources=ResourceRecommendations(
            minimum=HostResources(cpu_threads=1, memory_mb=512, free_disk_bytes=1),
            recommended=HostResources(cpu_threads=1, memory_mb=512, free_disk_bytes=1),
        ),
        networks=(LabNetwork(id="isolated", subnet_cidr="192.0.2.0/29"),),
        vms=(
            LabVM(
                id="target",
                role="target",
                image_id="ubuntu-24.04",
                vcpus=1,
                memory_mb=512,
                disk_gb=1,
                network_ids=("isolated",),
            ),
        ),
        experience=LabExperience(),
    )


def file_session_scope(database_path: Path):
    engine = create_engine(
        f"sqlite:///{database_path}", connect_args={"check_same_thread": False, "timeout": 10}, future=True
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        connection.exec_driver_sql("PRAGMA busy_timeout=10000")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    @contextmanager
    def scope():
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return scope


def service_definition() -> LabDefinition:
    return LabDefinition(
        package_id="service-lab",
        version="1.0.0",
        title="Service lab",
        creator="Researcher",
        license="CC-BY-4.0",
        reproducibility="unlocked",
        resources=ResourceRecommendations(
            minimum=HostResources(cpu_threads=2, memory_mb=4096, free_disk_bytes=40 * 1024**3),
            recommended=HostResources(cpu_threads=4, memory_mb=8192, free_disk_bytes=40 * 1024**3),
        ),
        networks=(LabNetwork(id="isolated", subnet_cidr="192.0.2.0/29"),),
        vms=(
            LabVM(
                id="target",
                role="target",
                image_id="ubuntu-24.04",
                vcpus=2,
                memory_mb=2048,
                disk_gb=10,
                network_ids=("isolated",),
            ),
        ),
        experience=LabExperience(),
    )


def service_package(tmp_path: Path, *, with_asset: bool = True) -> tuple[Path, bytes]:
    content = b"embedded clean layer"
    assets = {hashlib.sha256(content).hexdigest(): content} if with_asset else None
    archive = write_archive(service_definition(), "# Runbook\n", assets=assets, allow_legacy_unreferenced_assets=True)
    path = tmp_path / "input.astinuslab"
    path.write_bytes(archive)
    return path, content


def prepare_definition() -> LabDefinition:
    return LabDefinition(
        package_id="prepare-lab",
        version="1.0.0",
        title="Prepare lab",
        creator="Researcher",
        license="CC-BY-4.0",
        reproducibility="unlocked",
        resources=ResourceRecommendations(
            minimum=HostResources(cpu_threads=2, memory_mb=4096, free_disk_bytes=40 * 1024**3),
            recommended=HostResources(cpu_threads=4, memory_mb=8192, free_disk_bytes=40 * 1024**3),
        ),
        networks=(LabNetwork(id="isolated", subnet_cidr="192.0.2.0/29"),),
        vms=(
            LabVM(
                id="target",
                role="target",
                image_id="ubuntu-24.04",
                vcpus=2,
                memory_mb=2048,
                disk_gb=10,
                network_ids=("isolated",),
            ),
        ),
        experience=LabExperience(),
    )
