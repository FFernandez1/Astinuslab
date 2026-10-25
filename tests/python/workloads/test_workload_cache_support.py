"""Unit tests for the workload disk-image cache."""

from __future__ import annotations

import json
import stat
import threading
from pathlib import Path

import pytest

from astinuslab.workloads.workload_cache_support import (
    check_workload_cache,
    save_workload_cache,
    update_workload_cache_index,
    workload_cache_key,
    workload_cache_path,
)
from tests.python._support.helpers import autospec_setattr


@pytest.fixture()
def fake_image(tmp_path) -> Path:
    p = tmp_path / "base-image.qcow2"
    p.write_bytes(b"fake qcow2 content")
    return p


@pytest.fixture()
def fake_settings(tmp_path):
    class _S:
        base_dir = tmp_path / "checkout"
        data_dir = tmp_path / "data"
        storage_dir = tmp_path / "configured-storage"

    return _S()


class TestWorkloadCacheKey:
    def test_returns_16_char_hex(self, fake_image):
        key = workload_cache_key("ubuntu-22", "wordpress", fake_image)
        assert len(key) == 16
        assert all(c in "0123456789abcdef" for c in key)

    def test_key_is_deterministic(self, fake_image):
        k1 = workload_cache_key("ubuntu-22", "wordpress", fake_image)
        k2 = workload_cache_key("ubuntu-22", "wordpress", fake_image)
        assert k1 == k2

    def test_different_target_produces_different_key(self, fake_image):
        k1 = workload_cache_key("ubuntu-22", "wordpress", fake_image)
        k2 = workload_cache_key("debian-12", "wordpress", fake_image)
        assert k1 != k2

    def test_different_workload_produces_different_key(self, fake_image):
        k1 = workload_cache_key("ubuntu-22", "wordpress", fake_image)
        k2 = workload_cache_key("ubuntu-22", "dvwa", fake_image)
        assert k1 != k2

    def test_different_workload_fingerprint_produces_different_key(self, fake_image):
        k1 = workload_cache_key("ubuntu-22", "wordpress", fake_image, workload_fingerprint="aaa")
        k2 = workload_cache_key("ubuntu-22", "wordpress", fake_image, workload_fingerprint="bbb")
        assert k1 != k2

    def test_default_fingerprint_preserves_existing_call_shape(self, fake_image):
        key = workload_cache_key("ubuntu-22", "wordpress", fake_image)
        assert len(key) == 16


class TestCheckWorkloadCache:
    import logging as _logging

    _logger = _logging.getLogger("test")

    def test_cache_path_uses_configured_storage_directory(self, fake_settings):
        path = workload_cache_path(fake_settings, "ubuntu-24.04", "dvwa", "abc123")

        assert path == fake_settings.storage_dir / "workload-cache" / "ubuntu-24.04-dvwa-abc123.qcow2"
        assert fake_settings.data_dir not in path.parents

    def test_returns_none_when_cache_missing(self, fake_image, fake_settings):
        import logging

        result = check_workload_cache(logging.getLogger("test"), fake_settings, "ubuntu-22", "wordpress", fake_image)
        assert result is None

    def test_returns_path_when_cache_exists(self, fake_image, fake_settings):
        import logging

        key = workload_cache_key("ubuntu-22", "wordpress", fake_image)
        cache_p = workload_cache_path(fake_settings, "ubuntu-22", "wordpress", key)
        cache_p.parent.mkdir(parents=True, exist_ok=True)
        cache_p.write_bytes(b"cached qcow2")

        result = check_workload_cache(logging.getLogger("test"), fake_settings, "ubuntu-22", "wordpress", fake_image)
        assert result == cache_p

    @pytest.mark.parametrize("preexisting", [False, True])
    def test_ensure_workload_cache_dir_is_not_group_writable(self, fake_settings, preexisting):
        from astinuslab.workloads import workload_cache_support as support

        cache_dir = fake_settings.storage_dir / "workload-cache"
        if preexisting:
            cache_dir.mkdir(parents=True, mode=0o775)
            cache_dir.chmod(0o775)

        result = support.ensure_workload_cache_dir(fake_settings)

        assert result == cache_dir
        assert stat.S_IMODE(cache_dir.stat().st_mode) == 0o750

    def test_returns_none_for_empty_cache_file(self, fake_image, fake_settings):
        import logging

        key = workload_cache_key("ubuntu-22", "wordpress", fake_image)
        cache_p = workload_cache_path(fake_settings, "ubuntu-22", "wordpress", key)
        cache_p.parent.mkdir(parents=True, exist_ok=True)
        cache_p.write_bytes(b"")  # empty = invalid

        result = check_workload_cache(logging.getLogger("test"), fake_settings, "ubuntu-22", "wordpress", fake_image)
        assert result is None

    def test_old_call_shape_honors_monkeypatched_key(self, fake_image, fake_settings, monkeypatch):
        import logging

        from astinuslab.workloads import workload_cache_support as support

        cache_p = workload_cache_path(fake_settings, "ubuntu-22", "wordpress", "patched")
        cache_p.parent.mkdir(parents=True, exist_ok=True)
        cache_p.write_bytes(b"cached qcow2")
        autospec_setattr(
            monkeypatch, support, "workload_cache_key", lambda target_id, workload_id, image_path: "patched"
        )

        result = support.check_workload_cache(
            logging.getLogger("test"), fake_settings, "ubuntu-22", "wordpress", fake_image
        )
        assert result == cache_p


class TestUpdateWorkloadCacheIndex:
    def test_creates_index_file(self, fake_image, fake_settings):
        key = workload_cache_key("ubuntu-22", "wordpress", fake_image)
        cache_p = fake_settings.storage_dir / "workload-cache" / "cached.qcow2"
        cache_p.parent.mkdir(parents=True, exist_ok=True)
        cache_p.write_bytes(b"data")

        update_workload_cache_index(fake_settings, threading.Lock(), "ubuntu-22", "wordpress", key, cache_p, fake_image)

        index_path = fake_settings.storage_dir / "workload-cache" / "index.json"
        assert index_path.exists()
        index = json.loads(index_path.read_text())
        assert key in index
        assert index[key]["target_id"] == "ubuntu-22"
        assert index[key]["workload_id"] == "wordpress"

    def test_appends_to_existing_index(self, fake_image, fake_settings):
        key1 = workload_cache_key("ubuntu-22", "wordpress", fake_image)
        cache_p = fake_settings.storage_dir / "workload-cache" / "c.qcow2"
        cache_p.parent.mkdir(parents=True, exist_ok=True)
        cache_p.write_bytes(b"data")
        lock = threading.Lock()

        update_workload_cache_index(fake_settings, lock, "ubuntu-22", "wordpress", key1, cache_p, fake_image)

        fake_image2 = fake_image.parent / "other.qcow2"
        fake_image2.write_bytes(b"other content")
        key2 = workload_cache_key("ubuntu-22", "dvwa", fake_image2)
        cache_p2 = fake_settings.storage_dir / "workload-cache" / "c2.qcow2"
        cache_p2.write_bytes(b"data2")
        update_workload_cache_index(fake_settings, lock, "ubuntu-22", "dvwa", key2, cache_p2, fake_image2)

        index_path = fake_settings.storage_dir / "workload-cache" / "index.json"
        index = json.loads(index_path.read_text())
        assert key1 in index
        assert key2 in index

    def test_corrupt_cache_index_logs_and_rebuilds(self, fake_image, fake_settings, caplog):
        import logging

        index_path = fake_settings.storage_dir / "workload-cache" / "index.json"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text("{not json")

        key = workload_cache_key("ubuntu-22", "wordpress", fake_image)
        cache_p = fake_settings.storage_dir / "workload-cache" / "cached.qcow2"
        cache_p.write_bytes(b"data")

        with caplog.at_level(logging.WARNING, logger="astinuslab.workloads.workload_cache_support"):
            update_workload_cache_index(
                fake_settings, threading.Lock(), "ubuntu-22", "wordpress", key, cache_p, fake_image
            )

        index = json.loads(index_path.read_text())
        assert key in index
        assert any("unreadable" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


class TestSaveWorkloadCacheFlush:
    """The cache copies a frozen Job disk. Data the guest has not flushed is not on
    that disk: on ext4, files written seconds earlier come back zero-length, which
    left a cache-booted DVWA serving empty pages while Playground reported ready."""

    def _save(self, tmp_path, fake_image, *, flush, agent_ok, logger=None):
        import logging
        import subprocess

        job_disk = tmp_path / "job-disk.qcow2"
        job_disk.write_bytes(b"frozen job disk")
        cache_path = tmp_path / "workload-cache" / "t-w-key.qcow2"
        calls: list[object] = []

        def flush_guest_filesystems():
            calls.append("flush")
            flush()

        def run_command(cmd, **kwargs):
            calls.append(tuple(cmd))
            if "qemu-agent-command" in cmd and not agent_ok:
                raise subprocess.CalledProcessError(1, cmd)
            if cmd[0] == "cp":
                Path(cmd[2]).write_bytes(Path(cmd[1]).read_bytes())
            return ""

        def update_index(*args):
            calls.append(("update",) + args)

        save_workload_cache(
            logger or logging.getLogger("test"),
            type("S", (), {"libvirt_uri": "qemu:///system"})(),
            run_command,
            lambda job_id, target_id: job_disk,
            update_index,
            lambda target_id, workload_id, image: "key",
            lambda target_id, workload_id, key: cache_path,
            job_id="job-1",
            target_id="t",
            workload_id="w",
            image_path=fake_image,
            flush_guest_filesystems=flush_guest_filesystems,
        )
        snapshots = [c for c in calls if isinstance(c, tuple) and "snapshot-create-as" in c]
        return calls, snapshots, cache_path

    def test_flushes_guest_before_freezing_the_disk(self, tmp_path, fake_image):
        calls, snapshots, cache_path = self._save(tmp_path, fake_image, flush=lambda: None, agent_ok=False)

        assert len(snapshots) == 1
        assert calls.index("flush") < calls.index(snapshots[0])
        assert "--quiesce" not in snapshots[0]
        assert cache_path.read_bytes() == b"frozen job disk"

    def test_quiesces_filesystems_when_the_guest_agent_responds(self, tmp_path, fake_image):
        calls, snapshots, cache_path = self._save(tmp_path, fake_image, flush=lambda: None, agent_ok=True)

        assert snapshots and "--quiesce" in snapshots[0]
        assert cache_path.exists()

    def test_agent_quiesce_alone_is_sufficient(self, tmp_path, fake_image):
        def failing_flush():
            raise RuntimeError("ssh unavailable")

        _calls, snapshots, cache_path = self._save(tmp_path, fake_image, flush=failing_flush, agent_ok=True)

        assert snapshots and "--quiesce" in snapshots[0]
        assert cache_path.exists()

    def test_does_not_cache_a_disk_that_could_not_be_flushed(self, tmp_path, fake_image, caplog):
        import logging

        def failing_flush():
            raise RuntimeError("ssh unavailable")

        with caplog.at_level(logging.WARNING, logger="test"):
            calls, snapshots, cache_path = self._save(tmp_path, fake_image, flush=failing_flush, agent_ok=False)

        assert snapshots == []
        assert not any(isinstance(c, tuple) and c[:1] in {("cp",), ("update",)} for c in calls)
        assert not cache_path.exists()
        assert any("not saved" in r.getMessage() for r in caplog.records)
