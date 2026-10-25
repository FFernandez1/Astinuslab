"""Tests for ISF artifact metadata persistence."""

from __future__ import annotations

from astinuslab.api.schemas import JobCreate
from astinuslab.db.repositories import JobRepository


def _make_job(scope) -> str:
    with scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
            )
        )
        return job.id


class TestISFArtifactRepository:
    def test_create_and_get_artifact(self, in_memory_session_scope):
        job_id = _make_job(in_memory_session_scope)
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            artifact = repo.create_isf_artifact(
                job_id,
                "5.15.0-101-generic",
                "/vol3/symbols/linux/5.15.0-101-generic.json",
                "host_fetch",
                distro="ubuntu",
                version="22.04",
                sha256="abc123",
            )
            assert artifact.id is not None
            assert artifact.source == "host_fetch"
            assert artifact.sha256 == "abc123"
            assert artifact.architecture == "x86_64"

    def test_get_latest_returns_most_recent(self, in_memory_session_scope):
        job_id = _make_job(in_memory_session_scope)
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.create_isf_artifact(job_id, "5.15.0-100-generic", "/path/a.json", "host_fetch")
            repo.create_isf_artifact(job_id, "5.15.0-101-generic", "/path/b.json", "cache")
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            latest = repo.get_latest_isf_artifact(job_id)
            assert latest is not None
            kernel_release = latest.kernel_release
        assert kernel_release == "5.15.0-101-generic"

    def test_get_latest_returns_none_when_no_artifacts(self, in_memory_session_scope):
        job_id = _make_job(in_memory_session_scope)
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            assert repo.get_latest_isf_artifact(job_id) is None

    def test_cache_source_recorded(self, in_memory_session_scope):
        job_id = _make_job(in_memory_session_scope)
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            artifact = repo.create_isf_artifact(
                job_id,
                "5.15.0-101-generic",
                "/path/x.json",
                "cache",
            )
            assert artifact.source == "cache"
            assert artifact.validation_status == "valid"


class TestResolveIsfOriginSource:
    def test_non_cache_source_returned_verbatim(self, in_memory_session_scope):
        job_id = _make_job(in_memory_session_scope)
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            artifact = repo.create_isf_artifact(
                job_id,
                "5.15.0-101-generic",
                "/path/live.json",
                "live_guest",
                sha256="c" * 64,
            )
            assert repo.resolve_isf_origin_source(artifact) == "live_guest"

    def test_cache_hit_resolves_to_earliest_origin(self, in_memory_session_scope):
        job_a = _make_job(in_memory_session_scope)
        job_b = _make_job(in_memory_session_scope)
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.create_isf_artifact(
                job_a,
                "5.15.0-101-generic",
                "/path/origin.json",
                "live_guest",
                sha256="c" * 64,
            )
            cache = repo.create_isf_artifact(
                job_b,
                "5.15.0-101-generic",
                "/path/cache.json",
                "cache",
                sha256="c" * 64,
            )
            assert repo.resolve_isf_origin_source(cache) == "live_guest"

    def test_cache_hit_without_match_stays_cache(self, in_memory_session_scope):
        job_id = _make_job(in_memory_session_scope)
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            artifact = repo.create_isf_artifact(
                job_id,
                "5.15.0-101-generic",
                "/path/cache.json",
                "cache",
                sha256="d" * 64,
            )
            assert repo.resolve_isf_origin_source(artifact) == "cache"

    def test_cache_hit_without_sha256_stays_cache(self, in_memory_session_scope):
        job_id = _make_job(in_memory_session_scope)
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            artifact = repo.create_isf_artifact(
                job_id,
                "5.15.0-101-generic",
                "/path/cache.json",
                "cache",
                sha256=None,
            )
            assert repo.resolve_isf_origin_source(artifact) == "cache"
