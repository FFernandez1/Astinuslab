from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import timedelta

import pytest

from astinuslab.db.models import JobVM
from astinuslab.playground import repositories as pr
from astinuslab.playground.resource_custody import LostResourceCustody, ResourceClaimLease


@pytest.fixture()
def claimed(in_memory_session_scope):
    with in_memory_session_scope() as session:
        pr.create_playground_session(session, job_id="pg-custody")
        pr.register_vm(
            session,
            vm_id="vm-custody",
            job_id="pg-custody",
            label="Target",
            image_id="fixture",
            domain_name="fixture",
            disk_path="/unused/fixture.qcow2",
        )
        assert pr.claim_vm_for_spawn(session, "vm-custody")
        session.flush()
        token = session.get(JobVM, "vm-custody").updated_at
    return ResourceClaimLease(
        kind="vm",
        resource_id="vm-custody",
        state="starting",
        token=token,
        session_scope_factory=in_memory_session_scope,
        grace_seconds=120,
    )


def test_renewal_and_publication_use_injected_session(claimed, in_memory_session_scope):
    assert claimed._renew()
    with in_memory_session_scope() as session:
        first = session.get(JobVM, "vm-custody").updated_at
    token = claimed.publication_token()
    assert token >= first and claimed._stop.is_set()
    with in_memory_session_scope() as session:
        assert session.get(JobVM, "vm-custody").updated_at == token


@pytest.mark.parametrize("replacement", ["same_state", "destroyed"])
def test_replaced_claim_cannot_renew_or_publish(claimed, in_memory_session_scope, replacement):
    with in_memory_session_scope() as session:
        vm = session.get(JobVM, "vm-custody")
        vm.updated_at += timedelta(seconds=1)
        vm.state = "starting" if replacement == "same_state" else "destroyed"
        replacement_token = vm.updated_at
    assert not claimed._renew()
    with pytest.raises(LostResourceCustody):
        claimed.publication_token()
    with in_memory_session_scope() as session:
        assert session.get(JobVM, "vm-custody").updated_at == replacement_token


def test_database_error_loses_custody(claimed):
    @contextmanager
    def failed_scope():
        raise RuntimeError("database unavailable")
        yield  # pragma: no cover - a failing context-manager factory

    claimed._session_scope = failed_scope
    with pytest.raises(LostResourceCustody, match="could not prove"):
        claimed.assert_owned()
    assert claimed._lost.is_set()


def test_custody_module_has_no_task_registration_dependency(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import astinuslab.playground.resource_custody; "
            "assert 'astinuslab.playground.orchestrator' not in sys.modules; "
            "assert 'astinuslab.jobs.celery_app' not in sys.modules",
        ],
        env={
            **os.environ,
            "ASTINUSLAB_FAKE_LIBVIRT": "1",
            "ASTINUSLAB_DB_PATH": str(tmp_path / "empty.db"),
            "ASTINUSLAB_STORAGE_DIR": str(tmp_path / "storage"),
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
