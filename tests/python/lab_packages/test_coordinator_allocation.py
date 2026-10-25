"""Overlay-allocation verification contract for the instance coordinator.

A launch reserves capacity in journal-owned files, then hands the launch to the
Playground, which creates real qcow2 overlays. ``qemu-img`` cannot rename a
preallocated inode into an overlay, so the reservation is only safe to release
once the coordinator has seen a real overlay in the same directory and on the
same filesystem the reservation covered. Everything this refuses is a way a
launch could otherwise free reserved bytes without ever consuming them, and
then run out of disk mid-boot.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace

import pytest

from astinuslab.db.models import (
    JobVM,
    LabInstance,
    LabInstanceAction,
    LabOperationOutbox,
    LabPackage,
    PlaygroundSession,
)
from astinuslab.jobs import package_instance_coordinator as coordinator
from astinuslab.lab_packages.preflight import DiskReservation, reserve_disk_space

pytestmark = pytest.mark.unit


def _payload(*directories: Path) -> dict:
    return {
        "disk_requirements": [
            {"vm_id": f"vm-{index}", "directory": str(directory), "bytes": 1}
            for index, directory in enumerate(directories)
        ]
    }


def _add_vm(session, *, session_id: str, vm_id: str, disk_path: Path) -> None:
    session.add(PlaygroundSession(id=session_id, status="running"))
    session.add(
        JobVM(
            id=vm_id,
            session_id=session_id,
            label=vm_id,
            domain_name=f"astinuslab-{vm_id}",
            role="target",
            image_id="ubuntu-24.04",
            vcpus=1,
            memory_mb=512,
            disk_gb=1,
            disk_path=str(disk_path),
            state="running",
        )
    )


def _seed_claimable_launch(session, *, instance_id: str, payload: dict) -> None:
    package_id = f"lp-{instance_id}"
    session_id = f"pg-{instance_id}"
    action_id = instance_id.replace("lpi-", "lia-", 1)
    session.add(
        LabPackage(
            id=package_id,
            package_id=f"package-{instance_id}",
            version="1.0.0",
            title="Reservation lab",
            creator="Researcher",
            license="CC-BY-4.0",
            reproducibility="unlocked",
            definition_json="{}",
            manifest_json="{}",
            manifest_sha256="a" * 64,
            archive_sha256="b" * 64,
            archive_path="/nonexistent/reservation.astinuslab",
            archive_size_bytes=1,
        )
    )
    session.add(PlaygroundSession(id=session_id, lab_package_record_id=package_id, status="running"))
    session.add(
        LabInstance(
            id=instance_id,
            package_record_id=package_id,
            playground_session_id=session_id,
            idempotency_key=instance_id,
            state="registered",
        )
    )
    session.add(
        LabInstanceAction(
            id=action_id,
            instance_id=instance_id,
            action_type="launch",
            state="queued",
            payload_json=json.dumps(payload),
        )
    )
    session.add(
        LabOperationOutbox(
            id=f"loo-{instance_id}",
            operation_kind="package_instance_launch",
            operation_id=instance_id,
            state="pending",
            payload_json=json.dumps(payload),
        )
    )


# --- _reservation_directories --------------------------------------------------


def test_reservation_directories_are_deduplicated_and_sorted(tmp_path: Path):
    first = tmp_path / "b"
    second = tmp_path / "a"

    directories = coordinator._reservation_directories(_payload(first, second, first))

    assert directories == (second, first)


def test_reservation_directories_ignore_malformed_entries():
    payload = {"disk_requirements": ["not-a-mapping", 42, None]}

    assert coordinator._reservation_directories(payload) == ()


def test_reservation_directories_are_empty_without_requirements():
    assert coordinator._reservation_directories({}) == ()


# --- _verify_overlay_allocation ------------------------------------------------


def test_verification_accepts_a_real_overlay_beside_its_reservation(
    in_memory_session_scope, monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)
    overlay = tmp_path / "vm-0.qcow2"
    overlay.write_bytes(b"qcow2 overlay bytes")
    reservation = reserve_disk_space(((tmp_path, 1024),), owner_id="lpi-ok")

    with in_memory_session_scope() as session:
        _add_vm(session, session_id="pg-ok", vm_id="vm-0", disk_path=overlay)
        session.commit()

    coordinator._verify_overlay_allocation("pg-ok", _payload(tmp_path), reservation)

    reservation.release()


def test_verification_accepts_an_absent_reservation(in_memory_session_scope, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)
    overlay = tmp_path / "vm-0.qcow2"
    overlay.write_bytes(b"qcow2 overlay bytes")

    with in_memory_session_scope() as session:
        _add_vm(session, session_id="pg-none", vm_id="vm-0", disk_path=overlay)
        session.commit()

    coordinator._verify_overlay_allocation("pg-none", _payload(tmp_path), None)


def test_verification_refuses_a_launch_that_created_no_overlay(in_memory_session_scope, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)

    with pytest.raises(RuntimeError, match="overlay allocation verification failed"):
        coordinator._verify_overlay_allocation("pg-empty", _payload(tmp_path), None)


def test_verification_refuses_an_empty_overlay(in_memory_session_scope, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)
    overlay = tmp_path / "vm-0.qcow2"
    overlay.touch()

    with in_memory_session_scope() as session:
        _add_vm(session, session_id="pg-empty-file", vm_id="vm-0", disk_path=overlay)
        session.commit()

    with pytest.raises(RuntimeError, match="overlay allocation verification failed"):
        coordinator._verify_overlay_allocation("pg-empty-file", _payload(tmp_path), None)


def test_verification_refuses_a_symlinked_overlay(in_memory_session_scope, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)
    (tmp_path / "real.qcow2").write_bytes(b"qcow2 overlay bytes")
    overlay = tmp_path / "vm-0.qcow2"
    overlay.symlink_to(tmp_path / "real.qcow2")

    with in_memory_session_scope() as session:
        _add_vm(session, session_id="pg-link", vm_id="vm-0", disk_path=overlay)
        session.commit()

    with pytest.raises(RuntimeError, match="overlay allocation verification failed"):
        coordinator._verify_overlay_allocation("pg-link", _payload(tmp_path), None)


def test_verification_refuses_an_overlay_in_another_directory(in_memory_session_scope, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)
    reserved_dir = tmp_path / "reserved"
    other_dir = tmp_path / "elsewhere"
    reserved_dir.mkdir()
    other_dir.mkdir()
    overlay = other_dir / "vm-0.qcow2"
    overlay.write_bytes(b"qcow2 overlay bytes")

    with in_memory_session_scope() as session:
        _add_vm(session, session_id="pg-astray", vm_id="vm-0", disk_path=overlay)
        session.commit()

    with pytest.raises(RuntimeError, match="overlay allocation verification failed"):
        coordinator._verify_overlay_allocation("pg-astray", _payload(reserved_dir), None)


def test_verification_refuses_a_reservation_path_that_is_no_longer_regular(
    in_memory_session_scope, monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)
    overlay = tmp_path / "vm-0.qcow2"
    overlay.write_bytes(b"qcow2 overlay bytes")
    swapped = tmp_path / "swapped-reservation"
    swapped.mkdir()

    with in_memory_session_scope() as session:
        _add_vm(session, session_id="pg-swapped", vm_id="vm-0", disk_path=overlay)
        session.commit()

    with pytest.raises(RuntimeError, match="reservation allocation verification failed"):
        coordinator._verify_overlay_allocation(
            "pg-swapped", _payload(tmp_path), DiskReservation((swapped,), "posix_fallocate")
        )


# --- _record_reservation -------------------------------------------------------


def test_recording_a_reservation_stores_its_paths_and_method(in_memory_session_scope, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)
    reservation = reserve_disk_space(((tmp_path, 1024),), owner_id="lpi-record")

    with in_memory_session_scope() as session:
        _seed_claimable_launch(session, instance_id="lpi-record", payload={"locked": False})
    claim = coordinator._claim_launch("lpi-record")
    assert claim is not None

    assert coordinator._record_reservation(claim, reservation) is True

    with in_memory_session_scope() as session:
        action = session.get(LabInstanceAction, "lia-record")
        payload = json.loads(action.payload_json)
        assert payload["reservation_paths"] == [str(reservation.paths[0])]
        assert payload["allocation_method"] == reservation.allocation_method
        assert payload["_coordinator"]["reservation_claimed_at"] == claim.claimed_at.isoformat()
        assert action.state == "running"

    reservation.release()


def test_recording_no_reservation_is_a_no_op(in_memory_session_scope, monkeypatch):
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)
    with in_memory_session_scope() as session:
        _seed_claimable_launch(session, instance_id="lpi-no-reservation", payload={"disk_requirements": []})
    claim = coordinator._claim_launch("lpi-no-reservation")
    assert claim is not None

    assert coordinator._record_reservation(claim, None) is True


def test_recording_a_reservation_without_its_action_is_refused(in_memory_session_scope, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)
    reservation = reserve_disk_space(((tmp_path, 1024),), owner_id="lpi-orphan")
    claim = coordinator._OperationClaim(
        instance_id="lpi-orphan",
        action_type="launch",
        action_id="lia-orphan",
        generation=0,
        session_id="pg-orphan",
        claimed_at=datetime.now(UTC),
        payload={},
        phase="running",
        stale_claim=False,
    )

    assert coordinator._record_reservation(claim, reservation) is False

    reservation.release()


# --- _reservation_lock ---------------------------------------------------------


def test_reservation_lock_creates_one_private_global_lock_per_directory(monkeypatch, tmp_path: Path):
    storage_dir = tmp_path / "storage"
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    monkeypatch.setattr(coordinator, "settings", SimpleNamespace(storage_dir=storage_dir))

    with coordinator._reservation_lock(_payload(first, second), "lpi-lock"):
        locks = sorted((storage_dir / "tmp" / "package-instance-reservation-locks").glob("lpi-lock-*.lock"))

    assert len(locks) == 2
    assert all(os.stat(path).st_mode & 0o077 == 0 for path in locks)
    assert list(first.iterdir()) == []
    assert list(second.iterdir()) == []


def test_reservation_lock_does_not_prevent_real_playground_artifact_cleanup(monkeypatch, tmp_path: Path):
    from astinuslab.playground import orchestrator

    storage_dir = tmp_path / "storage"
    playground_disk_dir = storage_dir / "playground" / "disks"
    session_dir = playground_disk_dir / "pg-reservation-cleanup"
    session_dir.mkdir(parents=True)
    disk_path = session_dir / "target.qcow2"
    disk_path.write_bytes(b"overlay")
    monkeypatch.setattr(coordinator, "settings", SimpleNamespace(storage_dir=storage_dir))
    monkeypatch.setattr(orchestrator, "settings", SimpleNamespace(playground_disk_dir=playground_disk_dir))

    with coordinator._reservation_lock(_payload(session_dir), "lpi-reservation-cleanup"):
        pass

    orchestrator._remove_playground_artifacts(disk_path)

    global_lock_dir = storage_dir / "tmp" / "package-instance-reservation-locks"
    global_locks = list(global_lock_dir.glob("*.lock"))
    assert not session_dir.exists()
    assert len(global_locks) == 1
    assert global_locks[0].is_file()
    assert not global_locks[0].is_relative_to(playground_disk_dir)


def test_reservation_lock_keeps_three_waiters_on_one_inode(monkeypatch, tmp_path: Path):
    storage_dir = tmp_path / "storage"
    payload = _payload(tmp_path)
    first_entered = Event()
    release_first = Event()
    second_opened = Event()
    second_entered = Event()
    release_second = Event()
    third_entered = Event()
    open_guard = Lock()
    open_count = 0
    real_open = coordinator.os.open
    monkeypatch.setattr(coordinator, "settings", SimpleNamespace(storage_dir=storage_dir))

    def observed_open(*args, **kwargs):
        nonlocal open_count
        descriptor = real_open(*args, **kwargs)
        with open_guard:
            open_count += 1
            if open_count == 2:
                second_opened.set()
        return descriptor

    def hold_lock(entered: Event, release: Event):
        with coordinator._reservation_lock(payload, "lpi-three-waiters"):
            entered.set()
            assert release.wait(timeout=10)

    def take_third_lock():
        with coordinator._reservation_lock(payload, "lpi-three-waiters"):
            third_entered.set()

    monkeypatch.setattr(coordinator.os, "open", observed_open)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(hold_lock, first_entered, release_first)
        assert first_entered.wait(timeout=10)
        second = pool.submit(hold_lock, second_entered, release_second)
        assert second_opened.wait(timeout=10)
        release_first.set()
        first.result(timeout=10)
        assert second_entered.wait(timeout=10)
        third = pool.submit(take_third_lock)
        third_overlapped_second = third_entered.wait(timeout=0.5)
        release_second.set()
        second.result(timeout=10)
        assert third_entered.wait(timeout=10)
        third.result(timeout=10)

    assert third_overlapped_second is False
    locks = list((storage_dir / "tmp" / "package-instance-reservation-locks").glob("lpi-three-waiters-*.lock"))
    assert len(locks) == 1
    assert locks[0].is_file()


def test_tmp_purge_preserves_the_inode_shared_by_a_reservation_lock_holder_and_waiter(monkeypatch, tmp_path: Path):
    from astinuslab.storage import storage_admin_support as support

    storage_dir = tmp_path / "storage"
    settings = SimpleNamespace(storage_dir=storage_dir)
    payload = _payload(tmp_path / "target")
    holder_entered = Event()
    release_holder = Event()
    waiter_opened = Event()
    waiter_entered = Event()
    open_guard = Lock()
    open_count = 0
    real_open = coordinator.os.open
    monkeypatch.setattr(coordinator, "settings", settings)

    def observed_open(*args, **kwargs):
        nonlocal open_count
        descriptor = real_open(*args, **kwargs)
        with open_guard:
            open_count += 1
            if open_count == 2:
                waiter_opened.set()
        return descriptor

    def hold_lock():
        with coordinator._reservation_lock(payload, "lpi-purge-fence"):
            holder_entered.set()
            assert release_holder.wait(timeout=10)

    def wait_for_lock():
        with coordinator._reservation_lock(payload, "lpi-purge-fence"):
            waiter_entered.set()

    monkeypatch.setattr(coordinator.os, "open", observed_open)
    with ThreadPoolExecutor(max_workers=2) as pool:
        holder = pool.submit(hold_lock)
        assert holder_entered.wait(timeout=10)
        waiter = pool.submit(wait_for_lock)
        assert waiter_opened.wait(timeout=10)
        lock_dir = storage_dir / "tmp" / "package-instance-reservation-locks"
        lock_path = next(lock_dir.glob("lpi-purge-fence-*.lock"))
        inode_before = lock_path.stat().st_ino
        try:
            result = support.purge_tmp_orphans(
                settings,
                lambda: [{"name": lock_dir.name, "removable": True}],
            )
            path_survived = lock_path.is_file()
            inode_after = lock_path.stat().st_ino if path_survived else None
            waiter_bypassed_holder = waiter_entered.wait(timeout=0.5)
        finally:
            release_holder.set()
        holder.result(timeout=10)
        waiter.result(timeout=10)

    assert result["deleted"] == 0
    assert result["skipped"] == 1
    assert path_survived is True
    assert inode_after == inode_before
    assert waiter_bypassed_holder is False


def test_clear_runtime_preserves_a_reservation_lock_inode_across_three_acquirers(monkeypatch, tmp_path: Path):
    from astinuslab.storage import storage_admin_support as support

    storage_dir = tmp_path / "storage"
    settings = SimpleNamespace(storage_dir=storage_dir)
    payload = _payload(tmp_path / "target")
    holder_entered = Event()
    release_holder = Event()
    waiter_opened = Event()
    waiter_entered = Event()
    third_entered = Event()
    open_guard = Lock()
    open_count = 0
    real_open = coordinator.os.open
    monkeypatch.setattr(coordinator, "settings", settings)

    trace = storage_dir / "tmp" / "orphan-upload"
    trace.parent.mkdir(parents=True)
    trace.write_bytes(b"trace")
    runner_log = storage_dir / "logs" / "runner.log"
    runner_log.parent.mkdir()
    runner_log.write_bytes(b"log")
    dump = storage_dir / "dumps" / "memory.bin"
    dump.parent.mkdir()
    dump.write_bytes(b"dump")
    api_pid = storage_dir / "api.pid"
    api_pid.write_text("123", encoding="utf-8")
    runner_pid = storage_dir / "runner.pid"
    runner_pid.write_text("456", encoding="utf-8")

    def observed_open(*args, **kwargs):
        nonlocal open_count
        descriptor = real_open(*args, **kwargs)
        with open_guard:
            open_count += 1
            if open_count == 2:
                waiter_opened.set()
        return descriptor

    def hold_lock():
        with coordinator._reservation_lock(payload, "lpi-clear-runtime-fence"):
            holder_entered.set()
            assert release_holder.wait(timeout=10)

    def wait_for_lock(entered: Event):
        with coordinator._reservation_lock(payload, "lpi-clear-runtime-fence"):
            entered.set()

    monkeypatch.setattr(coordinator.os, "open", observed_open)
    with ThreadPoolExecutor(max_workers=3) as pool:
        holder = pool.submit(hold_lock)
        assert holder_entered.wait(timeout=10)
        waiter = pool.submit(wait_for_lock, waiter_entered)
        assert waiter_opened.wait(timeout=10)
        lock_dir = storage_dir / "tmp" / "package-instance-reservation-locks"
        lock_path = next(lock_dir.glob("lpi-clear-runtime-fence-*.lock"))
        inode_before = lock_path.stat().st_ino
        result = support.clear_runtime_traces(settings, lambda: False)
        third = pool.submit(wait_for_lock, third_entered)
        try:
            third_bypassed_holder = third_entered.wait(timeout=0.5)
            path_survived = lock_path.is_file()
            inode_after = lock_path.stat().st_ino if path_survived else None
        finally:
            release_holder.set()
        holder.result(timeout=10)
        waiter.result(timeout=10)
        third.result(timeout=10)

    assert result["ok"] is True
    assert third_bypassed_holder is False
    assert path_survived is True
    assert inode_after == inode_before
    assert not trace.exists()
    assert not runner_log.exists()
    assert not dump.exists()
    assert not api_pid.exists()
    assert not runner_pid.exists()
    assert (storage_dir / "logs").is_dir()


def test_clear_runtime_fails_closed_when_directory_becomes_protected_lock_alias(monkeypatch, tmp_path: Path):
    from astinuslab.storage import storage_admin_support as support

    storage_dir = tmp_path / "storage"
    settings = SimpleNamespace(storage_dir=storage_dir)
    payload = _payload(tmp_path / "target")
    trace_dir = storage_dir / "tmp" / "ordinary-trace"
    trace_dir.mkdir(parents=True)
    holder_entered = Event()
    release_holder = Event()
    waiter_opened = Event()
    waiter_entered = Event()
    third_entered = Event()
    type_checked = Event()
    swap_complete = Event()
    open_guard = Lock()
    type_check_guard = Lock()
    open_count = 0
    trace_lstat_count = 0
    type_check_paused = False
    real_open = coordinator.os.open
    real_is_symlink = support.Path.is_symlink
    real_lstat = support.os.lstat
    monkeypatch.setattr(coordinator, "settings", settings)

    def observed_open(*args, **kwargs):
        nonlocal open_count
        descriptor = real_open(*args, **kwargs)
        with open_guard:
            open_count += 1
            if open_count == 2:
                waiter_opened.set()
        return descriptor

    def pause_after_type_check() -> None:
        nonlocal type_check_paused
        with type_check_guard:
            if type_check_paused:
                return
            type_check_paused = True
        type_checked.set()
        assert swap_complete.wait(timeout=10)

    def observed_is_symlink(path: Path) -> bool:
        result = real_is_symlink(path)
        if path == trace_dir and not result:
            pause_after_type_check()
        return result

    def observed_lstat(path, *, dir_fd=None):
        nonlocal trace_lstat_count
        result = real_lstat(path, dir_fd=dir_fd)
        if dir_fd is not None and path == trace_dir.name:
            trace_lstat_count += 1
            if trace_lstat_count == 2:
                pause_after_type_check()
        return result

    def hold_lock():
        with coordinator._reservation_lock(payload, "lpi-clear-runtime-swap"):
            holder_entered.set()
            assert release_holder.wait(timeout=10)

    def wait_for_lock(entered: Event):
        with coordinator._reservation_lock(payload, "lpi-clear-runtime-swap"):
            entered.set()

    monkeypatch.setattr(coordinator.os, "open", observed_open)
    monkeypatch.setattr(support.Path, "is_symlink", observed_is_symlink)
    monkeypatch.setattr(support.os, "lstat", observed_lstat)
    with ThreadPoolExecutor(max_workers=4) as pool:
        holder = pool.submit(hold_lock)
        assert holder_entered.wait(timeout=10)
        waiter = pool.submit(wait_for_lock, waiter_entered)
        assert waiter_opened.wait(timeout=10)
        lock_dir = storage_dir / "tmp" / "package-instance-reservation-locks"
        lock_path = next(lock_dir.glob("lpi-clear-runtime-swap-*.lock"))
        inode_before = lock_path.stat().st_ino
        cleanup = pool.submit(support.clear_runtime_traces, settings, lambda: False)
        assert type_checked.wait(timeout=10)
        trace_dir.rmdir()
        trace_dir.symlink_to(lock_dir, target_is_directory=True)
        swap_complete.set()
        result = cleanup.result(timeout=10)
        third = pool.submit(wait_for_lock, third_entered)
        try:
            third_bypassed_holder = third_entered.wait(timeout=0.5)
            inode_after = lock_path.stat().st_ino if lock_path.is_file() else None
        finally:
            release_holder.set()
        holder.result(timeout=10)
        waiter.result(timeout=10)
        third.result(timeout=10)

    assert result["ok"] is True
    assert result["tmp_skipped"] == 2
    assert third_bypassed_holder is False
    assert inode_after == inode_before
    assert lock_path.is_file()


# --- run_instance_action: claim and teardown -----------------------------------


def _seed_teardown(
    session,
    *,
    instance_id: str,
    session_id: str | None,
    event_state: str = "pending",
    instance_state: str = "destroying",
    action: str = "teardown",
) -> None:
    from astinuslab.db.models import LabInstance, LabInstanceAction, LabOperationOutbox, LabPackage

    session.add(
        LabPackage(
            id="lp-teardown",
            package_id="teardown-lab",
            version="1.0.0",
            title="Teardown lab",
            creator="Researcher",
            license="CC-BY-4.0",
            reproducibility="unlocked",
            definition_json="{}",
            manifest_json="{}",
            manifest_sha256="d" * 64,
            archive_sha256="e" * 64,
            archive_path="/nonexistent/teardown.astinuslab",
            archive_size_bytes=1,
        )
    )
    if session_id is not None:
        session.add(PlaygroundSession(id=session_id, status="running"))
    session.add(
        LabInstance(
            id=instance_id,
            package_record_id="lp-teardown",
            playground_session_id=session_id,
            idempotency_key=instance_id,
            state=instance_state,
        )
    )
    session.add(
        LabInstanceAction(
            id=f"action-{instance_id}",
            instance_id=instance_id,
            action_type=action,
            state="queued",
        )
    )
    session.add(
        LabOperationOutbox(
            id=f"outbox-{instance_id}",
            operation_kind=f"package_instance_{action}",
            operation_id=instance_id,
            state=event_state,
        )
    )


def test_an_action_without_an_outbox_event_is_ignored(in_memory_session_scope, monkeypatch):
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)

    assert coordinator.run_instance_action("lpi-absent", "teardown") == "lpi-absent"


def test_a_completed_event_is_never_claimed_again(in_memory_session_scope, monkeypatch):
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(
        coordinator,
        "_invoke",
        lambda *_a, **_k: pytest.fail("a completed event must not be worked again"),
    )

    with in_memory_session_scope() as session:
        _seed_teardown(session, instance_id="lpi-done", session_id="pg-done", event_state="completed")
        session.commit()

    coordinator.run_instance_action("lpi-done", "teardown")


def test_teardown_destroys_the_session_and_records_the_instance(in_memory_session_scope, monkeypatch):
    from astinuslab.db.models import LabInstance, LabOperationOutbox

    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)
    destroyed: list[str] = []

    def destroy_session(_task, *, job_id: str):
        destroyed.append(job_id)
        with in_memory_session_scope() as session:
            session.get(PlaygroundSession, job_id).status = "cancelled"

    monkeypatch.setattr(coordinator, "_invoke", destroy_session)

    with in_memory_session_scope() as session:
        _seed_teardown(session, instance_id="lpi-down", session_id="pg-down")
        session.commit()

    coordinator.run_instance_action("lpi-down", "teardown")

    with in_memory_session_scope() as session:
        instance = session.get(LabInstance, "lpi-down")
        event = session.get(LabOperationOutbox, "outbox-lpi-down")
        assert destroyed == ["pg-down"]
        assert instance.state == "destroyed"
        assert instance.playground_session_id is None
        assert session.get(PlaygroundSession, "pg-down") is None
        assert event.state == "completed"


def test_teardown_without_a_remaining_session_completes_its_claimed_event(in_memory_session_scope, monkeypatch):
    from astinuslab.db.models import LabInstance, LabOperationOutbox

    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)

    with in_memory_session_scope() as session:
        _seed_teardown(
            session,
            instance_id="lpi-already-down",
            session_id=None,
            event_state="claimed",
            instance_state="destroying",
        )
        session.flush()
        session.get(LabOperationOutbox, "outbox-lpi-already-down").claimed_at = datetime.now(UTC)
        session.commit()

    coordinator.run_instance_action("lpi-already-down", "teardown")

    with in_memory_session_scope() as session:
        assert session.get(LabInstance, "lpi-already-down").state == "destroyed"
        assert session.get(LabOperationOutbox, "outbox-lpi-already-down").state == "completed"


def test_a_failed_teardown_leaves_the_instance_marked_for_cleanup(in_memory_session_scope, monkeypatch):
    from astinuslab.db.models import LabInstance, LabOperationOutbox

    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)

    def refuse(*_args, **_kwargs):
        raise RuntimeError("libvirt refused the destroy")

    monkeypatch.setattr(coordinator, "_invoke", refuse)

    with in_memory_session_scope() as session:
        _seed_teardown(session, instance_id="lpi-stuck", session_id="pg-stuck")
        session.commit()

    coordinator.run_instance_action("lpi-stuck", "teardown")

    with in_memory_session_scope() as session:
        instance = session.get(LabInstance, "lpi-stuck")
        assert instance.state == "cleanup_incomplete"
        assert "libvirt refused" in (instance.error_summary or "")
        assert session.get(LabOperationOutbox, "outbox-lpi-stuck").state == "failed"
        assert session.get(PlaygroundSession, "pg-stuck") is not None


@pytest.mark.parametrize(
    ("action", "instance_state"),
    [("cancel", "cancel_requested"), ("teardown", "destroying")],
)
def test_normal_return_with_destroy_failed_keeps_cleanup_custody(
    in_memory_session_scope,
    monkeypatch,
    action: str,
    instance_state: str,
):
    from astinuslab.db.models import LabInstance, LabOperationOutbox

    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)

    def record_destroy_failure(_task, *, job_id: str):
        with in_memory_session_scope() as session:
            session.get(PlaygroundSession, job_id).status = "destroy_failed"

    monkeypatch.setattr(coordinator, "_invoke", record_destroy_failure)
    with in_memory_session_scope() as session:
        _seed_teardown(
            session,
            instance_id=f"lpi-{action}-destroy-failed",
            session_id=f"pg-{action}-destroy-failed",
            instance_state=instance_state,
            action=action,
        )
        session.commit()

    coordinator.run_instance_action(f"lpi-{action}-destroy-failed", action)

    with in_memory_session_scope() as session:
        instance = session.get(LabInstance, f"lpi-{action}-destroy-failed")
        event = session.get(LabOperationOutbox, f"outbox-lpi-{action}-destroy-failed")
        action_record = session.get(LabInstanceAction, f"action-lpi-{action}-destroy-failed")
        playground = session.get(PlaygroundSession, f"pg-{action}-destroy-failed")
        assert instance.state == "cleanup_incomplete"
        assert instance.playground_session_id == f"pg-{action}-destroy-failed"
        assert playground is not None and playground.status == "destroy_failed"
        assert action_record.state == "failed"
        assert event.state == "failed"


# --- payload and reservation helpers -------------------------------------------


def test_an_unreadable_event_payload_reads_as_empty():
    from astinuslab.db.models import LabOperationOutbox

    event = LabOperationOutbox(
        id="outbox-bad", operation_kind="package_instance_launch", operation_id="lpi-1", payload_json="{not json"
    )

    assert coordinator._payload(event) == {}


def test_an_event_payload_that_is_not_an_object_reads_as_empty():
    from astinuslab.db.models import LabOperationOutbox

    event = LabOperationOutbox(
        id="outbox-list", operation_kind="package_instance_launch", operation_id="lpi-1", payload_json="[1, 2]"
    )

    assert coordinator._payload(event) == {}


def test_an_event_payload_object_is_returned():
    from astinuslab.db.models import LabOperationOutbox

    event = LabOperationOutbox(
        id="outbox-ok",
        operation_kind="package_instance_launch",
        operation_id="lpi-1",
        payload_json='{"locked": true}',
    )

    assert coordinator._payload(event) == {"locked": True}


def test_invoking_a_celery_task_prefers_its_run_attribute():
    class _Task:
        def run(self, **kwargs):
            return ("run", kwargs)

        def __call__(self, **kwargs):
            raise AssertionError("the task body must be called through run")

    assert coordinator._invoke(_Task(), job_id="pg-1") == ("run", {"job_id": "pg-1"})


def test_invoking_a_plain_callable_calls_it_directly():
    assert coordinator._invoke(lambda **kwargs: ("called", kwargs), job_id="pg-1") == (
        "called",
        {"job_id": "pg-1"},
    )


def test_reserving_creates_each_requested_directory(tmp_path: Path):
    target = tmp_path / "nested" / "storage"

    reservation = coordinator._reserve(_payload(target), "lpi-mkdir")

    assert target.is_dir()
    assert os.stat(target).st_mode & 0o077 == 0
    assert reservation is not None
    reservation.release()


def test_reserving_nothing_returns_no_reservation():
    assert coordinator._reserve({}, "lpi-none") is None


def test_a_malformed_reservation_intent_is_refused():
    with pytest.raises(ValueError, match="invalid disk reservation intent"):
        coordinator._reserve({"disk_requirements": ["not a mapping"]}, "lpi-bad")


def test_releasing_recorded_reservations_clears_them_from_the_action(
    in_memory_session_scope, monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)
    reservation = reserve_disk_space(((tmp_path, 1024),), owner_id="lpi-clear")
    payload = {
        **_payload(tmp_path),
        "_coordinator": {
            "action_id": "lia-clear",
            "generation": 0,
            "session_id": "pg-clear",
            "reservation_claimed_at": datetime(2026, 1, 1).isoformat(),
        },
    }

    with in_memory_session_scope() as session:
        session.add(
            LabInstanceAction(
                id="lia-clear",
                instance_id="lpi-clear",
                action_type="launch",
                payload_json=json.dumps(
                    {
                        **payload,
                        "reservation_paths": [str(reservation.paths[0])],
                        "allocation_method": reservation.allocation_method,
                    }
                ),
            )
        )
        session.commit()

    coordinator._release_recorded_reservations("lpi-clear", payload)

    with in_memory_session_scope() as session:
        stored = json.loads(session.get(LabInstanceAction, "lia-clear").payload_json)
        assert "reservation_paths" not in stored
        assert "allocation_method" not in stored

    assert not reservation.paths[0].exists()


def test_old_worker_release_cannot_remove_a_newer_reservation_journal(
    in_memory_session_scope,
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)
    old_claimed_at = datetime(2026, 1, 1)
    new_claimed_at = datetime(2026, 1, 2)
    old_payload = {
        **_payload(tmp_path),
        "_coordinator": {
            "action_id": "lia-owned-reservation",
            "generation": 2,
            "session_id": "pg-owned-reservation",
            "reservation_claimed_at": old_claimed_at.isoformat(),
        },
    }
    new_reservation = tmp_path / ".astinuslab-reservation-lpi-owned-reservation-new"
    new_reservation.write_bytes(b"new owner")
    new_payload = {
        **_payload(tmp_path),
        "reservation_paths": [str(new_reservation)],
        "allocation_method": "journaled_write",
        "_coordinator": {
            "action_id": "lia-owned-reservation",
            "generation": 2,
            "session_id": "pg-owned-reservation",
            "reservation_claimed_at": new_claimed_at.isoformat(),
        },
    }
    with in_memory_session_scope() as session:
        session.add(
            LabInstanceAction(
                id="lia-owned-reservation",
                instance_id="lpi-owned-reservation",
                action_type="reset",
                state="running",
                payload_json=json.dumps(new_payload),
            )
        )
        session.commit()

    coordinator._release_recorded_reservations(
        "lpi-owned-reservation",
        old_payload,
        action_type="reset",
        action_id="lia-owned-reservation",
    )

    with in_memory_session_scope() as session:
        stored = json.loads(session.get(LabInstanceAction, "lia-owned-reservation").payload_json)
        assert stored == new_payload
    assert new_reservation.read_bytes() == b"new owner"


def test_releasing_reservations_without_an_action_is_a_no_op(in_memory_session_scope, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)

    coordinator._release_recorded_reservations("lpi-absent", _payload(tmp_path))


def test_releasing_reservations_tolerates_an_unreadable_action_payload(
    in_memory_session_scope, monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(coordinator, "session_scope", in_memory_session_scope)

    with in_memory_session_scope() as session:
        session.add(
            LabInstanceAction(
                id="lia-bad",
                instance_id="lpi-bad-payload",
                action_type="launch",
                payload_json="{not json",
            )
        )
        session.commit()

    coordinator._release_recorded_reservations("lpi-bad-payload", _payload(tmp_path))

    with in_memory_session_scope() as session:
        assert session.get(LabInstanceAction, "lia-bad").payload_json == "{not json"


@pytest.mark.parametrize("name", ["dumps", "ssh_keys"])
@pytest.mark.parametrize("lock_name", ["package-instance-locks", "package-instance-reservation-locks"])
def test_fixed_cleanup_cannot_split_holder_waiter_and_third_lock(
    monkeypatch, tmp_path: Path, name: str, lock_name: str
):
    """A third acquirer must contend with the waiter after cleanup and handoff."""
    from astinuslab.storage import storage_admin_support as support

    storage = tmp_path / "storage"
    settings = SimpleNamespace(storage_dir=storage)
    monkeypatch.setattr(coordinator, "settings", settings)
    lock_dir = storage / "tmp" / lock_name
    lock_dir.mkdir(parents=True)
    (storage / name).symlink_to(lock_dir, target_is_directory=True)
    holder_entered = Event()
    waiter_probed = Event()
    waiter_entered = Event()
    third_probed = Event()
    third_entered = Event()
    release_holder = Event()
    release_waiter = Event()
    guard = Lock()
    attempts = 0
    contention: dict[int, bool] = {}
    real_flock = coordinator.fcntl.flock

    def observed_flock(descriptor, operation):
        nonlocal attempts
        if operation != coordinator.fcntl.LOCK_EX:
            return real_flock(descriptor, operation)
        with guard:
            attempts += 1
            index = attempts
        if index == 1:
            return real_flock(descriptor, operation)
        # Probe the real kernel lock before blocking. The event proves this
        # contender reached the lock syscall, so scheduler delay cannot pass.
        try:
            real_flock(descriptor, operation | coordinator.fcntl.LOCK_NB)
        except BlockingIOError:
            contention[index] = True
        else:
            contention[index] = False
        (waiter_probed if index == 2 else third_probed).set()
        return real_flock(descriptor, operation)

    def acquire(entered: Event, release: Event | None = None):
        if lock_name == "package-instance-locks":
            lock = coordinator.blocking_file_lock(coordinator._lifecycle_lock_path("lpi-fixed-cleanup"))
        else:
            lock = coordinator._reservation_lock(_payload(tmp_path / "target"), "lpi-fixed-cleanup")
        with lock:
            entered.set()
            if release is not None:
                assert release.wait(timeout=10)

    monkeypatch.setattr(coordinator.fcntl, "flock", observed_flock)
    with ThreadPoolExecutor(max_workers=3) as pool:
        holder = pool.submit(acquire, holder_entered, release_holder)
        try:
            assert holder_entered.wait(timeout=10)
            waiter = pool.submit(acquire, waiter_entered, release_waiter)
            assert waiter_probed.wait(timeout=10)
            assert contention[2] is True
            lock_path = next(lock_dir.glob("*.lock"))
            inode = lock_path.stat().st_ino
            clear = support.clear_storage_state if name == "ssh_keys" else support.clear_runtime_traces
            result = clear(settings, lambda: False)
            release_holder.set()
            assert waiter_entered.wait(timeout=10)
            third = pool.submit(acquire, third_entered)
            assert third_probed.wait(timeout=10)
            assert contention[3] is True
            assert not third_entered.is_set()
            assert lock_path.stat().st_ino == inode
            assert result["ok"] is True
            assert result["fixed_skipped"] == 1
        finally:
            release_holder.set()
            release_waiter.set()
        holder.result(timeout=10)
        waiter.result(timeout=10)
        third.result(timeout=10)
        assert third_entered.is_set()
