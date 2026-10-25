from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from astinuslab.db import Base
from astinuslab.db.models import (
    Acquisition,
    JobNetwork,
    JobVM,
    JobVMNic,
    PlaygroundSession,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, future=True)
    s = SessionLocal()
    yield s
    s.close()


def _make_playground_job(session, job_id="pg1") -> PlaygroundSession:
    playground_session = PlaygroundSession(id=job_id, status="running")
    session.add(playground_session)
    session.commit()
    return playground_session


def test_jobvm_persists_with_label_and_image(session):
    _make_playground_job(session)
    vm = JobVM(
        id="vm1",
        session_id="pg1",
        label="web-1",
        image_id="ubuntu-22.04-cloud",
        state="running",
        domain_name="astinuslab-pg-pg1-web-1",
        disk_path="/var/lib/astinuslab/playground/pg1/web-1.qcow2",
    )
    session.add(vm)
    session.commit()
    fetched = session.query(JobVM).filter_by(id="vm1").one()
    assert fetched.label == "web-1"
    assert fetched.image_id == "ubuntu-22.04-cloud"
    assert fetched.state == "running"


def test_jobnetwork_and_nic_round_trip(session):
    _make_playground_job(session)
    vm = JobVM(
        id="vm1",
        session_id="pg1",
        label="b1",
        image_id="img",
        state="running",
        domain_name="d",
        disk_path="p",
    )
    net = JobNetwork(
        id="net1",
        session_id="pg1",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
        libvirt_net_name="astinuslab-pg-pg1-ab",
        state="active",
    )
    session.add_all([vm, net])
    session.commit()

    nic = JobVMNic(id="nic1", job_vm_id="vm1", job_network_id="net1", mac="52:54:00:aa:bb:01", ip="10.42.1.10")
    session.add(nic)
    session.commit()

    fetched_vm = session.query(JobVM).filter_by(id="vm1").one()
    assert len(fetched_vm.nics) == 1
    assert fetched_vm.nics[0].ip == "10.42.1.10"


def test_acquisition_can_reference_jobvm(session):
    _make_playground_job(session)
    vm = JobVM(id="vm1", session_id="pg1", label="x", image_id="img", state="running", domain_name="d", disk_path="p")
    acq = Acquisition(id="acq1", name="capture-1", status="live", job_vm_id="vm1")
    session.add_all([vm, acq])
    session.commit()
    fetched = session.query(Acquisition).filter_by(id="acq1").one()
    assert fetched.job_vm_id == "vm1"


from astinuslab.playground import repositories as pr


def test_create_playground_session_inserts_session_row(session):
    playground_session = pr.create_playground_session(session, job_id="pg2")
    assert playground_session.id == "pg2"
    assert playground_session.status == "running"
    assert playground_session.started_at is not None


def test_create_playground_session_rejects_second_active_session(session):
    pr.create_playground_session(session, job_id="pg-active-1")

    with pytest.raises(IntegrityError):
        pr.create_playground_session(session, job_id="pg-active-2")


def test_create_playground_session_allows_historical_terminal_sessions(session):
    pr.create_playground_session(session, job_id="pg-history-1")
    pr.mark_session_completed(session, "pg-history-1")

    job = pr.create_playground_session(session, job_id="pg-history-2")

    assert job.status == "running"
    active = pr.active_playground_session(session)
    assert active is not None
    assert active.id == "pg-history-2"


def test_terminal_transition_promotes_cleanup_backlog_in_deterministic_order(session):
    active = pr.create_playground_session(session, job_id="pg-active")
    older = PlaygroundSession(id="pg-older", status="destroy_cleanup_pending", created_at=active.created_at)
    newer = PlaygroundSession(id="pg-newer", status="failure_cleanup_pending")
    session.add_all([newer, older])
    session.flush()

    pr.mark_session_completed(session, active.id)

    assert older.status == "destroy_failed"
    assert newer.status == "failure_cleanup_pending"
    promoted = pr.active_playground_session(session)
    assert promoted is not None
    assert promoted.id == older.id

    pr.mark_session_cancelled(session, older.id)

    assert newer.status == "failure_cleanup"
    promoted = pr.active_playground_session(session)
    assert promoted is not None
    assert promoted.id == newer.id


def test_mark_session_failed_keeps_dirty_children_in_cleanup_exclusion(session):
    playground_session = pr.create_playground_session(session, job_id="pg-dirty")
    session.add(
        JobNetwork(
            id="net-dirty",
            session_id=playground_session.id,
            name="dirty",
            subnet_cidr="10.42.1.0/24",
            isolated=True,
            libvirt_net_name="astinuslab-pg-dirty",
            state="failed",
        )
    )
    session.flush()

    pr.mark_session_failed(session, playground_session.id, "broker unavailable")

    assert playground_session.status == "failure_cleanup"
    assert playground_session.finished_at is None
    active = pr.active_playground_session(session)
    assert active is not None
    assert active.id == playground_session.id


def test_stale_finish_token_cannot_publish_after_same_state_reclaim(session):
    pr.create_playground_session(session, job_id="pg-aba")
    pr.register_vm(
        session,
        vm_id="vm-aba",
        job_id="pg-aba",
        label="web",
        image_id="img",
        domain_name="astinuslab-pg-aba-web",
        disk_path="/tmp/vm-aba.qcow2",
    )
    assert pr.claim_vm_for_spawn(session, "vm-aba") is True
    vm = session.get(JobVM, "vm-aba")
    session.refresh(vm)
    stale_token = vm.updated_at

    vm.state = "teardown_failed"
    vm.updated_at = stale_token + timedelta(seconds=1)
    session.flush()
    assert pr.claim_vm_for_spawn(session, "vm-aba") is True
    session.refresh(vm)
    replacement_token = vm.updated_at

    assert replacement_token != stale_token
    assert (
        pr.finish_vm_spawn(
            session,
            "vm-aba",
            "failed",
            expected_updated_at=stale_token,
            error="stale worker",
        )
        is False
    )
    session.refresh(vm)
    assert vm.state == "starting"
    assert vm.error_summary is None


@pytest.mark.parametrize(
    ("initial_state", "claim_state", "final_state"),
    [
        ("stopped", "powering_on", "booting"),
        ("running", "powering_off", "stopping"),
        ("running", "rebooting", "booting"),
    ],
)
def test_power_action_claims_require_exact_transition_and_timestamp(
    session,
    initial_state,
    claim_state,
    final_state,
):
    pr.create_playground_session(session, job_id="pg-power")
    pr.register_vm(
        session,
        vm_id="vm-power",
        job_id="pg-power",
        label="web",
        image_id="img",
        domain_name="d",
        disk_path="/p",
    )
    pr.update_vm_state(session, "vm-power", initial_state)

    claim_token = pr.claim_vm_for_power_action(
        session,
        "vm-power",
        expected_state=initial_state,
        claim_state=claim_state,
    )

    assert claim_token is not None
    vm = session.get(JobVM, "vm-power")
    session.refresh(vm)
    assert vm.state == claim_state
    assert pr.queue_vm_for_destroy(session, "vm-power") is False
    assert (
        pr.finish_vm_power_action(
            session,
            "vm-power",
            claim_state=claim_state,
            expected_updated_at=claim_token + timedelta(microseconds=1),
            state=final_state,
        )
        is False
    )
    assert (
        pr.finish_vm_power_action(
            session,
            "vm-power",
            claim_state=claim_state,
            expected_updated_at=claim_token,
            state=final_state,
        )
        is True
    )
    session.refresh(vm)
    assert vm.state == final_state


def test_power_action_claim_rejects_unsupported_transition(session):
    pr.create_playground_session(session, job_id="pg-power-invalid")
    pr.register_vm(
        session,
        vm_id="vm-power-invalid",
        job_id="pg-power-invalid",
        label="web",
        image_id="img",
        domain_name="d",
        disk_path="/p",
    )

    with pytest.raises(ValueError, match="unsupported VM power transition"):
        pr.claim_vm_for_power_action(
            session,
            "vm-power-invalid",
            expected_state="pending",
            claim_state="powering_on",
        )


def test_register_vm_and_list_session_vms(session):
    pr.create_playground_session(session, job_id="pg3")
    vm = pr.register_vm(
        session,
        vm_id="v1",
        job_id="pg3",
        label="t1",
        image_id="img",
        domain_name="d1",
        disk_path="/p",
    )
    assert vm.state == "pending"
    listed = pr.list_session_vms(session, "pg3")
    assert [v.id for v in listed] == ["v1"]


def test_vm_labels_are_unique_per_job(session):
    pr.create_playground_session(session, job_id="pg-labels")
    pr.register_vm(
        session,
        vm_id="v1",
        job_id="pg-labels",
        label="web",
        image_id="img",
        domain_name="d1",
        disk_path="/p1",
    )

    with pytest.raises(IntegrityError):
        pr.register_vm(
            session,
            vm_id="v2",
            job_id="pg-labels",
            label="web",
            image_id="img",
            domain_name="d2",
            disk_path="/p2",
        )


def test_register_network_and_attach_nic(session):
    pr.create_playground_session(session, job_id="pg4")
    net = pr.register_network(
        session,
        network_id="n1",
        job_id="pg4",
        name="ab",
        subnet_cidr="10.0.0.0/24",
        isolated=True,
        libvirt_net_name="astinuslab-pg-pg4-ab",
    )
    assert net.state == "pending"
    vm = pr.register_vm(  # noqa: F841
        session,
        vm_id="v1",
        job_id="pg4",
        label="x",
        image_id="img",
        domain_name="d",
        disk_path="/p",
    )
    nic = pr.attach_nic(session, nic_id="nic1", vm_id="v1", network_id="n1", mac="52:54:00:aa:bb:01")
    assert nic.ip is None
    pr.update_nic_ip(session, "nic1", "10.0.0.5")
    refreshed = session.query(pr.JobVMNic).filter_by(id="nic1").one()
    assert refreshed.ip == "10.0.0.5"


def test_nic_macs_are_globally_unique(session):
    pr.create_playground_session(session, job_id="pg-macs")
    pr.register_network(
        session,
        network_id="n1",
        job_id="pg-macs",
        name="ab",
        subnet_cidr="10.0.0.0/24",
        isolated=True,
        libvirt_net_name="astinuslab-pg-pg-macs-ab",
    )
    pr.register_network(
        session,
        network_id="n2",
        job_id="pg-macs",
        name="cd",
        subnet_cidr="10.1.0.0/24",
        isolated=True,
        libvirt_net_name="astinuslab-pg-pg-macs-cd",
    )
    pr.register_vm(
        session,
        vm_id="v1",
        job_id="pg-macs",
        label="one",
        image_id="img",
        domain_name="d1",
        disk_path="/p1",
    )
    pr.register_vm(
        session,
        vm_id="v2",
        job_id="pg-macs",
        label="two",
        image_id="img",
        domain_name="d2",
        disk_path="/p2",
    )
    pr.attach_nic(session, nic_id="nic1", vm_id="v1", network_id="n1", mac="52:54:00:aa:bb:01")

    with pytest.raises(IntegrityError):
        pr.attach_nic(session, nic_id="nic2", vm_id="v2", network_id="n2", mac="52:54:00:aa:bb:01")


def test_network_names_are_unique_per_job(session):
    pr.create_playground_session(session, job_id="pg4")
    pr.register_network(
        session,
        network_id="n1",
        job_id="pg4",
        name="ab",
        subnet_cidr="10.0.0.0/24",
        isolated=True,
        libvirt_net_name="astinuslab-pg-pg4-ab",
    )

    with pytest.raises(IntegrityError):
        pr.register_network(
            session,
            network_id="n2",
            job_id="pg4",
            name="ab",
            subnet_cidr="10.1.0.0/24",
            isolated=True,
            libvirt_net_name="astinuslab-pg-pg4-ab-2",
        )


def test_register_network_rejects_overlapping_cidr(session):
    pr.create_playground_session(session, job_id="pg-cidr")
    pr.register_network(
        session,
        network_id="n1",
        job_id="pg-cidr",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
        libvirt_net_name="astinuslab-pg-pg-cidr-ab",
    )

    with pytest.raises(RuntimeError, match="overlaps existing network"):
        pr.register_network(
            session,
            network_id="n2",
            job_id="pg-cidr",
            name="cd",
            subnet_cidr="10.42.1.128/25",
            isolated=True,
            libvirt_net_name="astinuslab-pg-pg-cidr-cd",
        )


def test_register_network_uses_sqlite_immediate_lock_before_cidr_check(session):
    pr.create_playground_session(session, job_id="pg-lock")
    session.commit()
    statements: list[str] = []
    original_execute = session.execute

    def tracking_execute(statement, *args, **kwargs):
        statements.append(str(statement).strip().upper())
        return original_execute(statement, *args, **kwargs)

    session.execute = tracking_execute

    pr.register_network(
        session,
        network_id="n1",
        job_id="pg-lock",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
        libvirt_net_name="astinuslab-pg-pg-lock-ab",
    )

    assert "BEGIN IMMEDIATE" in statements


def test_register_network_skips_begin_immediate_when_write_lock_already_held(session):
    # A flushed write earlier in the caller's transaction already holds
    # SQLite's write lock; issuing BEGIN again would be an error.
    pr.create_playground_session(session, job_id="pg-lock-held")
    statements: list[str] = []
    original_execute = session.execute

    def tracking_execute(statement, *args, **kwargs):
        statements.append(str(statement).strip().upper())
        return original_execute(statement, *args, **kwargs)

    session.execute = tracking_execute

    pr.register_network(
        session,
        network_id="n1",
        job_id="pg-lock-held",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
        libvirt_net_name="astinuslab-pg-pg-lock-held-ab",
    )

    assert "BEGIN IMMEDIATE" not in statements


def test_repository_calls_do_not_commit_callers_transaction(session):
    # Transaction ownership lives with the caller: a rollback after a
    # multi-step register must leave no partial session/VM/network/NIC rows.
    pr.create_playground_session(session, job_id="pg-uow")
    pr.register_network(
        session,
        network_id="n1",
        job_id="pg-uow",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
        libvirt_net_name="astinuslab-pg-pg-uow-ab",
    )
    pr.register_vm(
        session,
        vm_id="v1",
        job_id="pg-uow",
        label="web",
        image_id="img",
        domain_name="d1",
        disk_path="/p1",
    )
    pr.attach_nic(session, nic_id="nic1", vm_id="v1", network_id="n1", mac="52:54:00:aa:bb:01")

    session.rollback()

    assert session.query(PlaygroundSession).filter_by(id="pg-uow").one_or_none() is None
    assert session.query(JobVM).count() == 0
    assert session.query(JobNetwork).count() == 0
    assert session.query(JobVMNic).count() == 0


def test_active_playground_session_returns_running_only(session):
    pr.create_playground_session(session, job_id="pg5")
    active = pr.active_playground_session(session)
    assert active is not None
    assert active.id == "pg5"
    pr.mark_session_completed(session, "pg5")
    assert pr.active_playground_session(session) is None


def test_destroy_failed_session_remains_active_until_resource_cleanup_finishes(session):
    pr.create_playground_session(session, job_id="pg-dirty")
    pr.mark_session_destroy_failed(session, "pg-dirty", "cleanup pending")

    active = pr.active_playground_session(session)

    assert active is not None
    assert active.id == "pg-dirty"


def test_delayed_workload_claim_is_rejected_and_queued_after_parent_destroy(session):
    pr.create_playground_session(session, job_id="pg-ending")
    pr.register_vm(
        session,
        vm_id="vm-1",
        job_id="pg-ending",
        label="web",
        image_id="img",
        domain_name="astinuslab-pg-pg-ending-web",
        disk_path="/tmp/web.qcow2",
        workload_id="dvwa",
    )
    pr.update_vm_state(session, "vm-1", "provisioning_workload")
    pr.mark_session_destroy_requested(session, "pg-ending")

    assert pr.claim_vm_for_workload(session, "vm-1") is False
    assert pr.queue_rejected_workload_for_destroy(session, "vm-1") is True
    assert session.get(JobVM, "vm-1").state == "destroy_queued"


def test_next_free_cidr_empty_returns_first_slot():
    assert pr.next_free_cidr([]) == "10.42.1.0/24"


def test_next_free_cidr_skips_taken_slots():
    assert pr.next_free_cidr(["10.42.1.0/24", "10.42.2.0/24"]) == "10.42.3.0/24"


def test_next_free_cidr_fills_lowest_gap():
    assert pr.next_free_cidr(["10.42.1.0/24", "10.42.3.0/24"]) == "10.42.2.0/24"


def test_next_free_cidr_detects_partial_overlap():
    # A /25 inside 10.42.2.0/24 must still mark that /24 as taken.
    assert pr.next_free_cidr(["10.42.1.0/24", "10.42.2.128/25"]) == "10.42.3.0/24"


def test_next_free_cidr_raises_when_pool_exhausted():
    full = [f"10.42.{octet}.0/24" for octet in range(1, 255)]
    with pytest.raises(RuntimeError, match="exhausted"):
        pr.next_free_cidr(full)


# test_save_topology_layout_replaces_saved_node_set removed: save_topology_layout
# was dropped in F-13 (PUT topology route removal).


def test_resolve_cidr_keeps_non_overlapping_request(session):
    pr.create_playground_session(session, job_id="pg-resolve-a")
    pr.register_network(
        session,
        network_id="n1",
        job_id="pg-resolve-a",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
        libvirt_net_name="astinuslab-pg-pg-resolve-a-ab",
    )
    final, substituted_from = pr.resolve_cidr(session, "pg-resolve-a", "10.42.2.0/24")
    assert final == "10.42.2.0/24"
    assert substituted_from is None


def test_resolve_cidr_substitutes_overlapping_request(session):
    pr.create_playground_session(session, job_id="pg-resolve-b")
    pr.register_network(
        session,
        network_id="n1",
        job_id="pg-resolve-b",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
        libvirt_net_name="astinuslab-pg-pg-resolve-b-ab",
    )
    final, substituted_from = pr.resolve_cidr(session, "pg-resolve-b", "10.42.1.128/25")
    assert final == "10.42.2.0/24"
    assert substituted_from == "10.42.1.128/25"


def test_resolve_cidr_ignores_a_destroyed_network(session):
    """A released subnet is handed back, not quietly swapped for another.

    resolve_cidr substitutes the next free range when a request overlaps, so
    counting destroyed rows did not raise - it silently moved the caller onto a
    different subnet than the one they asked for.
    """
    pr.create_playground_session(session, job_id="pg-resolve-c")
    pr.register_network(
        session,
        network_id="n1",
        job_id="pg-resolve-c",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
        libvirt_net_name="astinuslab-pg-pg-resolve-c-ab",
    )
    pr.update_network_state(session, "n1", "destroyed")

    final, substituted_from = pr.resolve_cidr(session, "pg-resolve-c", "10.42.1.0/24")

    assert final == "10.42.1.0/24"
    assert substituted_from is None


def test_register_network_reuses_the_cidr_of_a_destroyed_network(session):
    """A destroyed network no longer holds its subnet.

    Its row is kept for the audit trail, so an overlap check that ignores state
    made that subnet unusable for the rest of the session. create_network
    already skips destroyed rows before it gets here, so the two checks
    disagreed: the caller allowed the request and this one refused it with
    "CIDR ... overlaps existing network" while nothing was using the range.
    """
    pr.create_playground_session(session, job_id="pg-reuse")
    pr.register_network(
        session,
        network_id="n1",
        job_id="pg-reuse",
        name="ab",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
        libvirt_net_name="astinuslab-pg-pg-reuse-ab",
    )
    pr.update_network_state(session, "n1", "destroyed")

    reused = pr.register_network(
        session,
        network_id="n2",
        job_id="pg-reuse",
        name="cd",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
        libvirt_net_name="astinuslab-pg-pg-reuse-cd",
    )

    assert reused.subnet_cidr == "10.42.1.0/24"
    assert reused.state == "pending"


def test_register_network_reuses_the_name_of_a_destroyed_network(session):
    """Teardown releases the name as well as the subnet.

    create_network has always compared a requested name against non-destroyed
    rows, but the unique index counted every row, so recreating a session's
    "default" network died on an IntegrityError the caller never anticipated.
    a024 narrows the index to match; a live namesake is still rejected.
    """
    pr.create_playground_session(session, job_id="pg-rename")
    pr.register_network(
        session,
        network_id="n1",
        job_id="pg-rename",
        name="default",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
        libvirt_net_name="astinuslab-pg-pg-rename-default",
    )
    pr.update_network_state(session, "n1", "destroyed")

    pr.register_network(
        session,
        network_id="n2",
        job_id="pg-rename",
        name="default",
        subnet_cidr="10.42.1.0/24",
        isolated=True,
        libvirt_net_name="astinuslab-pg-pg-rename-default-2",
    )
    session.flush()

    with pytest.raises(IntegrityError):
        pr.register_network(
            session,
            network_id="n3",
            job_id="pg-rename",
            name="default",
            subnet_cidr="10.42.9.0/24",
            isolated=True,
            libvirt_net_name="astinuslab-pg-pg-rename-default-3",
        )
        session.flush()
