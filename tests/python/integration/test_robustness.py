"""Tests for error reporting and robustness logic."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from astinuslab.db.repositories import JobRepository
from astinuslab.orchestration.support import handle_exception


@pytest.fixture
def mock_repo_cls():
    repo = MagicMock(spec=JobRepository)
    cls = MagicMock(return_value=repo)
    return cls


def test_handle_exception_reports_enospc_hint(mock_repo_cls):
    logger = MagicMock()
    # Simulate a subprocess failure with "No space left on device"
    exc = subprocess.CalledProcessError(
        1, ["apt-get", "install", "x"], output="", stderr="E: dpkg returned an error code (2)\nNo space left on device"
    )

    # We need a session_scope that doesn't actually hit a DB
    session_scope = MagicMock()

    handle_exception(session_scope, mock_repo_cls, logger, exc, "job-1", None)

    # Verify transition_job_status was called with the hint
    repo = mock_repo_cls.return_value
    args, kwargs = repo.transition_job_status.call_args
    error_summary = kwargs.get("error_summary", "")

    assert "HINT: The guest VM ran out of disk space" in error_summary
    assert "Try increasing 'disk_gb'" in error_summary


def test_handle_exception_reports_php_brace_hint(mock_repo_cls):
    logger = MagicMock()
    # Simulated PHP fatal error
    msg = "PHP Fatal error: Array and string offset access syntax with curly braces is no longer supported in x.php"
    exc = RuntimeError(msg)

    session_scope = MagicMock()
    handle_exception(session_scope, mock_repo_cls, logger, exc, "job-1", None)

    repo = mock_repo_cls.return_value
    _, kwargs = repo.transition_job_status.call_args
    error_summary = kwargs.get("error_summary", "")

    assert "HINT: PHP Fatal Error: Your workload script uses deprecated curly-brace syntax" in error_summary


def test_handle_exception_reports_mysql_socket_hint(mock_repo_cls):
    logger = MagicMock()
    msg = "Can't connect to local MySQL server through socket '/var/run/mysqld/mysqld.sock'"
    exc = RuntimeError(msg)

    session_scope = MagicMock()
    handle_exception(session_scope, mock_repo_cls, logger, exc, "job-1", None)

    repo = mock_repo_cls.return_value
    _, kwargs = repo.transition_job_status.call_args
    error_summary = kwargs.get("error_summary", "")

    assert "HINT: MySQL Connection Error" in error_summary


def test_handle_exception_reports_apt_lock_hint(mock_repo_cls):
    logger = MagicMock()
    msg = "E: Could not get lock /var/lib/dpkg/lock-frontend - open (11: Resource temporarily unavailable)"
    exc = RuntimeError(msg)

    session_scope = MagicMock()
    handle_exception(session_scope, mock_repo_cls, logger, exc, "job-1", None)

    repo = mock_repo_cls.return_value
    _, kwargs = repo.transition_job_status.call_args
    error_summary = kwargs.get("error_summary", "")

    assert "HINT: APT Lock Error" in error_summary


def test_handle_exception_reports_ssh_perms_hint(mock_repo_cls):
    logger = MagicMock()
    msg = 'Load key "/storage/ssh_keys/key": bad permissions'
    exc = RuntimeError(msg)

    session_scope = MagicMock()
    handle_exception(session_scope, mock_repo_cls, logger, exc, "job-1", None)

    repo = mock_repo_cls.return_value
    _, kwargs = repo.transition_job_status.call_args
    error_summary = kwargs.get("error_summary", "")

    assert "HINT: SSH Key Permission Error" in error_summary


def test_handle_exception_reports_missing_apt_hint(mock_repo_cls):
    logger = MagicMock()
    msg = "apt-get: command not found"
    handle_exception(MagicMock(), mock_repo_cls, logger, RuntimeError(msg), "job-1", None)

    repo = mock_repo_cls.return_value
    _, kwargs = repo.transition_job_status.call_args
    assert "HINT: Workload/package-manager mismatch" in kwargs.get("error_summary", "")


def test_handle_exception_reports_declared_compatibility_hint(mock_repo_cls):
    logger = MagicMock()
    msg = "Workload 'wordpress-6.4' is incompatible with detected guest fedora 40"
    handle_exception(MagicMock(), mock_repo_cls, logger, RuntimeError(msg), "job-1", None)

    repo = mock_repo_cls.return_value
    _, kwargs = repo.transition_job_status.call_args
    assert "HINT: Workload compatibility rejected this target" in kwargs.get("error_summary", "")
