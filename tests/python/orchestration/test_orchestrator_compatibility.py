from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from tests.python._support.helpers import autospec_setattr


def test_live_session_uses_vertical_package():
    live_session = importlib.import_module("astinuslab.live_session")
    support = importlib.import_module("astinuslab.live_session.support")

    assert hasattr(live_session, "__path__")
    assert live_session.run_live_session_job
    assert callable(support.resolve_live_session_workload_cache)
    assert callable(support.provision_live_session_workload)


def test_live_session_package_preserves_late_binding(monkeypatch):
    live_session = importlib.import_module("astinuslab.live_session")
    runner = importlib.import_module("astinuslab.live_session.runner")
    calls: list[tuple[str, int | None]] = []

    def fake_run(job_id: str, action_id: int | None = None) -> None:
        calls.append((job_id, action_id))

    monkeypatch.setattr(runner, "run_live_session_job", fake_run)

    live_session.run_live_session_job("job-live", action_id=42)

    assert calls == [("job-live", 42)]


def test_celery_live_session_task_uses_package(monkeypatch):
    from inspect import unwrap

    from astinuslab import live_session
    from astinuslab.jobs import celery_app

    calls: list[tuple[str, int | None]] = []
    dispatched: list[str] = []

    def fake_run(job_id: str, action_id: int | None = None) -> None:
        calls.append((job_id, action_id))

    monkeypatch.setattr(live_session, "run_live_session_job", fake_run)
    autospec_setattr(monkeypatch, celery_app, "_dispatch_pending_actions", lambda job_id: dispatched.append(job_id))

    # This checks package late binding only. SQLite claim/reservation behavior
    # is exercised by test_capacity_scheduler and test_action_ownership.
    unwrap(celery_app.run_live_session_job_task.run)("job-celery", action_id=7)

    assert calls == [("job-celery", 7)]
    assert dispatched == ["job-celery"]


def test_runtime_guard_rejects_detected_distro_mismatch():
    from astinuslab.catalog import runtime_workload_context_or_raise as _runtime_workload_context_or_raise
    from astinuslab.core.compatibility import normalize_compatibility

    target = {"id": "mislabelled", "family": "debian", "distro": "ubuntu", "version": "22.04", "disk_gb": 20}
    workload = {
        "id": "wordpress-6.4",
        "display_name": "WordPress 6.4",
        "compatibility": normalize_compatibility(
            {"include": [{"family": "debian", "distro": "ubuntu", "version": "22.04"}]}
        ),
    }
    metadata: dict[str, str | None] = {"distro_id": "fedora", "distro_version": "40", "arch": "x86_64"}

    with pytest.raises(RuntimeError, match="detected guest"):
        _runtime_workload_context_or_raise(
            "job-1", workload, target, metadata, workload_map={"wordpress-6.4": workload}
        )


def test_runtime_guard_accepts_matching_detected_distro():
    from astinuslab.catalog import runtime_workload_context_or_raise as _runtime_workload_context_or_raise
    from astinuslab.core.compatibility import normalize_compatibility

    target = {"id": "ubuntu-22", "family": "debian", "distro": "ubuntu", "version": "22.04", "disk_gb": 20}
    workload = {
        "id": "wordpress-6.4",
        "display_name": "WordPress 6.4",
        "compatibility": normalize_compatibility(
            {"include": [{"family": "debian", "distro": "ubuntu", "version": "22.04"}]}
        ),
    }
    metadata: dict[str, str | None] = {"distro_id": "ubuntu", "distro_version": "22.04", "arch": "x86_64"}

    runtime_target = _runtime_workload_context_or_raise(
        "job-1", workload, target, metadata, workload_map={"wordpress-6.4": workload}
    )
    assert runtime_target["distro"] == "ubuntu"


def test_orchestrator_user_data_render_honors_profile_monkeypatch(monkeypatch, tmp_path):
    from astinuslab.orchestration.deps import JobRunDeps

    deps = JobRunDeps.default()
    template_dir = tmp_path / "guest" / "cloud-init"
    template_dir.mkdir(parents=True)
    template_dir.joinpath("user-data.custom.yaml").write_text(
        "user: REPLACE_GUEST_USER\nkey: ssh-ed25519 REPLACE_WITH_LAB_PUBLIC_KEY\ntarget: REPLACE_TARGET_ID\n",
        encoding="utf-8",
    )
    public_key = tmp_path / "id_ed25519.pub"
    public_key.write_text("ssh-ed25519 test-key", encoding="utf-8")

    deps.settings = SimpleNamespace(base_dir=tmp_path, data_dir=tmp_path)
    deps.resolve_cloud_init_profile = lambda **kwargs: "custom"

    rendered = deps.render_target_user_data(
        job_target_id="target-1",
        guest_user="analyst",
        ssh_public_key_path=public_key,
        target_distro="ignored",
        target_family="ignored",
        target_cloud_init_profile="ignored",
        target_guest_mount_path=None,
    )

    assert "user: analyst" in rendered
    assert "key: ssh-ed25519 test-key" in rendered
    assert "target: target-1" in rendered


def test_orchestrator_network_config_render_honors_profile_monkeypatch(monkeypatch, tmp_path):
    from astinuslab.orchestration.deps import JobRunDeps

    deps = JobRunDeps.default()
    template_dir = tmp_path / "guest" / "cloud-init"
    template_dir.mkdir(parents=True)
    template_dir.joinpath("network-config.custom.yaml").write_text("version: 2\n", encoding="utf-8")

    deps.settings = SimpleNamespace(base_dir=tmp_path, data_dir=tmp_path)
    deps.resolve_cloud_init_profile = lambda **kwargs: "custom"

    rendered = deps.render_target_network_config(
        target_distro="ignored",
        target_family="ignored",
        target_cloud_init_profile="ignored",
    )

    assert rendered == "version: 2\n"


def test_orchestrator_check_workload_cache_honors_wrapper_chain(monkeypatch, tmp_path):
    from astinuslab.orchestration.deps import JobRunDeps

    deps = JobRunDeps.default()
    image_path = tmp_path / "base.qcow2"
    image_path.write_bytes(b"base")
    cache_path = tmp_path / "cache.qcow2"
    cache_path.write_bytes(b"cached")
    calls: list[tuple[str, tuple[object, ...]]] = []

    def fake_key(target_id, workload_id, image):
        calls.append(("key", (target_id, workload_id, image)))
        return "abc123"

    def fake_path(target_id, workload_id, key):
        calls.append(("path", (target_id, workload_id, key)))
        return cache_path

    deps.workload_cache_key = fake_key
    deps.workload_cache_path = fake_path

    assert deps.check_workload_cache("target-1", "workload-1", image_path) == cache_path
    assert calls == [
        ("key", ("target-1", "workload-1", image_path)),
        ("path", ("target-1", "workload-1", "abc123")),
    ]


def test_orchestrator_save_workload_cache_honors_wrapper_chain(monkeypatch, tmp_path):
    from astinuslab.orchestration.deps import JobRunDeps

    deps = JobRunDeps.default()
    image_path = tmp_path / "base.qcow2"
    image_path.write_bytes(b"base")
    job_disk = tmp_path / "job-disk.qcow2"
    job_disk.write_bytes(b"job disk")
    cache_path = tmp_path / "workload-cache" / "target-1-workload-1-abc123.qcow2"
    calls: list[tuple[str, tuple[object, ...]]] = []

    deps.settings = SimpleNamespace(base_dir=tmp_path, data_dir=tmp_path, libvirt_uri="qemu:///system")

    def fake_key(target_id, workload_id, image):
        calls.append(("key", (target_id, workload_id, image)))
        return "abc123"

    def fake_path(target_id, workload_id, key):
        calls.append(("path", (target_id, workload_id, key)))
        return cache_path

    def fake_job_disk(job_id, target_id):
        calls.append(("job_disk", (job_id, target_id)))
        return job_disk

    def fake_update(target_id, workload_id, key, saved_cache_path, image):
        calls.append(("update", (target_id, workload_id, key, saved_cache_path, image)))

    def fake_run_command(cmd, *, job_id=None, **_kwargs):
        calls.append(("run", (tuple(cmd), job_id)))
        if cmd[0] == "cp":
            Path(cmd[2]).write_bytes(Path(cmd[1]).read_bytes())

    deps.workload_cache_key = fake_key
    deps.workload_cache_path = fake_path
    deps.job_disk_image_path = fake_job_disk
    deps.update_workload_cache_index = fake_update
    deps.run_command = fake_run_command
    deps.get_active_key_paths = lambda: (tmp_path / "lab-key", tmp_path / "lab-key.pub")

    deps.save_workload_cache("job-1", "target-1", "workload-1", image_path, "192.0.2.10", "ubuntu")

    assert cache_path.read_bytes() == b"job disk"
    assert calls[0] == ("key", ("target-1", "workload-1", image_path))
    assert calls[1] == ("path", ("target-1", "workload-1", "abc123"))
    assert calls[2] == ("job_disk", ("job-1", "target-1"))
    assert calls[-1] == ("update", ("target-1", "workload-1", "abc123", cache_path, image_path))
    commands = [cast(tuple[str, ...], call[1][0]) for call in calls if call[0] == "run"]
    flush = next(i for i, cmd in enumerate(commands) if cmd[0] == "ssh")
    snapshot = next(i for i, cmd in enumerate(commands) if "snapshot-create-as" in cmd)
    assert commands[flush][-2:] == ("ubuntu@192.0.2.10", "sync")
    assert str(tmp_path / "lab-key") in commands[flush]
    assert flush < snapshot


def test_job_run_deps_run_command_accepts_vm_lifecycle_info_kwargs(monkeypatch, tmp_path):
    import astinuslab.orchestration.deps as deps_mod
    from astinuslab.orchestration.deps import JobRunDeps

    calls: list[tuple[list[str], dict]] = []

    def fake_process_run_command(*args, **kwargs):
        calls.append((args[6], kwargs))
        return '{"virtual-size": 1073741824}'

    monkeypatch.setattr(deps_mod.process_support, "run_command", fake_process_run_command)
    deps = JobRunDeps.default()
    deps.settings = SimpleNamespace(storage_dir=tmp_path / "storage")

    result = deps.run_command(
        ["qemu-img", "info", "--output=json", str(tmp_path / "base.qcow2")],
        cwd=tmp_path,
        job_id="job-1",
        capture_output=True,
        text=True,
    )

    assert result == '{"virtual-size": 1073741824}'
    assert calls[0][0][0:3] == ["qemu-img", "info", "--output=json"]
    assert calls[0][1]["cwd"] == tmp_path
    assert calls[0][1]["job_id"] == "job-1"


def test_orchestrator_write_guest_metadata_json_output_unchanged(tmp_path):
    from astinuslab.virtualization.boot_staging import write_guest_metadata_json

    output = tmp_path / "guest-metadata.json"

    write_guest_metadata_json(
        output,
        "job-test-1234567890",
        {
            "distro_id": "debian",
            "distro_version": "12",
            "kernel_release": "6.1.0",
            "arch": "x86_64",
            "source": "ssh",
        },
    )

    assert json.loads(output.read_text(encoding="utf-8")) == {
        "distro_id": "debian",
        "distro_version": "12",
        "pretty_name": "debian 12",
        "hostname": "astinuslab-job-test-1234567",
        "arch": "x86_64",
        "kernel_release": "6.1.0",
        "kernel_version": "",
        "source": "ssh",
    }
    assert output.read_text(encoding="utf-8").endswith("\n")


def test_orchestrator_stage_target_cloud_init_honors_render_wrapper_monkeypatches(monkeypatch, tmp_path):
    from astinuslab.orchestration.deps import JobRunDeps

    deps = JobRunDeps.default()

    calls: list[tuple[str, dict[str, object]]] = []
    public_key = tmp_path / "id_ed25519.pub"
    public_key.write_text("ssh-ed25519 test-key", encoding="utf-8")

    def fake_user_data(**kwargs):
        calls.append(("user", kwargs))
        return "user-data-body\n"

    def fake_meta_data(**kwargs):
        calls.append(("meta", kwargs))
        return "meta-data-body\n"

    def fake_network_config(**kwargs):
        calls.append(("network", kwargs))
        return "network-config-body\n"

    deps.render_target_user_data = fake_user_data
    deps.render_target_meta_data = fake_meta_data
    deps.render_target_network_config = fake_network_config

    user_data_path, meta_data_path, network_config_path = deps.stage_target_cloud_init(
        cloud_init_dir=tmp_path,
        job_id="job-1",
        job_target_id="target-1",
        guest_user="analyst",
        ssh_public_key_path=public_key,
        target_distro="ubuntu",
        target_family="debian",
        target_cloud_init_profile="cloud",
        target_inject_network_config=True,
        target_guest_mount_path="/mnt/lab",
        cache_instance_id="cache-1",
    )

    assert user_data_path == tmp_path / "user-data"
    assert meta_data_path == tmp_path / "meta-data"
    assert network_config_path == tmp_path / "network-config"
    assert user_data_path.read_text(encoding="utf-8") == "user-data-body\n"
    assert meta_data_path.read_text(encoding="utf-8") == "meta-data-body\n"
    assert network_config_path is not None
    assert network_config_path.read_text(encoding="utf-8") == "network-config-body\n"
    assert calls == [
        (
            "user",
            {
                "job_target_id": "target-1",
                "guest_user": "analyst",
                "ssh_public_key_path": public_key,
                "target_distro": "ubuntu",
                "target_family": "debian",
                "target_cloud_init_profile": "cloud",
                "target_guest_mount_path": "/mnt/lab",
            },
        ),
        (
            "meta",
            {
                "job_id": "job-1",
                "job_target_id": "target-1",
                "cache_instance_id": "cache-1",
            },
        ),
        (
            "network",
            {
                "target_distro": "ubuntu",
                "target_family": "debian",
                "target_cloud_init_profile": "cloud",
            },
        ),
    ]


def test_orchestrator_stage_target_cloud_init_skips_network_config_when_disabled(monkeypatch, tmp_path):
    from astinuslab.orchestration.deps import JobRunDeps

    deps = JobRunDeps.default()

    public_key = tmp_path / "id_ed25519.pub"
    public_key.write_text("ssh-ed25519 test-key", encoding="utf-8")

    deps.render_target_user_data = lambda **kwargs: "user-data-body\n"
    deps.render_target_meta_data = lambda **kwargs: "meta-data-body\n"

    def fail_network_config(**kwargs):
        raise AssertionError("network renderer should not be called")

    deps.render_target_network_config = fail_network_config

    _, _, network_config_path = deps.stage_target_cloud_init(
        cloud_init_dir=tmp_path,
        job_id="job-1",
        job_target_id="target-1",
        guest_user="analyst",
        ssh_public_key_path=public_key,
        target_distro="ubuntu",
        target_family="debian",
        target_cloud_init_profile="cloud",
        target_inject_network_config=False,
        target_guest_mount_path="/mnt/lab",
        cache_instance_id="cache-1",
    )

    assert network_config_path is None
    assert not (tmp_path / "network-config").exists()


def test_orchestrator_stage_target_cloud_init_skips_none_network_config(monkeypatch, tmp_path):
    from astinuslab.orchestration.deps import JobRunDeps

    deps = JobRunDeps.default()

    public_key = tmp_path / "id_ed25519.pub"
    public_key.write_text("ssh-ed25519 test-key", encoding="utf-8")

    deps.render_target_user_data = lambda **kwargs: "user-data-body\n"
    deps.render_target_meta_data = lambda **kwargs: "meta-data-body\n"
    deps.render_target_network_config = lambda **kwargs: None

    _, _, network_config_path = deps.stage_target_cloud_init(
        cloud_init_dir=tmp_path,
        job_id="job-1",
        job_target_id="target-1",
        guest_user="analyst",
        ssh_public_key_path=public_key,
        target_distro="ubuntu",
        target_family="debian",
        target_cloud_init_profile="cloud",
        target_inject_network_config=True,
        target_guest_mount_path="/mnt/lab",
        cache_instance_id="cache-1",
    )

    assert network_config_path is None
    assert not (tmp_path / "network-config").exists()


def test_prepare_live_session_boot_context_preserves_external_image_path(tmp_path):
    from astinuslab.storage.run_path_support import prepare_live_session_boot_context

    settings = SimpleNamespace(images_dir=tmp_path / "images")
    target = {"image_ref": "target.qcow2", "vcpu": 2, "memory_mb": 2048, "disk_gb": 20}
    external_image = tmp_path / "uploads" / "manual.qcow2"

    image_path, vcpus, memory_mb, disk_gb = prepare_live_session_boot_context(
        target,
        settings,
        str(external_image),
        None,
        None,
        None,
    )

    assert image_path == external_image
    assert vcpus == 2
    assert memory_mb == 2048
    assert disk_gb == 20


def test_prepare_live_session_boot_context_preserves_default_image_path_and_overrides(tmp_path):
    from astinuslab.storage.run_path_support import prepare_live_session_boot_context

    settings = SimpleNamespace(images_dir=tmp_path / "images")
    target = {"image_ref": "target.qcow2", "vcpu": 2, "memory_mb": 2048, "disk_gb": 20}

    image_path, vcpus, memory_mb, disk_gb = prepare_live_session_boot_context(
        target,
        settings,
        None,
        4,
        4096,
        40,
    )

    assert image_path == settings.images_dir / "target.qcow2"
    assert vcpus == 4
    assert memory_mb == 4096
    assert disk_gb == 40


class _RecordingRepo:
    def __init__(self, events):
        self._events = events

    def add_event(self, job_id, state, message):
        self._events.append((job_id, state, message))


class _RecordingSessionScope:
    def __init__(self, events):
        self._events = events

    def __call__(self):
        return self

    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return False


def _make_recording_session(events):
    scope = _RecordingSessionScope(events)
    repo_cls = lambda _session: _RecordingRepo(events)  # noqa: E731
    return scope, repo_cls


def test_resolve_live_session_workload_cache_no_workload_id():
    from astinuslab.live_session.support import resolve_live_session_workload_cache

    events: list = []
    session_scope, repo_cls = _make_recording_session(events)
    logger = SimpleNamespace(info=lambda *a, **k: None)
    image_path = Path("/tmp/img.qcow2")

    calls: list = []

    def load_workload_id_fn(job_id):
        calls.append(("load_workload_id", job_id))
        return None

    def workload_by_id_fn(_wid):
        calls.append(("workload_by_id",))
        return {}

    def check_workload_cache_fn(*_a, **_k):
        calls.append(("check_workload_cache",))
        return Path("/never")

    def workload_cache_key_fn(*_a, **_k):
        calls.append(("workload_cache_key",))
        return "never"

    result = resolve_live_session_workload_cache(
        logger,
        session_scope,
        repo_cls,
        load_workload_id_fn,
        workload_by_id_fn,
        check_workload_cache_fn,
        workload_cache_key_fn,
        "job-1",
        "target-x",
        image_path,
    )

    assert result == (None, None, None, None, None)
    assert calls == [("load_workload_id", "job-1")]
    assert events == []


def test_resolve_live_session_workload_cache_missing_workload_raises():
    from astinuslab.live_session.support import resolve_live_session_workload_cache

    events: list = []
    session_scope, repo_cls = _make_recording_session(events)
    logger = SimpleNamespace(info=lambda *a, **k: None)

    with pytest.raises(RuntimeError, match="Workload not found: wid-missing"):
        resolve_live_session_workload_cache(
            logger,
            session_scope,
            repo_cls,
            lambda _j: "wid-missing",
            lambda _w: None,
            lambda *_a, **_k: None,
            lambda *_a, **_k: "k",
            "job-2",
            "target-x",
            Path("/tmp/img.qcow2"),
        )
    assert events == []


def test_resolve_live_session_workload_cache_cache_miss():
    from astinuslab.live_session.support import resolve_live_session_workload_cache

    events: list = []
    session_scope, repo_cls = _make_recording_session(events)
    logger = SimpleNamespace(info=lambda *a, **k: None)
    workload = {"id": "wid-1"}

    workload_id, returned_workload, cache_path, boot_cache_image, boot_cache_instance_id = (
        resolve_live_session_workload_cache(
            logger,
            session_scope,
            repo_cls,
            lambda _j: "wid-1",
            lambda _w: workload,
            lambda *_a, **_k: None,
            lambda *_a, **_k: pytest.fail("workload_cache_key must not be called on cache miss"),
            "job-3",
            "target-x",
            Path("/tmp/img.qcow2"),
        )
    )

    assert workload_id == "wid-1"
    assert returned_workload is workload
    assert cache_path is None
    assert boot_cache_image is None
    assert boot_cache_instance_id is None
    assert events == []


def test_resolve_live_session_workload_cache_cache_hit_uses_wrappers_and_writes_event():
    from astinuslab.live_session.support import resolve_live_session_workload_cache

    events: list = []
    session_scope, repo_cls = _make_recording_session(events)
    log_records: list = []
    logger = SimpleNamespace(info=lambda *args, **_kwargs: log_records.append(args))
    workload = {"id": "wid-9"}
    cache_path = Path("/cache/snap.qcow2")
    call_order: list = []

    def check_workload_cache_fn(target_id, workload_id, image_path):
        call_order.append("check")
        assert target_id == "target-y"
        assert workload_id == "wid-9"
        assert image_path == Path("/tmp/img.qcow2")
        return cache_path

    def workload_cache_key_fn(target_id, workload_id, image_path):
        call_order.append("key")
        assert target_id == "target-y"
        assert workload_id == "wid-9"
        assert image_path == Path("/tmp/img.qcow2")
        return "abcdef0123456789"

    result = resolve_live_session_workload_cache(
        logger,
        session_scope,
        repo_cls,
        lambda _j: "wid-9",
        lambda _w: workload,
        check_workload_cache_fn,
        workload_cache_key_fn,
        "job-9",
        "target-y",
        Path("/tmp/img.qcow2"),
    )

    workload_id, returned_workload, returned_cache_path, boot_cache_image, boot_cache_instance_id = result
    assert workload_id == "wid-9"
    assert returned_workload is workload
    assert returned_cache_path == cache_path
    assert boot_cache_image == cache_path
    assert boot_cache_instance_id == "workload-cache-abcdef0123456789"
    assert call_order == ["check", "key"]
    assert events == [
        ("job-9", "preparing_image", "Workload cache hit: booting from snapshot (abcdef01…)"),
    ]
    assert log_records and log_records[0][0] == "[%s] workload cache hit — booting from %s"


class _ProvisionRepo:
    def __init__(self, transitions, events):
        self._transitions = transitions
        self._events = events

    def transition_job_status(self, job_id, status):
        self._transitions.append((job_id, status))

    def add_event(self, job_id, state, message):
        self._events.append((job_id, state, message))


def _make_provision_session(transitions, events):
    scope = _RecordingSessionScope(events)
    repo_cls = lambda _session: _ProvisionRepo(transitions, events)  # noqa: E731
    return scope, repo_cls


def _make_callback_recorders():
    finalize_calls: list = []
    provision_calls: list = []
    save_calls: list = []
    snapshot_calls: list = []

    def finalize_fn(*args, **kwargs):
        finalize_calls.append((args, kwargs))

    def provision_fn(*args, **kwargs):
        provision_calls.append((args, kwargs))

    def save_fn(*args, **kwargs):
        save_calls.append((args, kwargs))

    def snapshot_fn(*args, **kwargs):
        snapshot_calls.append((args, kwargs))

    return (finalize_fn, provision_fn, save_fn, snapshot_fn), (
        finalize_calls,
        provision_calls,
        save_calls,
        snapshot_calls,
    )


def test_provision_live_session_workload_skips_when_no_workload_id():
    from astinuslab.live_session.support import provision_live_session_workload

    transitions: list = []
    events: list = []
    session_scope, repo_cls = _make_provision_session(transitions, events)
    (finalize_fn, provision_fn, save_fn, snapshot_fn), recs = _make_callback_recorders()

    provision_live_session_workload(
        session_scope,
        repo_cls,
        finalize_fn,
        provision_fn,
        save_fn,
        snapshot_fn,
        "job-1",
        "target-x",
        None,
        {"id": "ignored"},
        Path("/cache/snap.qcow2"),
        Path("/tmp/img.qcow2"),
        "10.0.0.1",
        "user",
        None,
    )

    assert transitions == []
    assert events == []
    assert all(call_list == [] for call_list in recs)


def test_provision_live_session_workload_skips_when_workload_none():
    from astinuslab.live_session.support import provision_live_session_workload

    transitions: list = []
    events: list = []
    session_scope, repo_cls = _make_provision_session(transitions, events)
    (finalize_fn, provision_fn, save_fn, snapshot_fn), recs = _make_callback_recorders()

    provision_live_session_workload(
        session_scope,
        repo_cls,
        finalize_fn,
        provision_fn,
        save_fn,
        snapshot_fn,
        "job-2",
        "target-x",
        "wid-1",
        None,
        None,
        Path("/tmp/img.qcow2"),
        "10.0.0.1",
        "user",
        None,
    )

    assert transitions == []
    assert events == []
    assert all(call_list == [] for call_list in recs)


def test_provision_live_session_workload_cache_hit():
    from astinuslab.live_session.support import provision_live_session_workload

    transitions: list = []
    events: list = []
    session_scope, repo_cls = _make_provision_session(transitions, events)
    (
        (finalize_fn, provision_fn, save_fn, snapshot_fn),
        (
            finalize_calls,
            provision_calls,
            save_calls,
            snapshot_calls,
        ),
    ) = _make_callback_recorders()

    workload = {"id": "wid-3"}
    cache_path = Path("/cache/snap.qcow2")
    image_path = Path("/tmp/img.qcow2")
    runtime_target = {"distro": "ubuntu"}

    provision_live_session_workload(
        session_scope,
        repo_cls,
        finalize_fn,
        provision_fn,
        save_fn,
        snapshot_fn,
        "job-3",
        "target-y",
        "wid-3",
        workload,
        cache_path,
        image_path,
        "10.0.0.2",
        "guest",
        runtime_target,
    )

    assert transitions == [("job-3", "provisioning_workload")]
    assert events == [
        ("job-3", "provisioning_workload", "Workload restored from snapshot cache — skipping install"),
    ]
    assert finalize_calls == [(("job-3", workload, "10.0.0.2"), {})]
    assert provision_calls == []
    assert save_calls == []
    assert snapshot_calls == [
        (
            ("job-3",),
            {
                "description": "Post-workload clean state",
                "event_state": "provisioning_workload",
                "event_message": "Lab snapshot created after workload provisioning: lab-clean",
            },
        ),
    ]


def test_provision_live_session_workload_cache_miss():
    from astinuslab.live_session.support import provision_live_session_workload

    transitions: list = []
    events: list = []
    session_scope, repo_cls = _make_provision_session(transitions, events)
    (
        (finalize_fn, provision_fn, save_fn, snapshot_fn),
        (
            finalize_calls,
            provision_calls,
            save_calls,
            snapshot_calls,
        ),
    ) = _make_callback_recorders()

    workload = {"id": "wid-4"}
    image_path = Path("/tmp/img.qcow2")
    runtime_target = {"distro": "debian"}

    provision_live_session_workload(
        session_scope,
        repo_cls,
        finalize_fn,
        provision_fn,
        save_fn,
        snapshot_fn,
        "job-4",
        "target-z",
        "wid-4",
        workload,
        None,
        image_path,
        "10.0.0.3",
        "guest2",
        runtime_target,
    )

    assert transitions == []
    assert events == []
    assert finalize_calls == []
    assert provision_calls == [
        (("job-4", workload, "10.0.0.3", "guest2"), {"target_context": runtime_target}),
    ]
    # The cache save flushes the guest over SSH, so it needs the guest address and login.
    assert save_calls == [(("job-4", "target-z", "wid-4", image_path, "10.0.0.3", "guest2"), {})]
    assert snapshot_calls == [
        (
            ("job-4",),
            {
                "description": "Post-workload clean state",
                "event_state": "provisioning_workload",
                "event_message": "Lab snapshot created after workload provisioning: lab-clean",
            },
        ),
    ]
