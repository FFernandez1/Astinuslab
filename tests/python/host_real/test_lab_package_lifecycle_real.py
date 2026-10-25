"""Opt-in real-KVM lifecycle acceptance for one locked portable lab package.

This row is intentionally separate from the hermetic package tests.  It needs
an approved Ubuntu 24.04 LTS x86_64 host with KVM, a prepared libvirt ACL
policy, and a previously exported locked package.  It is inactive unless
``ASTINUSLAB_REAL_PACKAGE_E2E=1`` is set in addition to the directory-wide
``ASTINUSLAB_REAL_HOST=1`` gate.  The package path is supplied by
``ASTINUSLAB_REAL_LAB_PACKAGE`` so this test never creates a synthetic locked
claim or silently falls back to fake mode.

When ``ASTINUSLAB_REAL_PACKAGE_LIFECYCLE_JSON`` is set, the successful run
writes the small private lifecycle observation consumed by
``tools/release/collect_lab_package_host_evidence.py``.  It contains no guest
memory, disk bytes, credentials, or private keys.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from astinuslab.db import session_scope
from astinuslab.db.models import JobNetwork, JobVM, LabInstance, PlaygroundSession
from astinuslab.lab_packages.package_service import LabPackageService
from tests.python._support.helpers import assert_fresh_ubuntu_kvm as _assert_fresh_ubuntu_kvm
from tests.python._support.lab_package_observations import assert_native_wiring, observe_phase, resource_residue

pytestmark = [pytest.mark.host_real, pytest.mark.timeout(3600)]

_TRUE = {"1", "true", "yes", "on"}


def _enabled(value: str | None) -> bool:
    return (value or "").lower() in _TRUE


def _package_path() -> Path:
    if not _enabled(os.getenv("ASTINUSLAB_REAL_PACKAGE_E2E")):
        pytest.skip("set ASTINUSLAB_REAL_PACKAGE_E2E=1 to run the destructive package lifecycle row")
    if not _enabled(os.getenv("ASTINUSLAB_REAL_PACKAGE_FRESH_STORAGE")):
        raise RuntimeError(
            "ASTINUSLAB_REAL_PACKAGE_FRESH_STORAGE=1 must confirm dedicated disposable storage; this row runs zero-grace GC"
        )
    value = os.getenv("ASTINUSLAB_REAL_LAB_PACKAGE")
    if not value:
        raise RuntimeError("ASTINUSLAB_REAL_LAB_PACKAGE is required when package lifecycle e2e is enabled")
    path = Path(value)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"configured locked package is unavailable: {path}") from exc
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"configured locked package must be a regular file: {path}")
    if metadata.st_size < 1:
        raise RuntimeError(f"configured locked package is empty: {path}")
    return path


def _wait_for_state(
    client: TestClient, instance_id: str, expected: set[str], *, timeout: float = 1800
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/lab-packages/operations/{instance_id}")
        response.raise_for_status()
        latest = response.json()
        if latest.get("state") in expected:
            return latest
        if latest.get("state") in {"failed", "cleanup_incomplete"}:
            raise AssertionError(f"package instance {instance_id} ended in {latest.get('state')}: {latest}")
        time.sleep(5)
    raise AssertionError(f"package instance {instance_id} did not reach {sorted(expected)}: {latest}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _qemu_info(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        ["qemu-img", "info", "--output=json", str(path)],
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise AssertionError(f"qemu-img returned a non-object for {path}")
    return value


def _assert_backing_digest(overlay: Path, clean_layer: Path, expected_sha256: str) -> None:
    assert overlay.is_file() and not overlay.is_symlink(), f"overlay is missing: {overlay}"
    info = _qemu_info(overlay)
    backing_name = info.get("full-backing-filename") or info.get("backing-filename")
    assert isinstance(backing_name, str) and backing_name, f"overlay has no immutable backing: {overlay}"
    backing = Path(backing_name)
    if not backing.is_absolute():
        backing = overlay.parent / backing
    assert backing.resolve() == clean_layer.resolve(), f"overlay backing escaped clean layer: {backing}"
    assert _sha256(backing) == expected_sha256, "immutable backing digest changed"


def _libvirt_names(session_id: str) -> tuple[set[str], set[str]]:
    from astinuslab.core.config import settings

    with session_scope() as session:
        domains = {vm.domain_name for vm in session.query(JobVM).filter_by(session_id=session_id).all()}
        networks = {
            network.libvirt_net_name for network in session.query(JobNetwork).filter_by(session_id=session_id).all()
        }
    return _libvirt_objects_exist(domains, networks, uri=settings.libvirt_uri)


def _libvirt_objects_exist(domains: set[str], networks: set[str], *, uri: str) -> tuple[set[str], set[str]]:
    import libvirt

    conn = libvirt.open(uri)
    if conn is None:
        raise AssertionError("could not open libvirt connection")
    try:
        live_domains = {name for name in domains if _exists(conn, "lookupByName", name)}
        live_networks = {name for name in networks if _exists(conn, "networkLookupByName", name)}
    finally:
        conn.close()
    return live_domains, live_networks


def _exists(connection: Any, method: str, name: str) -> bool:
    try:
        getattr(connection, method)(name)
    except Exception as exc:
        import libvirt

        absent = {libvirt.VIR_ERR_NO_DOMAIN, libvirt.VIR_ERR_NO_NETWORK}
        if isinstance(exc, libvirt.libvirtError) and exc.get_error_code() in absent:
            return False
        raise
    return True


def _observe_session(definition, session_id: str, service: LabPackageService) -> tuple[dict[str, Any], dict[str, Any]]:
    import libvirt

    from astinuslab.core.config import settings
    from astinuslab.lab_packages.verification_evaluators import build_checkpoint_evaluator, build_readiness_checker

    def backing(overlay: Path, digest: str, size: int) -> tuple[str, int]:
        clean = service.content_store.path_for(digest)
        _assert_backing_digest(overlay, clean, digest)
        observed_size = clean.stat().st_size
        assert observed_size == size, "backing size changed"
        return digest, observed_size

    with session_scope() as session:
        vms = list(session.query(JobVM).filter_by(session_id=session_id).all())
        networks = list(session.query(JobNetwork).filter_by(session_id=session_id).all())
        domains = {vm.domain_name for vm in vms}
        network_names = {network.libvirt_net_name for network in networks}
        extras: set[Path] = set()
        domain_xml: dict[str, str] = {}
        network_xml: dict[str, str] = {}
        connection = libvirt.open(settings.libvirt_uri)
        assert connection is not None
        try:
            for vm in vms:
                domain = connection.lookupByName(vm.domain_name)
                assert domain.isActive() == 1, f"VM is not running: {vm.label}"
                domain_xml[vm.domain_name] = domain.XMLDesc(0)
                xml = ET.fromstring(domain_xml[vm.domain_name])
                for node in xml.findall("./os/nvram"):
                    if node.text and node.text.strip():
                        extras.add(Path(node.text.strip()))
                for node in xml.findall("./devices/disk/source"):
                    if node.get("file") and node.get("file") != vm.disk_path:
                        extras.add(Path(node.attrib["file"]))
            for name in network_names:
                assert connection.networkLookupByName(name).isActive() == 1
                network_xml[name] = connection.networkLookupByName(name).XMLDesc(0)
            assert_native_wiring(vms, networks, domain_xml, network_xml)
        finally:
            connection.close()
        phase = observe_phase(
            definition,
            vms,
            networks,
            backing_observer=backing,
            readiness_checker=build_readiness_checker(session, session_id),
            checkpoint_evaluator=build_checkpoint_evaluator(session, session_id),
        )
        snapshot = {
            "session_id": session_id,
            "domains": sorted(domains),
            "networks": sorted(network_names),
            "overlays": [vm.disk_path for vm in vms],
            "temporary_paths": sorted(str(p) for p in extras),
            "directory": str(settings.playground_disk_dir / session_id),
        }
    return phase, snapshot


def _residue(snapshots: list[dict[str, Any]]) -> dict[str, list[str]]:
    from astinuslab.core.config import settings

    overlays = {Path(p) for row in snapshots for p in row["overlays"]}
    extras = {Path(p) for row in snapshots for p in row["temporary_paths"]}
    reservations = []
    for row in snapshots:
        directory = Path(row["directory"])
        if directory.is_symlink():
            extras.add(directory)
        elif directory.exists():
            # Walk current state too: an unrecorded seed/reservation must not escape the snapshot.
            for root, dirs, files in os.walk(directory, followlinks=False):
                for name in dirs + files:
                    path = Path(root) / name
                    if name.startswith(".astinuslab-reservation-"):
                        reservations.append(str(path))
                    elif path not in overlays:
                        extras.add(path)
    with session_scope() as session:
        for row in snapshots:
            if session.get(PlaygroundSession, row["session_id"]) is not None:
                reservations.append(row["session_id"])
    return resource_residue(
        domains={n for row in snapshots for n in row["domains"]},
        networks={n for row in snapshots for n in row["networks"]},
        overlays=overlays,
        temporary_paths=extras,
        reservations=reservations,
        lookup=lambda domains, networks: _libvirt_objects_exist(domains, networks, uri=settings.libvirt_uri),
    )


def _publish_lifecycle(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.partial")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def test_locked_package_lifecycle_real_host():
    """Observe every locked VM at launch/reset and both sessions after teardown."""

    _assert_fresh_ubuntu_kvm()
    package_path = _package_path()
    from astinuslab.lab_packages.api import router
    from astinuslab.lab_packages.archive import inspect_archive
    from astinuslab.lab_packages.package_catalog import LabPackageRepository

    app = FastAPI()
    app.include_router(router)
    service = LabPackageService()
    archive = service.inspect(package_path)[0]
    assert archive.definition.reproducibility == "locked", "acceptance package must be locked"
    assert archive.definition.vms, "acceptance package must contain a VM"
    assert all(vm.effective_boot_layer is not None for vm in archive.definition.vms)
    # Re-inspection of the exact file catches a package changing between setup
    # and import; the content store is still populated only by the import path.
    inspected_again = inspect_archive(package_path)
    assert inspected_again.definition.model_dump(mode="json") == archive.definition.model_dump(mode="json")

    instance_id: str | None = None
    old_session_id: str | None = None
    clean_layers: dict[str, tuple[Path, str, int]] = {}
    lifecycle: dict[str, Any] = {}
    snapshots: list[dict[str, Any]] = []
    try:
        with session_scope() as session:
            existing = LabPackageRepository(session, archive_root=service.package_root).get(
                archive.definition.package_id, archive.definition.version
            )
            if existing is not None:
                raise RuntimeError(
                    "package identity is already registered; use a fresh package version before acceptance"
                )
            imported = service.import_path(package_path, session)
            record = imported.record

        for contract_vm in archive.definition.vms:
            layer = contract_vm.effective_boot_layer
            assert layer is not None
            path = service.content_store.path_for(layer.sha256)
            clean_layers[contract_vm.id] = (path, layer.sha256, layer.size_bytes)

        with TestClient(app, raise_server_exceptions=True) as client:
            prepared = client.post(
                f"/api/v1/lab-packages/{record.id}/prepare",
                json={"idempotency_key": f"real-package-{os.getpid()}", "network_opt_in": False},
            )
            prepared.raise_for_status()
            prepare_id = prepared.json()["id"]
            deadline = time.monotonic() + 1800
            while True:
                status = client.get(f"/api/v1/lab-packages/prepare/{prepare_id}")
                status.raise_for_status()
                prepared_payload = status.json()
                if prepared_payload["state"] == "completed":
                    break
                if prepared_payload["state"] in {"failed", "cancelled"}:
                    raise AssertionError(f"prepare failed: {prepared_payload}")
                if time.monotonic() >= deadline:
                    raise AssertionError(f"prepare timed out: {prepared_payload}")
                time.sleep(5)
            assert all(asset["state"] == "ready" for asset in prepared_payload["assets"])

            preflight = client.post(f"/api/v1/lab-packages/{record.id}/preflight")
            preflight.raise_for_status()
            preflight_payload = preflight.json()
            assert preflight_payload["ready"], preflight_payload
            launch = client.post(
                f"/api/v1/lab-packages/{record.id}/launch",
                json={
                    "idempotency_key": f"real-package-launch-{os.getpid()}",
                    "acknowledged_warning_token": preflight_payload["warning_token"],
                    "acknowledge_guest_egress": False,
                },
            )
            launch.raise_for_status()
            launched = launch.json()
            instance_id = launched["id"]
            old_session_id = launched["session_id"]
            assert isinstance(old_session_id, str) and old_session_id
            ready = _wait_for_state(client, instance_id, {"ready"})
            assert ready["session_id"] == old_session_id

            with session_scope() as session:
                instance = session.get(LabInstance, instance_id)
                assert instance is not None and instance.playground_session_id == old_session_id
            lifecycle["launch"], launch_snapshot = _observe_session(archive.definition, old_session_id, service)
            snapshots.append(launch_snapshot)

            reset_request = client.post(f"/api/v1/lab-packages/operations/{instance_id}/reset")
            reset_request.raise_for_status()
            reset_ready = _wait_for_state(client, instance_id, {"ready"})
            new_session_id = reset_ready["session_id"]
            assert new_session_id and new_session_id != old_session_id
            lifecycle["reset"], reset_snapshot = _observe_session(archive.definition, new_session_id, service)
            snapshots.append(reset_snapshot)
            previous_residue = _residue([launch_snapshot])
            assert not any(previous_residue.values()), f"reset retained old session resources: {previous_residue}"
            lifecycle["reset"]["previous_session_residue"] = previous_residue

            teardown = client.post(f"/api/v1/lab-packages/operations/{instance_id}/teardown")
            teardown.raise_for_status()
            destroyed = _wait_for_state(client, instance_id, {"destroyed"})
            assert destroyed["session_id"] is None
            residue = _residue(snapshots)
            assert not any(residue.values()), f"teardown retained resources: {residue}"
            retained = []
            retained_contracts = {
                layer.sha256: layer.size_bytes
                for vm in archive.definition.vms
                if (layer := vm.effective_boot_layer) is not None
            }
            for payload in archive.manifest.payloads:
                if payload.path.startswith("assets/sha256/"):
                    retained_contracts[payload.sha256] = payload.size_bytes
            for digest, size_bytes in retained_contracts.items():
                path = service.content_store.path_for(digest)
                assert path.is_file() and not path.is_symlink()
                assert path.stat().st_size == size_bytes and _sha256(path) == digest
                retained.append(digest)
            lifecycle["teardown"] = {"state": "destroyed", **residue, "retained_asset_digests": sorted(retained)}
            lifecycle["resource_snapshots"] = snapshots

            deleted = client.delete(f"/api/v1/lab-packages/{record.id}")
            deleted.raise_for_status()
            with session_scope() as session:
                service.garbage_collect(session, grace_seconds=0)
            assert all(not path.exists() for path, _digest, _size in clean_layers.values())

        evidence_path = os.getenv("ASTINUSLAB_REAL_PACKAGE_LIFECYCLE_JSON")
        if evidence_path:
            _publish_lifecycle(Path(evidence_path), lifecycle)
    finally:
        # A failed assertion must not strand a real domain.  This cleanup is
        # best-effort; the durable instance state and host acceptance operator
        # remain responsible for investigating ``cleanup_incomplete``.
        if instance_id is not None:
            try:
                with TestClient(app, raise_server_exceptions=False) as cleanup_client:
                    response = cleanup_client.post(f"/api/v1/lab-packages/operations/{instance_id}/teardown")
                    if response.status_code < 500:
                        _wait_for_state(cleanup_client, instance_id, {"destroyed"}, timeout=300)
            except Exception:
                pass
