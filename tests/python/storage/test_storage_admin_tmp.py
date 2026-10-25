"""Temporary-storage inventory shown to the operator before a cleanup.

The admin view has to be honest about what is safe to remove: anything named
after a job that still exists in the ledger is not removable, an orphaned
job directory is, and everything else is auxiliary. A directory the process
cannot read must not make the whole listing fail — a partially readable
storage tree still needs to render.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from astinuslab.storage.storage_admin_support import list_tmp_entries, storage_stats

pytestmark = pytest.mark.unit


class _Repo:
    """A ledger holding whichever jobs the test declares."""

    def __init__(self, session, jobs: dict[str, str] | None = None) -> None:
        self._jobs = jobs or {}

    def get_job(self, job_id: str):
        status = self._jobs.get(job_id)
        return SimpleNamespace(id=job_id, status=status) if status else None


class _FailingRepo:
    def __init__(self, session) -> None:
        pass

    def get_job(self, job_id: str):
        raise RuntimeError("ledger is unavailable")


def _scope():
    from contextlib import contextmanager

    @contextmanager
    def scope():
        yield object()

    return scope


def _settings(tmp_path: Path):
    return SimpleNamespace(storage_dir=tmp_path)


def _repo_cls(jobs: dict[str, str] | None = None):
    return lambda session: _Repo(session, jobs)


def _by_name(entries: list[dict]) -> dict[str, dict]:
    return {entry["name"]: entry for entry in entries}


# --- listing -------------------------------------------------------------------


def test_a_missing_temporary_root_lists_nothing(tmp_path: Path):
    assert list_tmp_entries(_scope(), _repo_cls(), _settings(tmp_path)) == []


def test_an_empty_temporary_root_lists_nothing(tmp_path: Path):
    (tmp_path / "tmp").mkdir()

    assert list_tmp_entries(_scope(), _repo_cls(), _settings(tmp_path)) == []


def test_an_auxiliary_file_is_listed_as_removable(tmp_path: Path):
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    (tmp_root / "scratch.bin").write_bytes(b"x" * 2048)

    entries = _by_name(list_tmp_entries(_scope(), _repo_cls(), _settings(tmp_path)))

    assert entries["scratch.bin"]["removable"] is True
    assert entries["scratch.bin"]["reason"] == "aux"
    assert entries["scratch.bin"]["is_dir"] is False
    assert entries["scratch.bin"]["size_mb"] == pytest.approx(2048 / (1024 * 1024))


@pytest.mark.parametrize(
    "name",
    ["package-instance-locks", "package-instance-reservation-locks"],
)
def test_package_instance_lock_infrastructure_is_listed_as_non_removable(tmp_path: Path, name: str):
    lock_dir = tmp_path / "tmp" / name
    lock_dir.mkdir(parents=True)
    (lock_dir / "active.lock").write_bytes(b"")

    entries = _by_name(list_tmp_entries(_scope(), _repo_cls(), _settings(tmp_path)))

    assert entries[name]["removable"] is False
    assert entries[name]["reason"] == "lock_infrastructure"


@pytest.mark.parametrize(
    "name",
    ["package-instance-locks", "package-instance-reservation-locks"],
)
def test_package_instance_lock_symlink_alias_is_listed_as_non_removable(tmp_path: Path, name: str):
    tmp_root = tmp_path / "tmp"
    lock_dir = tmp_root / name
    lock_dir.mkdir(parents=True)
    alias = tmp_root / f"{name}-alias"
    alias.symlink_to(lock_dir, target_is_directory=True)

    entries = _by_name(list_tmp_entries(_scope(), _repo_cls(), _settings(tmp_path)))

    assert entries[alias.name]["removable"] is False
    assert entries[alias.name]["reason"] == "lock_infrastructure"


@pytest.mark.parametrize(
    "name",
    ["package-instance-locks", "package-instance-reservation-locks"],
)
def test_package_instance_lock_normalized_symlink_alias_is_listed_as_non_removable(tmp_path: Path, name: str):
    tmp_root = tmp_path / "tmp"
    lock_dir = tmp_root / name
    lock_dir.mkdir(parents=True)
    (tmp_root / "nested").mkdir()
    alias = tmp_root / f"{name}-normalized-alias"
    alias.symlink_to(Path("nested") / ".." / name, target_is_directory=True)

    entries = _by_name(list_tmp_entries(_scope(), _repo_cls(), _settings(tmp_path)))

    assert entries[alias.name]["removable"] is False
    assert entries[alias.name]["reason"] == "lock_infrastructure"


def test_a_directory_reports_its_recursive_size(tmp_path: Path):
    tmp_root = tmp_path / "tmp"
    nested = tmp_root / "work" / "deeper"
    nested.mkdir(parents=True)
    (nested / "a.bin").write_bytes(b"x" * 1024)
    (tmp_root / "work" / "b.bin").write_bytes(b"y" * 1024)

    entries = _by_name(list_tmp_entries(_scope(), _repo_cls(), _settings(tmp_path)))

    assert entries["work"]["is_dir"] is True
    assert entries["work"]["size_mb"] == pytest.approx(2048 / (1024 * 1024))


def test_entries_are_listed_in_name_order(tmp_path: Path):
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    for name in ("charlie", "alpha", "bravo"):
        (tmp_root / name).write_bytes(b"x")

    entries = list_tmp_entries(_scope(), _repo_cls(), _settings(tmp_path))

    assert [entry["name"] for entry in entries] == ["alpha", "bravo", "charlie"]


# --- job custody ---------------------------------------------------------------


def test_a_directory_for_a_live_job_is_not_removable(tmp_path: Path):
    tmp_root = tmp_path / "tmp"
    (tmp_root / "job-123").mkdir(parents=True)

    entries = _by_name(list_tmp_entries(_scope(), _repo_cls({"job-123": "running"}), _settings(tmp_path)))

    assert entries["job-123"]["removable"] is False
    assert entries["job-123"]["reason"] == "job_exists:running"


def test_a_directory_for_a_job_the_ledger_forgot_is_an_orphan(tmp_path: Path):
    tmp_root = tmp_path / "tmp"
    (tmp_root / "job-gone").mkdir(parents=True)

    entries = _by_name(list_tmp_entries(_scope(), _repo_cls(), _settings(tmp_path)))

    assert entries["job-gone"]["removable"] is True
    assert entries["job-gone"]["reason"] == "orphan_job_tmp"


def test_a_ledger_failure_is_reported_rather_than_guessed(tmp_path: Path):
    tmp_root = tmp_path / "tmp"
    (tmp_root / "job-123").mkdir(parents=True)

    entries = _by_name(list_tmp_entries(_scope(), _FailingRepo, _settings(tmp_path)))

    assert entries["job-123"]["reason"] == "lookup_failed"


def test_a_name_that_only_resembles_a_job_is_auxiliary(tmp_path: Path):
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    (tmp_root / "jobless.bin").write_bytes(b"x")

    entries = _by_name(list_tmp_entries(_scope(), _repo_cls(), _settings(tmp_path)))

    assert entries["jobless.bin"]["reason"] == "aux"


def test_an_unreadable_directory_does_not_break_the_listing(tmp_path: Path):
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    (tmp_root / "readable.bin").write_bytes(b"x")
    blocked = tmp_root / "blocked"
    blocked.mkdir()
    (blocked / "inner.bin").write_bytes(b"y")
    blocked.chmod(0o000)
    try:
        entries = _by_name(list_tmp_entries(_scope(), _repo_cls(), _settings(tmp_path)))

        assert "readable.bin" in entries
        assert entries["blocked"]["size_mb"] == 0.0
    finally:
        blocked.chmod(0o700)


# --- storage_stats -------------------------------------------------------------


def test_storage_stats_reports_zero_for_an_empty_tree(tmp_path: Path):
    stats = storage_stats(_settings(tmp_path))

    assert all(isinstance(value, (int, float)) for value in stats.values())


def test_storage_stats_counts_files_it_can_read(tmp_path: Path):
    dumps = tmp_path / "dumps"
    dumps.mkdir()
    (dumps / "capture.raw").write_bytes(b"x" * 4096)

    stats = storage_stats(_settings(tmp_path))

    assert stats["dumps_count"] == 1
    assert stats["dumps_mb"] == pytest.approx(4096 / (1024 * 1024))
