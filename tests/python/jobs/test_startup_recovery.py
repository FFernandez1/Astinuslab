"""Tests for startup recovery: orphaned job reconciliation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from astinuslab.api.schemas import JobCreate
from astinuslab.db.repositories import JobRepository


def _payload(**kwargs) -> JobCreate:
    defaults: dict[str, Any] = dict(
        target_id="ubuntu-22-04-default",
        scenario_id="immediate-capture",
        execution_mode="immediate",
    )
    defaults.update(kwargs)
    return JobCreate(**defaults)


def _make_job(repo: JobRepository, *, status: str, started: bool = False) -> str:
    """Create a job and optionally set it to started+non-queued status."""
    job = repo.create_job(_payload())
    fields: dict = {"status": status, "current_stage": status}
    if started:
        fields["started_at"] = datetime.now(UTC)
    repo.update_job(job.id, **fields)
    return job.id


class TestListOrphanedJobs:
    def test_returns_empty_when_no_jobs(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            assert repo.list_orphaned_jobs() == []

    def test_ignores_terminal_completed(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            _make_job(repo, status="completed", started=True)
            assert repo.list_orphaned_jobs() == []

    def test_ignores_terminal_failed(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            _make_job(repo, status="failed", started=True)
            assert repo.list_orphaned_jobs() == []

    def test_ignores_terminal_cancelled(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            _make_job(repo, status="cancelled", started=True)
            assert repo.list_orphaned_jobs() == []

    def test_ignores_queued_not_yet_started(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            # create_job leaves started_at=None and status="queued"
            repo.create_job(_payload())
            assert repo.list_orphaned_jobs() == []

    def test_returns_started_non_terminal_job(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job_id = _make_job(repo, status="preparing_image", started=True)
            orphans = repo.list_orphaned_jobs()
            assert len(orphans) == 1
            assert orphans[0].id == job_id

    def test_returns_interrupted_non_terminal_statuses(self, in_memory_session_scope):
        non_terminal = [
            "preparing_image",
            "waiting_for_guest_access",
            "mounting_output_share",
            "provisioning_workload",
            "cancel_requested",
            "capture_requested",
        ]
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            for status in non_terminal:
                _make_job(repo, status=status, started=True)
            orphans = repo.list_orphaned_jobs()
            assert len(orphans) == len(non_terminal)


import logging
from unittest.mock import MagicMock


class TestReconcileOrphanedJobs:
    """Tests for the startup_recovery.reconcile_orphaned_jobs() function."""

    def _make_settings(self, tmp_path):
        s = MagicMock()
        s.libvirt_uri = "qemu:///system"
        s.base_dir = tmp_path
        s.data_dir = tmp_path
        return s

    def test_returns_zero_when_no_orphans(self, in_memory_session_scope, tmp_path):
        from astinuslab.jobs.startup_recovery import reconcile_orphaned_jobs

        teardown = MagicMock()
        mark_destroyed = MagicMock()

        count = reconcile_orphaned_jobs(
            in_memory_session_scope,
            JobRepository,
            teardown_fn=teardown,
            mark_runtime_destroyed_fn=mark_destroyed,
            logger=logging.getLogger("test"),
            settings=self._make_settings(tmp_path),
        )

        assert count == 0
        teardown.assert_not_called()
        mark_destroyed.assert_not_called()

    def test_queued_not_started_job_is_not_touched(self, in_memory_session_scope, tmp_path):
        from astinuslab.jobs.startup_recovery import reconcile_orphaned_jobs

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.create_job(_payload())  # queued, started_at=None

        teardown = MagicMock()
        mark_destroyed = MagicMock()

        count = reconcile_orphaned_jobs(
            in_memory_session_scope,
            JobRepository,
            teardown_fn=teardown,
            mark_runtime_destroyed_fn=mark_destroyed,
            logger=logging.getLogger("test"),
            settings=self._make_settings(tmp_path),
        )

        assert count == 0
        teardown.assert_not_called()

    def test_orphaned_job_is_marked_failed(self, in_memory_session_scope, tmp_path):
        from astinuslab.jobs.startup_recovery import reconcile_orphaned_jobs

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job_id = _make_job(repo, status="preparing_image", started=True)

        reconcile_orphaned_jobs(
            in_memory_session_scope,
            JobRepository,
            teardown_fn=MagicMock(),
            mark_runtime_destroyed_fn=MagicMock(),
            logger=logging.getLogger("test"),
            settings=self._make_settings(tmp_path),
        )

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job = repo.get_job(job_id)
            assert job is not None
            assert job.status == "failed"
            assert job.current_stage == "failed"
            assert job.finished_at is not None
            assert job.error_summary is not None
            assert "worker restarted" in job.error_summary.lower()

    def test_orphaned_job_gets_failed_event(self, in_memory_session_scope, tmp_path):
        from astinuslab.jobs.startup_recovery import reconcile_orphaned_jobs

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job_id = _make_job(repo, status="provisioning_workload", started=True)

        reconcile_orphaned_jobs(
            in_memory_session_scope,
            JobRepository,
            teardown_fn=MagicMock(),
            mark_runtime_destroyed_fn=MagicMock(),
            logger=logging.getLogger("test"),
            settings=self._make_settings(tmp_path),
        )

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            events = repo.list_events(job_id)
            failed_events = [e for e in events if e.state == "failed"]
            assert len(failed_events) == 1
            assert "worker restarted" in failed_events[0].message.lower()

    def test_teardown_called_for_orphaned_job(self, in_memory_session_scope, tmp_path):
        from astinuslab.jobs.startup_recovery import reconcile_orphaned_jobs

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job_id = _make_job(repo, status="waiting_for_guest_access", started=True)

        teardown = MagicMock()

        reconcile_orphaned_jobs(
            in_memory_session_scope,
            JobRepository,
            teardown_fn=teardown,
            mark_runtime_destroyed_fn=MagicMock(),
            logger=logging.getLogger("test"),
            settings=self._make_settings(tmp_path),
        )

        teardown.assert_called_once_with(job_id)

    def test_mark_runtime_destroyed_called_for_target(self, in_memory_session_scope, tmp_path):
        from astinuslab.jobs.startup_recovery import reconcile_orphaned_jobs

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job_id = _make_job(repo, status="preparing_image", started=True)

        mark_destroyed = MagicMock()

        reconcile_orphaned_jobs(
            in_memory_session_scope,
            JobRepository,
            teardown_fn=MagicMock(),
            mark_runtime_destroyed_fn=mark_destroyed,
            logger=logging.getLogger("test"),
            settings=self._make_settings(tmp_path),
        )

        mark_destroyed.assert_any_call(job_id, "target")

    def test_does_not_mark_runtime_destroyed_when_teardown_returns_false(self, in_memory_session_scope, tmp_path):
        from astinuslab.jobs.startup_recovery import reconcile_orphaned_jobs

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            _make_job(repo, status="preparing_image", started=True)

        mark_destroyed = MagicMock()

        reconcile_orphaned_jobs(
            in_memory_session_scope,
            JobRepository,
            teardown_fn=MagicMock(return_value=False),
            mark_runtime_destroyed_fn=mark_destroyed,
            logger=logging.getLogger("test"),
            settings=self._make_settings(tmp_path),
        )

        mark_destroyed.assert_not_called()

    def test_mark_runtime_destroyed_called_for_attacker_when_present(self, in_memory_session_scope, tmp_path):
        from astinuslab.jobs.startup_recovery import reconcile_orphaned_jobs

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job_id = _make_job(repo, status="provisioning_workload", started=True)
            # simulate attacker VM present
            repo.update_job(job_id, attacker_vm_name="astinuslab-job-abc-attacker")

        mark_destroyed = MagicMock()

        reconcile_orphaned_jobs(
            in_memory_session_scope,
            JobRepository,
            teardown_fn=MagicMock(),
            mark_runtime_destroyed_fn=mark_destroyed,
            logger=logging.getLogger("test"),
            settings=self._make_settings(tmp_path),
        )

        mark_destroyed.assert_any_call(job_id, "target")
        mark_destroyed.assert_any_call(job_id, "attacker")

    def test_running_host_action_marked_failed(self, in_memory_session_scope, tmp_path):
        from astinuslab.jobs.startup_recovery import reconcile_orphaned_jobs

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job_id = _make_job(repo, status="preparing_image", started=True)
            action = repo.enqueue_action(job_id, "run_immediate_job")
            repo.update_action(action.id, status="running")
            action_id = action.id

        reconcile_orphaned_jobs(
            in_memory_session_scope,
            JobRepository,
            teardown_fn=MagicMock(),
            mark_runtime_destroyed_fn=MagicMock(),
            logger=logging.getLogger("test"),
            settings=self._make_settings(tmp_path),
        )

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            stored_action = repo.get_action(action_id)
            assert stored_action is not None
            assert stored_action.status == "failed"
            assert stored_action.finished_at is not None

    def test_publishing_job_with_completed_action_is_marked_completed(
        self,
        in_memory_session_scope,
        tmp_path,
    ):
        from astinuslab.jobs.startup_recovery import reconcile_orphaned_jobs

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job_id = _make_job(repo, status="publishing_artifact", started=True)
            action = repo.enqueue_action(job_id, "run_immediate_job")
            repo.update_action(
                action.id,
                status="completed",
                finished_at=datetime.now(UTC),
            )
            action_id = action.id

        reconcile_orphaned_jobs(
            in_memory_session_scope,
            JobRepository,
            teardown_fn=MagicMock(),
            mark_runtime_destroyed_fn=MagicMock(),
            logger=logging.getLogger("test"),
            settings=self._make_settings(tmp_path),
        )

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job = repo.get_job(job_id)
            stored_action = repo.get_action(action_id)
            assert job is not None
            assert stored_action is not None
            job_status = job.status
            job_finished_at = job.finished_at
            action_status = stored_action.status
            events = [(event.state, event.message) for event in repo.list_events(job_id)]

        assert job_status == "completed"
        assert job_finished_at is not None
        assert action_status == "completed"
        assert any(state == "completed" and "already completed" in message.lower() for state, message in events)

    def test_queued_host_action_marked_failed(self, in_memory_session_scope, tmp_path):
        from astinuslab.jobs.startup_recovery import reconcile_orphaned_jobs

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job_id = _make_job(repo, status="preparing_image", started=True)
            action = repo.enqueue_action(job_id, "run_immediate_job")
            # leave action as "queued" (default)
            action_id = action.id

        reconcile_orphaned_jobs(
            in_memory_session_scope,
            JobRepository,
            teardown_fn=MagicMock(),
            mark_runtime_destroyed_fn=MagicMock(),
            logger=logging.getLogger("test"),
            settings=self._make_settings(tmp_path),
        )

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            stored_action = repo.get_action(action_id)
            assert stored_action is not None
            assert stored_action.status == "failed"

    def test_teardown_error_is_non_fatal(self, in_memory_session_scope, tmp_path):
        from astinuslab.jobs.startup_recovery import reconcile_orphaned_jobs

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job_id = _make_job(repo, status="preparing_image", started=True)

        def _bad_teardown(jid):
            raise RuntimeError("libvirt exploded")

        # Must not raise
        reconcile_orphaned_jobs(
            in_memory_session_scope,
            JobRepository,
            teardown_fn=_bad_teardown,
            mark_runtime_destroyed_fn=MagicMock(),
            logger=logging.getLogger("test"),
            settings=self._make_settings(tmp_path),
        )

        # Job still marked failed despite teardown error
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job = repo.get_job(job_id)
            assert job is not None
            assert job.status == "failed"

    def test_returns_count_of_recovered_jobs(self, in_memory_session_scope, tmp_path):
        from astinuslab.jobs.startup_recovery import reconcile_orphaned_jobs

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            _make_job(repo, status="preparing_image", started=True)
            _make_job(repo, status="provisioning_workload", started=True)
            _make_job(repo, status="completed", started=True)  # terminal — ignored

        count = reconcile_orphaned_jobs(
            in_memory_session_scope,
            JobRepository,
            teardown_fn=MagicMock(),
            mark_runtime_destroyed_fn=MagicMock(),
            logger=logging.getLogger("test"),
            settings=self._make_settings(tmp_path),
        )

        assert count == 2

    def test_idempotent_second_run_finds_zero_orphans(self, in_memory_session_scope, tmp_path):
        from astinuslab.jobs.startup_recovery import reconcile_orphaned_jobs

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            _make_job(repo, status="preparing_image", started=True)

        kwargs = dict(
            session_scope=in_memory_session_scope,
            repo_cls=JobRepository,
            teardown_fn=MagicMock(),
            mark_runtime_destroyed_fn=MagicMock(),
            logger=logging.getLogger("test"),
            settings=self._make_settings(tmp_path),
        )

        reconcile_orphaned_jobs(**kwargs)
        count2 = reconcile_orphaned_jobs(**kwargs)

        assert count2 == 0


class TestRecoverAttackerVm:
    def test_attacker_vm_is_torn_down(self, in_memory_session_scope):
        """Recovery must destroy the attacker libvirt domain, not just mark DB runtime state."""
        from astinuslab.jobs.startup_recovery import reconcile_orphaned_jobs

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job = repo.create_job(_payload(attacker_vm_id="kali"))
            repo.update_job(
                job.id,
                status="provisioning_workload",
                current_stage="provisioning_workload",
                started_at=datetime.now(UTC),
                attacker_vm_name=f"{job.id}-attacker",
            )
            job_id = job.id

        teardown_calls: list[str] = []
        marked: list[tuple[str, str]] = []

        def teardown_fn(vm_name: str) -> None:
            teardown_calls.append(vm_name)

        def mark_runtime_destroyed_fn(jid: str, role: str) -> None:
            marked.append((jid, role))

        recovered = reconcile_orphaned_jobs(
            in_memory_session_scope,
            JobRepository,
            teardown_fn,
            mark_runtime_destroyed_fn,
            logging.getLogger("test"),
            type("Settings", (), {})(),
        )

        assert recovered == 1
        assert job_id in teardown_calls, "target VM should be torn down"
        assert f"{job_id}-attacker" in teardown_calls, "attacker VM should be torn down"
        assert (job_id, "target") in marked
        assert (job_id, "attacker") in marked

    def test_prefixed_attacker_vm_name_is_normalized_for_teardown(self, in_memory_session_scope):
        from astinuslab.jobs.startup_recovery import reconcile_orphaned_jobs

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job = repo.create_job(_payload(attacker_vm_id="kali"))
            repo.update_job(
                job.id,
                status="provisioning_workload",
                current_stage="provisioning_workload",
                started_at=datetime.now(UTC),
                attacker_vm_name=f"astinuslab-{job.id}-attacker",
            )
            job_id = job.id

        teardown_calls: list[str] = []

        def teardown_fn(vm_name: str) -> None:
            teardown_calls.append(vm_name)

        reconcile_orphaned_jobs(
            in_memory_session_scope,
            JobRepository,
            teardown_fn,
            MagicMock(),
            logging.getLogger("test"),
            type("Settings", (), {})(),
        )

        assert job_id in teardown_calls
        assert f"{job_id}-attacker" in teardown_calls
        assert f"astinuslab-{job_id}-attacker" not in teardown_calls

    def test_no_attacker_means_no_extra_teardown(self, in_memory_session_scope):
        from astinuslab.jobs.startup_recovery import reconcile_orphaned_jobs

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job = repo.create_job(_payload())
            repo.update_job(
                job.id,
                status="provisioning_workload",
                current_stage="provisioning_workload",
                started_at=datetime.now(UTC),
            )
            job_id = job.id

        teardown_calls: list[str] = []

        reconcile_orphaned_jobs(
            in_memory_session_scope,
            JobRepository,
            lambda vm_name: teardown_calls.append(vm_name),
            lambda _jid, _role: None,
            logging.getLogger("test"),
            type("Settings", (), {})(),
        )

        assert teardown_calls == [job_id]


class TestCelerySignalWiring:
    def test_worker_ready_signal_has_handler(self):
        """Verify the worker_ready signal has a receiver registered by worker startup wiring."""
        from celery.signals import worker_ready

        # Import worker-only signals to trigger signal registration.
        import astinuslab.jobs.celery_worker_signals as _  # noqa: F401

        receivers = worker_ready.receivers
        assert len(receivers) > 0, "worker_ready signal has no registered handlers"

    def test_worker_ready_handler_calls_reconcile(self, in_memory_session_scope, monkeypatch, tmp_path):
        """The registered startup handler recovers an orphan in an isolated DB."""
        from astinuslab import db
        from astinuslab.jobs import capacity_runtime, package_instance_coordinator, startup_recovery
        from astinuslab.jobs import celery_worker_signals as signals
        from astinuslab.lab_packages import export_service, prepare_service, verification_service
        from astinuslab.virtualization import host_script_manifest, vm_support

        monkeypatch.setattr(db, "session_scope", in_memory_session_scope)
        # Exercise native recovery through the fully stubbed teardown below.
        monkeypatch.setattr(signals.fake_mode, "FAKE_ORCHESTRATION", False)
        monkeypatch.setattr(signals, "settings", signals.settings.model_copy(update={"storage_dir": tmp_path}))
        monkeypatch.setattr(host_script_manifest, "preflight_problems", lambda **kw: [])
        monkeypatch.setattr(startup_recovery, "recover_playground_sessions", lambda: None)
        monkeypatch.setattr(startup_recovery, "reconcile_publication_attempts", lambda *a: None)
        monkeypatch.setattr(startup_recovery, "reconcile_lab_package_storage", lambda *a: None)
        monkeypatch.setattr(package_instance_coordinator, "reconcile_package_instances", lambda: None)
        monkeypatch.setattr(verification_service, "reconcile_authored_sessions", lambda: None)
        monkeypatch.setattr(verification_service, "reconcile_verifications", lambda: None)
        monkeypatch.setattr(prepare_service, "reconcile_prepares", lambda: None)
        monkeypatch.setattr(export_service, "reconcile_exports", lambda *a: None)
        monkeypatch.setattr(capacity_runtime, "adopt_legacy_reservations", lambda *a: 0)
        monkeypatch.setattr(capacity_runtime, "schedule_capacity", lambda *a: {})
        teardown_calls = []

        def teardown(*args):
            teardown_calls.append(args[4])
            return True

        monkeypatch.setattr(vm_support, "teardown_vm", teardown)
        with in_memory_session_scope() as session:
            job_id = _make_job(JobRepository(session), status="preparing_image", started=True)
        # Connection to worker_ready is verified by the preceding registration
        # test. Invoke this receiver only; unrelated global receivers cannot run.
        signals._on_worker_ready(type("Runner", (), {"hostname": "runner@fixture"})())
        assert teardown_calls == [job_id]
        with in_memory_session_scope() as session:
            job = JobRepository(session).get_job(job_id)
            assert job is not None and job.status == "failed"

    def test_analysis_worker_skips_global_and_libvirt_recovery(self, monkeypatch, in_memory_session_scope):
        import astinuslab.jobs.celery_worker_signals as signals_mod
        import astinuslab.jobs.startup_recovery as recovery_mod
        import astinuslab.virtualization.host_script_manifest as manifest_mod
        from astinuslab import db

        monkeypatch.setattr(db, "session_scope", in_memory_session_scope)
        monkeypatch.setattr(manifest_mod, "preflight_problems", lambda **_kwargs: [])
        monkeypatch.setattr(
            recovery_mod,
            "reconcile_orphaned_jobs",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("analysis ran global recovery")),
        )
        sender = type("AnalysisWorker", (), {"hostname": "analysis@host"})()

        signals_mod._on_worker_ready(sender)


class TestPublicationReconciliation:
    """Task 21 — the worker resolves interrupted publications when it comes back."""

    def test_reconciles_an_interrupted_publication_and_logs_it(self, in_memory_session_scope, tmp_path, caplog):
        import logging

        from astinuslab.db.models import Artifact, ArtifactPublicationAttempt
        from astinuslab.jobs import startup_recovery
        from astinuslab.storage import publication
        from astinuslab.storage.artifacts import build_artifact_manifest

        storage_root = tmp_path / "storage"
        (storage_root / "dumps").mkdir(parents=True)
        final_path = storage_root / "dumps" / "memory.dump"

        # An attempt that published its bytes and then lost its worker.
        handle = publication.attempt(
            in_memory_session_scope,
            producer="job_capture",
            kind="memory",
            final_path=final_path,
            storage_root=storage_root,
            job_id=None,
        )
        attempt = handle.__enter__()
        with attempt.open_payload() as out:
            out.write(b"interrupted capture")
        attempt.stage(
            lambda sha256, size: build_artifact_manifest(
                artifact_path=final_path,
                kind="memory",
                producer="job_capture",
                sha256=sha256,
                size_bytes=size,
            )
        )
        attempt.publish()

        with caplog.at_level(logging.WARNING):
            report = startup_recovery.reconcile_publication_attempts(
                session_scope=in_memory_session_scope,
                storage_root=storage_root,
            )

        assert report.completed == 1
        assert "publication" in caplog.text.lower()

        with in_memory_session_scope() as session:
            assert session.query(Artifact).one().path == str(final_path)
            assert session.query(ArtifactPublicationAttempt).one().state == "completed"
        assert handle is not None  # keep the interrupted context alive for the test

    def test_quiet_when_there_is_nothing_to_reconcile(self, in_memory_session_scope, tmp_path, caplog):
        import logging

        from astinuslab.jobs import startup_recovery

        with caplog.at_level(logging.WARNING):
            report = startup_recovery.reconcile_publication_attempts(
                session_scope=in_memory_session_scope,
                storage_root=tmp_path,
            )

        assert report.summary().startswith("closed=0")
        assert "publication" not in caplog.text.lower()
