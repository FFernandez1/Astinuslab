"""Recipe import admits the whole recipe or none of it.

These tests run against a real SQLite file so the reservation uses the
transaction boundary it will use in production. Only the task boundary is
stubbed: every `delay` call is recorded instead of reaching a broker, which is
also how a test can tell whether a rejected import published anything.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from astinuslab import db as db_mod
from astinuslab.db.models import JobNetwork, JobVM, JobVMNic, PlaygroundSession, PlaygroundTopologyLayout
from astinuslab.main import app
from astinuslab.playground import orchestrator as po
from astinuslab.playground import repositories as pr
from tests.python._support.helpers import autospec_setattr, configure_csrf_client, isolated_playground_database


class Dispatches:
    """Every task the import published, in publication order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    @property
    def names(self) -> list[str]:
        return [name for name, _kwargs in self.calls]

    def recorder(self, name: str):
        def _delay(**kwargs):
            self.calls.append((name, kwargs))
            return MagicMock(id=f"task-{len(self.calls)}")

        return _delay


@pytest.fixture()
def dispatches(monkeypatch, tmp_path):
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as orchestrator_mod

    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    autospec_setattr(monkeypatch, orchestrator_mod, "_vm_lifecycle", lambda: MagicMock())
    monkeypatch.setattr(
        "astinuslab.playground.routes.vms._validate_spawn_image",
        lambda payload: {"id": payload.image_id, "disk_gb": 10},
    )
    monkeypatch.setattr(
        "astinuslab.playground.routes.vms._validate_spawn_workload",
        lambda payload, image=None: None,
    )
    recorded = Dispatches()
    for task_name in ("create_network", "spawn_vm", "destroy_session", "destroy_network", "destroy_vm"):
        task = getattr(po, task_name, None)
        if task is not None:
            monkeypatch.setattr(task, "delay", recorded.recorder(task_name))
    with isolated_playground_database(monkeypatch, tmp_path, globals()):
        yield recorded


@pytest.fixture()
def client(dispatches):
    return configure_csrf_client(TestClient(app))


def _recipe(networks, vms, topology=()):
    return {
        "schema_version": 1,
        "name": "imported",
        "networks": [{"name": n, "subnet_cidr": cidr, "isolated": True} for n, cidr in networks],
        "vms": [
            {"label": label, "role": "target", "image_id": "ubuntu-22.04", "networks": list(nets)}
            for label, nets in vms
        ],
        "topology": list(topology),
    }


def _seed_session(job_id="pg-existing", *, networks=(), vms=()):
    """Seed a session directly so the fixture records only import dispatches."""
    with db_mod.session_scope() as s:
        pr.create_playground_session(s, job_id=job_id)
        for index, (name, cidr) in enumerate(networks):
            pr.register_network(
                s,
                network_id=f"net-seed{index}",
                job_id=job_id,
                name=name,
                subnet_cidr=cidr,
                isolated=True,
                libvirt_net_name=po.libvirt_net_name(job_id, name),
            )
        for index, label in enumerate(vms):
            pr.register_vm(
                s,
                vm_id=f"vm-seed{index}",
                job_id=job_id,
                label=label,
                image_id="ubuntu-22.04",
                domain_name=f"astinuslab-pg-{job_id}-{label}",
                disk_path=f"/tmp/{label}.qcow2",
            )
    return job_id


def _snapshot():
    with db_mod.session_scope() as s:
        return {
            "sessions": {(j.id, j.status) for j in s.query(PlaygroundSession).all()},
            "networks": {(n.id, n.name, n.state) for n in s.query(JobNetwork).all()},
            "vms": {(v.id, v.label, v.state) for v in s.query(JobVM).all()},
            "nics": {n.id for n in s.query(JobVMNic).all()},
            "topology": {(t.session_id, t.node_id) for t in s.query(PlaygroundTopologyLayout).all()},
        }


@pytest.mark.parametrize("failure_at", [1, 2, 3, 4])
def test_dispatch_failure_fences_only_import_resources(client, dispatches, monkeypatch, failure_at):
    _seed_session(networks=[("prior", "10.42.1.0/24")], vms=["prior"])
    before = _snapshot()
    delivered = []

    def maybe_deliver(name):
        def send(**kwargs):
            delivered.append((name, kwargs))  # Includes ambiguous broker delivery followed by an error.
            if len(delivered) == failure_at:
                raise RuntimeError("broker lost acknowledgement")
            return MagicMock(id="accepted")

        return send

    monkeypatch.setattr(po.create_network, "delay", maybe_deliver("network"))
    monkeypatch.setattr(po.spawn_vm, "delay", maybe_deliver("vm"))
    response = client.post(
        "/api/v1/playground/recipes/import",
        json=_recipe(
            networks=[("a", "10.42.7.0/24"), ("b", "10.42.8.0/24")],
            vms=[("a", ["a"]), ("b", ["b"])],
            topology=[{"node_type": "vm", "node_id": "a", "x": 1, "y": 2}],
        ),
    )
    assert response.status_code == (500 if failure_at <= 2 else 503)
    after = _snapshot()
    assert after["sessions"] == before["sessions"]
    assert before["networks"].issubset(after["networks"])
    assert before["vms"].issubset(after["vms"])
    assert after["topology"] == before["topology"]
    assert all(state == "teardown_failed" for _id, _name, state in after["networks"] - before["networks"])
    assert all(state == "teardown_failed" for _id, _name, state in after["vms"] - before["vms"])
    for kind, kwargs in delivered:
        task = po.create_network if kind == "network" else po.spawn_vm
        with pytest.raises(RuntimeError, match="reservation"):
            task.run(**kwargs)
    assert _snapshot() == after


def test_new_then_existing_network_conflict_rolls_back_the_whole_import(client, dispatches):
    _seed_session(networks=[("lab", "10.42.1.0/24")])
    before = _snapshot()

    res = client.post(
        "/api/v1/playground/recipes/import",
        json=_recipe(
            networks=[("fresh", "10.42.7.0/24"), ("lab", "10.42.8.0/24")],
            vms=[("web", ["fresh"])],
            topology=[{"node_id": "fresh", "node_type": "network", "x": 1.0, "y": 2.0}],
        ),
    )

    assert res.status_code == 409
    assert "lab" in res.json()["detail"]
    # The first network of the recipe was reserved before the conflict was
    # found; rollback has to take it back out along with the VM and its NIC.
    assert _snapshot() == before
    assert dispatches.calls == []


def test_compensation_revokes_claimed_workers_and_reconciles_without_broker(client, dispatches, monkeypatch, tmp_path):
    from datetime import datetime
    from types import SimpleNamespace

    from astinuslab.api.schemas import PlaygroundRecipe
    from astinuslab.playground.recipe_import import compensate_plan, reserve_recipe
    from astinuslab.playground.reconciliation import reconcile_libvirt_resources

    class MissingHostResource(Exception):
        def get_error_code(self):
            return 42

    def host_resource(name):
        resource = MagicMock()
        resource.name.return_value = name
        resource.isActive.return_value = True
        resource.XMLDesc.return_value = "<domain/>"
        return resource

    def lookup(resource, name):
        if resource.name() == name:
            return resource
        raise MissingHostResource(name)

    disk_root = tmp_path / "playground"
    disk_root.mkdir()
    monkeypatch.setattr(po, "settings", po.settings.model_copy(update={"playground_disk_dir": disk_root}))
    job_id = _seed_session(networks=[("prior", "10.42.1.0/24")], vms=["prior"])
    with db_mod.session_scope() as session:
        prior_vm = session.query(JobVM).filter_by(label="prior").one()
        prior_vm.state = "running"
        prior_network = session.query(JobNetwork).filter_by(name="prior").one()
        prior_network.state = "active"
        prior_domain = host_resource(prior_vm.domain_name)
        prior_host_network = host_resource(prior_network.libvirt_net_name)
    before = _snapshot()
    recipe = PlaygroundRecipe.model_validate(_recipe(networks=[("fresh", "10.42.7.0/24")], vms=[("fresh", ["fresh"])]))
    with db_mod.session_scope() as session:
        plan = reserve_recipe(session, recipe, images_by_label={"fresh": {"id": "ubuntu-22.04", "disk_gb": 10}})
    vm_id, network_id = plan.vms[0].vm_id, plan.networks[0].network_id
    # Positive token claims prove normal dispatch remains executable.
    with db_mod.session_scope() as session:
        pr.lock_resource_transitions(session)
        assert pr.claim_vm_for_spawn(
            session, vm_id, expected_updated_at=datetime.fromisoformat(plan.vms[0].reservation_token)
        )
        assert pr.claim_network_for_create(
            session, network_id, expected_updated_at=datetime.fromisoformat(plan.networks[0].reservation_token)
        )
        vm_token = session.query(JobVM).filter_by(id=vm_id).one().updated_at
        network_token = session.query(JobNetwork).filter_by(id=network_id).one().updated_at

    def offline(**kwargs):
        raise RuntimeError("broker offline")

    monkeypatch.setattr(po.destroy_vm, "delay", offline)
    monkeypatch.setattr(po.destroy_network, "delay", offline)
    compensate_plan(plan, "ambiguous delivery")
    with db_mod.session_scope() as session:
        assert not pr.finish_vm_spawn(session, vm_id, "running", expected_updated_at=vm_token)
        assert not pr.finish_network_create(session, network_id, "active", expected_updated_at=network_token)
        assert session.query(JobVMNic).filter_by(job_vm_id=vm_id).count() == 1
    # No host object ever appeared, yet cleanup must settle under an active parent.
    connection = MagicMock()
    connection.listAllDomains.return_value = [prior_domain]
    connection.listAllNetworks.return_value = [prior_host_network]
    connection.lookupByName.side_effect = lambda name: lookup(prior_domain, name)
    connection.networkLookupByName.side_effect = lambda name: lookup(prior_host_network, name)
    libvirt = SimpleNamespace(
        libvirtError=MissingHostResource,
        VIR_ERR_NO_DOMAIN=42,
        VIR_ERR_NO_NETWORK=42,
        open=lambda _uri: connection,
    )
    reconcile_libvirt_resources(
        session_scope=db_mod.session_scope,
        libvirt_module=libvirt,
        libvirt_uri="qemu:///test",
        grace_seconds=0,
    )
    after = _snapshot()
    assert after["sessions"] == before["sessions"]
    assert (network_id, "fresh", "destroyed") in after["networks"]
    assert (vm_id, "fresh", "destroyed") in after["vms"]
    assert before["networks"].issubset(after["networks"])
    assert before["vms"].issubset(after["vms"])
    assert after["nics"] == before["nics"]
    prior_domain.destroy.assert_not_called()
    prior_domain.undefine.assert_not_called()
    prior_host_network.destroy.assert_not_called()
    prior_host_network.undefine.assert_not_called()
    with db_mod.session_scope() as session:
        assert session.query(PlaygroundSession).filter_by(id=job_id).one().status == "running"


@pytest.mark.parametrize(
    ("first", "second"),
    [("import", "import"), ("network", "import"), ("import", "network"), ("close", "import"), ("import", "close")],
)
def test_import_serializes_with_competing_authoring(client, dispatches, monkeypatch, first, second):
    import threading
    from concurrent.futures import ThreadPoolExecutor, TimeoutError

    from fastapi import HTTPException

    from astinuslab.api.errors import ApiError
    from astinuslab.api.schemas import PlaygroundCreateNetworkRequest, PlaygroundRecipe
    from astinuslab.playground.routes.networks import create_network_endpoint
    from astinuslab.playground.routes.recipes import import_recipe_endpoint

    job_id = _seed_session()
    recipe = PlaygroundRecipe.model_validate(_recipe(networks=[("fresh", "10.42.7.0/24")], vms=[("fresh", ["fresh"])]))
    ready, release, contender = threading.Event(), threading.Event(), threading.Event()
    local = threading.local()
    original_lock = pr._begin_immediate_for_sqlite

    def controlled_lock(session):
        if local.is_first:
            original_lock(session)
            local.is_first = False
            ready.set()
            assert release.wait(10)
        else:
            contender.set()
            original_lock(session)

    monkeypatch.setattr(pr, "_begin_immediate_for_sqlite", controlled_lock)

    def perform(kind, is_first):
        local.is_first = is_first
        try:
            if kind == "import":
                import_recipe_endpoint(recipe)
            elif kind == "network":
                create_network_endpoint(
                    job_id, PlaygroundCreateNetworkRequest(name="fresh", subnet_cidr="10.42.7.0/24")
                )
            else:
                with db_mod.session_scope() as session:
                    pr.lock_resource_transitions(session)
                    pr.mark_session_destroy_requested(session, job_id)
            return 202
        except (ApiError, HTTPException) as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as workers:
        winner = workers.submit(perform, first, True)
        try:
            assert ready.wait(10)
            loser = workers.submit(perform, second, False)
            assert contender.wait(10)
            with pytest.raises(TimeoutError):
                loser.result(timeout=0.05)
        finally:
            release.set()
        assert winner.result(timeout=10) == 202
        assert loser.result(timeout=10) == (202 if second == "close" else 409)
    imported = first == "import"
    assert dispatches.names.count("spawn_vm") == int(imported)
    assert dispatches.names.count("create_network") == int(first != "close")
    with db_mod.session_scope() as session:
        assert session.query(JobVM).count() == int(imported)
        assert session.query(JobNetwork).count() == int(first != "close")


def test_late_vm_label_conflict_rolls_back_networks_reserved_earlier(client, dispatches):
    _seed_session(networks=[("lab", "10.42.1.0/24")], vms=["web"])
    before = _snapshot()

    res = client.post(
        "/api/v1/playground/recipes/import",
        json=_recipe(
            networks=[("fresh", "10.42.7.0/24")],
            vms=[("api", ["fresh"]), ("web", ["fresh"])],
        ),
    )

    assert res.status_code == 409
    assert "web" in res.json()["detail"]
    assert _snapshot() == before
    assert dispatches.calls == []


def test_reservation_failure_leaves_no_empty_parent_session(client, dispatches, monkeypatch):
    from astinuslab.playground import recipe_import

    def boom(*args, **kwargs):
        raise RuntimeError("topology write failed")

    monkeypatch.setattr(recipe_import.pr, "save_topology_node", boom)

    with pytest.raises(RuntimeError, match="topology write failed"):
        client.post(
            "/api/v1/playground/recipes/import",
            json=_recipe(
                networks=[("fresh", "10.42.7.0/24")],
                vms=[("web", ["fresh"])],
                topology=[{"node_id": "web", "node_type": "vm", "x": 3.0, "y": 4.0}],
            ),
        )

    # Rollback owns the parent too, so no empty session is left for cleanup to
    # find and no destroy task had to be published.
    assert _snapshot() == {"sessions": set(), "networks": set(), "vms": set(), "nics": set(), "topology": set()}
    assert dispatches.calls == []
    assert client.get("/api/v1/playground/sessions/current-or-null").json() is None


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("boundary", ["workload", "capacity"])
def test_import_admission_failure_preserves_all_prior_state(client, dispatches, monkeypatch, existing, boundary):
    from fastapi import HTTPException

    from astinuslab.playground import admission
    from astinuslab.playground.routes import vms

    if existing:
        _seed_session(networks=[("prior", "10.42.1.0/24")], vms=["prior"])
    before = _snapshot()

    def reject(*args, **kwargs):
        raise HTTPException(status_code=409, detail=f"{boundary} unavailable")

    if boundary == "workload":
        monkeypatch.setattr(vms, "_validate_spawn_workload", reject)
    else:
        monkeypatch.setattr(admission, "ensure_vm_capacity", reject)
    response = client.post(
        "/api/v1/playground/recipes/import",
        json=_recipe(networks=[("fresh", "10.42.7.0/24")], vms=[("fresh", ["fresh"])]),
    )
    assert response.status_code == 409
    assert _snapshot() == before
    assert dispatches.calls == []


def test_new_session_dispatch_failure_keeps_durable_cleanup_intent(client, dispatches, monkeypatch):
    def offline(**kwargs):
        raise RuntimeError("broker offline")

    monkeypatch.setattr(po.create_network, "delay", offline)
    monkeypatch.setattr(po.destroy_session, "delay", offline)
    response = client.post(
        "/api/v1/playground/recipes/import",
        json=_recipe(networks=[("fresh", "10.42.7.0/24")], vms=[("fresh", ["fresh"])]),
    )
    assert response.status_code == 500
    with db_mod.session_scope() as session:
        assert session.query(PlaygroundSession).one().status == "destroy_failed"
        assert session.query(JobVM).one().state == "teardown_failed"
        assert session.query(JobNetwork).one().state == "teardown_failed"


def test_successful_import_commits_before_it_dispatches(client, dispatches, monkeypatch):
    """Every row a task can read must already be durable when the task is sent."""
    visible_at_dispatch: list[tuple[str, bool]] = []
    original = dispatches.recorder("create_network")

    def recording_create_network(**kwargs):
        with db_mod.session_scope() as s:
            row = s.query(JobNetwork).filter_by(id=kwargs["network_id"]).one_or_none()
            vm_count = s.query(JobVM).count()
        visible_at_dispatch.append((kwargs["network_id"], row is not None and vm_count == 1))
        return original(**kwargs)

    monkeypatch.setattr(po.create_network, "delay", recording_create_network)

    res = client.post(
        "/api/v1/playground/recipes/import",
        json=_recipe(
            networks=[("fresh", "10.42.7.0/24")],
            vms=[("web", ["fresh"])],
            topology=[{"node_id": "web", "node_type": "vm", "x": 3.0, "y": 4.0}],
        ),
    )

    assert res.status_code == 202
    body = res.json()
    assert [n["name"] for n in body["networks"]] == ["fresh"]
    assert [v["label"] for v in body["vms"]] == ["web"]
    # Networks are published before VMs so the worker keeps its existing
    # network-readiness behaviour.
    assert dispatches.names == ["create_network", "spawn_vm"]
    assert visible_at_dispatch and all(visible for _id, visible in visible_at_dispatch)


def test_import_does_not_call_the_committing_resource_endpoints(client, dispatches, monkeypatch):
    from astinuslab.playground.routes import networks as networks_routes
    from astinuslab.playground.routes import vms as vms_routes

    def fail(*args, **kwargs):
        raise AssertionError("import must not reuse a committing HTTP handler")

    monkeypatch.setattr(networks_routes, "create_network_endpoint", fail)
    monkeypatch.setattr(vms_routes, "spawn_vm_endpoint", fail)

    res = client.post(
        "/api/v1/playground/recipes/import",
        json=_recipe(networks=[("fresh", "10.42.7.0/24")], vms=[("web", ["fresh"])]),
    )

    assert res.status_code == 202
    assert dispatches.names == ["create_network", "spawn_vm"]


def test_import_into_an_existing_session_keeps_its_prior_resources(client, dispatches):
    job_id = _seed_session(networks=[("lab", "10.42.1.0/24")], vms=["web"])

    res = client.post(
        "/api/v1/playground/recipes/import",
        json=_recipe(networks=[("fresh", "10.42.7.0/24")], vms=[("api", ["fresh"])]),
    )

    assert res.status_code == 202
    body = res.json()
    assert body["job_id"] == job_id
    assert {n["name"] for n in body["networks"]} == {"lab", "fresh"}
    assert {v["label"] for v in body["vms"]} == {"web", "api"}
