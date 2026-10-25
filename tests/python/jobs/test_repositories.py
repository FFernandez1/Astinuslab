"""Unit tests for the JobRepository data-access layer."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from astinuslab.api.schemas import JobCreate, JobOut, JobRuntimeOut
from astinuslab.db.repositories import JobRepository, has_active_non_playground_job, has_active_playground_session
from tests.python._support.helpers import autospec_setattr


@pytest.fixture()
def repo(in_memory_session_scope):
    """Yields a (repo, session) pair inside a transaction for each test."""
    with in_memory_session_scope() as session:
        yield JobRepository(session), session


def _payload(**kwargs) -> JobCreate:
    defaults: dict[str, Any] = dict(
        target_id="ubuntu-22-04-default",
        scenario_id="immediate-capture",
        execution_mode="immediate",
    )
    defaults.update(kwargs)
    return JobCreate(**defaults)


class TestCreateJob:
    def test_creates_job_with_queued_status(self, repo):
        r, _ = repo
        job = r.create_job(_payload())
        assert job.id.startswith("job-")
        assert job.status == "queued"
        assert job.target_id == "ubuntu-22-04-default"

    def test_initial_event_is_queued(self, repo):
        r, _ = repo
        job = r.create_job(_payload())
        events = r.list_events(job.id)
        assert len(events) == 1
        assert events[0].state == "queued"

    def test_ids_are_unique(self, repo):
        r, _ = repo
        j1 = r.create_job(_payload())
        j2 = r.create_job(_payload())
        assert j1.id != j2.id

    def test_creates_default_target_runtime_with_capture_flags(self, repo):
        r, _ = repo
        job = r.create_job(_payload())

        runtimes = r.list_runtimes(job.id)

        assert len(runtimes) == 1
        runtime = runtimes[0]
        assert runtime.role == "target"
        assert runtime.vm_name == f"astinuslab-{job.id}"
        assert runtime.state == "pending"
        assert runtime.power_state == "unknown"
        assert runtime.capture_memory_enabled is True
        assert runtime.capture_disk_enabled is True

    def test_creates_attacker_runtime_when_attacker_vm_requested(self, repo):
        r, _ = repo
        job = r.create_job(_payload(attacker_vm_id="kali"))

        runtimes = {runtime.role: runtime for runtime in r.list_runtimes(job.id)}

        assert set(runtimes) == {"target", "attacker"}
        attacker = runtimes["attacker"]
        assert attacker.vm_name == f"astinuslab-{job.id}-attacker"
        assert attacker.capture_memory_enabled is False
        assert attacker.capture_disk_enabled is False


class TestHasActiveNonPlaygroundJob:
    @pytest.mark.parametrize(
        "status",
        ["queued", "waiting_for_capture", "acquiring_memory", "publishing_artifact", "generating_isf"],
    )
    def test_detects_active_forensic_statuses(self, repo, status):
        r, session = repo
        job = r.create_job(_payload())
        r.update_job(job.id, status=status)

        assert has_active_non_playground_job(session) is True

    @pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
    def test_ignores_terminal_forensic_statuses(self, repo, status):
        r, session = repo
        job = r.create_job(_payload())
        r.update_job(job.id, status=status)

        assert has_active_non_playground_job(session) is False

    def test_ignores_playground_running_session(self, repo):
        from astinuslab.playground import repositories as pr

        _, session = repo
        pr.create_playground_session(session, job_id="pg-active")

        assert has_active_non_playground_job(session) is False


class TestHasActivePlaygroundSession:
    @pytest.mark.parametrize("status", ["queued", "running"])
    def test_detects_active_playground_statuses(self, repo, status):
        from astinuslab.playground import repositories as pr

        _, session = repo
        playground_session = pr.create_playground_session(session, job_id="pg-active")
        playground_session.status = status
        session.flush()

        assert has_active_playground_session(session) is True

    @pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
    def test_ignores_terminal_playground_statuses(self, repo, status):
        from astinuslab.playground import repositories as pr

        _, session = repo
        playground_session = pr.create_playground_session(session, job_id="pg-done")
        playground_session.status = status
        session.flush()

        assert has_active_playground_session(session) is False

    def test_ignores_normal_forensic_job(self, repo):
        r, session = repo
        r.create_job(_payload())

        assert has_active_playground_session(session) is False


class TestGetJob:
    def test_returns_job_by_id(self, repo):
        r, _ = repo
        job = r.create_job(_payload())
        found = r.get_job(job.id)
        assert found is not None
        assert found.id == job.id

    def test_returns_none_for_unknown_id(self, repo):
        r, _ = repo
        assert r.get_job("does-not-exist") is None


class TestListJobs:
    def test_empty_initially(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            r = JobRepository(session)
            assert r.list_jobs() == []

    def test_lists_all_jobs(self, repo):
        r, _ = repo
        r.create_job(_payload())
        r.create_job(_payload())
        assert len(r.list_jobs()) == 2

    def test_ordered_newest_first(self, repo):
        r, _ = repo
        j1 = r.create_job(_payload())
        j2 = r.create_job(_payload())
        jobs = r.list_jobs()
        # newest (j2) should come first
        assert jobs[0].id == j2.id
        assert jobs[1].id == j1.id

    def test_list_jobs_ignores_separate_playground_session_table(self, repo):
        from astinuslab.playground import repositories as pr

        r, session = repo
        normal = r.create_job(_payload())
        r.update_job(normal.id, status="completed")
        pr.create_playground_session(session, job_id="pg-active")

        jobs = r.list_jobs()

        assert [job.id for job in jobs] == [normal.id]

    def test_respects_limit(self, repo):
        r, _ = repo
        r.create_job(_payload())
        r.create_job(_payload())
        r.create_job(_payload())

        assert len(r.list_jobs(limit=2)) == 2

    def test_respects_offset(self, repo):
        r, _ = repo
        j1 = r.create_job(_payload())
        j2 = r.create_job(_payload())
        j3 = r.create_job(_payload())

        jobs = r.list_jobs(offset=1)

        assert [job.id for job in jobs] == [j2.id, j1.id]
        assert jobs[0].id != j3.id


class TestUpdateJob:
    def test_updates_fields(self, repo):
        r, _ = repo
        job = r.create_job(_payload())
        updated = r.update_job(job.id, status="completed", guest_ip="192.168.100.5")
        assert updated.status == "completed"
        assert updated.guest_ip == "192.168.100.5"

    def test_returns_none_for_unknown_id(self, repo):
        r, _ = repo
        assert r.update_job("ghost-id", status="completed") is None

    def test_rejects_unknown_field(self, repo):
        r, _ = repo
        job = r.create_job(_payload())

        with pytest.raises(ValueError, match="unknown field"):
            r.update_job(job.id, finised_at="oops")

    def test_accepts_real_field(self, repo):
        r, _ = repo
        job = r.create_job(_payload())

        updated = r.update_job(job.id, finished_at=datetime.now(UTC))

        assert updated is not None
        assert updated.finished_at is not None

    def test_update_job_stores_celery_task_id(self, in_memory_session_scope):
        from astinuslab.api.schemas import JobCreate
        from astinuslab.db.repositories import JobRepository
        from astinuslab.jobs.job_services import create_job_service

        with in_memory_session_scope() as session:
            job = create_job_service(
                session,
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                ),
            )
            job_id = job.id

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.update_job(job_id, celery_task_id="celery-abc-123")

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job = repo.get_job(job_id)
            assert job is not None
            assert job.celery_task_id == "celery-abc-123"


def test_create_job_persists_existing_request_fields(in_memory_session_scope, monkeypatch, tmp_path):
    from astinuslab.api import schemas
    from astinuslab.api.schemas import JobCreate
    from astinuslab.db.repositories import JobRepository

    images_dir = tmp_path / "images"
    images_dir.mkdir()
    image_path = images_dir / "image.qcow2"
    image_path.write_bytes(b"")
    autospec_setattr(monkeypatch, schemas, "_images_dir", lambda: images_dir)

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="manual-lab",
                execution_mode="manual_trigger",
                capture_type="both",
                image_mode="qcow2",
                image_path=str(image_path),
                custom_script="#!/bin/bash\necho hi",
                workload_id="juice-shop",
                attacker_vm_id="kali-rolling",
                vm_vcpus=4,
                vm_memory_mb=4096,
                vm_disk_gb=40,
                attacker_vm_vcpus=2,
                attacker_vm_memory_mb=2048,
                attacker_vm_disk_gb=20,
            )
        )

        assert job.target_id == "ubuntu-22-04-default"
        assert job.scenario_id == "manual-lab"
        assert job.execution_mode == "manual_trigger"
        assert job.capture_type == "both"
        assert job.image_mode == "qcow2"
        assert job.image_path == str(image_path.resolve())
        assert job.custom_script == "#!/bin/bash\necho hi"
        assert job.workload_id == "juice-shop"
        assert job.attacker_vm_id == "kali-rolling"
        assert job.vm_vcpus == 4
        assert job.vm_memory_mb == 4096
        assert job.vm_disk_gb == 40
        assert job.attacker_vm_vcpus == 2
        assert job.attacker_vm_memory_mb == 2048
        assert job.attacker_vm_disk_gb == 20


class TestSchemaContracts:
    def test_job_out_serializes_repository_job_with_optional_fields_missing(self, repo):
        r, _ = repo
        job = r.create_job(_payload())
        r.update_job(
            job.id,
            detected_architecture=None,
            network_isolated=None,
            exploit_tags="cve-2024-1111, privesc",
            attacker_ip=None,
            lab_url=None,
            error_summary=None,
        )

        out = JobOut.model_validate(r.get_job(job.id))

        assert out.id == job.id
        assert out.detected_architecture is None
        assert out.network_isolated is None
        assert out.attacker_ip is None
        assert out.lab_url is None
        assert out.error_summary is None
        assert out.exploit_tags == ["cve-2024-1111", "privesc"]

    def test_job_runtime_out_serializes_runtime_defaults_and_nullable_fields(self, repo):
        r, _ = repo
        job = r.create_job(_payload(attacker_vm_id="kali"))
        runtime = r.get_runtime(job.id, "attacker")

        out = JobRuntimeOut.model_validate(runtime)

        assert out.job_id == job.id
        assert out.role == "attacker"
        assert out.ip_address is None
        assert out.snapshot_name is None
        assert out.state == "pending"
        assert out.power_state == "unknown"
        assert out.capture_memory_enabled is False
        assert out.capture_disk_enabled is False


class TestEvents:
    def test_add_and_list_events(self, repo):
        r, _ = repo
        job = r.create_job(_payload())
        r.add_event(job.id, "preparing_image", "Starting image build")
        events = r.list_events(job.id)
        # initial queued + new event
        assert len(events) == 2
        assert events[1].state == "preparing_image"
        assert events[1].message == "Starting image build"

    def test_events_ordered_chronologically(self, repo):
        r, _ = repo
        job = r.create_job(_payload())
        r.add_event(job.id, "preparing_image", "msg1")
        r.add_event(job.id, "building_lime", "msg2")
        states = [e.state for e in r.list_events(job.id)]
        assert states == ["queued", "preparing_image", "building_lime"]


class TestActions:
    def test_enqueue_and_retrieve_action(self, repo):
        r, _ = repo
        job = r.create_job(_payload())
        action = r.enqueue_action(job.id, "run_immediate_job", {"target_id": "ubuntu-22-04-default"})
        assert action.status == "queued"
        assert action.action_type == "run_immediate_job"

    def test_get_next_queued_action(self, repo):
        r, _ = repo
        job = r.create_job(_payload())
        r.enqueue_action(job.id, "run_immediate_job")
        next_action = r.get_next_queued_action()
        assert next_action is not None
        assert next_action.status == "queued"

    def test_no_queued_action_when_empty(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            r = JobRepository(session)
            assert r.get_next_queued_action() is None

    def test_update_action_status(self, repo):
        r, _ = repo
        job = r.create_job(_payload())
        action = r.enqueue_action(job.id, "run_immediate_job")
        updated = r.update_action(action.id, status="running", active_pid=1234)
        assert updated.status == "running"
        assert updated.active_pid == 1234

    def test_update_action_rejects_unknown_field(self, repo):
        r, _ = repo
        job = r.create_job(_payload())
        action = r.enqueue_action(job.id, "run_immediate_job")

        with pytest.raises(ValueError, match="unknown field"):
            r.update_action(action.id, statuz="running")


class TestRuntimes:
    def test_update_runtime_rejects_unknown_field(self, repo):
        r, _ = repo
        job = r.create_job(_payload())

        with pytest.raises(ValueError, match="unknown field"):
            r.update_runtime(job.id, "target", power_statte="on")

    def test_upsert_runtime_rejects_unknown_field_on_create(self, repo):
        r, _ = repo
        job = r.create_job(_payload())

        with pytest.raises(ValueError, match="unknown field"):
            r.upsert_runtime(job.id, "observer", "vm-observer", power_statte="on")

    def test_upsert_runtime_rejects_unknown_field_on_update(self, repo):
        r, _ = repo
        job = r.create_job(_payload())

        with pytest.raises(ValueError, match="unknown field"):
            r.upsert_runtime(job.id, "target", "vm-target", power_statte="on")


class TestForceTransition:
    def test_force_transition_bypasses_guards(self, repo):
        r, _ = repo
        job = r.create_job(_payload())
        # Normally queued -> completed is not allowed directly

        r.force_transition(job.id, "completed")

        assert job.status == "completed"
        assert job.current_stage == "completed"

    def test_force_transition_fails_active_actions(self, repo):
        r, _ = repo
        job = r.create_job(_payload())
        a1 = r.enqueue_action(job.id, "some_action")  # noqa: F841
        a2 = r.enqueue_action(job.id, "other_action")
        r.update_action(a2.id, status="running")

        r.force_transition(job.id, "failed", error_summary="Forced")

        assert job.status == "failed"
        assert job.error_summary == "Forced"

        actions = r.list_actions(job.id)
        assert len(actions) == 2
        for a in actions:
            assert a.status == "failed"
            assert a.error_summary == "Job force-failed by user"
            assert a.finished_at is not None

    def test_force_transition_raises_if_job_not_found(self, repo):
        r, _ = repo
        with pytest.raises(ValueError, match="Job not found"):
            r.force_transition("ghost-id", "failed")


class TestJobIndexes:
    def test_status_and_created_at_indexes_exist(self):
        """Job table must have indexes on status and created_at for query performance.

        list_jobs() and list_orphaned_jobs() filter/sort by these columns.
        Without indexes, both queries do full table scans — O(n) per worker startup.
        """
        from sqlalchemy import inspect as sa_inspect

        from astinuslab.db import engine, initialize_database

        initialize_database()

        inspector = sa_inspect(engine)
        index_names = {idx["name"] for idx in inspector.get_indexes("jobs") if isinstance(idx["name"], str)}
        assert any("status" in name for name in index_names), (
            f"No index on jobs.status found. Indexes present: {index_names}"
        )
        assert any("created_at" in name for name in index_names), (
            f"No index on jobs.created_at found. Indexes present: {index_names}"
        )


class TestIsfArtifactCacheQueries:
    """F02: repository methods that power build_id-aware cache enforcement."""

    def _make_job(self, r: JobRepository, kernel_release: str = "6.8.0-101-generic") -> str:
        job = r.create_job(_payload())
        r.force_transition(
            job.id,
            "completed",
            detected_kernel_release=kernel_release,
            detected_distro="ubuntu",
            detected_version="22.04",
        )
        return job.id

    def test_find_valid_isf_by_kernel_returns_none_when_empty(self, repo):
        r, _ = repo
        assert r.find_valid_isf_by_kernel("6.8.0-101-generic") is None

    def test_find_valid_isf_by_kernel_returns_matching_artifact(self, repo):
        r, _ = repo
        job_id = self._make_job(r)
        r.create_isf_artifact(job_id, "6.8.0-101-generic", "/sym/linux/6.8.json", "live_guest", build_id="aabbcc")
        result = r.find_valid_isf_by_kernel("6.8.0-101-generic")
        assert result is not None
        assert result.build_id == "aabbcc"
        assert result.kernel_release == "6.8.0-101-generic"

    def test_find_valid_isf_by_kernel_ignores_other_kernels(self, repo):
        r, _ = repo
        job_id = self._make_job(r, "5.15.0-50-generic")
        r.create_isf_artifact(job_id, "5.15.0-50-generic", "/sym/linux/5.15.json", "host_fetch", build_id="deadbeef")
        assert r.find_valid_isf_by_kernel("6.8.0-101-generic") is None

    def test_find_valid_isf_by_kernel_returns_most_recent(self, repo):
        r, _ = repo
        job_id = self._make_job(r)
        r.create_isf_artifact(job_id, "6.8.0-101-generic", "/sym/linux/6.8.json", "live_guest", build_id="aaaa")
        r.create_isf_artifact(job_id, "6.8.0-101-generic", "/sym/linux/6.8.json", "host_fetch", build_id="bbbb")
        result = r.find_valid_isf_by_kernel("6.8.0-101-generic")
        assert result is not None
        assert result.build_id == "bbbb"  # most recent wins

    def test_resolve_isf_origin_source_resolves_cache_soft_hit(self, repo):
        r, _ = repo
        origin_job = self._make_job(r)
        cached_job = self._make_job(r)
        r.create_isf_artifact(
            origin_job,
            "6.8.0-101-generic",
            "/sym/linux/origin.json",
            "host_fetch",
            sha256="aa" * 32,
        )
        soft = r.create_isf_artifact(
            cached_job,
            "6.8.0-101-generic",
            "/sym/linux/soft.json",
            "cache_soft_hit",
            sha256="aa" * 32,
        )
        assert r.resolve_isf_origin_source(soft) == "host_fetch"

        unresolved = r.create_isf_artifact(
            cached_job,
            "6.8.0-101-generic",
            "/sym/linux/unresolved.json",
            "cache_soft_hit",
            sha256="bb" * 32,
        )
        assert r.resolve_isf_origin_source(unresolved) == "cache_soft_hit"

    def test_get_job_known_build_id_returns_none_when_no_artifacts(self, repo):
        r, _ = repo
        job_id = self._make_job(r)
        assert r.get_job_known_build_id(job_id) is None

    def test_get_job_known_build_id_returns_none_when_all_null(self, repo):
        r, _ = repo
        job_id = self._make_job(r)
        r.create_isf_artifact(job_id, "6.8.0-101-generic", "/sym/linux/6.8.json", "live_guest")
        assert r.get_job_known_build_id(job_id) is None

    def test_get_job_known_build_id_returns_non_null_build_id(self, repo):
        r, _ = repo
        job_id = self._make_job(r)
        r.create_isf_artifact(job_id, "6.8.0-101-generic", "/sym/linux/6.8.json", "live_guest", build_id="cafe1234")
        assert r.get_job_known_build_id(job_id) == "cafe1234"

    def test_get_job_known_build_id_skips_null_rows_to_find_non_null(self, repo):
        r, _ = repo
        job_id = self._make_job(r)
        r.create_isf_artifact(job_id, "6.8.0-101-generic", "/sym/linux/6.8.json", "live_guest")
        r.create_isf_artifact(job_id, "6.8.0-101-generic", "/sym/linux/6.8.json", "host_fetch", build_id="1234abcd")
        assert r.get_job_known_build_id(job_id) == "1234abcd"
