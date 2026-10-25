from __future__ import annotations

import pytest

from astinuslab.api.schemas import JobCreate
from astinuslab.jobs.job_services import build_lab_definition


def test_build_lab_definition_for_immediate_job():
    scenario = {
        "id": "immediate-capture",
        "mode": "immediate",
        "script_content": None,
    }
    payload = JobCreate(
        target_id="ubuntu-22-04-default",
        scenario_id="immediate-capture",
        execution_mode="immediate",
        capture_type="memory",
    )

    definition = build_lab_definition(payload, scenario)

    assert definition.target_id == "ubuntu-22-04-default"
    assert definition.mode == "immediate"
    assert definition.capture_type == "memory"
    assert definition.post_boot_script is None
    assert definition.workload_id is None


def test_build_lab_definition_for_post_script_job_uses_scenario_script():
    scenario = {
        "id": "post-boot",
        "mode": "post_script",
        "script_content": "#!/bin/bash\necho hi\n",
    }
    payload = JobCreate(
        target_id="ubuntu-22-04-default",
        scenario_id="post-boot",
        execution_mode="post_script",
        capture_type="disk",
    )

    definition = build_lab_definition(payload, scenario)

    assert definition.mode == "post_script"
    assert definition.capture_type == "disk"
    assert definition.post_boot_script == "#!/bin/bash\necho hi\n"


def test_build_lab_definition_preserves_manual_trigger_overrides():
    scenario = {
        "id": "manual-lab",
        "mode": "manual_trigger",
        "script_content": None,
    }
    payload = JobCreate(
        target_id="ubuntu-22-04-default",
        scenario_id="manual-lab",
        execution_mode="manual_trigger",
        capture_type="both",
        workload_id="juice-shop",
        attacker_vm_id="kali-rolling",
        vm_vcpus=4,
        vm_memory_mb=4096,
        vm_disk_gb=40,
        attacker_vm_vcpus=2,
        attacker_vm_memory_mb=2048,
        attacker_vm_disk_gb=20,
    )

    definition = build_lab_definition(payload, scenario)

    assert definition.mode == "manual_trigger"
    assert definition.capture_type == "both"
    assert definition.workload_id == "juice-shop"
    assert definition.attacker_vm_id == "kali-rolling"
    assert definition.vm_vcpus == 4
    assert definition.vm_memory_mb == 4096
    assert definition.vm_disk_gb == 40
    assert definition.attacker_vm_vcpus == 2
    assert definition.attacker_vm_memory_mb == 2048
    assert definition.attacker_vm_disk_gb == 20


def test_build_lab_definition_rejects_mismatched_payload_and_scenario():
    payload = JobCreate(
        target_id="ubuntu-22-04-default",
        scenario_id="manual-lab",
        execution_mode="manual_trigger",
    )
    scenario = {
        "id": "other-scenario",
        "mode": "post_script",
        "script_content": "#!/bin/bash\necho hi\n",
    }

    with pytest.raises(ValueError, match="same lab definition"):
        build_lab_definition(payload, scenario)
