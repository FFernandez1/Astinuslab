from __future__ import annotations

import subprocess
from contextlib import contextmanager
from unittest.mock import ANY, MagicMock

from astinuslab.orchestration.support import (
    complete_action,
    create_lab_snapshot,
    handle_exception,
    mark_action_running,
    revert_lab_snapshot,
)


@contextmanager
def mock_session_scope():
    yield MagicMock()


# --- Orchestration Support Tests ---


def test_mark_action_running():
    repo_cls_mock = MagicMock()
    action_by_job: dict[str, int] = {}

    mark_action_running(mock_session_scope, repo_cls_mock, action_by_job, "job1", 123)

    assert action_by_job["job1"] == 123
    repo = repo_cls_mock.return_value
    repo.update_action.assert_called_once_with(123, status="running", started_at=ANY)


def test_mark_action_running_noop_when_action_id_none():
    repo_cls_mock = MagicMock()
    action_by_job: dict[str, int] = {}

    mark_action_running(mock_session_scope, repo_cls_mock, action_by_job, "job1", None)

    assert action_by_job == {}
    repo_cls_mock.return_value.update_action.assert_not_called()


def test_complete_action():
    repo_cls_mock = MagicMock()

    complete_action(mock_session_scope, repo_cls_mock, 123)

    repo_cls_mock.return_value.update_action.assert_called_once_with(
        123, status="completed", finished_at=ANY, active_pid=None
    )


def test_complete_action_noop_when_action_id_none():
    repo_cls_mock = MagicMock()

    complete_action(mock_session_scope, repo_cls_mock, None)

    repo_cls_mock.return_value.update_action.assert_not_called()


def test_handle_exception_cancelled():
    repo_cls_mock = MagicMock()
    logger_mock = MagicMock()

    exc = subprocess.CalledProcessError(returncode=-15, cmd="test")
    handle_exception(mock_session_scope, repo_cls_mock, logger_mock, exc, "job1", 123)

    repo = repo_cls_mock.return_value
    repo.transition_job_status.assert_called_once_with("job1", "cancelled", finished_at=ANY)
    repo.add_event.assert_called_once_with("job1", "cancelled", "Job interrupted by cancellation")
    repo.update_action.assert_called_once_with(123, status="cancelled", finished_at=ANY, active_pid=None)


def test_create_lab_snapshot_success():
    repo_cls_mock = MagicMock()
    run_command_mock = MagicMock()
    sync_runtime_record_mock = MagicMock()
    settings_mock = MagicMock(libvirt_uri="test_uri")
    logger_mock = MagicMock()
    repo_cls_mock.return_value.get_job.return_value = MagicMock(lab_snapshot_name=None)

    res = create_lab_snapshot(
        mock_session_scope,
        repo_cls_mock,
        run_command_mock,
        sync_runtime_record_mock,
        settings_mock,
        logger_mock,
        "job1",
    )

    assert res == "lab-clean"
    # The virsh snapshot is created for this job's domain with the lab-clean name.
    cmd = run_command_mock.call_args.args[0]
    assert cmd[:2] == ["virsh", "-c"]
    assert "snapshot-create-as" in cmd
    assert "astinuslab-job1" in cmd and "lab-clean" in cmd
    # The snapshot name is persisted and mirrored to the runtime record.
    repo_cls_mock.return_value.update_job.assert_called_once_with("job1", lab_snapshot_name="lab-clean")
    sync_runtime_record_mock.assert_called_once_with("job1", "target", snapshot_name="lab-clean")


def test_create_lab_snapshot_already_exists(monkeypatch):
    session_scope_mock = MagicMock()
    session_scope_mock.side_effect = lambda: mock_session_scope()
    repo_cls_mock = MagicMock()
    repo_mock = repo_cls_mock.return_value
    repo_mock.get_job.return_value = MagicMock(lab_snapshot_name="existing")

    res = create_lab_snapshot(session_scope_mock, repo_cls_mock, None, None, None, None, "job1")
    assert res == "existing"


def test_create_lab_snapshot_failure(monkeypatch):
    session_scope_mock = MagicMock()
    session_scope_mock.side_effect = lambda: mock_session_scope()
    repo_cls_mock = MagicMock()
    run_command_mock = MagicMock(side_effect=subprocess.CalledProcessError(1, "cmd"))
    settings_mock = MagicMock(libvirt_uri="test_uri")
    logger_mock = MagicMock()

    repo_mock = repo_cls_mock.return_value
    repo_mock.get_job.return_value = MagicMock(lab_snapshot_name=None)

    res = create_lab_snapshot(
        session_scope_mock, repo_cls_mock, run_command_mock, MagicMock(), settings_mock, logger_mock, "job1"
    )
    assert res == ""
    assert logger_mock.warning.called


def test_revert_lab_snapshot_success(monkeypatch):
    session_scope_mock = MagicMock()
    session_scope_mock.side_effect = lambda: mock_session_scope()
    repo_cls_mock = MagicMock()
    run_command_mock = MagicMock()
    # Mock domblklist output - 4 columns
    run_command_mock.side_effect = [
        None,  # destroy
        "Type  Device  Target  Source\n----------------------------\nfile  disk    vda     /path/to/overlay\n",  # domblklist
        '{"backing-filename": "/path/to/base"}',  # qemu-img info
        None,  # qemu-img create
        None,  # start
    ]
    sync_runtime_record_mock = MagicMock()
    settings_mock = MagicMock(libvirt_uri="test_uri")
    logger_mock = MagicMock()

    repo_mock = repo_cls_mock.return_value
    repo_mock.get_job.return_value = MagicMock(lab_snapshot_name="lab-clean")

    res = revert_lab_snapshot(
        session_scope_mock,
        repo_cls_mock,
        run_command_mock,
        sync_runtime_record_mock,
        settings_mock,
        logger_mock,
        "job1",
    )
    assert res is True
    assert run_command_mock.call_count == 5
    sync_runtime_record_mock.assert_called_once_with("job1", "target", state="running", power_state="on")


def test_revert_lab_snapshot_no_job(monkeypatch):
    session_scope_mock = MagicMock()
    session_scope_mock.side_effect = lambda: mock_session_scope()
    repo_cls_mock = MagicMock()
    repo_mock = repo_cls_mock.return_value
    repo_mock.get_job.return_value = None

    res = revert_lab_snapshot(session_scope_mock, repo_cls_mock, None, None, None, None, "job1")
    assert res is False


def test_revert_lab_snapshot_no_snapshot(monkeypatch):
    session_scope_mock = MagicMock()
    session_scope_mock.side_effect = lambda: mock_session_scope()
    repo_cls_mock = MagicMock()
    repo_mock = repo_cls_mock.return_value
    repo_mock.get_job.return_value = MagicMock(lab_snapshot_name=None)

    res = revert_lab_snapshot(session_scope_mock, repo_cls_mock, None, None, None, None, "job1")
    assert res is False


def test_revert_lab_snapshot_failure(monkeypatch):
    session_scope_mock = MagicMock()
    session_scope_mock.side_effect = lambda: mock_session_scope()
    repo_cls_mock = MagicMock()
    run_command_mock = MagicMock(side_effect=Exception("error"))
    settings_mock = MagicMock(libvirt_uri="test_uri")
    logger_mock = MagicMock()

    repo_mock = repo_cls_mock.return_value
    repo_mock.get_job.return_value = MagicMock(lab_snapshot_name="lab-clean")

    res = revert_lab_snapshot(
        session_scope_mock, repo_cls_mock, run_command_mock, MagicMock(), settings_mock, logger_mock, "job1"
    )
    assert res is False
    assert logger_mock.error.called
