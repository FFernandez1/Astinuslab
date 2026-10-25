import pytest

from astinuslab.playground import orchestrator as po
from astinuslab.playground.workloads import (
    build_playground_service_outputs,
    target_context_from_playground_image,
    validate_playground_workload_selection,
)
from tests.python._support.helpers import autospec_setattr


def test_target_context_from_playground_image_preserves_os_metadata():
    image = {
        "id": "ubuntu-22-04-default",
        "distro": "ubuntu",
        "version": "22.04",
        "family": "debian",
        "architecture": "x86_64",
        "disk_gb": 80,
    }

    ctx = target_context_from_playground_image(image)

    assert ctx["id"] == "ubuntu-22-04-default"
    assert ctx["distro"] == "ubuntu"
    assert ctx["version"] == "22.04"
    assert ctx["family"] == "debian"
    assert ctx["arch"] == "x86_64"
    assert ctx["disk_gb"] == 80


def test_build_playground_service_outputs_uses_vm_ip_and_state():
    workload = {
        "id": "dvwa",
        "display_name": "DVWA",
        "services": [
            {"name": "http", "port": 80, "url_path": "/", "credentials": {"user": "admin"}},
            {"name": "mysql", "port": 3306, "url_path": "", "credentials": None},
        ],
    }

    services = build_playground_service_outputs(workload, guest_ip="10.42.2.15", status="ready")

    assert services == [
        {
            "name": "http",
            "port": 80,
            "url_path": "/",
            "url": "http://10.42.2.15:80/",
            "credentials": {"user": "admin"},
            "status": "ready",
        },
        {
            "name": "mysql",
            "port": 3306,
            "url_path": "",
            "url": "http://10.42.2.15:3306",
            "credentials": None,
            "status": "ready",
        },
    ]


def test_validate_playground_workload_selection_rejects_attacker(monkeypatch):
    from astinuslab.playground import workloads as pw

    autospec_setattr(monkeypatch, pw.catalog, "workload_by_id", lambda workload_id: {"id": workload_id})

    result = validate_playground_workload_selection(
        role="attacker",
        workload_id="dvwa",
        image={"id": "kali"},
    )

    assert result.ok is False
    assert result.status_code == 400
    assert "targets only" in result.detail


def test_validate_playground_workload_selection_allows_unknown_compatibility(monkeypatch):
    from astinuslab.playground import workloads as pw

    workload = {"id": "sample", "display_name": "Sample", "compatibility": {}}
    autospec_setattr(monkeypatch, pw.catalog, "workload_by_id", lambda workload_id: workload)
    autospec_setattr(
        monkeypatch,
        pw.catalog,
        "workload_compatibility_for_target",
        lambda workload, target: {"compatibility_status": "unknown", "compatibility_reason": "not declared"},
    )

    result = validate_playground_workload_selection(
        role="target",
        workload_id="sample",
        image={"id": "ubuntu", "distro": "ubuntu", "version": "22.04", "family": "debian"},
    )

    assert result.ok is True
    assert result.workload == workload
    assert result.compatibility_status == "unknown"
    assert result.compatibility_reason == "not declared"


def test_validate_playground_workload_selection_rejects_known_incompatible(monkeypatch):
    from astinuslab.playground import workloads as pw

    workload = {"id": "sample", "display_name": "Sample"}
    autospec_setattr(monkeypatch, pw.catalog, "workload_by_id", lambda workload_id: workload)
    autospec_setattr(
        monkeypatch,
        pw.catalog,
        "workload_compatibility_for_target",
        lambda workload, target: {
            "compatibility_status": "incompatible",
            "compatibility_reason": "Target ubuntu 22.04 is not in the workload compatibility matrix",
        },
    )

    result = validate_playground_workload_selection(
        role="target",
        workload_id="sample",
        image={"id": "ubuntu", "distro": "ubuntu", "version": "22.04", "family": "debian"},
    )

    assert result.ok is False
    assert result.status_code == 400
    assert "incompatible" in result.detail


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ({"username": "ubuntu"}, "ubuntu"),
        ({"username": "kali"}, "kali"),
        ({"username": "", "default_username": "root"}, "root"),
        ({"default_username": "debian"}, "debian"),
        ({}, "ubuntu"),
        ({"username": None, "default_username": None}, "ubuntu"),
    ],
)
def test_playground_guest_user_derives_from_image(image, expected):
    assert po._playground_guest_user(image) == expected


def test_provision_vm_workload_uses_image_user_not_analyst(in_memory_session_scope, monkeypatch):
    from astinuslab.playground import repositories as pr
    from astinuslab.playground import workload_provisioning as wp

    monkeypatch.setattr(po, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(
        po,
        "lookup_playground_image",
        lambda image_id, role="target": {"id": image_id, "username": "ubuntu", "distro": "ubuntu"},
    )
    monkeypatch.setattr(po.catalog, "workload_by_id", lambda wid: {"id": wid, "install_script": "x.sh"})

    captured = {}

    def _spy_provision(*, workload, guest_ip, image, guest_user):
        captured["guest_user"] = guest_user
        captured["guest_ip"] = guest_ip

    monkeypatch.setattr(wp, "_provision_playground_workload", _spy_provision)

    with po.session_scope() as s:
        pr.create_playground_session(s, job_id="pg-1")
        pr.register_network(
            s,
            network_id="net-1",
            job_id="pg-1",
            name="default",
            subnet_cidr="10.42.1.0/24",
            isolated=False,
            libvirt_net_name="astinuslab-pg-pg-1-default",
        )
        pr.update_network_state(s, "net-1", "active")
        pr.register_vm(
            s,
            vm_id="vm-1",
            job_id="pg-1",
            label="tgt",
            image_id="ubuntu-22.04",
            domain_name="astinuslab-pg-pg-1-tgt",
            disk_path="/x.qcow2",
            role="target",
            workload_id="dvwa",
        )
        pr.attach_nic(s, nic_id="nic-1", vm_id="vm-1", network_id="net-1", mac="52:54:00:00:00:01")
        pr.update_nic_ip(s, "nic-1", "10.42.1.10")
        pr.update_vm_state(s, "vm-1", "provisioning_workload")

    po.provision_vm_workload(vm_id="vm-1")

    assert captured["guest_user"] == "ubuntu"
    assert captured["guest_ip"] == "10.42.1.10"


def _workload_nic(nic_id, ip, isolated, name, created_offset=0):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    net = SimpleNamespace(isolated=isolated, name=name)
    return SimpleNamespace(
        id=nic_id,
        ip=ip,
        network=net,
        job_network_id=f"net-{name}",
        created_at=datetime(2026, 1, 1, tzinfo=UTC).replace(second=created_offset),
    )


def test_select_guest_ip_prefers_routed():
    from types import SimpleNamespace

    from astinuslab.playground import workload_provisioning as wp

    vm = SimpleNamespace(
        nics=[
            _workload_nic("n1", "10.0.0.5", isolated=True, name="inside", created_offset=1),
            _workload_nic("n2", "10.0.1.5", isolated=False, name="dmz", created_offset=2),
        ]
    )

    ip, err = wp._select_workload_guest_ip(vm)

    assert err is None
    assert ip == "10.0.1.5"


def test_select_guest_ip_multi_isolated_picks_earliest_attached():
    from types import SimpleNamespace

    from astinuslab.playground import workload_provisioning as wp

    vm = SimpleNamespace(
        nics=[
            _workload_nic("n2", "10.0.1.5", isolated=True, name="b", created_offset=2),
            _workload_nic("n1", "10.0.0.5", isolated=True, name="a", created_offset=1),
        ]
    )

    ip, err = wp._select_workload_guest_ip(vm)

    assert err is None
    assert ip == "10.0.0.5"


def test_select_guest_ip_single_isolated_ok():
    from types import SimpleNamespace

    from astinuslab.playground import workload_provisioning as wp

    vm = SimpleNamespace(nics=[_workload_nic("n1", "10.0.0.5", isolated=True, name="a")])

    ip, err = wp._select_workload_guest_ip(vm)

    assert err is None
    assert ip == "10.0.0.5"


def test_select_guest_ip_no_ip_errors():
    from types import SimpleNamespace

    from astinuslab.playground import workload_provisioning as wp

    vm = SimpleNamespace(nics=[_workload_nic("n1", None, isolated=True, name="a")])

    ip, err = wp._select_workload_guest_ip(vm)

    assert ip is None
    assert err is not None
    assert "requires a guest IP" in err
