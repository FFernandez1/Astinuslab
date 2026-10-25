from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import textwrap

import pytest
from celery import Celery
from celery.beat import ScheduleEntry, SchedulingError
from celery.utils.imports import symbol_by_name


@pytest.mark.parametrize("backend", ["native", "dumb"])
@pytest.mark.parametrize("umask, expected_mode", [(0o007, 0o640), (0o027, 0o640), (0o077, 0o600)])
def test_schedule_creation_matches_installer_custody_without_changing_umask(tmp_path, backend, umask, expected_mode):
    # Isolate umask from the test runner and exercise real DBM files, including
    # the multi-file fallback. No worker or broker connection is started.
    code = textwrap.dedent("""
        import dbm
        import dbm.dumb
        import os
        import stat
        import sys
        from pathlib import Path
        from celery import Celery
        from astinuslab.jobs.beat_scheduler import RecoveringPersistentScheduler

        root, backend, mask, expected = sys.argv[1:]
        root = Path(root)
        os.umask(int(mask))
        if backend == 'dumb':
            dbm._defaultmod = dbm.dumb
            dbm._modules['dbm.dumb'] = dbm.dumb
        app = Celery('schedule-custody-test', set_as_current=False)
        app.conf.update(beat_schedule={'probe': {'task': 'probe', 'schedule': 60}}, timezone='UTC')
        filename = str(root / 'celerybeat-schedule')
        scheduler = RecoveringPersistentScheduler(app=app, schedule_filename=filename)
        last_run = scheduler.schedule['probe'].last_run_at
        scheduler.close()
        before = {p.name: (p.stat().st_uid, p.stat().st_gid, stat.S_IMODE(p.stat().st_mode))
                  for p in root.glob('celerybeat-schedule*')}
        assert before and all(info[2] == int(expected) for info in before.values()), before
        restored = RecoveringPersistentScheduler(app=app, schedule_filename=filename)
        assert restored.schedule['probe'].last_run_at == last_run
        restored.close()
        after = {p.name: (p.stat().st_uid, p.stat().st_gid, stat.S_IMODE(p.stat().st_mode))
                 for p in root.glob('celerybeat-schedule*')}
        assert after == before
        unrelated = root / 'unrelated'
        unrelated.write_bytes(b'worker umask is unchanged')
        assert stat.S_IMODE(unrelated.stat().st_mode) == 0o666 & ~int(mask)
        app.close()
    """)
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), backend, str(umask), str(expected_mode)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_schedule_creation_creates_missing_scratch_directory(tmp_path):
    # A freshly registered custom storage root has no tmp/ until some other
    # component happens to create it. Beat opened its schedule first, got ENOENT
    # and died for good while the worker kept running, so no waiting Job was
    # ever admitted again.
    code = textwrap.dedent("""
        import os
        import stat
        import sys
        from pathlib import Path
        from celery import Celery
        from astinuslab.jobs.beat_scheduler import RecoveringPersistentScheduler

        root = Path(sys.argv[1])
        os.umask(0o007)
        app = Celery('schedule-scratch-test', set_as_current=False)
        app.conf.update(beat_schedule={'probe': {'task': 'probe', 'schedule': 60}}, timezone='UTC')
        scheduler = RecoveringPersistentScheduler(app=app, schedule_filename=str(root / 'tmp' / 'celerybeat-schedule'))
        assert 'probe' in scheduler.schedule
        scheduler.close()
        app.close()
        assert stat.S_IMODE((root / 'tmp').stat().st_mode) == 0o750, oct((root / 'tmp').stat().st_mode)
    """)
    root = tmp_path / "storage"
    root.mkdir()
    result = subprocess.run([sys.executable, "-c", code, str(root)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_schedule_creation_does_not_fabricate_missing_storage_root(tmp_path):
    from astinuslab.jobs.beat_scheduler import RecoveringPersistentScheduler

    root = tmp_path / "unregistered-root"
    app = Celery("schedule-root-test", set_as_current=False)
    app.conf.update(beat_schedule={}, timezone="UTC")
    try:
        with pytest.raises(OSError):
            RecoveringPersistentScheduler(app=app, schedule_filename=str(root / "tmp" / "celerybeat-schedule"))
    finally:
        app.close()
    assert not root.exists()


@pytest.mark.parametrize("failed_ticks", [1, 2])
def test_beat_recovers_scheduled_publication_after_sqlite_lock(tmp_path, failed_ticks):
    """A failed publish must not strand every subsequent Beat tick on a dead channel."""
    from astinuslab.jobs.celery_app import celery_app

    broker_path = tmp_path / "broker.db"
    app = Celery("beat-recovery-test", broker=f"sqla+sqlite:///{broker_path}", set_as_current=False)
    app.conf.update(
        broker_transport_options={"connect_args": {"timeout": 0.01}},
        task_publish_retry_policy={"max_retries": 1, "interval_start": 0, "interval_step": 0, "interval_max": 0},
        beat_schedule={},
    )
    scheduler = symbol_by_name(celery_app.conf.beat_scheduler)(
        app=app, schedule_filename=str(tmp_path / "beat-schedule")
    )
    entry = ScheduleEntry(name="admission", task="admit_waiting_jobs", schedule=15, app=app)
    try:
        scheduler.apply_async(entry, producer=scheduler.producer)
        with sqlite3.connect(broker_path) as lock:
            lock.execute("BEGIN EXCLUSIVE")
            for _ in range(failed_ticks):
                with pytest.raises(SchedulingError):
                    scheduler.apply_async(entry, producer=scheduler.producer)
            lock.rollback()

        # Exercise successive normal ticks after the lock is gone, using the
        # scheduler's own cached producer exactly as Celery Scheduler.tick does.
        scheduler.apply_async(entry, producer=scheduler.producer)
        scheduler.apply_async(entry, producer=scheduler.producer)
        with sqlite3.connect(broker_path) as connection:
            payloads = [json.loads(row[0]) for row in connection.execute("SELECT payload FROM kombu_message")]
        assert [message["headers"]["task"] for message in payloads] == ["admit_waiting_jobs"] * 3
    finally:
        scheduler.close()
        if "connection" in scheduler.__dict__:
            scheduler.connection.close()
        app.close()
