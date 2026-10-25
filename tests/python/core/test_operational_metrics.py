"""Operational metrics computed from rows the database already holds.

No counters, no collector, no new dependency: Job and HostAction already record
created/started/finished timestamps and terminal status. Nothing read them back,
so "how long does memory acquisition take on this host" and "is the runner queue
backing up" needed hand-written SQL. These aggregate the existing rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from astinuslab.core import operational_metrics

pytestmark = pytest.mark.unit


def _naive(moment: datetime) -> datetime:
    """The DB columns are naive UTC; match how the repositories write them."""
    return moment.replace(tzinfo=None)


def _add_action(session, *, job_id, action_type, status, started_ago_s, duration_s, created_ago_s=None):
    from astinuslab.db.models import HostAction

    now = datetime.now(UTC)
    started = now - timedelta(seconds=started_ago_s)
    created = now - timedelta(seconds=created_ago_s if created_ago_s is not None else started_ago_s)
    action = HostAction(
        job_id=job_id,
        action_type=action_type,
        status=status,
        created_at=_naive(created),
        started_at=_naive(started),
        finished_at=_naive(started + timedelta(seconds=duration_s)) if duration_s is not None else None,
    )
    session.add(action)
    return action


def _make_job(session, job_id, status="completed"):
    from astinuslab.api.schemas import JobCreate
    from astinuslab.db.repositories import JobRepository

    job = JobRepository(session).create_job(
        JobCreate(
            target_id="ubuntu-22-04-default",
            scenario_id="immediate-capture",
            execution_mode="immediate",
        )
    )
    job.id = job_id
    job.status = status
    session.flush()
    return job


class TestActionDurations:
    @pytest.mark.parametrize("sample_size, p50, p95", [(2, 1, 2), (6, 3, 6), (20, 10, 19)])
    def test_percentiles_use_nearest_rank_at_integer_boundaries(self, in_memory_session_scope, sample_size, p50, p95):
        with in_memory_session_scope() as session:
            _make_job(session, "job-1")
            for duration in range(1, sample_size + 1):
                _add_action(
                    session,
                    job_id="job-1",
                    action_type="acquire_memory_dump",
                    status="completed",
                    started_ago_s=3600,
                    duration_s=duration,
                )
            session.commit()
            row = operational_metrics.collect_metrics(session)["actions"][0]

        assert row["p50_seconds"] == p50
        assert row["p95_seconds"] == p95

    def test_it_reports_count_and_percentiles_per_action_type(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            _make_job(session, "job-1")
            for duration in (10, 20, 30, 40, 100):
                _add_action(
                    session,
                    job_id="job-1",
                    action_type="acquire_memory_dump",
                    status="completed",
                    started_ago_s=3600,
                    duration_s=duration,
                )
            session.commit()

            report = operational_metrics.collect_metrics(session, window_hours=24)

        actions = {row["action_type"]: row for row in report["actions"]}
        assert actions["acquire_memory_dump"]["completed"] == 5
        assert actions["acquire_memory_dump"]["p50_seconds"] == 30.0
        assert actions["acquire_memory_dump"]["p95_seconds"] == 100.0

    def test_a_single_sample_is_its_own_percentile(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            _make_job(session, "job-1")
            _add_action(
                session,
                job_id="job-1",
                action_type="run_vol3_plugin",
                status="completed",
                started_ago_s=60,
                duration_s=7,
            )
            session.commit()

            report = operational_metrics.collect_metrics(session, window_hours=24)

        row = next(r for r in report["actions"] if r["action_type"] == "run_vol3_plugin")
        assert row["p50_seconds"] == 7.0
        assert row["p95_seconds"] == 7.0

    def test_failures_are_counted_but_excluded_from_the_duration_sample(self, in_memory_session_scope):
        """A capture that died after 2s would otherwise flatter the p50."""
        with in_memory_session_scope() as session:
            _make_job(session, "job-1")
            _add_action(
                session,
                job_id="job-1",
                action_type="acquire_memory_dump",
                status="completed",
                started_ago_s=600,
                duration_s=300,
            )
            _add_action(
                session,
                job_id="job-1",
                action_type="acquire_memory_dump",
                status="failed",
                started_ago_s=600,
                duration_s=2,
            )
            session.commit()

            report = operational_metrics.collect_metrics(session, window_hours=24)

        row = next(r for r in report["actions"] if r["action_type"] == "acquire_memory_dump")
        assert row["completed"] == 1
        assert row["failed"] == 1
        assert row["p50_seconds"] == 300.0

    def test_an_unfinished_action_contributes_no_duration(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            _make_job(session, "job-1")
            _add_action(
                session,
                job_id="job-1",
                action_type="acquire_memory_dump",
                status="running",
                started_ago_s=120,
                duration_s=None,
            )
            session.commit()

            report = operational_metrics.collect_metrics(session, window_hours=24)

        row = next(r for r in report["actions"] if r["action_type"] == "acquire_memory_dump")
        assert row["completed"] == 0
        assert row["p50_seconds"] is None

    def test_rows_outside_the_window_are_excluded(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            _make_job(session, "job-1")
            _add_action(
                session,
                job_id="job-1",
                action_type="acquire_memory_dump",
                status="completed",
                started_ago_s=60 * 60 * 24 * 10,
                duration_s=42,
            )
            session.commit()

            report = operational_metrics.collect_metrics(session, window_hours=24)

        assert report["actions"] == []


class TestQueueDepth:
    def test_queued_but_undispatched_actions_are_the_backlog(self, in_memory_session_scope):
        """The runner is concurrency 1 by design; a backlog is invisible otherwise."""
        with in_memory_session_scope() as session:
            _make_job(session, "job-1")
            for _ in range(3):
                _add_action(
                    session,
                    job_id="job-1",
                    action_type="run_immediate_job",
                    status="queued",
                    started_ago_s=0,
                    duration_s=None,
                    created_ago_s=120,
                )
            session.commit()

            report = operational_metrics.collect_metrics(session, window_hours=24)

        assert report["queue"]["queued"] == 3
        assert report["queue"]["oldest_queued_age_seconds"] >= 115

    def test_running_actions_are_reported_apart_from_the_queue(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            _make_job(session, "job-1")
            _add_action(
                session,
                job_id="job-1",
                action_type="run_immediate_job",
                status="running",
                started_ago_s=30,
                duration_s=None,
            )
            session.commit()

            report = operational_metrics.collect_metrics(session, window_hours=24)

        assert report["queue"]["queued"] == 0
        assert report["queue"]["running"] == 1

    def test_an_empty_queue_reports_no_age(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            report = operational_metrics.collect_metrics(session, window_hours=24)

        assert report["queue"]["queued"] == 0
        assert report["queue"]["oldest_queued_age_seconds"] is None

    def test_the_queue_is_not_limited_to_the_window(self, in_memory_session_scope):
        """A job stuck queued for a week is exactly what this must surface."""
        with in_memory_session_scope() as session:
            _make_job(session, "job-1")
            _add_action(
                session,
                job_id="job-1",
                action_type="run_immediate_job",
                status="queued",
                started_ago_s=0,
                duration_s=None,
                created_ago_s=60 * 60 * 24 * 7,
            )
            session.commit()

            report = operational_metrics.collect_metrics(session, window_hours=24)

        assert report["queue"]["queued"] == 1


class TestJobOutcomes:
    def test_jobs_are_counted_by_terminal_status(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            _make_job(session, "job-1", status="completed")
            _make_job(session, "job-2", status="completed")
            _make_job(session, "job-3", status="failed")
            session.commit()

            report = operational_metrics.collect_metrics(session, window_hours=24)

        assert report["jobs"]["completed"] == 2
        assert report["jobs"]["failed"] == 1


class TestShape:
    def test_the_window_is_reported_back(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            report = operational_metrics.collect_metrics(session, window_hours=6)

        assert report["window_hours"] == 6
        assert "generated_at" in report

    def test_an_empty_database_produces_a_complete_payload(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            report = operational_metrics.collect_metrics(session, window_hours=24)

        assert report["actions"] == []
        assert report["jobs"] == {}
        assert report["queue"]["queued"] == 0
