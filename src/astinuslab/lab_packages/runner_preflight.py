"""Package host checks executed in the runner's existing trust domain."""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from kombu import Connection
from sqlalchemy import Column, MetaData, PickleType, String, Table, create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from ..core.config import settings
from ..jobs.celery_app import celery_app
from .preflight import (
    HostFacts,
    PreflightIssue,
    RunnerHostFacts,
    _check_libvirt_reachability,
    _directory_writable,
    measure_host,
)


def initialize_runner_storage(config=settings) -> None:
    """Create first-use disk directories without following links or widening modes."""
    root = Path(config.storage_dir)
    path = Path(config.playground_disk_dir)
    try:
        relative = path.relative_to(root)
    except ValueError:
        # Independently configured external storage must already be provisioned.
        if not path.is_dir() or path.is_symlink():
            raise OSError("configured external Playground disk root must be provisioned") from None
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(root, flags)
    try:
        for name in relative.parts:
            if name in {".", ".."}:
                raise OSError("invalid configured Playground disk path")
            try:
                os.mkdir(name, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(name, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    finally:
        os.close(descriptor)


def unavailable_runner_facts(error: str) -> RunnerHostFacts:
    return RunnerHostFacts(
        HostFacts(0, 0, 0, "unknown", False), str(settings.playground_disk_dir), 0, 0, False, time.time(), error
    )


def measure_runner_host() -> RunnerHostFacts:
    path = Path(settings.playground_disk_dir)
    try:
        inspected = path.stat()
        host = measure_host(path)
        libvirt_issues: list[PreflightIssue] = []
        _check_libvirt_reachability(libvirt_issues)
        return RunnerHostFacts(
            host,
            str(path),
            inspected.st_dev,
            shutil.disk_usage(path).free,
            _directory_writable(path),
            time.time(),
            libvirt_available=not libvirt_issues,
        )
    except OSError:
        return unavailable_runner_facts("runner storage unavailable")


@celery_app.task(name="lab_packages.probe_runner_host")
def probe_runner_host_task() -> dict:
    measured = measure_runner_host()
    payload = asdict(measured)
    payload["host"]["capabilities"] = sorted(measured.host.capabilities)
    return payload


def _decode(payload: object) -> RunnerHostFacts:
    if not isinstance(payload, dict) or not isinstance(payload.get("host"), dict):
        raise ValueError("invalid runner payload")
    host = payload["host"].copy()
    for name in ("cpu_threads", "memory_mb", "free_disk_bytes"):
        if type(host.get(name)) is not int or host[name] < 0:
            raise ValueError("invalid host count")
    if type(host.get("kvm_available")) is not bool or not isinstance(host.get("architecture"), str):
        raise ValueError("invalid host capability")
    caps = host.get("capabilities")
    if not isinstance(caps, list) or not all(isinstance(value, str) for value in caps):
        raise ValueError("invalid capabilities")
    host["capabilities"] = frozenset(caps)
    for name in ("device", "free_bytes"):
        if type(payload.get(name)) is not int or payload[name] < 0:
            raise ValueError("invalid storage count")
    if not isinstance(payload.get("disk_path"), str) or type(payload.get("writable")) is not bool:
        raise ValueError("invalid disk facts")
    if type(payload.get("measured_at")) not in (int, float):
        raise ValueError("invalid timestamp")
    if type(payload.get("libvirt_available")) is not bool:
        raise ValueError("invalid libvirt measurement")
    return RunnerHostFacts(**(payload | {"host": HostFacts(**host)}))


_PROBE_SECONDS = 5.0
_SQLITE_BUSY_SECONDS = 0.1


def _sqlite_url(value: str, prefix: str):
    if not isinstance(value, str) or not value.startswith(prefix):
        raise ValueError("runner probe requires the configured SQLite transport/backend")
    url = make_url(value.removeprefix(prefix))
    if url.get_backend_name() != "sqlite":
        raise ValueError("runner probe requires SQLite")
    return url


def _publish_probe_request() -> str:
    # A distinct URL query gives this request its own short-timeout Kombu
    # engine cache entry, even if the API already used the broker elsewhere.
    url = _sqlite_url(celery_app.conf.broker_url, "sqla+").update_query_dict({"timeout": str(_SQLITE_BUSY_SECONDS)})
    task_id = str(uuid4())
    name = probe_runner_host_task.name
    message = celery_app.amqp.create_task_message(
        task_id, name, args=(), kwargs={}, expires=datetime.now(UTC) + timedelta(seconds=10)
    )
    with Connection("sqla+" + url.render_as_string(hide_password=False)) as connection:
        producer = connection.Producer()
        # Use Celery's task protocol and default lifecycle queue, but do not
        # initialize its DatabaseBackend in API: setup performs unbounded
        # schema retries before AsyncResult.get's timeout even starts.
        celery_app.amqp.send_task_message(producer, name, message, retry=False)
    return task_id


def _read_probe_result(connection, table, task_id):
    row = connection.execute(select(table.c.status, table.c.result).where(table.c.task_id == task_id)).first()
    if row is None or row.status in {"PENDING", "STARTED", "RETRY"}:
        return None
    if row.status != "SUCCESS":
        raise ValueError("runner probe task did not succeed")
    return _decode(row.result)


def request_runner_facts() -> RunnerHostFacts:
    """Five-second monotonic poll budget plus at most one short SQLite wait.

    Read the existing Celery DatabaseBackend task table through SQLAlchemy's
    same result type. This avoids its retry decorator and schema-setup waits.
    The trusted result database and task protocol remain unchanged.
    """
    from ..core.fake_mode import FAKE_LIBVIRT

    if FAKE_LIBVIRT:
        return unavailable_runner_facts("real runner measurements unavailable in fake mode")
    engine = None
    deadline = time.monotonic() + _PROBE_SECONDS
    try:
        task_id = _publish_probe_request()
        url = _sqlite_url(celery_app.conf.result_backend, "db+")
        engine = create_engine(url, connect_args={"timeout": _SQLITE_BUSY_SECONDS}, poolclass=NullPool)
        names = celery_app.conf.database_table_names or {}
        table = Table(
            names.get("task", "celery_taskmeta"),
            MetaData(),
            Column("task_id", String),
            Column("status", String),
            Column("result", PickleType),
        )
        while time.monotonic() < deadline:
            try:
                with engine.connect() as connection:
                    measured = _read_probe_result(connection, table, task_id)
                if measured is not None:
                    return measured
            except SQLAlchemyError:
                # First-use tables may not exist until the worker initializes
                # its backend. Locked/unavailable databases get the same budget.
                pass
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    except Exception:
        return unavailable_runner_facts("runner probe failed or timed out")
    finally:
        if engine is not None:
            engine.dispose()
    return unavailable_runner_facts("runner probe failed or timed out")


def require_runner_host_ready() -> None:
    """Repeat execution-domain checks before package host mutations."""
    from ..core.fake_mode import FAKE_LIBVIRT

    if FAKE_LIBVIRT:
        return
    facts = measure_runner_host()
    if (
        not facts.current_for(settings.playground_disk_dir)
        or not facts.host.kvm_available
        or not facts.writable
        or not facts.libvirt_available
    ):
        raise RuntimeError("runner KVM, libvirt or Playground storage is unavailable")
