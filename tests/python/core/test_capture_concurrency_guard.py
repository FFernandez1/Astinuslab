from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from astinuslab.api.errors import ApiError
from astinuslab.api.schemas import JobCreate
from astinuslab.core.locks import capture_lock
from astinuslab.db import Base, engine, initialize_database, session_scope
from astinuslab.db.models import Acquisition, AcquisitionEvent
from astinuslab.db.repositories import JobRepository
from astinuslab.forensics import services as forensic_services
from astinuslab.jobs.celery_worker import TASK_MAP, celery_app
from astinuslab.playground import acquisition as pa
from astinuslab.playground import orchestrator as po
from astinuslab.playground import repositories as pr


@pytest.fixture
def db():
    initialize_database()
    yield
    with engine.begin() as conn:
        for t in reversed(Base.metadata.sorted_tables):
            conn.execute(t.delete())


def test_capture_concurrency_guard_rejects_second_call(db, monkeypatch):
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    with session_scope() as s:
        pr.create_playground_session(s, job_id="pg-9")
        pr.register_vm(
            s,
            vm_id="vm-9",
            job_id="pg-9",
            label="x",
            image_id="img",
            domain_name="astinuslab-pg-pg-9-x",
            disk_path="/tmp/x.qcow2",
        )
        pr.update_vm_state(s, "vm-9", "running")

    with capture_lock("vm-9", base_dir=pa._playground_capture_lock_dir()):
        assert po.capture_memory(vm_id="vm-9", acquisition_id="a2") == "a2"
    with session_scope() as s:
        acq = s.get(Acquisition, "a2")
        assert acq is not None
        events = s.query(AcquisitionEvent).filter_by(acquisition_id="a2").all()
        assert acq.status == "failed"
        assert any("capture already in progress" in event.message for event in events)


def test_enqueue_acquire_memory_dump_service_real_thread_race(db, monkeypatch):
    """Real-thread race against the read-check guard at forensic_services.py:76-78.

    Pattern mirrors tests/python/test_F06_race_returns_409.py: a threading.Barrier
    releases N=8 ThreadPoolExecutor workers simultaneously, each opening its own
    session via session_scope() and calling enqueue_acquire_memory_dump_service
    against a single manual-trigger job parked in waiting_for_capture.

    Expected outcome: exactly 1 success and N-1 HTTPException(409). The guard is
    a non-atomic read-then-write (list_actions then enqueue_action with a commit
    boundary inside _dispatch_action_after_commit), so multiple winners can slip
    through. Race-window validation: temporarily inserting time.sleep(0.01) right
    after the for-loop guard in forensic_services.py reliably produced 8 successes / 0
    conflicts in this test, confirming the test exercises the real window.

    The shared capture lock serializes the enqueue path so only one request can
    transition the job and queue the acquisition action.
    """
    monkeypatch.setattr(celery_app.conf, "task_always_eager", False)
    monkeypatch.setitem(
        TASK_MAP,
        "acquire_memory_dump",
        SimpleNamespace(delay=lambda *a, **kw: SimpleNamespace(id="noop-task-id")),
    )

    with session_scope() as s:
        repo = JobRepository(s)
        job = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="lab-session",
                execution_mode="manual_trigger",
            )
        )
        repo.force_transition(job.id, "waiting_for_capture")
        job_id = job.id

    n_threads = 8
    barrier = Barrier(n_threads)
    results: list[tuple[str, object]] = []

    def attempt() -> tuple[str, object]:
        barrier.wait()
        try:
            with session_scope() as s:
                forensic_services.enqueue_acquire_memory_dump_service(s, job_id)
            return ("ok", None)
        except (ApiError, HTTPException) as exc:
            return ("http", exc.status_code)
        except Exception as exc:  # noqa: BLE001 - diagnostic capture
            return ("err", repr(exc))

    with ThreadPoolExecutor(max_workers=n_threads) as ex:
        results = list(ex.map(lambda _: attempt(), range(n_threads)))

    successes = [r for r in results if r[0] == "ok"]
    conflicts = [r for r in results if r[0] == "http" and r[1] == 409]
    other = [r for r in results if r not in successes and r not in conflicts]

    assert not other, f"unexpected non-409 errors: {other}"
    assert len(successes) + len(conflicts) == n_threads, results
    assert len(successes) == 1, f"guard let {len(successes)} writers through (expected 1); results={results}"
