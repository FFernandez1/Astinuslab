"""Image deletion must preserve pending and retained Playground backings."""

from __future__ import annotations

import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from astinuslab import catalog
from astinuslab.api.schemas import PlaygroundNicSpec, PlaygroundSpawnVMRequest
from astinuslab.db import Base
from astinuslab.db.models import JobVM
from astinuslab.playground import orchestrator as po
from astinuslab.playground import repositories as pr
from astinuslab.playground.routes import vms
from astinuslab.ui import image_routes


@pytest.fixture()
def image_state(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'custody.db'}", connect_args={"timeout": 3})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)

    @contextmanager
    def scope():
        with sessions.begin() as session:
            yield session

    images = tmp_path / "images"
    images.mkdir()
    disks = tmp_path / "disks"
    disks.mkdir()
    source = images / "base.qcow2"
    source.write_bytes(b"source fixture")
    unrelated = images / "unused.qcow2"
    unrelated.write_bytes(b"unrelated fixture")
    monkeypatch.setattr(image_routes, "settings", SimpleNamespace(images_dir=images, playground_disk_dir=disks))
    monkeypatch.setattr(image_routes, "session_scope", scope)
    monkeypatch.setattr(catalog, "load_targets", lambda: [{"id": "base", "image_ref": source.name}])
    monkeypatch.setattr(catalog, "load_attackers", lambda: [])
    monkeypatch.setattr(catalog, "load_network_nodes", lambda: [])
    with scope() as session:
        pr.create_playground_session(session, job_id="pg-custody")
    yield source, unrelated, disks, scope
    engine.dispose()


def _register(scope, disks, state, image_id="base"):
    with scope() as session:
        vm = pr.register_vm(
            session,
            vm_id="vm-custody",
            job_id="pg-custody",
            label="custody",
            image_id=image_id,
            domain_name="astinuslab-pg-custody",
            disk_path=str(disks / "custody.qcow2"),
        )
        vm.state = state


@pytest.mark.parametrize("state", ["pending", "starting", "running", "stopped", "failed", "destroying"])
def test_single_image_delete_refuses_retained_vm_source(image_state, state):
    source, unrelated, disks, scope = image_state
    _register(scope, disks, state)

    with pytest.raises(HTTPException) as refusal:
        image_routes.admin_images_delete(filename=source.name)

    assert refusal.value.status_code == 409
    assert source.read_bytes() == b"source fixture"
    assert unrelated.is_file()


def test_bulk_image_delete_validates_all_custody_before_first_unlink(image_state):
    source, unrelated, disks, scope = image_state
    _register(scope, disks, "failed")

    with pytest.raises(HTTPException) as refusal:
        image_routes.admin_images_bulk_delete(filenames=[unrelated.name, source.name])

    assert refusal.value.status_code == 409
    assert source.is_file() and unrelated.is_file()


def test_unrelated_source_can_be_deleted_while_a_vm_is_pending(image_state):
    source, unrelated, disks, scope = image_state
    source.unlink()
    subprocess.run(["qemu-img", "create", "-f", "qcow2", str(source), "1M"], check=True, capture_output=True)
    _register(scope, disks, "pending")

    response = image_routes.admin_images_delete(filename=unrelated.name)

    assert response.status_code == 303
    assert source.is_file() and not unrelated.exists()


def test_destroyed_vm_without_retained_disk_releases_image(image_state):
    source, _unrelated, disks, scope = image_state
    _register(scope, disks, "destroyed")

    response = image_routes.admin_images_delete(filename=source.name)

    assert response.status_code == 303
    assert not source.exists()


def test_pending_raw_source_allows_unrelated_image_deletion(image_state):
    source, unrelated, disks, scope = image_state
    source.unlink()
    subprocess.run(["qemu-img", "create", "-f", "raw", str(source), "1M"], check=True, capture_output=True)
    _register(scope, disks, "pending")

    response = image_routes.admin_images_delete(filename=unrelated.name)

    assert response.status_code == 303
    assert source.is_file() and not unrelated.exists()


def _retained_chain(source, disks):
    source.unlink()
    subprocess.run(["qemu-img", "create", "-f", "qcow2", str(source), "1M"], check=True, capture_output=True)
    disk = disks / "custody.qcow2"
    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(source), str(disk)],
        check=True,
        capture_output=True,
    )
    return disk


@pytest.mark.parametrize("state", ["stopped", "failed", "destroyed"])
def test_actual_backing_survives_catalog_remap_or_terminal_state(image_state, monkeypatch, state):
    source, unrelated, disks, scope = image_state
    _retained_chain(source, disks)
    _register(scope, disks, state)
    monkeypatch.setattr(catalog, "load_targets", lambda: [{"id": "base", "image_ref": unrelated.name}])

    with pytest.raises(HTTPException) as refusal:
        image_routes.admin_images_delete(filename=source.name)

    assert refusal.value.status_code == 409
    assert source.is_file()


def test_unrelated_source_deletion_inspects_retained_chain(image_state, monkeypatch):
    source, unrelated, disks, scope = image_state
    _retained_chain(source, disks)
    _register(scope, disks, "stopped")
    monkeypatch.setattr(catalog, "load_targets", lambda: [])

    response = image_routes.admin_images_delete(filename=unrelated.name)

    assert response.status_code == 303
    assert source.is_file() and not unrelated.exists()


@pytest.mark.parametrize("relative", [False, True], ids=["absolute", "relative"])
@pytest.mark.parametrize("state", ["pending", "stopped"])
@pytest.mark.parametrize("bulk", [False, True], ids=["single", "bulk"])
def test_external_qcow_data_is_retained_before_and_after_overlay_creation(image_state, relative, state, bulk):
    source, unrelated, disks, scope = image_state
    source.unlink()
    data = source.with_name("base-data.raw")
    data_reference = data.name if relative else str(data)
    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", "-o", f"data_file={data_reference},data_file_raw=on", source.name, "1M"],
        cwd=source.parent,
        check=True,
        capture_output=True,
    )
    if state != "pending":
        subprocess.run(
            ["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(source), str(disks / "custody.qcow2")],
            check=True,
            capture_output=True,
        )
    _register(scope, disks, state)

    with pytest.raises(HTTPException) as refusal:
        if bulk:
            image_routes.admin_images_bulk_delete(filenames=[unrelated.name, data.name])
        else:
            image_routes.admin_images_delete(filename=data.name)

    assert refusal.value.status_code == 409
    assert data.is_file() and source.is_file() and unrelated.is_file()
    subprocess.run(["qemu-img", "check", str(source)], cwd=source.parent, check=True, capture_output=True)


def test_pending_source_backing_chain_is_retained(image_state):
    source, _unrelated, disks, scope = image_state
    source.unlink()
    backing = source.with_name("older.qcow2")
    subprocess.run(["qemu-img", "create", "-f", "qcow2", str(backing), "1M"], check=True, capture_output=True)
    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(backing), str(source)],
        check=True,
        capture_output=True,
    )
    _register(scope, disks, "pending")

    with pytest.raises(HTTPException) as refusal:
        image_routes.admin_images_delete(filename=backing.name)

    assert refusal.value.status_code == 409
    assert backing.is_file() and source.is_file()


@pytest.mark.parametrize("state", ["pending", "stopped"])
def test_unhandled_vmdk_extent_dependencies_fail_closed(image_state, state):
    source, _unrelated, disks, scope = image_state
    source.unlink()
    backing = source.with_name("older.vmdk")
    subprocess.run(
        ["qemu-img", "create", "-f", "vmdk", "-o", "subformat=twoGbMaxExtentSparse", str(backing), "1M"],
        check=True,
        capture_output=True,
    )
    extent = source.with_name("older-s001.vmdk")
    assert extent.is_file()
    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", "-F", "vmdk", "-b", str(backing), str(source)],
        check=True,
        capture_output=True,
    )
    if state != "pending":
        subprocess.run(
            ["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(source), str(disks / "custody.qcow2")],
            check=True,
            capture_output=True,
        )
    _register(scope, disks, state)

    with pytest.raises(HTTPException) as refusal:
        image_routes.admin_images_delete(filename=extent.name)

    assert refusal.value.status_code == 409
    assert "reconcile" in refusal.value.detail
    assert extent.is_file() and backing.is_file() and source.is_file()


def test_unreadable_retained_overlay_fails_closed(image_state):
    source, unrelated, disks, scope = image_state
    _register(scope, disks, "failed")
    (disks / "custody.qcow2").write_bytes(b"incomplete disk metadata")

    with pytest.raises(HTTPException) as refusal:
        image_routes.admin_images_delete(filename=unrelated.name)

    assert refusal.value.status_code == 409
    assert source.is_file() and unrelated.is_file()


@pytest.mark.parametrize("state", ["starting", "running", "stopped", "failed"])
def test_missing_nonpending_disk_cannot_prove_unrelated_source_safe(image_state, state):
    source, unrelated, disks, scope = image_state
    _register(scope, disks, state)

    with pytest.raises(HTTPException) as refusal:
        image_routes.admin_images_delete(filename=unrelated.name)

    assert refusal.value.status_code == 409
    assert source.is_file() and unrelated.is_file()


def _pending_registration(scope):
    with scope() as session:
        return vms._register_pending_vm(
            session,
            job_id="pg-custody",
            vm_id="vm-new",
            payload=PlaygroundSpawnVMRequest(label="new", image_id="base", nics=[PlaygroundNicSpec(network_id="net")]),
            nic_specs=[],
            resources=(1, 512, 1),
        )


def test_deletion_finishes_before_registration_revalidates_source(image_state, monkeypatch):
    from astinuslab.images import image_custody

    source, _unrelated, disks, scope = image_state
    _register(scope, disks, "destroyed")
    monkeypatch.setattr(po, "lookup_playground_image", lambda *_a, **_kw: {"id": "base", "path": str(source)})
    deletion_locked = threading.Event()
    registration_entered = threading.Event()
    release_deletion = threading.Event()
    original_sources = image_custody._catalog_sources
    original_lock = pr.lock_resource_transitions

    def hold_deletion(root):
        deletion_locked.set()
        assert release_deletion.wait(3)
        return original_sources(root)

    def mark_registration(session):
        registration_entered.set()
        return original_lock(session)

    monkeypatch.setattr(image_custody, "_catalog_sources", hold_deletion)
    monkeypatch.setattr(pr, "lock_resource_transitions", mark_registration)
    with ThreadPoolExecutor(max_workers=2) as workers:
        deletion = workers.submit(image_routes.admin_images_delete, source.name)
        assert deletion_locked.wait(3)
        registration = workers.submit(_pending_registration, scope)
        assert registration_entered.wait(3)
        release_deletion.set()
        assert deletion.result(timeout=5).status_code == 303
        with pytest.raises(HTTPException) as refusal:
            registration.result(timeout=5)
        assert refusal.value.status_code == 400
    with scope() as session:
        assert session.get(JobVM, "vm-new") is None


def test_pending_registration_blocks_competing_image_deletion(image_state, monkeypatch):
    from astinuslab.images import image_custody

    source, _unrelated, _disks, scope = image_state
    monkeypatch.setattr(po, "lookup_playground_image", lambda *_a, **_kw: {"id": "base", "path": str(source)})
    registered = threading.Event()
    deletion_entered = threading.Event()
    release_registration = threading.Event()
    original_reserve = image_custody.reserve_sqlite_write

    def hold_registration():
        with scope() as session:
            vms._register_pending_vm(
                session,
                job_id="pg-custody",
                vm_id="vm-new",
                payload=PlaygroundSpawnVMRequest(
                    label="new", image_id="base", nics=[PlaygroundNicSpec(network_id="net")]
                ),
                nic_specs=[],
                resources=(1, 512, 1),
            )
            registered.set()
            assert release_registration.wait(3)

    def mark_deletion(session):
        deletion_entered.set()
        return original_reserve(session)

    monkeypatch.setattr(image_custody, "reserve_sqlite_write", mark_deletion)
    with ThreadPoolExecutor(max_workers=2) as workers:
        registration = workers.submit(hold_registration)
        assert registered.wait(3)
        deletion = workers.submit(image_routes.admin_images_delete, source.name)
        assert deletion_entered.wait(3)
        release_registration.set()
        registration.result(timeout=5)
        with pytest.raises(HTTPException) as refusal:
            deletion.result(timeout=5)
        assert refusal.value.status_code == 409
    assert source.is_file()
