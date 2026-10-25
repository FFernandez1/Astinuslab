from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from astinuslab.db.models import LabVerificationAttempt
from astinuslab.lab_packages.models import (
    HostResources,
    LabDefinition,
    LabExperience,
    LabNetwork,
    LabVM,
    ResourceRecommendations,
)
from astinuslab.lab_packages.verification_service import save_draft, start_verification, verify_clean_run_task
from astinuslab.playground import repositories


def _definition() -> LabDefinition:
    return LabDefinition(
        package_id="task-wiring-lab",
        version="1.0.0",
        title="Task wiring lab",
        creator="Creator",
        license="MIT",
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
                image_id="ubuntu",
                vcpus=1,
                memory_mb=512,
                disk_gb=1,
                network_ids=("isolated",),
            ),
        ),
        experience=LabExperience(),
    )


def test_task_constructs_and_passes_production_evaluators(in_memory_session_scope, monkeypatch):
    with in_memory_session_scope() as session:
        authored = repositories.create_playground_session(session, job_id="pg-task-wiring")
        authored_id = authored.id
        repositories.register_network(
            session,
            network_id="net-task-wiring",
            job_id=authored_id,
            name="isolated",
            subnet_cidr="192.0.2.0/29",
            isolated=True,
            libvirt_net_name="astinuslab-pg-task-wiring",
        )
        repositories.update_network_state(session, "net-task-wiring", "active")
        vm = repositories.register_vm(
            session,
            vm_id="vm-task-wiring",
            job_id=authored_id,
            label="target",
            image_id="ubuntu",
            domain_name="domain-task-wiring",
            disk_path="/tmp/unused.qcow2",
        )
        repositories.update_vm_state(session, vm.id, "running")
        draft = save_draft(session, _definition(), authored_session_id=authored_id)
        handle = start_verification(session, draft_id=draft.id)

    captured: dict[str, Any] = {}

    def readiness(session, authored_session_id):
        captured["readiness_args"] = (session, authored_session_id)
        return "readiness"

    def checkpoints(session, authored_session_id):
        captured["checkpoint_args"] = (session, authored_session_id)
        return "checkpoints"

    def fake_run(session, attempt_id, **kwargs):
        captured["run_kwargs"] = kwargs
        attempt = session.get(LabVerificationAttempt, attempt_id)
        attempt.state = "verified"
        attempt.progress_percent = 100
        session.commit()
        return SimpleNamespace(attempt_id=attempt_id)

    monkeypatch.setattr("astinuslab.lab_packages.verification_service.session_scope", in_memory_session_scope)
    monkeypatch.setattr("astinuslab.lab_packages.verification_service.build_readiness_checker", readiness)
    monkeypatch.setattr("astinuslab.lab_packages.verification_service.build_checkpoint_evaluator", checkpoints)
    monkeypatch.setattr("astinuslab.lab_packages.verification_service.run_verification", fake_run)
    monkeypatch.setattr("astinuslab.lab_packages.verification_service.release_authored_session", lambda _id: True)

    assert verify_clean_run_task.run(handle.id) == handle.id
    assert captured["readiness_args"][1] == authored_id
    assert captured["checkpoint_args"][1] == authored_id
    assert captured["run_kwargs"]["readiness_checker"] == "readiness"
    assert captured["run_kwargs"]["checkpoint_evaluator"] == "checkpoints"
