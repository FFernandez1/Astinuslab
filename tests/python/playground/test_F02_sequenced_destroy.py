import pytest

from astinuslab.jobs.celery_worker import celery_app
from astinuslab.playground import cleanup as playground_cleanup
from astinuslab.playground import orchestrator as po
from astinuslab.playground import repositories as pr


@pytest.fixture
def db(monkeypatch, in_memory_session_scope):
    monkeypatch.setattr(po, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(playground_cleanup, "session_scope", in_memory_session_scope)
    return in_memory_session_scope


def test_F02_vm_destroyed_before_network(db, monkeypatch):
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    order: list[str] = []

    def fake_destroy_vm(*, vm_id):
        order.append(f"vm:{vm_id}")
        return vm_id

    def fake_destroy_network(*, network_id):
        order.append(f"net:{network_id}")
        return network_id

    monkeypatch.setattr(po.destroy_vm, "run", fake_destroy_vm)
    monkeypatch.setattr(po.destroy_network, "run", fake_destroy_network)

    with db() as s:
        pr.create_playground_session(s, job_id="pg-3")
        pr.register_network(
            s,
            network_id="net-A",
            job_id="pg-3",
            name="lan",
            subnet_cidr="10.99.0.0/24",
            isolated=True,
            libvirt_net_name="astinuslab-pg-pg-3-lan-abcd",
        )
        pr.update_network_state(s, "net-A", "active")
        pr.register_vm(
            s, vm_id="vm-A", job_id="pg-3", label="a", image_id="img", domain_name="d", disk_path="/tmp/a.qcow2"
        )
        pr.update_vm_state(s, "vm-A", "running")

    playground_cleanup.purge_session_resources(job_id="pg-3", final_status="cancelled")
    # VM destroy must come before network destroy when running eagerly through the chain.
    vm_index = next(i for i, e in enumerate(order) if e.startswith("vm:"))
    net_index = next(i for i, e in enumerate(order) if e.startswith("net:"))
    assert vm_index < net_index
