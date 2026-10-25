from typing import Any

import pytest
from pydantic import ValidationError

from astinuslab.api.schemas import (
    PlaygroundCaptureRequest,
    PlaygroundCreateNetworkRequest,
    PlaygroundNicSpec,
    PlaygroundRecipeVM,
    PlaygroundSpawnVMRequest,
)


def test_spawn_request_requires_label_and_image():
    req = PlaygroundSpawnVMRequest(label="web-1", image_id="img", nics=[PlaygroundNicSpec(network_id="n1")])
    assert req.label == "web-1"
    assert len(req.nics) == 1
    assert req.nics[0].network_id == "n1"


def test_spawn_request_rejects_empty_label():
    with pytest.raises(ValidationError):
        PlaygroundSpawnVMRequest(label="", image_id="img", nics=[])


def test_create_network_request_validates_cidr():
    req = PlaygroundCreateNetworkRequest(name="ab", subnet_cidr="10.42.1.0/24", isolated=True)
    assert req.subnet_cidr == "10.42.1.0/24"
    with pytest.raises(ValidationError):
        PlaygroundCreateNetworkRequest(name="ab", subnet_cidr="not-a-cidr", isolated=True)


def test_capture_request_kind_must_be_memory_or_disk():
    PlaygroundCaptureRequest(kind="memory")
    PlaygroundCaptureRequest(kind="disk")
    payload: dict[str, Any] = {"kind": "snapshot"}
    with pytest.raises(ValidationError):
        PlaygroundCaptureRequest(**payload)


def test_spawn_request_rejects_empty_nics():
    with pytest.raises(ValidationError):
        PlaygroundSpawnVMRequest(label="web-1", image_id="img", nics=[])


def test_spawn_request_accepts_network_role_with_one_or_more_nics():
    req = PlaygroundSpawnVMRequest(
        label="edge-node",
        image_id="openwrt",
        role="network",
        nics=[PlaygroundNicSpec(network_id="dmz")],
        vcpus=1,
        memory_mb=512,
        disk_gb=4,
    )

    assert req.role == "network"
    assert req.vcpus == 1
    assert req.memory_mb == 512
    assert req.disk_gb == 4


def test_recipe_vm_accepts_network_role():
    vm = PlaygroundRecipeVM(
        label="edge-node",
        role="network",
        image_id="openwrt",
        networks=["dmz", "internal"],
        disk_gb=4,
    )

    assert vm.role == "network"


def test_recipe_vm_accepts_machine_resource_overrides():
    vm = PlaygroundRecipeVM(
        label="web",
        image_id="ubuntu",
        networks=["dmz"],
        vcpus=4,
        memory_mb=8192,
        disk_gb=80,
    )

    assert vm.vcpus == 4
    assert vm.memory_mb == 8192
    assert vm.disk_gb == 80


def test_recipe_vm_rejects_invalid_machine_resource_overrides():
    with pytest.raises(ValidationError):
        PlaygroundRecipeVM(label="web", image_id="ubuntu", networks=["dmz"], vcpus=0)
    with pytest.raises(ValidationError):
        PlaygroundRecipeVM(label="web", image_id="ubuntu", networks=["dmz"], memory_mb=127)


def test_playground_vm_out_action_flag_defaults_are_safe():
    from datetime import UTC, datetime

    from astinuslab.api.schemas import PlaygroundVMOut

    vm = PlaygroundVMOut(
        id="v1",
        job_id="pg-1",
        session_id="pg-1",
        label="l",
        image_id="img",
        state="running",
        domain_name="d",
        role="target",
        created_at=datetime.now(UTC),
        nics=[],
    )
    assert vm.can_capture_memory is False
    assert vm.can_capture_disk is False
    assert vm.can_destroy is False
    assert vm.action_block_reason is None
    # A machine with no catalog function tags reports none rather than null, so
    # the map can iterate without a guard.
    assert vm.function_tags == []


def test_playground_nic_out_exposes_network_status_diagnostics():
    from astinuslab.api.schemas import PlaygroundNicOut

    nic = PlaygroundNicOut(
        id="nic-1",
        job_network_id="net-1",
        network_id="net-1",
        network_name="dmz",
        ordinal=1,
        mac="52:54:00:aa:bb:cc",
        ip=None,
        state="pending",
        diagnostic="no DHCP lease observed",
    )

    assert nic.network_id == "net-1"
    assert nic.network_name == "dmz"
    assert nic.ordinal == 1
    assert nic.state == "pending"
    assert nic.diagnostic == "no DHCP lease observed"


def test_playground_network_out_action_flag_defaults_are_safe():
    from astinuslab.api.schemas import PlaygroundNetworkOut

    net = PlaygroundNetworkOut(
        id="n1",
        job_id="pg-1",
        session_id="pg-1",
        name="n",
        subnet_cidr="10.0.0.0/24",
        isolated=True,
        libvirt_net_name="lv",
        state="active",
    )
    assert net.can_delete is False
    assert net.attached_vm_count == 0
    assert net.action_block_reason is None
