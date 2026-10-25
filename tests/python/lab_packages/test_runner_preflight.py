from __future__ import annotations

import json
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest


def test_fresh_worker_loader_registers_all_package_tasks(tmp_path):
    code = """
import json
from astinuslab.jobs.celery_main import celery_app
celery_app.loader.import_default_modules()
print(json.dumps(sorted(celery_app.tasks)))
"""
    result = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True, timeout=20)
    tasks = json.loads(result.stdout.splitlines()[-1])
    assert {
        "lab_packages.prepare_assets",
        "lab_packages.verify_clean_run",
        "packages.coordinate_instance",
        "packages.coordinate_instance_action",
    } <= set(tasks)


def test_runner_prepares_first_use_storage_without_widening_existing_modes(tmp_path):
    from astinuslab.lab_packages.runner_preflight import initialize_runner_storage

    root = tmp_path / "storage"
    root.mkdir(mode=0o770)
    config = SimpleNamespace(storage_dir=root, playground_disk_dir=root / "playground" / "disks")
    initial = root.stat().st_mode
    initialize_runner_storage(config)
    assert config.playground_disk_dir.is_dir()
    assert config.playground_disk_dir.stat().st_mode & 0o777 == 0o700
    assert root.stat().st_mode == initial
    initialize_runner_storage(config)
    assert config.playground_disk_dir.stat().st_mode & 0o777 == 0o700


def test_runner_storage_initialization_rejects_symlink(tmp_path):
    from astinuslab.lab_packages.runner_preflight import initialize_runner_storage

    root, other = tmp_path / "storage", tmp_path / "other"
    root.mkdir()
    other.mkdir()
    (root / "playground").symlink_to(other, target_is_directory=True)
    with pytest.raises(OSError):
        initialize_runner_storage(SimpleNamespace(storage_dir=root, playground_disk_dir=root / "playground" / "disks"))
    assert list(other.iterdir()) == []


def test_runner_measurement_is_used_without_api_disk_or_kvm_access(tmp_path, monkeypatch):
    from astinuslab.lab_packages import preflight
    from tests.python._support.lab_packages import preflight_documents as _documents

    directory = tmp_path / "api"
    directory.mkdir()
    runner_path = tmp_path / "runner-private"
    host = preflight.HostFacts(4, 8192, 2**40, "x86_64", True, frozenset({"kvm", "x86_64", "offline"}))
    measured = preflight.RunnerHostFacts(host, str(runner_path), directory.stat().st_dev, 2**40, True, time.time())
    monkeypatch.setattr(preflight, "_kvm_openable", lambda: False)
    result = preflight.preflight_package(
        *_documents(), storage_path=directory, playground_disk_path=runner_path, runner=measured
    )
    assert result.ready, result.errors
    assert not runner_path.exists(), "API must not create or probe the runner-owned path"


@pytest.mark.parametrize(
    "change,code",
    [
        ({"kvm": False}, "kvm-unavailable"),
        ({"writable": False}, "storage-not-writable"),
        ({"free": 1}, "insufficient-disk"),
        ({"age": 120}, "runner-host-unavailable"),
        ({"path": "/wrong"}, "runner-host-unavailable"),
    ],
)
def test_runner_failures_remain_blocking(tmp_path, change, code):
    from astinuslab.lab_packages import preflight
    from tests.python._support.lab_packages import preflight_documents as _documents

    host = preflight.HostFacts(
        4, 8192, 2**40, "x86_64", change.get("kvm", True), frozenset({"kvm", "x86_64", "offline"})
    )
    facts = preflight.RunnerHostFacts(
        host,
        change.get("path", str(tmp_path)),
        tmp_path.stat().st_dev,
        change.get("free", 2**40),
        change.get("writable", True),
        time.time() - change.get("age", 0),
    )
    result = preflight.preflight_package(*_documents(), playground_disk_path=tmp_path, runner=facts)
    assert code in {issue.code for issue in result.errors}


@pytest.mark.parametrize("shared_device,insufficient", [(True, True), (False, False)])
def test_runner_disk_and_api_package_bytes_are_combined_per_filesystem(
    tmp_path, monkeypatch, shared_device, insufficient
):
    from astinuslab.lab_packages import preflight
    from tests.python._support.lab_packages import preflight_documents as _documents

    definition, manifest = _documents()
    manifest = manifest.model_copy(
        update={"payloads": (manifest.payloads[0].model_copy(update={"size_bytes": 8 * 1024**3}),)}
    )
    api_path = tmp_path / "api"
    api_path.mkdir()
    runner_path = tmp_path / "runner"
    host = preflight.HostFacts(4, 8192, 2**40, "x86_64", True)
    device = api_path.stat().st_dev
    facts = preflight.RunnerHostFacts(
        host, str(runner_path), device if shared_device else device + 1, 14 * 1024**3, True, time.time()
    )
    monkeypatch.setattr(preflight.shutil, "disk_usage", lambda _path: SimpleNamespace(free=15 * 1024**3))
    result = preflight.preflight_package(
        definition, manifest, storage_path=api_path, playground_disk_path=runner_path, runner=facts
    )
    assert ("insufficient-disk" in {issue.code for issue in result.errors}) is insufficient


@pytest.mark.parametrize("response", [None, {}, {"host": {}}, {"host": {"cpu_threads": True}}])
def test_invalid_runner_reply_is_rejected(response):
    from astinuslab.lab_packages import runner_preflight

    with pytest.raises((TypeError, ValueError)):
        runner_preflight._decode(response)


def test_final_host_gate_refuses_newly_unavailable_runner(monkeypatch):
    from astinuslab.core import fake_mode
    from astinuslab.lab_packages import runner_preflight

    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    monkeypatch.setattr(
        runner_preflight, "measure_runner_host", lambda: runner_preflight.unavailable_runner_facts("lost KVM")
    )
    with pytest.raises(RuntimeError, match="runner KVM"):
        runner_preflight.require_runner_host_ready()


def test_runner_probe_crosses_real_sqlite_queue_and_result_backend(tmp_path):
    code = r"""
import json
from pathlib import Path
from celery.contrib.testing.worker import start_worker
from astinuslab.lab_packages import runner_preflight
app = runner_preflight.celery_app
root = Path(__import__('sys').argv[1])
app.conf.update(broker_url='sqla+sqlite:///' + str(root / 'broker.db'), result_backend='db+sqlite:///' + str(root / 'results.db'), task_always_eager=False, worker_send_task_events=False)
runner_preflight.initialize_runner_storage()
# Transport is real; the runner's libvirt service is a controlled capability
# fixture so this test never depends on the developer host's daemon.
runner_preflight._check_libvirt_reachability = lambda issues: None
with start_worker(app, pool='solo', concurrency=1, perform_ping_check=False, shutdown_timeout=10, loglevel='ERROR'):
    result = runner_preflight.request_runner_facts()
    assert result.error is None, result.error
    assert result.disk_path == str(root / 'storage' / 'playground' / 'disks')
    assert result.writable is True
    assert result.libvirt_available is True
    assert result.host.cpu_threads > 0
    print(json.dumps({'measured_at':result.measured_at, 'disk_path':result.disk_path, 'kvm_available':result.host.kvm_available, 'device':result.device}))
"""
    import os

    root = tmp_path
    (root / "storage").mkdir()
    env = os.environ | {
        "ASTINUSLAB_DATA_DIR": str(root),
        "ASTINUSLAB_STORAGE_DIR": str(root / "storage"),
        "ASTINUSLAB_PLAYGROUND_DISK_DIR": str(root / "storage" / "playground" / "disks"),
        "ASTINUSLAB_FAKE_LIBVIRT": "0",
        "ASTINUSLAB_FAKE_PLAYGROUND": "0",
    }
    result = subprocess.run(
        [sys.executable, "-c", code, str(root)], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.splitlines()[-1])
    assert report["disk_path"] == str(root / "storage" / "playground" / "disks")
    assert (root / "broker.db").is_file()
    assert (root / "results.db").is_file()


def test_api_waits_for_runner_before_launch_writer_lock_and_reuses_result(tmp_path, monkeypatch):
    import sqlite3

    from astinuslab.lab_packages import api, launch_service, preflight
    from astinuslab.lab_packages.archive import write_archive
    from astinuslab.lab_packages.content_store import ContentStore
    from astinuslab.lab_packages.package_service import LabPackageService
    from tests.python._support.lab_packages import file_session_scope as _file_session_scope
    from tests.python._support.lab_packages import launch_definition as _definition

    database = tmp_path / "app.db"
    scope = _file_session_scope(database)
    archive = tmp_path / "test.astinuslab"
    archive.write_bytes(write_archive(_definition(), "# test"))
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    monkeypatch.setattr(api, "default_package_service", lambda: service)
    monkeypatch.setattr(preflight, "_check_definition_runtime", lambda *_args: None)
    monkeypatch.setattr(preflight, "_kvm_openable", lambda: False)
    monkeypatch.setattr(launch_service, "lookup_playground_image", lambda *_args, **_kwargs: {"path": str(archive)})
    monkeypatch.setattr(
        launch_service, "validate_playground_workload_selection", lambda **_kwargs: SimpleNamespace(ok=True)
    )
    runner_path = tmp_path / "runner-private-disks"
    config = api.settings.model_copy(update={"playground_disk_dir": runner_path})
    monkeypatch.setattr(api, "settings", config)
    monkeypatch.setattr(launch_service, "settings", config)
    calls = []

    def measured():
        # An independent SQLite writer would fail here if the API retained
        # the launch writer mutex while waiting for the single runner.
        with sqlite3.connect(database, timeout=0.01) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()
        calls.append(True)
        return preflight.RunnerHostFacts(
            preflight.HostFacts(4, 8192, 2**40, "x86_64", True),
            str(runner_path),
            tmp_path.stat().st_dev,
            2**40,
            True,
            time.time(),
        )

    monkeypatch.setattr(api, "request_runner_facts", measured)
    with scope() as session:
        record = service.import_path(archive, session).record
        record_id = record.id
    with scope() as session:
        preview = api.preflight_lab_package(record_id, session)
        assert preview.ready
    with scope() as session:
        result = api.launch_lab_package(
            record_id,
            api.LabPackageLaunchRequest(
                idempotency_key="runner-proof", acknowledged_warning_token=preview.warning_token
            ),
            session,
        )
        assert result.state == "registered"
    assert len(calls) == 2, "transactional repeat must reuse current internal facts, never wait for another task"
    assert not runner_path.exists(), "API launch must leave runner-owned storage initialization to runner"


def test_api_writability_is_still_checked_when_roles_share_a_path(tmp_path, monkeypatch):
    from astinuslab.lab_packages import preflight
    from tests.python._support.lab_packages import preflight_documents as _documents

    facts = preflight.RunnerHostFacts(
        preflight.HostFacts(4, 8192, 2**40, "x86_64", True),
        str(tmp_path),
        tmp_path.stat().st_dev,
        2**40,
        True,
        time.time(),
    )
    monkeypatch.setattr(preflight, "_directory_writable", lambda _path: False)
    result = preflight.preflight_package(
        *_documents(), storage_path=tmp_path, playground_disk_path=tmp_path, runner=facts
    )
    assert "storage-not-writable" in {issue.code for issue in result.errors}


@pytest.mark.parametrize("locked_database", ["broker", "results"])
def test_locked_sqlite_probe_obeys_wall_clock_budget(tmp_path, locked_database):
    import os

    code = r"""
import json, sqlite3, sys, time
from pathlib import Path
from astinuslab.lab_packages import runner_preflight
root = Path(sys.argv[1]); locked = sys.argv[2]
app = runner_preflight.celery_app
app.conf.update(broker_url='sqla+sqlite:///' + str(root/'broker.db'), result_backend='db+sqlite:///' + str(root/'results.db'))
# Prime the normal long-timeout Kombu cache before the fault. The probe must
# avoid that entry, not merely set an option on an already cached engine.
with app.connection_for_write() as connection:
    connection.default_channel.queue_declare(queue='celery')
with sqlite3.connect(root/'results.db') as connection:
    connection.execute('CREATE TABLE celery_taskmeta (task_id TEXT,status TEXT,result BLOB)')
runner_preflight._PROBE_SECONDS = 0.3
with sqlite3.connect(root/(locked+'.db')) as connection:
    connection.execute('BEGIN EXCLUSIVE')
    start=time.monotonic()
    result=runner_preflight.request_runner_facts()
    elapsed=time.monotonic()-start
    connection.rollback()
assert result.error
assert elapsed < 1.5, elapsed
print(json.dumps({'locked':locked,'elapsed':elapsed,'error':result.error}))
"""
    env = os.environ | {
        "ASTINUSLAB_DATA_DIR": str(tmp_path),
        "ASTINUSLAB_FAKE_LIBVIRT": "0",
        "ASTINUSLAB_FAKE_PLAYGROUND": "0",
    }
    (tmp_path / "storage").mkdir()
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), locked_database],
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.splitlines()[-1])
    assert report["elapsed"] < 1.5


@pytest.mark.parametrize("available", [True, False])
def test_full_preflight_uses_runner_libvirt_without_api_connection(tmp_path, monkeypatch, available):
    from astinuslab.lab_packages import preflight
    from astinuslab.playground import orchestrator
    from tests.python._support.lab_packages import file_session_scope as _file_session_scope
    from tests.python._support.lab_packages import preflight_documents as _documents

    def api_libvirt_denied(_uri):
        pytest.fail("restricted API must not connect to libvirt when runner facts are supplied")

    monkeypatch.setattr(orchestrator, "libvirt", SimpleNamespace(open=api_libvirt_denied))
    monkeypatch.setattr(orchestrator, "lookup_playground_image", lambda *_args, **_kwargs: {"id": "ubuntu-24.04"})
    facts = preflight.RunnerHostFacts(
        preflight.HostFacts(4, 8192, 2**40, "x86_64", True),
        str(tmp_path),
        tmp_path.stat().st_dev,
        2**40,
        True,
        time.time(),
        libvirt_available=available,
    )
    with _file_session_scope(tmp_path / "app.db")() as session:
        result = preflight.preflight_package(
            *_documents(), session=session, playground_disk_path=tmp_path, runner=facts
        )
    assert result.ready is available, result.errors
    assert ("libvirt-unreachable" in {issue.code for issue in result.errors}) is not available


@pytest.mark.parametrize("outcome", ["available", "missing-library", "no-connection", "denied"])
def test_runner_measures_libvirt_and_final_gate_fails_closed(tmp_path, monkeypatch, outcome):
    from astinuslab.core import fake_mode
    from astinuslab.lab_packages import preflight, runner_preflight
    from astinuslab.playground import orchestrator

    calls = []

    def capabilities():
        calls.append("capabilities")
        if outcome == "denied":
            raise PermissionError("runner libvirt denied")
        return "<capabilities/>"

    def connect(uri):
        calls.append(uri)
        if outcome == "no-connection":
            return None
        return SimpleNamespace(getCapabilities=capabilities, close=lambda: calls.append("closed"))

    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    monkeypatch.setattr(
        orchestrator, "libvirt", None if outcome == "missing-library" else SimpleNamespace(open=connect)
    )
    monkeypatch.setattr(
        runner_preflight, "settings", runner_preflight.settings.model_copy(update={"playground_disk_dir": tmp_path})
    )
    monkeypatch.setattr(
        runner_preflight, "measure_host", lambda _path: preflight.HostFacts(4, 8192, 2**40, "x86_64", True)
    )
    facts = runner_preflight.measure_runner_host()
    assert facts.libvirt_available is (outcome == "available")
    if outcome in {"available", "denied"}:
        assert calls[-1] == "closed", "failed capabilities inspection must still close the connection"
    if outcome == "available":
        runner_preflight.require_runner_host_ready()
    else:
        with pytest.raises(RuntimeError, match="libvirt"):
            runner_preflight.require_runner_host_ready()


def test_runner_payload_requires_explicit_libvirt_measurement():
    from dataclasses import asdict

    from astinuslab.lab_packages import runner_preflight

    payload = asdict(runner_preflight.unavailable_runner_facts("offline"))
    payload["host"]["capabilities"] = []
    payload.pop("libvirt_available", None)
    with pytest.raises(ValueError, match="libvirt"):
        runner_preflight._decode(payload)


@pytest.fixture
def probe_database(tmp_path, monkeypatch):
    """Real isolated broker/backend databases, with a known request identity."""
    from celery import Celery

    from astinuslab.core import fake_mode
    from astinuslab.lab_packages import runner_preflight

    app = Celery("probe-contract", set_as_current=False)
    app.conf.update(
        broker_url=f"sqla+sqlite:///{tmp_path / 'broker.db'}",
        result_backend=f"db+sqlite:///{tmp_path / 'results.db'}",
        task_default_queue="probe-contract",
    )
    monkeypatch.setattr(runner_preflight, "celery_app", app)
    monkeypatch.setattr(runner_preflight, "uuid4", lambda: "probe-contract-id")
    monkeypatch.setattr(runner_preflight, "_PROBE_SECONDS", 1.0)
    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    payload = {
        "host": {
            "cpu_threads": 4,
            "memory_mb": 8192,
            "free_disk_bytes": 2**40,
            "architecture": "x86_64",
            "kvm_available": True,
            "capabilities": ["kvm", "offline", "x86_64"],
        },
        "disk_path": str(tmp_path),
        "device": tmp_path.stat().st_dev,
        "free_bytes": 2**40,
        "writable": True,
        "measured_at": time.time(),
        "libvirt_available": True,
    }
    try:
        yield app, payload
    finally:
        app.close()


def test_probe_publishes_request_and_reads_only_its_result(probe_database, tmp_path):
    import sqlite3
    from contextlib import closing

    from astinuslab.lab_packages import runner_preflight

    app, payload = probe_database
    app.backend.store_result("another-request", {"unrelated": True}, "SUCCESS")
    app.backend.store_result("probe-contract-id", payload, "SUCCESS")

    measured = runner_preflight.request_runner_facts()

    assert measured.error is None
    assert measured.host.cpu_threads == 4
    assert measured.host.capabilities == frozenset({"kvm", "offline", "x86_64"})
    assert measured.current_for(tmp_path)
    with closing(sqlite3.connect(tmp_path / "broker.db")) as connection:
        messages = connection.execute("SELECT payload FROM kombu_message").fetchall()
    assert len(messages) == 1
    headers = json.loads(messages[0][0])["headers"]
    assert headers["id"] == "probe-contract-id"
    assert headers["task"] == "lab_packages.probe_runner_host"


@pytest.mark.parametrize("status", ["PENDING", "STARTED", "RETRY", "FAILURE"])
def test_probe_never_treats_an_unfinished_or_failed_result_as_ready(probe_database, status):
    from astinuslab.lab_packages import runner_preflight

    app, payload = probe_database
    value = RuntimeError("runner unavailable") if status == "FAILURE" else payload
    app.backend.store_result("probe-contract-id", value, status)

    result = runner_preflight.request_runner_facts()

    assert result.error == "runner probe failed or timed out"
    assert not result.writable and not result.host.kvm_available


def test_probe_missing_backend_table_returns_unavailable_within_budget(probe_database, monkeypatch):
    from astinuslab.lab_packages import runner_preflight

    monkeypatch.setattr(runner_preflight, "_PROBE_SECONDS", 0.05)
    started = time.monotonic()
    result = runner_preflight.request_runner_facts()

    assert result.error == "runner probe failed or timed out"
    assert time.monotonic() - started < 1.0


@pytest.mark.parametrize(
    "section,field,value",
    [
        ("host", "cpu_threads", True),
        ("host", "memory_mb", -1),
        ("host", "kvm_available", "yes"),
        ("host", "capabilities", ["kvm", 123]),
        (None, "device", -1),
        (None, "writable", "yes"),
        (None, "measured_at", "now"),
        (None, "libvirt_available", None),
    ],
)
def test_probe_rejects_malformed_success_payload(probe_database, section, field, value):
    from astinuslab.lab_packages import runner_preflight

    app, payload = probe_database
    target = payload[section] if section else payload
    target[field] = value
    app.backend.store_result("probe-contract-id", payload, "SUCCESS")

    result = runner_preflight.request_runner_facts()

    assert result.error == "runner probe failed or timed out"
    assert not result.writable and not result.host.kvm_available


@pytest.mark.parametrize("url", ["redis://localhost/0", "sqla+postgresql://localhost/database"])
def test_probe_refuses_unsupported_broker_without_connecting(probe_database, url):
    from astinuslab.lab_packages import runner_preflight

    app, _ = probe_database
    app.conf.broker_url = url

    result = runner_preflight.request_runner_facts()

    assert result.error == "runner probe failed or timed out"


def test_runner_probe_task_serializes_capabilities_as_sorted_json(monkeypatch, tmp_path):
    from astinuslab.lab_packages import preflight, runner_preflight

    facts = preflight.RunnerHostFacts(
        preflight.HostFacts(4, 8192, 2**40, "x86_64", True, frozenset({"offline", "kvm"})),
        str(tmp_path),
        tmp_path.stat().st_dev,
        2**40,
        True,
        time.time(),
        libvirt_available=True,
    )
    monkeypatch.setattr(runner_preflight, "measure_runner_host", lambda: facts)

    payload = runner_preflight.probe_runner_host_task.run()

    assert json.loads(json.dumps(payload))["host"]["capabilities"] == ["kvm", "offline"]
    assert runner_preflight._decode(payload) == facts


def test_missing_runner_storage_is_reported_as_unavailable(tmp_path, monkeypatch):
    from astinuslab.lab_packages import runner_preflight

    config = runner_preflight.settings.model_copy(update={"playground_disk_dir": tmp_path / "missing"})
    monkeypatch.setattr(runner_preflight, "settings", config)

    facts = runner_preflight.measure_runner_host()

    assert facts.error == "runner storage unavailable"
    assert not facts.writable and not facts.host.kvm_available


def test_external_runner_storage_requires_an_existing_real_directory(tmp_path):
    from astinuslab.lab_packages.runner_preflight import initialize_runner_storage

    external = tmp_path / "external"
    config = SimpleNamespace(storage_dir=tmp_path / "storage", playground_disk_dir=external)
    with pytest.raises(OSError, match="must be provisioned"):
        initialize_runner_storage(config)
    assert not external.exists()
    external.mkdir(mode=0o750)
    initialize_runner_storage(config)
    assert external.stat().st_mode & 0o777 == 0o750
    link = tmp_path / "link"
    link.symlink_to(external, target_is_directory=True)
    config.playground_disk_dir = link
    with pytest.raises(OSError, match="must be provisioned"):
        initialize_runner_storage(config)
