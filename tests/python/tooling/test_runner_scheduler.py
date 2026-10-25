"""Installed scheduler wiring without starting workers, signals, or services."""

from __future__ import annotations

import shlex
import subprocess
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from celery import Celery
from celery.bin.worker import worker as worker_command
from celery.worker.components import Beat

ROOT = Path(__file__).resolve().parents[3]


def _worker_options(command, app):
    argv = shlex.split(command)
    with worker_command.make_context(
        "worker", argv[argv.index("worker") + 1 :], obj=SimpleNamespace(app=app)
    ) as context:
        return context.params


@pytest.mark.parametrize("split", [False, True], ids=["operator", "split"])
@pytest.mark.parametrize("custom_storage", [False, True], ids=["default-storage", "custom-storage"])
def test_installed_runner_enables_one_persistent_schedule_producer(tmp_path, monkeypatch, split, custom_storage):
    from astinuslab.jobs.celery_app import celery_app

    install = tmp_path / "install"
    storage = tmp_path / "custom-storage" if custom_storage else install / "storage"
    (storage / "tmp").mkdir(parents=True)
    units = tmp_path / "units"
    units.mkdir()
    # Only render units into this fixture. Any attempted service operation other
    # than the expected, stubbed daemon-reload fails locally.
    result = subprocess.run(
        [
            "bash",
            "-c",
            f"""
        source {shlex.quote(str(ROOT / "ops/deploy/install.sh"))}
        INSTALL_DIR={shlex.quote(str(install))}
        STORAGE_ROOT={shlex.quote(str(storage))}
        RELEASE_DIR={shlex.quote(str(ROOT))}
        SYSTEMD_UNIT_DIR={shlex.quote(str(units))}
        SPLIT_USERS={str(split).lower()}
        API_USER=astinuslab-api
        RUNNER_USER=astinuslab-runner
        ANALYSIS_USER=astinuslab-analysis
        systemctl() {{ [[ "$*" == daemon-reload ]]; }}
        install_systemd_units
        """,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Systemd units installed" in result.stdout
    # Use the product's actual periodic schedule with an isolated Celery app;
    # the publish boundary is mocked and no task body can execute.
    app = Celery("installed-scheduler-fixture", broker="memory://", backend="cache+memory://", set_as_current=False)
    app.conf.update(beat_schedule=deepcopy(celery_app.conf.beat_schedule), timezone="UTC", enable_utc=True)
    publish = Mock()
    monkeypatch.setattr(app, "send_task", publish)
    beat_units = []
    for unit in sorted(units.glob("*.service")):
        content = unit.read_text()
        command = next(
            line.removeprefix("ExecStart=") for line in content.splitlines() if line.startswith("ExecStart=")
        )
        if " worker " not in command:
            continue
        options = _worker_options(command, app)
        assert options["concurrency"] == 1
        assert options["pool"].__module__ == "celery.concurrency.thread"
        if not options["beat"]:
            assert "analysis" in unit.name, "installed runner has no periodic schedule producer"
            continue
        beat_units.append(unit.name)
        assert "runner" in unit.name
        schedule_path = Path(options["schedule_filename"])
        assert schedule_path == storage / "tmp/celerybeat-schedule"
        assert f"ReadWritePaths={storage} " in content
        # The real Beat bootstep accepts the rendered CLI options and threads
        # pool. Construct its service, but never call start() on the process.
        worker = SimpleNamespace(
            app=app, pool_cls=options["pool"], schedule_filename=str(schedule_path), scheduler=None
        )
        step = Beat(worker, beat=options["beat"])
        assert step.enabled
        service = step.create(worker).service
        scheduler = service.scheduler
        try:
            entry = scheduler.schedule["admit-waiting-jobs"]
            assert entry.task == "admit_waiting_jobs"
            entry.last_run_at = app.now() - timedelta(minutes=1)
            # Skip even lazy broker connection creation at the producer boundary.
            scheduler.producer = object()
            scheduler.tick()
            assert publish.call_count == 1
            assert publish.call_args.args[0] == "admit_waiting_jobs"
            last_run = scheduler.schedule["admit-waiting-jobs"].last_run_at
        finally:
            scheduler.close()
        # A restarted scheduler keeps the last-run timestamp and does not
        # immediately republish the already dispatched interval task.
        restored = service.get_scheduler()
        try:
            assert restored.schedule["admit-waiting-jobs"].last_run_at == last_run
            restored.producer = object()
            assert restored.tick() > 0
            assert publish.call_count == 1
        finally:
            restored.close()
        assert list(schedule_path.parent.glob("celerybeat-schedule*"))
        assert not list((install / "current").glob("celerybeat-schedule*"))
    assert len(beat_units) == 1
    app.close()


def test_manual_launcher_embeds_beat_only_in_single_runner():
    # Read the real commands without sourcing the launcher or starting children.
    script = (ROOT / "astinuslab.sh").read_text().replace("\\\n", "")
    runner = script.split('info "Starting Celery worker..."', 1)[1].split("</dev/null", 1)[0]
    analysis = script.split('info "Starting analysis worker (interactive Vol3 queue)..."', 1)[1]
    analysis = analysis[analysis.index("nohup setsid") :].split("</dev/null", 1)[0]
    app = Celery("manual-scheduler-fixture", set_as_current=False)
    try:
        runner_options = _worker_options(runner, app)
        analysis_options = _worker_options(analysis, app)
        assert runner_options["beat"]
        assert runner_options["schedule_filename"] == "$PID_DIR/celerybeat-schedule"
        assert not analysis_options["beat"]
        assert runner_options["concurrency"] == analysis_options["concurrency"] == 1
    finally:
        app.close()
