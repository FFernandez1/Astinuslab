"""Durable claim/owner fixtures; no task broker, VM, or subprocess control."""

from astinuslab.api.schemas import JobCreate
from astinuslab.db.repositories import JobRepository
from astinuslab.jobs.action_lifecycle import claim_action, owner_alive, process_owner, reconcile_interrupted_actions


def test_claim_is_single_use_and_persists_owner(in_memory_session_scope):
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(JobCreate(target_id="fixture", scenario_id="fixture", execution_mode="immediate"))
        action = repo.enqueue_action(job.id, "run_vol3_plugin")
        assert claim_action(session, job.id, action.id, owner="fixture-owner", task_id="delivery-1")
        assert not claim_action(session, job.id, action.id, owner="duplicate-owner")
        session.refresh(action)
        assert action.owner_token == "fixture-owner"
        assert action.celery_task_id == "delivery-1"
        assert action.lease_expires_at is not None


def test_current_process_identity_is_live():
    assert owner_alive(process_owner())
    assert not owner_alive("old-boot:999999999:0")


def test_completed_parent_independent_dead_owner_recovery(in_memory_session_scope):
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(JobCreate(target_id="fixture", scenario_id="fixture", execution_mode="immediate"))
        repo.update_job(job.id, status="completed")
        dead = repo.enqueue_action(job.id, "run_vol3_plugin")
        live = repo.enqueue_action(job.id, "run_vol3_plugin")
        claim_action(session, job.id, dead.id, owner="old-boot:999999999:0")
        claim_action(session, job.id, live.id, owner=process_owner())
        job_id, dead_id, live_id = job.id, dead.id, live.id
    assert reconcile_interrupted_actions(in_memory_session_scope) == 1
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        assert _required(repo.get_job(job_id)).status == "completed"
        assert _required(repo.get_action(dead_id)).status == "failed"
        assert _required(repo.get_action(live_id)).status == "running"


def _required[T](value: T | None) -> T:
    assert value is not None
    return value
