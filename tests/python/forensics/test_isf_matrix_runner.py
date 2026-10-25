from __future__ import annotations

import logging
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from astinuslab.api.schemas import JobCreate
from astinuslab.db.repositories import JobRepository
from tests.python._support.helpers import autospec_setattr


def test_discover_matrix_targets_filters_to_enabled_bootable_unique_images():
    from astinuslab.orchestration.runners.isf_matrix_runner import discover_matrix_targets

    targets = [
        {
            "id": "ubuntu",
            "enabled": True,
            "image_available": True,
            "supports_cloud_image": True,
            "image_ref": "ubuntu.qcow2",
            "distro": "ubuntu",
            "version": "24.04",
            "display_name": "Ubuntu",
        },
        {
            "id": "ubuntu-duplicate",
            "enabled": True,
            "image_available": True,
            "supports_cloud_image": True,
            "image_ref": "ubuntu.qcow2",
            "distro": "ubuntu",
            "version": "24.04",
        },
        {
            "id": "iso",
            "enabled": True,
            "image_available": True,
            "supports_cloud_image": False,
            "image_ref": "installer.iso",
            "distro": "ubuntu",
            "version": "24.04",
        },
        {
            "id": "disabled",
            "enabled": False,
            "image_available": True,
            "supports_cloud_image": True,
            "image_ref": "disabled.qcow2",
            "distro": "debian",
            "version": "12",
        },
    ]

    planned = discover_matrix_targets(targets)

    assert [target.target_id for target in planned] == ["ubuntu"]
    assert planned[0].image_ref == "ubuntu.qcow2"
    assert planned[0].distro == "ubuntu"


def test_find_active_work_reports_non_terminal_jobs_and_actions(in_memory_session_scope):
    from astinuslab.orchestration.runners.isf_matrix_runner import find_active_work

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
            )
        )
        repo.enqueue_action(job.id, "run_immediate_job", {})
        job_id = job.id

    active = find_active_work(in_memory_session_scope)

    assert active
    assert any(item["kind"] == "job" and item["id"] == job_id for item in active)
    assert any(item["kind"] == "action" and item["job_id"] == job_id for item in active)


def test_check_resources_rejects_low_memory():
    from astinuslab.orchestration.runners.isf_matrix_runner import MatrixRunConfig, ResourceSnapshot, check_resources

    config = MatrixRunConfig(vm_memory_mb=4096, reserve_memory_mb=1024)
    snapshot = ResourceSnapshot(available_memory_mb=4095, free_storage_gb=100)

    with pytest.raises(RuntimeError, match="available memory"):
        check_resources(config, snapshot)


def test_check_resources_rejects_low_storage():
    from astinuslab.orchestration.runners.isf_matrix_runner import MatrixRunConfig, ResourceSnapshot, check_resources

    config = MatrixRunConfig(min_free_storage_gb=20)
    snapshot = ResourceSnapshot(available_memory_mb=16384, free_storage_gb=19)

    with pytest.raises(RuntimeError, match="free storage"):
        check_resources(config, snapshot)


def test_target_result_serializes_for_summary():
    from astinuslab.orchestration.runners.isf_matrix_runner import MatrixTarget, TargetResult

    target = MatrixTarget(
        target_id="ubuntu",
        image_ref="ubuntu.qcow2",
        distro="ubuntu",
        version="24.04",
        display_name="Ubuntu",
    )
    result = TargetResult(
        target=target,
        status="passed",
        job_id="job-123",
        started_at=datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
        finished_at=datetime(2026, 5, 4, 12, 5, tzinfo=UTC),
        summary="ok",
    )

    payload = result.to_dict()

    assert payload["target_id"] == "ubuntu"
    assert payload["image_ref"] == "ubuntu.qcow2"
    assert payload["status"] == "passed"
    assert payload["duration_seconds"] == 300.0


def test_current_resource_snapshot_reads_meminfo(monkeypatch, tmp_path):
    from unittest.mock import mock_open, patch

    from astinuslab.orchestration.runners.isf_matrix_runner import current_resource_snapshot

    mocked_meminfo = "MemTotal:       16000000 kB\nMemAvailable:    8388608 kB\n"

    with patch("builtins.open", mock_open(read_data=mocked_meminfo)):
        import shutil

        autospec_setattr(monkeypatch, shutil, "disk_usage", lambda _: type("Usage", (), {"free": 100 * (1024**3)})())

        snapshot = current_resource_snapshot(tmp_path)
        assert snapshot.available_memory_mb == 8192
        assert snapshot.free_storage_gb == 100.0


def test_current_resource_snapshot_falls_back_to_sysconf(monkeypatch, tmp_path):
    import os
    import shutil

    from astinuslab.orchestration.runners.isf_matrix_runner import current_resource_snapshot

    class MockOpenFails:
        def __init__(self, path, *args, **kwargs):
            if path == "/proc/meminfo":
                raise FileNotFoundError()
            self.file = open(path, *args, **kwargs)

        def __enter__(self):
            return self.file.__enter__()

        def __exit__(self, *args):
            return self.file.__exit__(*args)

    import builtins

    monkeypatch.setattr(builtins, "open", MockOpenFails)

    autospec_setattr(monkeypatch, shutil, "disk_usage", lambda _: type("Usage", (), {"free": 50 * (1024**3)})())

    def mock_sysconf(name):
        if name == "SC_AVPHYS_PAGES":
            return 1024
        if name == "SC_PAGE_SIZE":
            return 4096
        raise ValueError()

    monkeypatch.setattr(os, "sysconf", mock_sysconf)

    snapshot = current_resource_snapshot(tmp_path)
    assert snapshot.available_memory_mb == 4  # (1024 * 4096) / (1024 * 1024)
    assert snapshot.free_storage_gb == 50.0


def test_run_target_in_child_marks_failed_and_cleans_up_on_timeout(monkeypatch):
    import astinuslab.orchestration.runners.isf_matrix_runner as runner

    calls: list[str] = []

    class FakeProcess:
        exitcode = None

        def start(self):
            calls.append("start")

        def join(self, timeout=None):
            calls.append(f"join:{timeout}")

        def is_alive(self):
            return calls.count("alive") == 0

        def terminate(self):
            calls.append("terminate")

        def kill(self):
            calls.append("kill")

    def fake_is_alive(self):
        calls.append("alive")
        return len([call for call in calls if call == "alive"]) == 1

    monkeypatch.setattr(FakeProcess, "is_alive", fake_is_alive, raising=False)
    autospec_setattr(monkeypatch, runner.multiprocessing, "Process", lambda *a, **k: FakeProcess())
    autospec_setattr(
        monkeypatch,
        runner,
        "_mark_job_failed",
        lambda job_id, message, scope=None: calls.append(f"failed:{job_id}:{message}"),
    )
    autospec_setattr(monkeypatch, runner, "_cleanup_timed_out_job", lambda job_id: calls.append(f"cleanup:{job_id}"))

    exit_code = runner.run_target_in_child("job-timeout", 7, timeout_seconds=1)

    assert exit_code is None
    assert "terminate" in calls
    assert any(call.startswith("failed:job-timeout:") for call in calls)
    assert "cleanup:job-timeout" in calls


def test_write_junit_xml_generates_correct_format(tmp_path):
    import xml.etree.ElementTree as ET

    from astinuslab.orchestration.runners.isf_matrix_runner import MatrixTarget, TargetResult, write_junit_xml

    target = MatrixTarget("ubuntu", "img", "ubuntu", "24.04", "Ubuntu")
    result = TargetResult(
        target=target,
        status="failed",
        job_id="job-123",
        started_at=datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
        finished_at=datetime(2026, 5, 4, 12, 5, tzinfo=UTC),
        summary="timeout",
        error="process killed",
    )

    write_junit_xml(tmp_path, [result])

    xml_path = tmp_path / "junit.xml"
    assert xml_path.exists()

    tree = ET.parse(xml_path)
    root = tree.getroot()
    assert root.tag == "testsuite"
    assert root.attrib["failures"] == "1"
    assert root.attrib["tests"] == "1"

    testcase = root.find("testcase")
    assert testcase is not None
    assert testcase.attrib["classname"] == "isf_matrix.ubuntu"
    assert testcase.attrib["name"] == "ubuntu"
    assert testcase.attrib["time"] == "300.000"

    failure = testcase.find("failure")
    assert failure is not None
    assert failure.attrib["message"] == "timeout"
    assert failure.text == "process killed"


def test_run_matrix_resumes_from_previous_dir(monkeypatch, tmp_path):
    import json

    import astinuslab.orchestration.runners.isf_matrix_runner as runner
    from astinuslab.orchestration.runners.isf_matrix_runner import MatrixRunConfig, MatrixTarget, run_matrix

    autospec_setattr(monkeypatch, runner, "check_host_prerequisites", lambda uri: {})

    resume_dir = tmp_path / "resume"
    resume_dir.mkdir()
    summary_data = [
        {"target_id": "ubuntu-passed", "status": "passed"},
        {"target_id": "debian-failed", "status": "failed"},
    ]
    (resume_dir / "summary.json").write_text(json.dumps(summary_data), encoding="utf-8")

    config = MatrixRunConfig(dry_run=True, resume_dir=resume_dir, run_dir=tmp_path / "run")

    def mock_discover(*args, **kwargs):
        return [
            MatrixTarget("ubuntu-passed", "img", "dist", "ver", "name"),
            MatrixTarget("debian-failed", "img", "dist", "ver", "name"),
            MatrixTarget("new-target", "img", "dist", "ver", "name"),
        ]

    monkeypatch.setattr(runner, "discover_matrix_targets", mock_discover)
    autospec_setattr(monkeypatch, runner, "find_active_work", lambda *a: [])
    autospec_setattr(monkeypatch, runner, "check_resources", lambda *a, **k: None)

    # Dry run returns empty list, but the target filtering happens before.
    # Actually dry_run skips execution. Let's capture the planned targets instead.
    planned_targets = []

    def mock_write_json(path, data):
        if "targets.json" in str(path):
            planned_targets.extend(data)

    monkeypatch.setattr(runner, "_write_json", mock_write_json)

    run_matrix(config, scope=lambda: None)

    assert len(planned_targets) == 2
    target_ids = [t["target_id"] for t in planned_targets]
    assert "ubuntu-passed" not in target_ids
    assert "debian-failed" in target_ids
    assert "new-target" in target_ids


def test_run_matrix_cleans_up_on_keyboard_interrupt(monkeypatch, tmp_path):

    import astinuslab.orchestration.runners.isf_matrix_runner as runner
    from astinuslab.orchestration.runners.isf_matrix_runner import MatrixRunConfig, run_matrix

    autospec_setattr(monkeypatch, runner, "check_host_prerequisites", lambda uri: {})

    config = MatrixRunConfig(dry_run=False, force=True, target_ids=("test-target",), run_dir=tmp_path)

    def mock_discover(*args, **kwargs):
        return [runner.MatrixTarget("test-target", "img", "dist", "ver", "name")]

    monkeypatch.setattr(runner, "discover_matrix_targets", mock_discover)
    autospec_setattr(monkeypatch, runner, "find_active_work", lambda *a: [])
    autospec_setattr(monkeypatch, runner, "check_resources", lambda *a, **k: None)

    calls = []

    def mock_create(*args, **kwargs):
        return ("job-interrupted", 1)

    def mock_run_child(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(runner, "create_isf_only_job", mock_create)
    monkeypatch.setattr(runner, "run_target_in_child", mock_run_child)
    autospec_setattr(
        monkeypatch, runner, "_mark_job_failed", lambda job_id, message, scope=None: calls.append(f"failed:{job_id}")
    )
    autospec_setattr(monkeypatch, runner, "_cleanup_timed_out_job", lambda job_id: calls.append(f"cleanup:{job_id}"))

    import pytest

    with pytest.raises(KeyboardInterrupt):
        run_matrix(config, scope=lambda: None)

    assert "failed:job-interrupted" in calls
    assert "cleanup:job-interrupted" in calls


def test_phase_c_smoke_outputs_three_scenarios():
    from astinuslab.orchestration.runners.isf_matrix_runner import MatrixTarget, run_phase_c_smoke

    table = run_phase_c_smoke(
        [
            MatrixTarget("ubuntu-22-04", "ubuntu.img", "ubuntu", "22.04"),
        ]
    )

    assert "cache_hit" in table
    assert "prebuilt_miss_debuginfod" in table
    assert "all_providers_fail" in table
    assert "| ubuntu-22-04 |" in table


def test_phase_c_smoke_simulate_all_fail_reports_five_attempts():
    from astinuslab.orchestration.runners.isf_matrix_runner import MatrixTarget, run_phase_c_smoke

    table = run_phase_c_smoke(
        [
            MatrixTarget("ubuntu-22-04", "ubuntu.img", "ubuntu", "22.04"),
        ],
        simulate_all_fail=True,
    )

    assert "all_providers_fail" in table
    assert "attempts=5" in table


def test_phase_c_cli_accepts_targets_and_simulate_all_fail(capsys):
    import astinuslab.orchestration.runners.isf_matrix_runner as runner

    rc = runner.main(["--phase=c", "--targets=ubuntu-22-04", "--simulate-all-fail"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "ubuntu-22-04" in out
    assert "all_providers_fail" in out


def test_configure_run_logging_closes_replaced_log_file(tmp_path):
    from astinuslab.orchestration.runners.isf_matrix_runner import configure_run_logging

    logger = logging.Logger("matrix-test")
    first_handler = None
    try:
        with patch("logging.getLogger", return_value=logger):
            configure_run_logging(tmp_path / "first")
            first_handler = logger.handlers[0]
            logger.info("first run")
            configure_run_logging(tmp_path / "second")
            logger.info("second run")

        assert isinstance(first_handler, logging.FileHandler)
        assert first_handler.stream is None
        assert "first run" in (tmp_path / "first/runner.log").read_text()
        assert "second run" not in (tmp_path / "first/runner.log").read_text()
        assert "second run" in (tmp_path / "second/runner.log").read_text()
    finally:
        for handler in logger.handlers:
            handler.close()
        if first_handler is not None:
            first_handler.close()
